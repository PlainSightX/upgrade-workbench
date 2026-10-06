"""比较受审探针的显式读数；相同输出不能扩大成语义等价或业务正确。"""

from __future__ import annotations

import hashlib
import json
import math
import re

from .diagnostic_oracles import _unique_object, measurement
from .workflow import _fully_passed

MARKER = "UPGRADE_WORKBENCH_PROBE_SAMPLE:"
LIMIT = 2048
SCOPE = (
    "Only the explicitly recorded input and output of this reviewed probe. Different outputs "
    "distinguish these sources for this input, not which source is correct. Same outputs do not "
    "establish equivalence or coverage of other inputs. Recorded input and path completion are "
    "probe declarations; their alignment with the business contract still needs source review."
)
_SHA = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def validate_sample(value):
    """两种记录共用有限 JSON 规则；读数和数值判据可以写在同一条测量里。"""
    if (not isinstance(value, dict) or set(value) != {"input", "output", "path_completed"}
            or type(value["path_completed"]) is not bool):
        raise ValueError("invalid sample fields")
    if len(_json_bytes(value)) > LIMIT:
        raise ValueError("sample exceeds capacity")
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 6 or nodes > 128:
            raise ValueError("sample exceeds structural capacity")
        if isinstance(item, dict):
            if any(not isinstance(key, str) or "\x00" in key for key in item):
                raise ValueError("invalid sample key")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif type(item) in {int, float}:
            if abs(item) > 1e100 or not math.isfinite(item):
                raise ValueError("invalid sample number")
        elif isinstance(item, str):
            if "\x00" in item:
                raise ValueError("invalid sample text")
        elif item is not None and type(item) is not bool:
            raise ValueError("invalid sample value")
    return value


def sample(stdout):
    """具体输入必须来自显式记录，不能由旧数值读数补造。"""
    records = [line[len(MARKER):] for line in stdout.splitlines() if line.startswith(MARKER)]
    numeric = measurement(stdout)
    if len(records) > 1 or (records and "input" in numeric):
        return {"status": "ambiguous_sample"}
    try:
        if records:
            if len(records[0].encode("utf-8")) > LIMIT:
                raise ValueError("sample exceeds capacity")
            value = json.loads(records[0], object_pairs_hook=_unique_object)
        elif "input" in numeric:
            value = {key: numeric[key] for key in ("input", "output", "path_completed")}
        else:
            return {"status": "missing_sample", "measurement_status": numeric["status"]}
        validate_sample(value)
        return {"status": "sampled" if value["path_completed"] else "path_not_completed", **value}
    except (ValueError, TypeError, RecursionError, OverflowError):
        return {"status": "invalid_sample"}


def readings(report, stdout):
    """从宿主报告绑定探针、源码和镜像身份，不把模型自填身份当运行证据。"""
    result = {}
    for name, stage in report["stages"].items():
        if name not in {"old_original", "new_original", "new_candidate"} or stage.get("status") == "not_supplied":
            continue
        row = sample(stdout.get(name, ""))
        binding = report.get("probe_sample_identity", {})
        env = "old" if name == "old_original" else "new"
        environment = report.get("environments", {}).get(env, {})
        row["identity"] = {
            "case_fingerprint": report.get("case_fingerprint"),
            "probe_sha256": binding.get("probe_sha256"),
            "source_revision": binding.get(name, {}).get("revision"),
            "patch_sha256": binding.get(name, {}).get("patch_sha256"),
            "lock_sha256": binding.get("old_lock_sha256" if env == "old" else "lock_sha256"),
            "image_id": environment.get("image_id"),
        }
        row["execution_passed"] = _fully_passed(stage)
        row["environment"] = env
        environments = report.get("environments", {})
        row["version_pair"] = {
            "old_lock": binding.get("old_lock_sha256"), "new_lock": binding.get("lock_sha256"),
            "old_image": environments.get("old", {}).get("image_id"),
            "new_image": environments.get("new", {}).get("image_id"),
            "old_base": environments.get("old", {}).get("base_image_id"),
            "new_base": environments.get("new", {}).get("base_image_id"),
        }
        result[name] = row
    return result


def _identity_valid(value):
    return (isinstance(value, dict)
            and all(isinstance(value.get(key), str) and _SHA.fullmatch(value[key])
                    for key in ("case_fingerprint", "probe_sha256", "source_revision", "patch_sha256", "lock_sha256"))
            and isinstance(value.get("image_id"), str) and _IMAGE.fullmatch(value["image_id"]))


def compare(left, right):
    """未完成、不同输入或不同执行环境均不可比；不由失败文本猜测业务差异。"""
    result = {"status": "incomplete", "scope_limit": SCOPE,
              "left": left.get("identity"), "right": right.get("identity")}
    identities = [row.get("identity") for row in (left, right)]
    if not all(_identity_valid(value) for value in identities):
        return result | {"reason": "missing_or_invalid_execution_identity"}
    if any(identities[0][key] != identities[1][key]
           for key in ("case_fingerprint", "probe_sha256", "lock_sha256", "image_id")):
        return result | {"reason": "different_probe_case_or_environment"}
    if (identities[0]["source_revision"] == identities[1]["source_revision"]
            or identities[0]["patch_sha256"] == identities[1]["patch_sha256"]):
        return result | {"reason": "same_source_not_two_implementations"}
    if any(row.get("status") != "sampled" or row.get("execution_passed") is not True
           for row in (left, right)):
        return result | {"reason": "missing_sample_or_execution_not_passed"}
    if _json_bytes(left["input"]) != _json_bytes(right["input"]):
        return result | {"reason": "different_recorded_inputs"}
    return result | {
        "status": "same" if _json_bytes(left["output"]) == _json_bytes(right["output"]) else "different",
        "input": left["input"], "left_output": left["output"], "right_output": right["output"],
    }


def compare_versions(left, right):
    """只接受同一受审执行记录绑定的旧/新环境对，不放宽同环境比较的规则。"""
    result = {"status": "incomplete", "scope_limit": SCOPE,
              "left": left.get("identity"), "right": right.get("identity")}
    identities = [row.get("identity") for row in (left, right)]
    if not all(_identity_valid(value) for value in identities):
        return result | {"reason": "missing_or_invalid_execution_identity"}
    pair = left.get("version_pair")
    if (not isinstance(pair, dict) or pair != right.get("version_pair")
            or any(not isinstance(pair.get(key), str) or not _SHA.fullmatch(pair[key])
                   for key in ("old_lock", "new_lock"))
            or any(not isinstance(pair.get(key), str) or not _IMAGE.fullmatch(pair[key])
                   for key in ("old_image", "new_image", "old_base", "new_base"))
            or pair["old_base"] != pair["new_base"]
            or left.get("environment") != "old" or right.get("environment") != "new"
            or identities[0]["lock_sha256"] != pair["old_lock"]
            or identities[1]["lock_sha256"] != pair["new_lock"]
            or identities[0]["image_id"] != pair["old_image"]
            or identities[1]["image_id"] != pair["new_image"]):
        return result | {"reason": "unbound_version_environment_pair"}
    if any(identities[0][key] != identities[1][key] for key in ("case_fingerprint", "probe_sha256")):
        return result | {"reason": "different_probe_or_case"}
    if any(row.get("status") != "sampled" or row.get("execution_passed") is not True
           for row in (left, right)):
        return result | {"reason": "missing_sample_or_execution_not_passed"}
    if _json_bytes(left["input"]) != _json_bytes(right["input"]):
        return result | {"reason": "different_recorded_inputs"}
    return result | {
        "status": "same" if _json_bytes(left["output"]) == _json_bytes(right["output"]) else "different",
        "input": left["input"], "left_output": left["output"], "right_output": right["output"],
    }


def project(observations, current_revision, *, limit=4):
    """只消费调用者已公开且校验过的观察；旧候选仅作为具名参照，不证明当前源码。"""
    result, prior = [], {}
    for observation in observations:
        if observation.get("kind") != "probe":
            continue
        public = observation["result"]
        probe_id = public.get("assessment", {}).get("probe_id")
        rows = public.get("probe_samples", {})
        target = rows.get("new_candidate")
        if probe_id is None or target is None:
            continue
        if rows.get("old_original") is not None:
            result.append(compare_versions(rows["old_original"], target) | {
                "probe_id": probe_id, "kind": "old_original_vs_candidate",
                "left_observation_id": observation["id"], "right_observation_id": observation["id"],
                "right_is_current_source": target["identity"].get("source_revision") == current_revision,
            })
        comparator = prior.get(probe_id)
        if comparator is not None and comparator[1]["identity"] != target["identity"]:
            left_id, left = comparator
            kind = "candidate_vs_candidate"
        elif rows.get("new_original") is not None:
            left_id, left = observation["id"], rows["new_original"]
            kind = "direct_upgrade_vs_candidate"
        else:
            prior[probe_id] = (observation["id"], target)
            continue
        result.append(compare(left, target) | {
            "probe_id": probe_id, "kind": kind,
            "left_observation_id": left_id, "right_observation_id": observation["id"],
            "right_is_current_source": target["identity"].get("source_revision") == current_revision,
        })
        prior[probe_id] = (observation["id"], target)
    return {"comparisons": result[-limit:], "scope_limit": SCOPE}


def identity(case, snapshot, code):
    """执行前绑定已审快照；文件内容仍由现有 runner 在各阶段之间核验。"""
    from .candidates import load_candidate

    original = load_candidate(case)
    return {
        "probe_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "lock_sha256": case.manifest.file_hashes["requirements/new.txt"],
        "old_lock_sha256": case.manifest.file_hashes["requirements/old.txt"],
        "old_original": {"revision": original.revision, "patch_sha256": original.sha256},
        "new_original": {"revision": original.revision, "patch_sha256": original.sha256},
        "new_candidate": {"revision": snapshot.revision, "patch_sha256": snapshot.sha256},
    }
