"""诊断产物、执行审阅和公共观察。探针不进入正式检查或候选补丁。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .candidates import load_candidate
from .cases import CaseValidationError, PatchValidationError, load_case, stage_source
from .cases.manifest import assert_no_links, read_verified_file
from .diagnostic_evidence import (
    aggregate_failure_evidence,
    enrich_failure_detail,
    setup_failure,
)
from .diagnostic_oracles import evaluate
from .diagnostic_state import (
    failed_public_nodes,
    note_progress,
    probe_states,
    recurring_public_failures,
    runtime_findings,
)
from .generation.source_context import digest, encoded, navigate, source_state

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?[A-Za-z0-9.+-]*\Z")


def store(directory: Path, value: dict) -> dict:
    """按内容寻址，结果先落盘，任务游标随后更新。"""
    assert_no_links(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = encoded(value)
    identity = hashlib.sha256(data).hexdigest()
    path = directory / f"{identity}.json"
    assert_no_links(path)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("Artifact hash collision or corruption")
    else:
        temporary = path.with_suffix(".tmp")
        assert_no_links(temporary)
        temporary.write_bytes(data)
        temporary.replace(path)
    return {"id": identity, "path": str(path)}


def read(reference: dict, root: Path) -> dict:
    if not isinstance(reference, dict) or set(reference) != {"id", "path"}:
        raise ValueError("Invalid diagnostic artifact reference")
    path = Path(reference["path"])
    assert_no_links(path)
    if not path.resolve().is_relative_to(root.resolve()) or path.stem != reference["id"]:
        raise ValueError("Diagnostic artifact outside task")
    if path.stat().st_size > 2_000_000:
        raise ValueError("Diagnostic artifact too large")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != reference["id"]:
        raise ValueError("Diagnostic artifact changed")
    return json.loads(data)


def _root(task):
    return Path(task["task_path"]).parent


class DiagnosticContractError(ValueError):
    """已登记的诊断身份、输入或持久化报告违反合同。"""


def _diagnostic_failure(error: BaseException, phase: str) -> dict[str, str]:
    """把异常投影为有界、脱敏的诊断失败，不保存宿主路径或原始异常文本。"""
    if isinstance(error, (DiagnosticContractError, CaseValidationError, PatchValidationError)):
        category = "contract"
    elif isinstance(error, (OSError, RuntimeError, TimeoutError)):
        category = "infrastructure"
    else:
        category = "infrastructure"
    code = {
        "contract": "diagnostic_contract_rejected",
        "infrastructure": "diagnostic_execution_failed",
    }[category]
    return {"category": category, "code": code, "phase": phase,
            "error_type": type(error).__name__,
            "reason": "diagnostic execution stopped; inspect the bounded report and retry explicitly"}


def _checkpoint_report(report: dict, path: Path) -> None:
    """原子保存诊断阶段，硬退出后只能依据最后一个明确检查点恢复。"""
    assert_no_links(path)
    temporary = path.with_suffix(".tmp")
    assert_no_links(temporary)
    temporary.write_bytes(encoded(report))
    temporary.replace(path)


def _store_failure_receipt(task: dict, run: dict, receipt: dict) -> dict:
    """优先随执行目录落盘；目录本身故障时退回既有任务根，不掩盖原异常。"""
    directory = Path(run["directory"])
    if directory.resolve().is_relative_to(_root(task).resolve()) and directory.is_dir():
        try:
            return store(directory / "failed", receipt)
        except (OSError, ValueError):
            pass
    return store(_root(task) / "diagnostic_failures", receipt)


def _sync_diagnostic_review(task: dict, run: dict) -> None:
    """恢复后同步审阅历史副本，避免任务JSON残留伪running状态。"""
    for review in reversed(task.get("diagnostic_reviews", [])):
        if review.get("request") != run.get("request") or review.get("review") != run.get("review"):
            continue
        for key in (
            "status", "phase", "outcome", "last_phase", "report", "result", "observation",
            "failure", "failure_receipt", "failure_receipt_status", "recovery_cleanup",
            "last_action", "reconciliation_required",
        ):
            if key in run:
                review[key] = run[key]
        return


def _fail_run(task, run, error, *, failure=None, outcome="failed", reconcile=False):
    """正常执行与恢复共用失败出口；不以异常结束代替已验证的目标完成。"""
    phase = run.get("last_phase", run.get("phase", "unknown"))
    failure = failure or _diagnostic_failure(error, phase)
    receipt = {"kind": "diagnostic_execution_failure", "task_id": task["task_id"],
               "execution_id": Path(run["directory"]).name, "revision": run.get("revision"),
               "request": run["request"], "review": run["review"], "phase": phase,
               "last_action": run.get("last_action", "unknown"),
               "outcome": outcome, "failure": failure, "reconciliation_required": reconcile,
               **({"report": run["report"]} if run.get("report") else {})}
    run.update(status="failed", phase="failed", last_phase=phase, outcome=outcome,
               failure=failure, reconciliation_required=reconcile)
    try:
        run["failure_receipt"] = _store_failure_receipt(task, run, receipt)
    except (OSError, ValueError):
        run["failure_receipt_status"] = "unavailable"
    task.update(status="execution_incomplete", pending_diagnostic=None,
                stop_reason="diagnostic_integrity_requires_reconciliation" if reconcile
                else "diagnostic_execution_failed")
    _sync_diagnostic_review(task, run)


def _validate_execution_evidence(task, receipt, snapshot):
    if receipt["revision"] != snapshot.revision or receipt["task_id"] != task["task_id"]:
        raise DiagnosticContractError("Diagnostic execution identity changed")
    for ref in receipt.get("dependencies", []):
        path = Path(ref["path"])
        assert_no_links(path)
        if (not path.resolve().is_relative_to(Path(task["work_root"]).resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]):
            raise DiagnosticContractError("Diagnostic execution evidence changed")


def _completed_diagnostic_stage(stage, *, probe=False):
    """全跳过只补充探针执行完整性，不能建立通过或业务覆盖证据。"""
    if stage.get("status") in {"passed", "failed", "not_supplied"}:
        return True
    if (not probe or stage.get("status") != "error"
            or stage.get("execution_status") != "completed"
            or stage.get("test_outcome") != "all_skipped"
            or type(stage.get("exit_code")) is not int or stage["exit_code"] != 0
            or stage.get("process_exit_status") is not None):
        return False
    counts, nodeids = stage.get("tests"), stage.get("nodeids")
    expected, actual = stage.get("expected_python"), stage.get("python_version")
    cleanup = stage.get("cleanup")
    if (not isinstance(counts, dict) or set(counts) != {"collected", "passed", "failed", "errors", "skipped"}
            or any(type(value) is not int or value < 0 for value in counts.values())
            or counts["collected"] <= 0 or counts["skipped"] != counts["collected"]
            or any(counts[name] for name in ("passed", "failed", "errors"))
            or not isinstance(nodeids, list) or len(nodeids) != counts["collected"]
            or any(not isinstance(node, str) or not node for node in nodeids)
            or len(set(nodeids)) != len(nodeids)
            or not isinstance(expected, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", expected)
            or not isinstance(actual, str) or not _PYTHON_VERSION.fullmatch(actual)
            or not actual.startswith(expected + ".")
            or not isinstance(cleanup, dict)
            or not {"container", "materialization_container", "snapshot_image"} <= set(cleanup)
            or any(not isinstance(row, dict) or row.get("ok") is not True for row in cleanup.values())):
        return False
    return True


def _execution_phase(report):
    """依据阶段记录而非顶层终止字符串判断；失败不等于目标已经执行。"""
    stages = list(report.get("stages", {}).values())
    probe = report.get("kind") == "probe" and report.get("check_group") == "probe"
    if (any(s.get("status") != "not_supplied" and _completed_diagnostic_stage(s, probe=probe) for s in stages)
            and all(_completed_diagnostic_stage(s, probe=probe) for s in stages)):
        return "target_completed"
    phase = report.get("last_phase", report.get("phase"))
    if phase in {"started", "preparing", "target_started"}:
        return phase
    if any(s.get("status") in {"running", "incomplete", "error", "timeout", "passed", "failed"} for s in stages):
        return "target_started"
    if str(report.get("last_action", "")).startswith("prepare_"):
        return "preparing"
    return "started"


def public_knowledge(task, reference):
    from .generation.project_context import bind_pages

    value = read(reference, _root(task))
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, value["source_reference"])
    receipt = Path(value["receipt_path"])
    assert_no_links(receipt)
    if not receipt.resolve().is_relative_to(Path(task["work_root"]).resolve()):
        raise ValueError("Knowledge receipt outside task work root")
    data = receipt.read_bytes()
    report = json.loads(data)
    if (hashlib.sha256(data).hexdigest() != value["receipt_sha256"]
            or value["task_id"] != task["task_id"]
            or {k: v for k, v in report.get("action", {}).items() if k != "issues"} != value["action"]
            or report.get("status") != "action_ready"
            or report.get("case_fingerprint") != case.fingerprint
            or report.get("candidate_revision") != snapshot.revision):
        raise ValueError("Knowledge generation receipt changed")
    frozen = read(report["diagnostic_context_reference"], _root(task))
    author = "solver"
    preparation = frozen.get("preparation_binding")
    if preparation is None and (
        value.get("author") is not None or value.get("preparation_binding") is not None
        or report.get("preparation_binding") is not None or report.get("role") == "repository_reader"
        or report.get("output_format") == "repository_context_actions"
        or task.get("preparation_binding") is not None
        or task.get("protocol", {}).get("repository_preparation") is not None
    ):
        raise ValueError("Prepared knowledge cannot downgrade to legacy authorship")
    if preparation is not None:
        from .generation.preparation import action_options, validate_binding
        from .generation.project_context import validate_action
        from .generation.request import MAX_REQUEST_CAPACITY
        from .service.recovery import read_bound

        author, output = validate_binding(preparation)
        body = read_bound(receipt.with_name("request.json"), Path(task["work_root"]), MAX_REQUEST_CAPACITY)
        request_context = json.loads(json.loads(body)["messages"][1]["content"])
        if (str(receipt.with_name("request.json")) != report.get("request_path")
                or hashlib.sha256(body).hexdigest() != report.get("request_sha256")
                or request_context["diagnostic_state"].get("preparation_binding") != preparation
                or request_context["task_context"]["task_id"] != task["task_id"]):
            raise ValueError("Knowledge differs from its actual generation request")
        if (value.get("author") != author or value.get("preparation_binding") != preparation
                or report.get("preparation_binding") != preparation
                or report.get("role") != author or report["output_format"] != output
                or preparation["task_id"] != task["task_id"]):
            raise ValueError("Knowledge preparation author changed")
        validate_action(case, snapshot, value["action"], frozen["project_context_policy"],
                        **action_options(preparation))
    if frozen["task_id"] != task["task_id"] or frozen.get("context_role") != author:
        raise ValueError("Knowledge was generated for another task or role")
    from .generation.provider import _decode_json, _proposal

    response_path = Path(report["response_path"])
    assert_no_links(response_path)
    if response_path.parent != receipt.parent:
        raise ValueError("Knowledge provider response is outside its receipt")
    response_bytes = response_path.read_bytes()
    if (hashlib.sha256(response_bytes).hexdigest() != report["response_sha256"]
            or _proposal(_decode_json(response_bytes), report["output_format"])["action"] != report["action"]):
        raise ValueError("Knowledge differs from its recorded model response")
    result = bind_pages(case, snapshot, value["action"]["pages"])
    if value["knowledge"] != result:
        raise ValueError("Knowledge source identity changed")
    return result


def public_observation(task, reference):
    value = read(reference, _root(task))
    if value["task_id"] != task["task_id"] or value["case_fingerprint"] != task["case_fingerprint"]:
        raise ValueError("Observation belongs to another task")
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, value["source_reference"])
    if snapshot.revision != value["revision"]:
        raise ValueError("Observation source changed")
    if value["kind"] == "source":
        if navigate(case, snapshot, value["action"]) != value["result"]:
            raise ValueError("Source observation changed")
    elif value["kind"] == "project_context":
        from .generation.project_context import knowledge_result

        if value["action"]["type"] == "complete_knowledge_review":
            from .generation.knowledge_review import completion_result, require_action

            context = _validate_investigation_selection(task, value, snapshot)
            require_action(context["diagnostic_state"].get("knowledge_review_binding"), value["action"], snapshot)
            expected = completion_result(case, snapshot, value["action"], context["project_context"]["topic_maintenance"])
        elif value["action"]["type"] == "revise_project_topic":
            from .topic_maintenance import revision_result

            context = _validate_investigation_selection(task, value, snapshot)
            expected = revision_result(case, snapshot, value["action"], context["project_context"]["topic_maintenance"])
        elif (value["action"]["type"] == "read_project_topic"
              and (task.get("protocol", task).get("project_context_policy") or {}).get("version") in {"project-context-v8", "project-context-v9", "project-context-v10"}):
            context = _validate_investigation_selection(task, value, snapshot, roles={"solver", "investigator"})
            expected = knowledge_result(case, snapshot, value["action"],
                context_policy=task.get("protocol", task)["project_context_policy"],
                current_topics=context["project_context"]["topic_maintenance"])
        else:
            if value["action"]["type"] == "select_investigation":
                _validate_investigation_selection(task, value, snapshot)
            knowledge = public_knowledge(task, value["knowledge_reference"]) if value.get("knowledge_reference") else None
            expected = knowledge_result(case, snapshot, value["action"], knowledge,
                                        context_policy=task.get("protocol", task).get("project_context_policy"))
        if expected != value["result"]:
            raise ValueError("Project knowledge observation changed")
    elif value.get("execution_reference"):
        receipt = read(value["execution_reference"], _root(task))
        if receipt["public"] != value["result"]:
            raise ValueError("Diagnostic execution identity changed")
        _validate_execution_evidence(task, receipt, snapshot)
    return observation_projection(value, reference["id"])


def observation_projection(value, identity):
    """落盘前后使用同一公开容量；复核处置只在 result 中发送一次。"""
    omitted = {"code", "pages", "page"}
    if value["action"]["type"] == "complete_knowledge_review":
        omitted |= {"topics", "remaining_scope"}
    action = {k: v for k, v in value["action"].items() if k not in omitted}
    public = {"id": identity, "kind": value["kind"], "revision": value["revision"],
              "action": action, "result": value["result"]}
    if len(encoded(public)) > 24_000:
        raise ValueError("Public observation exceeds capacity")
    return public


def _validate_investigation_selection(task, value, snapshot, *, roles=("solver",)):
    """选择须来自本任务 Solver 的原始回执，不能只重写观察 hash 换掉策略。"""
    from .generation.project_context import validate_action
    from .generation.provider import _decode_json, _proposal

    binding = value["generation_receipt"]
    path = Path(binding["path"])
    assert_no_links(path)
    if not path.resolve().is_relative_to(Path(task["work_root"]).resolve()):
        raise ValueError("Investigation receipt outside task")
    data = path.read_bytes()
    report = json.loads(data)
    if hashlib.sha256(data).hexdigest() != binding["sha256"]:
        raise ValueError("Investigation receipt changed")
    frozen = read(report["diagnostic_context_reference"], _root(task))
    config = task.get("protocol", task).get("project_context_policy")
    role = frozen.get("context_role")
    expected_format = {"solver": "protocol_v6_actions", "investigator": "investigator_actions"}.get(role)
    if (frozen["task_id"] != task["task_id"] or role not in roles
            or report.get("report_path") != str(path)
            or report.get("output_format") != expected_format
            or frozen.get("project_context_policy") != config
            or report.get("status") != "action_ready"
            or report.get("case_fingerprint") != task["case_fingerprint"]
            or report.get("candidate_revision") != snapshot.revision
            or {k: v for k, v in report.get("action", {}).items() if k != "issues"} != value["action"]):
        raise ValueError("Investigation selection identity changed")
    request_path = Path(report["request_path"])
    assert_no_links(request_path)
    if request_path.parent != path.parent:
        raise ValueError("Investigation request outside receipt")
    request_data = request_path.read_bytes()
    context = json.loads(json.loads(request_data)["messages"][1]["content"])
    if (hashlib.sha256(request_data).hexdigest() != report["request_sha256"]
            or context["task_context"]["task_id"] != task["task_id"]
            or context["candidate"]["revision"] != snapshot.revision
            or context["diagnostic_state"].get("project_context_policy") != config):
        raise ValueError("Investigation request identity changed")
    response_path = Path(report["response_path"])
    assert_no_links(response_path)
    if response_path.parent != path.parent:
        raise ValueError("Investigation response outside receipt")
    response = response_path.read_bytes()
    if (hashlib.sha256(response).hexdigest() != report["response_sha256"]
            or _proposal(_decode_json(response), report["output_format"])["action"] != report["action"]):
        raise ValueError("Investigation selection differs from model response")
    validate_action(load_case(Path(task["manifest_path"])), snapshot, value["action"], config, role=role)
    return context


def public_historical_input(task: dict, reference: dict, *, snapshot=None) -> dict:
    """公开旧截点的派生副本；它只能解释起点，不能冒充当前执行或验收。"""
    if reference not in task.get("historical_inputs", []):
        raise ValueError("Historical input is not registered on this task")
    binding = task.get("historical_binding")
    if (
        not isinstance(binding, dict)
        or binding.get("input_id") != reference.get("id")
        or not isinstance(binding.get("candidate_reference"), dict)
    ):
        raise ValueError("Historical input is not bound to this task")
    value = read(reference, _root(task))
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "historical_decision_context"
        or value.get("classification") != "historical_only"
        or value.get("case_fingerprint") != task["case_fingerprint"]
        or value.get("primary_input_sha256") != binding.get("primary_input_sha256")
        or value.get("evidence_namespace")
        != f"historical:{binding.get('primary_input_sha256')}"
    ):
        raise ValueError("Historical input identity changed")
    case = load_case(Path(task["manifest_path"]))
    base = load_candidate(case, binding["candidate_reference"])
    snapshot = snapshot or load_candidate(case, task["current_candidate"])
    candidate = value.get("candidate", {})
    if (
        candidate.get("revision") != base.revision
        or candidate.get("patch_sha256") != base.sha256
        or binding.get("candidate_revision") != base.revision
        or binding.get("candidate_sha256") != base.sha256
    ):
        raise ValueError("Historical input belongs to another candidate")
    public = {
        "id": reference["id"],
        "classification": "historical_only",
        "primary_input_sha256": value["primary_input_sha256"],
        "evidence_namespace": value["evidence_namespace"],
        "historical_observation_refs": value.get("historical_observation_refs", []),
        "source": value["source"],
        "candidate": candidate,
        "stale": snapshot.revision != base.revision or snapshot.sha256 != base.sha256,
        "visible": value["visible"],
        "limitations": value["limitations"],
    }
    if len(encoded(public)) > 256_000:
        raise ValueError("Historical input exceeds public context capacity")
    return public


def _dependency_query_row(task: dict, request_id: str) -> dict:
    row = next(
        (
            item
            for item in task.get("dependency_queries", [])
            if item.get("request", {}).get("id") == request_id
        ),
        None,
    )
    if row is None:
        raise ValueError("Dependency query is not registered on this task")
    return row


def _dependency_environment(value: object, *, role: str, lock_sha256: str) -> dict:
    required = {"role", "lock_sha256", "image_id", "base_image_id", "python_version"}
    optional = {"cache_key", "preparation_sha256"}
    if not isinstance(value, dict) or not required <= set(value) <= required | optional:
        raise ValueError("Dependency query returned an invalid environment identity")
    if value["role"] != role or value["lock_sha256"] != lock_sha256:
        raise ValueError("Dependency query environment does not match the registered lock")
    if not _SHA256.fullmatch(value["lock_sha256"]):
        raise ValueError("Dependency query returned an invalid lock identity")
    if not _IMAGE_ID.fullmatch(value["image_id"]) or not _IMAGE_ID.fullmatch(
        value["base_image_id"]
    ):
        raise ValueError("Dependency query returned an invalid image identity")
    if not isinstance(value["python_version"], str) or not _PYTHON_VERSION.fullmatch(
        value["python_version"]
    ):
        raise ValueError("Dependency query returned an invalid Python identity")
    for field in optional.intersection(value):
        if not isinstance(value[field], str) or not _SHA256.fullmatch(value[field]):
            raise ValueError(f"Dependency query returned an invalid {field}")
    return dict(value)


def _validate_dependency_result(task: dict, case, request: dict, value: object) -> dict:
    required = {
        "case_fingerprint",
        "status",
        "environment",
        "distribution",
        "installed_version",
        "operation",
        "result",
        "limitations",
    }
    allowed = required | {"binding_sha256", "code", "scope_limit"}
    if not isinstance(value, dict) or not required <= set(value) <= allowed:
        raise ValueError("Dependency query returned an invalid receipt schema")
    action = request["action"]
    if value["case_fingerprint"] != case.fingerprint:
        raise ValueError("Dependency query result belongs to another case")
    if value["distribution"] != action["distribution"] or value["operation"] != action["operation"]:
        raise ValueError("Dependency query result does not match the requested operation")
    status = value["status"]
    if status not in {"observed", "unavailable"}:
        raise ValueError("Dependency query returned an invalid public status")
    version = value["installed_version"]
    if status == "observed" and (
        not isinstance(version, str) or not version or len(version) > 128
        or any(ord(character) < 32 for character in version)
    ):
        raise ValueError("Dependency query returned an invalid installed version")
    if status == "unavailable" and version is not None and (
        not isinstance(version, str) or not version or len(version) > 128
        or any(ord(character) < 32 for character in version)
    ):
        raise ValueError("Dependency query returned an invalid unavailable-version value")
    if status == "unavailable" and (
        not isinstance(value.get("code"), str)
        or not value["code"]
        or len(value["code"]) > 128
    ):
        raise ValueError("Unavailable dependency information requires one bounded code")
    limitations = value["limitations"]
    if (
        not isinstance(limitations, list)
        or len(limitations) > 16
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 1_000
            or any(ord(character) < 32 and character not in "\n\t" for character in item)
            for item in limitations
        )
    ):
        raise ValueError("Dependency query returned invalid limitations")
    if "binding_sha256" in value and (
        not isinstance(value["binding_sha256"], str)
        or not _SHA256.fullmatch(value["binding_sha256"])
    ):
        raise ValueError("Dependency query returned an invalid source binding")
    lock = case.old_lock if action["environment"] == "old" else case.new_lock
    lock_name = lock.relative_to(case.root).as_posix()
    lock_sha256 = hashlib.sha256(
        read_verified_file(case.root, lock_name, case.manifest.file_hashes[lock_name])
    ).hexdigest()
    environment = _dependency_environment(
        value["environment"], role=action["environment"], lock_sha256=lock_sha256
    )
    result = dict(value)
    result["environment"] = environment
    limit = task["protocol"]["dependency_query_policy"]["max_result_bytes"]
    if len(encoded(result)) > limit:
        raise ValueError("Dependency query result exceeds the frozen byte limit")
    return result


def _dependency_execution_result(task: dict, case, request: dict, directory: Path, raw: object):
    """把完整执行报告投影为事实输入，同时保留可追溯的原始收据。"""
    if not isinstance(raw, dict):
        raise ValueError("Dependency query runner did not return a report")
    required = {
        "schema_version", "kind", "case_fingerprint", "action", "lock_sha256",
        "query_id", "report_path", "status", "public",
    }
    if not required <= set(raw):
        raise ValueError("Dependency query runner report is incomplete")
    if (
        raw["schema_version"] != 1
        or raw["kind"] != "dependency_query"
        or raw["case_fingerprint"] != case.fingerprint
        or raw["action"] != request["action"]
        or not isinstance(raw["query_id"], str)
        or not _SHA256.fullmatch(raw["query_id"])
    ):
        raise ValueError("Dependency query runner report identity changed")
    lock = case.old_lock if request["action"]["environment"] == "old" else case.new_lock
    lock_name = lock.relative_to(case.root).as_posix()
    expected_lock = hashlib.sha256(
        read_verified_file(case.root, lock_name, case.manifest.file_hashes[lock_name])
    ).hexdigest()
    if raw["lock_sha256"] != expected_lock:
        raise ValueError("Dependency query runner used a different registered lock")
    report_path = Path(raw["report_path"])
    expected_path = directory / "report.json"
    assert_no_links(report_path)
    if (
        report_path != expected_path
        or not report_path.resolve().is_relative_to(_root(task).resolve())
        or report_path.stat().st_size > 2_000_000
    ):
        raise ValueError("Dependency query report is outside its registered run directory")
    report_bytes = report_path.read_bytes()
    if json.loads(report_bytes) != raw:
        raise ValueError("Dependency query report differs from its persisted receipt")
    reference = {
        "path": str(report_path),
        "sha256": hashlib.sha256(report_bytes).hexdigest(),
        "query_id": raw["query_id"],
        "status": raw["status"],
    }
    if raw["status"] == "execution_incomplete":
        if raw["public"] is not None:
            raise ValueError("Incomplete dependency execution cannot publish a public fact")
        return None, reference
    if raw["status"] not in {"observed", "unavailable"}:
        raise ValueError("Dependency query runner returned an unknown status")
    public = raw["public"]
    if not isinstance(public, dict) or public.get("status") != raw["status"]:
        raise ValueError("Dependency query public status disagrees with its execution report")
    projected = {
        key: public[key]
        for key in (
            "status", "environment", "distribution", "installed_version", "operation",
            "result", "limitations",
        )
    }
    for key in ("code", "scope_limit", "binding_sha256"):
        if key in public:
            projected[key] = public[key]
    return {"case_fingerprint": case.fingerprint, **projected}, reference


def _persisted_dependency_report(directory: Path) -> dict:
    """读取游标落后时已存在的报告；执行目录一旦出现就不允许隐式重跑。"""
    assert_no_links(directory)
    if not directory.is_dir():
        raise DiagnosticContractError(
            "Dependency query run directory exists but is not a directory"
        )
    report_path = directory / "report.json"
    assert_no_links(report_path)
    if not report_path.is_file():
        raise DiagnosticContractError(
            "Dependency query execution directory lacks its persisted report"
        )
    if report_path.stat().st_size > 2_000_000:
        raise DiagnosticContractError("Dependency query report exceeds its size limit")
    try:
        value = json.loads(report_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DiagnosticContractError(
            "Dependency query persisted report is unreadable"
        ) from error
    if not isinstance(value, dict):
        raise DiagnosticContractError("Dependency query persisted report is not an object")
    return value


def public_fact(task: dict, reference: dict) -> dict:
    """读取任务内不可变依赖事实；候选变化不影响纯依赖环境身份。"""
    value = read(reference, _root(task))
    if (
        value.get("kind") != "dependency_fact"
        or value.get("task_id") != task["task_id"]
        or value.get("case_fingerprint") != task["case_fingerprint"]
        or value.get("candidate_binding") is not None
    ):
        raise ValueError("Dependency fact belongs to another task or binding scope")
    query = _dependency_query_row(task, value["query_id"])
    request = read(query["request"], _root(task))
    receipt = read(value["receipt"], _root(task))
    if (
        query.get("fact") != reference
        or query.get("result") != value["receipt"]
        or request.get("action") != value["action"]
        or receipt.get("query_id") != value["query_id"]
        or receipt.get("result") != value["result"]
    ):
        raise ValueError("Dependency fact identity changed")
    execution_report = receipt.get("execution_report")
    if execution_report is not None:
        if not isinstance(execution_report, dict) or set(execution_report) != {
            "path", "sha256", "query_id", "status",
        }:
            raise ValueError("Dependency execution report reference changed")
        path = Path(execution_report["path"])
        assert_no_links(path)
        if (
            not path.resolve().is_relative_to(_root(task).resolve())
            or hashlib.sha256(path.read_bytes()).hexdigest() != execution_report["sha256"]
        ):
            raise ValueError("Dependency execution report changed")
        report = json.loads(path.read_bytes())
        if (
            report.get("query_id") != execution_report["query_id"]
            or report.get("status") != execution_report["status"]
            or report.get("public", {}).get("status") != value["result"]["status"]
        ):
            raise ValueError("Dependency execution report no longer supports this fact")
    environment = value["result"]["environment"]
    current = task.get("dependency_environment_identities", {}).get(environment["role"])
    public = {
        "id": reference["id"],
        "query_id": value["query_id"],
        "environment": environment,
        "distribution": value["result"]["distribution"],
        "installed_version": value["result"]["installed_version"],
        "operation": value["result"]["operation"],
        "status": value["result"]["status"],
        "result": value["result"]["result"],
        "limitations": value["result"]["limitations"],
        "candidate_binding": None,
        "stale": current is not None and current != environment,
    }
    for key in ("code", "scope_limit"):
        if key in value["result"]:
            public[key] = value["result"][key]
    query_policy = task.get("protocol", {}).get(
        "dependency_query_policy", task.get("dependency_query_policy", {})
    )
    if len(encoded(public)) > query_policy["max_result_bytes"] + 4_000:
        raise ValueError("Public dependency fact exceeds capacity")
    return public


def queue_dependency_query(task: dict, response: dict, case, action: dict) -> bool:
    """登记一次与候选无关的依赖查询；相同完成或失败请求不会重放。"""
    request = {
        "kind": "dependency_query_request",
        "task_id": task["task_id"],
        "case_fingerprint": case.fingerprint,
        "action": action,
        "candidate_binding": None,
    }
    key = digest(request)
    for row in task["dependency_queries"]:
        if row["key"] != key:
            continue
        read(row["request"], _root(task))
        task["dependency_query_reused"] = True
        task["latest_dependency_query"] = row["request"]["id"]
        if row["status"] == "completed":
            public_fact(task, row["fact"])
            task["latest_fact"] = row["fact"]["id"]
            task["status"] = "ready"
            return True
        if row["status"] == "failed":
            read(row["failure_receipt"], _root(task))
            task["dependency_query_feedback"] = {
                "code": "dependency_query_failed_no_automatic_replay",
                "query_id": row["request"]["id"],
            }
            task["status"] = "ready"
            return True
        task["pending_dependency_query"] = {"key": key, "request": row["request"]}
        task["status"] = "pending_dependency_query"
        return False
    policy = task["protocol"]["dependency_query_policy"]
    if len(task["dependency_queries"]) >= policy["max_queries"]:
        raise ValueError("Dependency query limit reached")
    reference = store(_root(task) / "dependency_query_requests", request)
    row = {
        "key": key,
        "request": reference,
        "status": "pending",
        "origin_receipt": response["report_path"],
    }
    task["dependency_queries"].append(row)
    task["pending_dependency_query"] = {"key": key, "request": reference}
    task["latest_dependency_query"] = reference["id"]
    task["dependency_query_reused"] = False
    task.pop("dependency_query_feedback", None)
    task["status"] = "pending_dependency_query"
    return False


def _pending_dependency_query(task: dict) -> tuple[dict, dict, Path]:
    pending = task.get("pending_dependency_query")
    if task.get("schema_version") != 5 or not pending:
        raise ValueError("No pending Protocol 6 dependency query")
    row = _dependency_query_row(task, pending["request"]["id"])
    if row["status"] != "pending" or row["key"] != pending["key"]:
        raise ValueError("Pending dependency query state changed")
    request = read(row["request"], _root(task))
    directory = _root(task) / "dependency_query_runs" / row["request"]["id"]
    return row, request, directory


def recover_pending_dependency_query(task: dict, case) -> dict:
    """仅接纳完整落盘的查询；调用方持有原作业锁，任何缺失或冲突均不执行补跑。"""
    if task.get("status") != "pending_dependency_query":
        raise ValueError("Dependency query is not pending recovery")
    row, request, directory = _pending_dependency_query(task)
    raw = _persisted_dependency_report(directory)
    projected, execution_reference = _dependency_execution_result(
        task, case, request, directory, raw
    )
    if projected is None:
        raise DiagnosticContractError("Dependency query execution did not complete")
    result = _validate_dependency_result(task, case, request, projected)
    return _register_dependency_query_result(task, row, request, result, execution_reference)


def run_pending_dependency_query(task: dict, case, *, runner=None) -> dict:
    """执行一个已登记查询；调用方负责持有任务锁，失败收据禁止隐式重放。"""
    row, request, directory = _pending_dependency_query(task)
    if runner is None:
        from .execution import run_case_dependency_query

        runner = run_case_dependency_query
    assert_no_links(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    execution = task["protocol"].get("execution", {})
    execution_reference = None
    recovering_persisted_report = directory.exists()
    try:
        if recovering_persisted_report:
            raw = _persisted_dependency_report(directory)
        else:
            raw = runner(
                case,
                request["action"],
                directory,
                prepare_timeout=execution.get("prepare_timeout", 300),
                query_timeout=execution.get("test_timeout", 120),
            )
        projected, execution_reference = _dependency_execution_result(
            task, case, request, directory, raw
        )
        if projected is None:
            raise DiagnosticContractError("Dependency query execution did not complete")
        result = _validate_dependency_result(task, case, request, projected)
    except Exception as error:
        failure = {
            "kind": "dependency_query_failure",
            "task_id": task["task_id"],
            "case_fingerprint": task["case_fingerprint"],
            "query_id": row["request"]["id"],
            "error_type": type(error).__name__,
            "reason": "Dependency query did not produce a valid immutable fact; inspect and retry with a new explicit action.",
            "reconciliation_required": recovering_persisted_report,
        }
        if execution_reference is not None:
            failure["execution_report"] = execution_reference
        row.update(
            status="failed",
            failure_receipt=store(_root(task) / "dependency_query_failures", failure),
            reconciliation_required=recovering_persisted_report,
        )
        task.update(
            status="execution_incomplete" if recovering_persisted_report else "ready",
            pending_dependency_query=None,
        )
        task["dependency_query_feedback"] = {
            "code": (
                "dependency_query_reconciliation_required"
                if recovering_persisted_report
                else "dependency_query_failed_no_automatic_replay"
            ),
            "query_id": row["request"]["id"],
            "error_type": type(error).__name__,
        }
        if recovering_persisted_report:
            task["stop_reason"] = "persisted_dependency_query_requires_reconciliation"
        return task
    return _register_dependency_query_result(task, row, request, result, execution_reference)


def _register_dependency_query_result(
    task: dict, row: dict, request: dict, result: dict, execution_reference: dict,
) -> dict:
    receipt_value = {
        "kind": "dependency_query_result",
        "task_id": task["task_id"],
        "case_fingerprint": task["case_fingerprint"],
        "query_id": row["request"]["id"],
        "result": result,
        "execution_report": execution_reference,
    }
    receipt = store(_root(task) / "dependency_query_receipts", receipt_value)
    fact_value = {
        "kind": "dependency_fact",
        "task_id": task["task_id"],
        "case_fingerprint": task["case_fingerprint"],
        "query_id": row["request"]["id"],
        "action": request["action"],
        "receipt": receipt,
        "result": result,
        "candidate_binding": None,
    }
    fact = store(_root(task) / "dependency_facts", fact_value)
    if fact not in task["dependency_facts"]:
        task["dependency_facts"].append(fact)
    row.update(status="completed", result=receipt, fact=fact)
    role = result["environment"]["role"]
    task["dependency_environment_identities"][role] = result["environment"]
    task.update(
        status="ready",
        pending_dependency_query=None,
        latest_fact=fact["id"],
        latest_dependency_query=row["request"]["id"],
    )
    task.pop("dependency_query_feedback", None)
    return task


def _handoff_evidence_refs(action: dict) -> list[str]:
    refs: list[str] = []
    refs.extend(
        ref
        for item in action.get("observed_facts", [])
        for ref in item.get("evidence_refs", [])
    )
    refs.extend(
        ref
        for item in action.get("remaining_hypotheses", [])
        for ref in item.get("evidence_refs", [])
    )
    refs.extend(
        ref
        for item in action.get("conflicts", [])
        for ref in item.get("evidence_refs", [])
    )
    next_action = action.get("next_discriminating_action")
    if isinstance(next_action, dict):
        refs.extend(next_action.get("evidence_refs", []))
    return refs


def public_investigator_handoff(task: dict, reference: dict, *, snapshot=None) -> dict:
    """读取只读调查交接；交接只转述来源，不升级为事实或候选决定。"""
    value = read(reference, _root(task))
    if (
        value.get("kind") != "investigator_handoff"
        or value.get("task_id") != task["task_id"]
        or value.get("case_fingerprint") != task["case_fingerprint"]
    ):
        raise ValueError("Investigator handoff belongs to another task")
    receipt = Path(value["response_receipt"]["path"])
    assert_no_links(receipt)
    if (
        not receipt.resolve().is_relative_to(Path(task["work_root"]).resolve())
        or hashlib.sha256(receipt.read_bytes()).hexdigest()
        != value["response_receipt"]["sha256"]
    ):
        raise ValueError("Investigator response receipt changed")
    registered = next(
        (item for item in task.get("investigator_handoffs", []) if item == reference),
        None,
    )
    if registered is None:
        raise ValueError("Investigator handoff is not registered on this task")
    snapshot = snapshot or load_candidate(
        load_case(Path(task["manifest_path"])), task["current_candidate"]
    )
    return {
        "id": reference["id"],
        "revision": value["revision"],
        "patch_sha256": value["patch_sha256"],
        "stale": (
            value["revision"] != snapshot.revision
            or value["patch_sha256"] != snapshot.sha256
        ),
        "observed_facts": value["handoff"]["observed_facts"],
        "scope_limits": value["handoff"]["scope_limits"],
        "remaining_hypotheses": value["handoff"]["remaining_hypotheses"],
        "conflicts": value["handoff"]["conflicts"],
        "next_discriminating_action": value["handoff"]["next_discriminating_action"],
        "underlying_evidence_refs": value["underlying_evidence_refs"],
        "advisory_only": True,
    }


def persist_investigator_handoff(task: dict, response: dict, case, action: dict) -> dict:
    """把交接绑定到模型收据和当时基底；不授予候选或终态写权限。"""
    snapshot = load_candidate(case, task["current_candidate"])
    receipt = Path(response["report_path"])
    assert_no_links(receipt)
    if not receipt.resolve().is_relative_to(Path(task["work_root"]).resolve()):
        raise ValueError("Investigator receipt is outside the task workspace")
    refs = _handoff_evidence_refs(action)
    resolve_refs(task, refs, response=response)
    value = {
        "kind": "investigator_handoff",
        "task_id": task["task_id"],
        "case_fingerprint": case.fingerprint,
        "revision": snapshot.revision,
        "patch_sha256": snapshot.sha256,
        "source_reference": snapshot.reference,
        "handoff": {key: item for key, item in action.items() if key != "type"},
        "underlying_evidence_refs": refs,
        "response_receipt": {
            "path": str(receipt),
            "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            "request_sha256": response.get("request_sha256"),
        },
    }
    reference = store(_root(task) / "investigator_handoffs", value)
    if reference not in task["investigator_handoffs"]:
        task["investigator_handoffs"].append(reference)
    session = next(
        (
            item
            for item in reversed(task.get("investigator_sessions", []))
            if item.get("status") == "active"
        ),
        None,
    )
    if session is None:
        session = {
            "id": digest([task["task_id"], "investigator", len(task["investigator_sessions"]) + 1]),
            "status": "active",
            "attempt_indices": [],
            "start_revision": snapshot.revision,
        }
        task["investigator_sessions"].append(session)
    session.update(
        status="completed",
        handoff=reference,
        end_revision=snapshot.revision,
        end_patch_sha256=snapshot.sha256,
    )
    task["latest_investigator_handoff"] = reference["id"]
    task["status"] = "ready"
    return reference


def consume_investigator_handoffs(task: dict, context_reference: dict) -> None:
    """仅在Solver请求已成功物化后消费该上下文实际携带的交接。"""
    context = read(context_reference, _root(task))
    if context.get("context_role") != "solver":
        return
    consumed = task.setdefault("consumed_investigator_handoffs", [])
    known = {item["handoff_id"] for item in consumed}
    for reference in context.get("investigator_handoffs", []):
        public_investigator_handoff(task, reference)
        if reference["id"] not in known:
            consumed.append(
                {"handoff_id": reference["id"], "context_id": context_reference["id"]}
            )
            known.add(reference["id"])


def _candidate_snapshot(task, case, candidate):
    """从历史候选的补丁位置重建不可变 revision，并核对补丁与来源身份。"""
    patch = Path(candidate["patch_path"])
    assert_no_links(patch)
    if not patch.resolve().is_relative_to(Path(task["work_root"]).resolve()):
        raise ValueError("Historical candidate patch is outside the task workspace")
    if hashlib.sha256(patch.read_bytes()).hexdigest() != candidate["sha256"]:
        raise ValueError("Historical candidate patch changed")
    reference = {"path": str(patch.with_name("revision.json")), "revision": candidate["revision"]}
    snapshot = load_candidate(case, reference)
    if snapshot.sha256 != candidate["sha256"] or snapshot.origin != candidate["origin"]:
        raise ValueError("Historical candidate revision disagrees with its metadata")
    return snapshot


def restorable_candidates(task, case, current_revision, *, observations=None, limit=8):
    """只公开经过精确审阅且已有公开通过观察的历史 revision。"""
    if task.get("schema_version") == 5:
        options, _ = candidate_option_state(
            task, case, current_revision, observations=observations, limit=limit
        )
        return [item for item in options if item["revision"] != current_revision]
    if task.get("schema_version") != 4:
        return []
    observations = observations or [public_observation(task, ref) for ref in task["observations"]]
    passed = {}
    for row in observations:
        if row["kind"] == "public_checks" and row["result"].get("status") == "passed":
            passed.setdefault(row["revision"], []).append("observation:" + row["id"])
    result = []
    seen = set()
    for candidate in reversed(task.get("candidate_history", [])):
        revision = candidate.get("revision")
        if revision in seen or revision == current_revision or revision not in passed:
            continue
        seen.add(revision)
        if candidate.get("reviewed") is not True:
            continue
        review = next((
            item for item in reversed(task.get("review_history", []))
            if item.get("revision") == revision
            and item.get("sha256") == candidate.get("sha256")
            and item.get("reviewer") and item.get("note")
            and item.get("decision") != "rejected"
        ), None)
        if review is None:
            continue
        snapshot = _candidate_snapshot(task, case, candidate)
        result.append({
            "revision": snapshot.revision,
            "patch_sha256": snapshot.sha256,
            "origin": snapshot.origin,
            "public_pass_observation_refs": passed[revision][:4],
            "scope_limit": "Public feedback passed for these immutable bytes; independent final acceptance remains separate.",
        })
        if len(result) >= limit:
            break
    return result


def candidate_option_state(task, case, current_revision, *, observations=None, limit=8):
    """为 schema 5 投影同一公开 suite 的候选选项，不暴露宿主候选路径。"""
    if task.get("schema_version") != 5:
        return [], None
    from .candidate_selection import comparable_candidate_groups

    observations = observations or [public_observation(task, ref) for ref in task["observations"]]
    groups = comparable_candidate_groups(task, observations)
    if not groups:
        return [], None
    options = []
    simple_tools = task.get("protocol", task).get("workflow_profile", "workbench") == "simple_tools"
    choices = groups[0]
    if simple_tools:
        # 基线保留历史事实和手动恢复，不接受按通过数给出的宿主推荐。
        order = {row["revision"]: index for index, row in enumerate(task.get("candidate_history", []))}
        choices = sorted(choices, key=lambda item: order[item.revision], reverse=True)
    for coverage in choices:
        candidate = next((
            item for item in reversed(task.get("candidate_history", []))
            if item.get("revision") == coverage.revision
            and item.get("sha256") == coverage.patch_sha256
        ), None)
        if candidate is None:
            raise ValueError("Comparable candidate metadata disappeared")
        snapshot = _candidate_snapshot(task, case, candidate)
        if snapshot.revision != coverage.revision or snapshot.sha256 != coverage.patch_sha256:
            raise ValueError("Comparable candidate bytes changed")
        options.append({
            "revision": coverage.revision,
            "patch_sha256": coverage.patch_sha256,
            "origin": coverage.origin,
            "suite_sha256": coverage.suite.sha256,
            "public_observation_ref": coverage.observation_ref,
            "passed_nodeids": list(coverage.passed_nodeids),
            "failed_nodeids": list(coverage.failed_nodeids),
            "passed_count": coverage.passed_count,
            "total_count": coverage.total_count,
            "current_edit_base": coverage.revision == current_revision,
            "scope_limit": (
                "This immutable candidate was compared only on the listed complete public suite; "
                "partial public coverage is not independent final acceptance."
            ),
        })
        if len(options) >= limit:
            break
    return options, (dict(options[0]) if options and not simple_tools else None)


def restore_candidate(task, case, action, current):
    """恢复已验证过的历史候选指针；失败 revision 与观察全部原样保留。"""
    schema_version = task.get("schema_version")
    if schema_version == 5 and task.get("status") in {
        "submitted", "no_candidate", "budget_exhausted", "execution_incomplete",
        "generation_failed", "outcome_unknown", "interrupted", "candidate_rejected",
        "seed_failed", "no_change_claimed", "unresolved",
    }:
        raise ValueError("Terminal task history is read-only; create a new task to continue editing")
    eligible = {
        item["revision"]: item
        for item in restorable_candidates(task, case, current.revision)
    }
    target = eligible.get(action["revision"])
    if target is None:
        requirement = (
            "complete comparable public evidence"
            if schema_version == 5 else "public-pass evidence"
        )
        raise ValueError(
            "Restore requires an exposed reviewed historical candidate with " + requirement
        )
    cited = set(action["evidence_refs"])
    supporting = (
        {target["public_observation_ref"]}
        if schema_version == 5 else set(target["public_pass_observation_refs"])
    )
    matched = sorted(cited.intersection(supporting))
    if not matched:
        if schema_version == 5:
            raise ValueError(
                "Restore must cite the exposed complete public observation for the selected revision"
            )
        raise ValueError(
            "Restore must cite one exposed public-pass observation for the selected revision"
        )
    candidate = next(
        item for item in reversed(task["candidate_history"])
        if item.get("revision") == target["revision"] and item.get("sha256") == target["patch_sha256"]
    )
    snapshot = _candidate_snapshot(task, case, candidate)
    restored = {
        key: candidate[key]
        for key in ("sha256", "patch_path", "reviewed", "receipt", "revision", "origin", "feedback_report")
        if key in candidate
    }
    restored["reviewed"] = True
    task["current_candidate"] = snapshot.reference
    task["candidate"] = restored
    task["candidate_history"].append(restored | {
        "restoration": {
            "from_revision": current.revision,
            "reason": action["reason"],
            "evidence_ref": matched[0],
        }
    })
    task["feedback"] = None
    task["tool_results"] = []
    task["protocol_feedback"] = None
    task["edit_feedback"] = None
    task["diagnostic_reused"] = False
    task["latest_observation"] = matched[0].removeprefix("observation:")
    transient = ("feedback_revision", "duplicate_submission", "stop_reason")
    if schema_version in {4, 5}:
        transient += ("finish", "verification_subject")
    for key in transient:
        task.pop(key, None)
    task["status"] = "ready"
    return True


def observe(task, case, action, result, *, kind, snapshot=None, execution_reference=None, publish=True, knowledge_reference=None, generation_receipt=None):
    snapshot = snapshot or load_candidate(case, task["current_candidate"])
    value = {"task_id": task["task_id"], "case_fingerprint": case.fingerprint,
             "revision": snapshot.revision, "source_reference": snapshot.reference,
             "kind": kind, "action": action, "result": result,
             "execution_reference": execution_reference}
    if knowledge_reference is not None:
        value["knowledge_reference"] = knowledge_reference
    if generation_receipt is not None:
        value["generation_receipt"] = generation_receipt
    ref = store(_root(task) / "observations", value)
    if publish and ref not in task["observations"]:
        task["observations"].append(ref)
    return ref


class UnknownPublicEvidenceReference(ValueError):
    """仅表示引用名未登记；产物损坏或收据身份冲突不能归入此类。"""


def resolve_refs(task, refs, *, response=None):
    case = load_case(Path(task["manifest_path"]))
    available = {"business_contract"} | {"source:" + n for n in load_candidate(case).files}
    available |= {"observation:" + r["id"] for r in task["observations"]}
    if task.get("schema_version") == 5:
        for reference in task.get("historical_inputs", []):
            historical = public_historical_input(task, reference)
            available.update(historical["historical_observation_refs"])
        for reference in task.get("dependency_facts", []):
            public_fact(task, reference)
            available.add("fact:" + reference["id"])
    if any(ref.startswith("version:") for ref in refs):
        from .generation.request_evidence import RequestEvidenceError, visible_version_refs

        if response is None:
            raise RequestEvidenceError("Version evidence references require a bound response receipt")
        available |= visible_version_refs(task, response)
    invalid = sorted(set(refs) - available)
    if invalid:
        raise UnknownPublicEvidenceReference("Unknown public evidence references: " + repr(invalid)[:500] +
                         ". Use source:<exact source_inventory path> without an added prefix; "
                          "version:<exact version_evidence evidence_key>; business_contract; "
                          "observation:<existing ID>; historical:<input hash>:observation:<historical ID>; "
                          "or fact:<existing dependency fact ID>.")


def freeze_context(task, *, role: str | None = None) -> dict:
    """只固定已存在的公共产物引用，不把自由对话变成真实执行证据。"""
    preparation = task["protocol"].get("repository_preparation")
    if role is None:
        if preparation is not None:
            from .generation.preparation import phase
            from .generation.roles import preparation_contract

            role = preparation_contract(preparation, phase(task))[0]
        else:
            role = "solver"
    if role not in {"solver", "investigator", "contract_auditor", "repository_reader"}:
        raise ValueError("Unknown diagnostic context role")
    frozen = {key: task[key] for key in ("task_id", "task_path", "manifest_path", "case_fingerprint",
                                       "work_root", "current_candidate", "observations", "issues",
                                       "diagnostic_runs", "probes")}
    if task["protocol"].get("project_context_policy") is not None:
        frozen["project_context_policy"] = task["protocol"]["project_context_policy"]
        frozen["project_knowledge_reference"] = task.get("project_knowledge_reference")
        if task["protocol"].get("knowledge_import") is not None:
            frozen["knowledge_import"] = task["protocol"]["knowledge_import"]
            frozen["knowledge_import_reference"] = task["knowledge_import_reference"]
            frozen["execution"] = {"base_image": task["protocol"].get("execution", {}).get("base_image")}
    from .generation.knowledge_review import binding as review_binding
    from .generation.knowledge_review import enabled as review_enabled

    if review_enabled(task):
        frozen["knowledge_review_binding"] = review_binding(task, load_candidate(load_case(Path(task["manifest_path"])), task["current_candidate"]))
    if task["protocol"].get("source_read_retention") is not None:
        frozen["source_read_retention"] = task["protocol"]["source_read_retention"]
    if task["protocol"].get("finish_policy") is not None:
        frozen["finish_policy"] = task["protocol"]["finish_policy"]
    if task["protocol"].get("investigation_policy") is not None:
        frozen["investigation_policy"] = task["protocol"]["investigation_policy"]
    frozen["context_role"] = role
    if task["protocol"].get("semantic_risk_policy") is not None:
        frozen["semantic_risk_policy"] = task["protocol"]["semantic_risk_policy"]
    if task["protocol"].get("contract_audit_policy") in {"delivery-audit-v1", "delivery-audit-v2"}:
        frozen["delivery_review_policy"] = task["protocol"]["contract_audit_policy"]
        if role == "contract_auditor":
            frozen["delivery_draft_reference"] = (task.get("pending_contract_audit") or {}).get("solver_finish")
    if task["protocol"].get("investigator_context_policy") is not None:
        frozen["investigator_context_policy"] = task["protocol"]["investigator_context_policy"]
        attempts, events = task.get("attempts", []), task.get("diagnostic_events", [])
        if role == "investigator" and attempts and attempts[-1].get("role") == role and events:
            event = read(events[-1], _root(task))
            if event["attempt_index"] == len(attempts) and event.get("feedback"):
                frozen["investigator_action_feedback"] = {
                    "attempt_index": event["attempt_index"], "action": event["action"].get("type"),
                    "feedback": event["feedback"], "event_id": events[-1]["id"],
                }
    frozen["policy"] = task["protocol"]["diagnostic_policy"]
    frozen["navigation_assistance"] = task["protocol"].get("navigation_assistance", "baseline")
    if task.get("schema_version") == 5:
        frozen["workflow_profile"] = task["protocol"].get("workflow_profile", "workbench")
    frozen["latest_observation"] = task.get("latest_observation")
    frozen["latest_observation_output"] = task.get("latest_observation_output")
    frozen["diagnostic_reused"] = task.get("diagnostic_reused", False)
    frozen["recent_actions"] = task.get("diagnostic_events", [])[-6:]
    if (task["protocol"].get("project_context_policy") or {}).get("version") == "project-context-v10":
        # 复用持久动作事件；重复取同一观察也有访问次序，不另建平行日志。
        frozen["navigation_events"] = task.get("diagnostic_events", [])
    frozen["diagnostic_progress"] = task.get("diagnostic_progress", {})
    if preparation is not None:
        from .generation.preparation import binding

        value = binding(task, role)
        frozen["preparation_binding"] = value
        if preparation["mode"] == "reader_then_solver" and value["phase"] == "solve":
            events = [(ref, read(ref, _root(task))) for ref in task.get("diagnostic_events", [])]
            solver = [(ref, row) for ref, row in events
                      if (row.get("role"), row.get("preparation_phase")) == ("solver", "solve")]
            hidden = {row["observation_id"] for _, row in events
                      if row.get("role") == "repository_reader" and row.get("observation_id")}
            exposed = set(task["preparation_state"]["shared_observation_ids"])
            exposed.update(row["observation_id"] for _, row in solver if row.get("observation_id"))
            frozen["hidden_observation_ids"] = sorted(hidden - exposed)
            frozen["recent_actions"] = [ref for ref, _ in solver][-6:]
            if task.get("issues_author") != {"role": "solver", "phase": "solve"}:
                frozen["issues"] = []
            if not solver:
                frozen["diagnostic_progress"] = {}
    from .generation.incomplete import enabled, scope

    if enabled(task):
        frozen["incomplete_response_scope"] = scope(task, role, load_candidate(
            load_case(Path(task["manifest_path"])), task["current_candidate"]).revision)
    if task.get("schema_version") in {4, 5}:
        frozen["contract_requirements"] = task["contract_requirements"]
        frozen["contract_audits"] = task.get("contract_audits", [])
        frozen["schema_version"] = task["schema_version"]
        frozen["candidate_history"] = task.get("candidate_history", [])
        frozen["review_history"] = task.get("review_history", [])
    if task.get("schema_version") == 5:
        from .generation.protocol_v6 import DEPENDENCY_QUERY_POLICY

        frozen["dependency_queries"] = task.get("dependency_queries", [])
        frozen["dependency_facts"] = task.get("dependency_facts", [])
        frozen["dependency_query_policy"] = task["protocol"].get(
            "dependency_query_policy", DEPENDENCY_QUERY_POLICY
        )
        frozen["dependency_environment_identities"] = task.get(
            "dependency_environment_identities", {}
        )
        frozen["latest_fact"] = task.get("latest_fact")
        frozen["latest_dependency_query"] = task.get("latest_dependency_query")
        frozen["dependency_query_feedback"] = task.get("dependency_query_feedback")
        frozen["historical_inputs"] = task.get("historical_inputs", [])
        frozen["historical_binding"] = task.get("historical_binding")
        frozen["role_budget"] = task["protocol"]["role_budget"]
        frozen["investigator_sessions"] = task.get("investigator_sessions", [])
        consumed = {
            item["handoff_id"]
            for item in task.get("consumed_investigator_handoffs", [])
        }
        frozen["investigator_handoffs"] = (
            [
                reference
                for reference in task.get("investigator_handoffs", [])
                if reference["id"] not in consumed or frozen.get("investigator_context_policy") is not None
            ][-2:]
            if role == "solver"
            else []
        )
    frozen["probe_reviews"] = []
    for review in task.get("diagnostic_reviews", []):
        request = read(review["request"], _root(task))
        frozen["probe_reviews"].append({"probe_id": request["probe_id"], "revision": request["revision"],
            "request_id": review["request"]["id"], "decision": review["review"]["decision"],
            "note": _clean(review["review"]["note"])[:1000]})
    return store(_root(task) / "contexts", frozen)


def diagnostic_options(probes: list[dict], *, remaining_runs: int, max_probes: int) -> dict:
    """投影真实可用的诊断路线；计数为正不等于存在可执行路线。"""
    roots = sum(not probe.get("parent_probe_id") for probe in probes)
    reusable = [
        probe["id"]
        for probe in probes
        if remaining_runs > 0
        and not probe["superseded_by"]
        and probe["last_review_decision"] == "accept"
        and (probe["last_observation_id"] is None or probe["last_observation_stale"] is True)
    ]
    return {
        "new_probe_definition_available": roots < max_probes and remaining_runs > 0,
        "revisable_probe_ids": [probe["id"] for probe in probes if probe["can_revise"] and remaining_runs > 0],
        "reusable_probe_ids": reusable,
        "execution_requires_review": True,
        "note": "Availability is not proof of runtime validity; rejected code is not approved for reuse.",
    }


def finish_coverage_decision(action: dict, context: dict) -> dict:
    """只根据结构化声明与宿主容量判断退出，不从解释文本猜业务语义。"""
    unresolved = [
        item for item in action["contract_coverage"]
        if item["scope"] == "in_contract" and item["status"] == "unverified"
    ]
    options = context["diagnostic_options"]
    routes = {
        "new_probe_definition_available": options["new_probe_definition_available"],
        "revisable_probe_ids": list(options["revisable_probe_ids"]),
        "reusable_probe_ids": list(options["reusable_probe_ids"]),
    }
    capacity = context["remaining_diagnostic_runs"] > 0 and (
        routes["new_probe_definition_available"]
        or routes["revisable_probe_ids"]
        or routes["reusable_probe_ids"]
    )
    base = {
        "unverified_in_contract": [item["requirement"] for item in unresolved],
        "remaining_diagnostic_runs": context["remaining_diagnostic_runs"],
        "available_routes": routes,
    }
    if not unresolved:
        return base | {"accepted": True, "code": "no_unverified_in_contract_requirement"}
    if action["reason"] != "unresolved":
        return base | {"accepted": False, "code": "unverified_contract_blocks_success"}
    actionable = [item for item in unresolved if item["tool_limitation"] is None]
    if actionable and capacity:
        return base | {
            "accepted": False,
            "code": "diagnostic_capacity_remaining",
            "actionable_requirements": [item["requirement"] for item in actionable],
        }
    return base | {
        "accepted": True,
        "code": "unresolved_with_tool_limit_or_no_diagnostic_route",
        "declared_tool_limitations": [
            {"requirement": item["requirement"], "tool_limitation": item["tool_limitation"]}
            for item in unresolved if item["tool_limitation"] is not None
        ],
    }


def _public_node_matches(expected: str, actual: str) -> bool:
    """运行器可能省略 checks/feedback 前缀；只允许同一文件与节点的后缀匹配。"""
    value = str(actual).replace("\\", "/")
    for prefix in ("/work/checks/", "checks/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    alternatives = {expected, expected.removeprefix("feedback/")}
    return any(value == candidate or value.endswith("/" + candidate) for candidate in alternatives)


def _contract_observation_states(observations: list[dict], requirements, revision: str) -> dict:
    """把当前 revision 的公开运行投影为有限支持/反例；不宣称语义充分。"""
    states = {item.id: {"support": [], "counterexample": []} for item in requirements}
    requirement_map = {item.id: item for item in requirements}
    for row in observations:
        if row["revision"] != revision:
            continue
        ref = "observation:" + row["id"]
        if row["kind"] == "public_checks" and row["result"].get("status") == "passed":
            stages = row["result"].get("stages", {})
            stage = stages.get("new_candidate", {})
            if stage.get("status") in {None, "not_supplied"}:
                stage = stages.get("new_original", {})
            if stage.get("status") != "passed":
                continue
            actual = stage.get("nodeids", [])
            for requirement in requirements:
                expected = requirement.public_check_nodeids
                if expected and all(any(_public_node_matches(node, value) for value in actual) for node in expected):
                    states[requirement.id]["support"].append(ref)
            continue
        if row["kind"] != "probe":
            continue
        requirement_id = row.get("action", {}).get("requirement_id")
        if requirement_id not in requirement_map:
            continue
        conclusion = row["result"].get("assessment", {}).get("conclusion")
        if conclusion == "counterexample_observed":
            states[requirement_id]["counterexample"].append(ref)
        elif conclusion == "no_counterexample_observed" and row["result"].get("status") == "passed":
            states[requirement_id]["support"].append(ref)
    return states


def simple_finish_decision(action: dict, requirements, observations: list[dict], revision: str) -> dict:
    """基线不填写覆盖声明；真实公开检查与当前反例仍由宿主核对。"""
    observed = _contract_observation_states(observations, requirements, revision)
    missing = [item.id for item in requirements if item.public_check_nodeids and not observed[item.id]["support"]]
    conflicts = [identity for identity, state in observed.items() if state["counterexample"]]
    accepted = action["reason"] == "unresolved" or not (missing or conflicts)
    return {
        "accepted": accepted,
        "code": "simple_tools_observed_checks" if accepted else "contract_not_ready_for_success",
        "workflow_profile": "simple_tools", "model_coverage_declarations": "not_required",
        "unobserved_development_requirement_ids": missing,
        "counterexample_requirement_ids": conflicts,
        "scope_limit": "Current public observations only; independent final acceptance is separate.",
    }


def finish_contract_decision(action: dict, context: dict, requirements, observations: list[dict], revision: str) -> dict:
    """Protocol 5：开发门禁与独立 final 验收共享目录，但不互相冒充。"""
    required = [item.id for item in requirements]
    declared = [item["requirement_id"] for item in action["contract_coverage"]]
    missing = sorted(set(required) - set(declared))
    unknown = sorted(set(declared) - set(required))
    duplicates = sorted({value for value in declared if declared.count(value) > 1})
    if missing or unknown or duplicates:
        return {"accepted": False, "code": "contract_requirement_set_mismatch",
                "missing_requirement_ids": missing, "unknown_requirement_ids": unknown,
                "duplicate_requirement_ids": duplicates}
    observed = _contract_observation_states(observations, requirements, revision)
    effective = []
    for item in action["contract_coverage"]:
        identity = item["requirement_id"]
        cited = set(item["evidence_refs"])
        support = observed[identity]["support"]
        counterexamples = observed[identity]["counterexample"]
        if counterexamples:
            state, reason = "counterexample", "current_revision_counterexample"
        elif item["state"] == "supported" and cited.intersection(support):
            state, reason = "structurally_supported", "current_revision_bound_observation"
        else:
            state = "unobserved"
            reason = "solver_declared_unobserved" if item["state"] == "unobserved" else "insufficient_evidence_scope"
        effective.append({"requirement_id": identity, "declared_state": item["state"],
                          "effective_state": state, "reason": reason,
                          "supporting_observation_refs": support,
                          "counterexample_observation_refs": counterexamples,
                          "tool_limitation": item["tool_limitation"]})
    development_ids = {item.id for item in requirements if item.public_check_nodeids}
    unresolved = [row for row in effective if row["effective_state"] == "unobserved"]
    unresolved_development = [row for row in unresolved if row["requirement_id"] in development_ids]
    unresolved_final = [row for row in unresolved if row["requirement_id"] not in development_ids]
    counterexamples = [row for row in effective if row["effective_state"] == "counterexample"]
    options = context["diagnostic_options"]
    routes = {"new_probe_definition_available": options["new_probe_definition_available"],
              "revisable_probe_ids": list(options["revisable_probe_ids"]),
              "reusable_probe_ids": list(options["reusable_probe_ids"])}
    capacity = context["remaining_diagnostic_runs"] > 0 and bool(
        routes["new_probe_definition_available"] or routes["revisable_probe_ids"] or routes["reusable_probe_ids"]
    )
    base = {"catalog_requirement_ids": required,
            "catalog_sha256": context["contract_requirements"]["sha256"],
            "effective_coverage": effective,
            "unobserved_requirement_ids": [row["requirement_id"] for row in unresolved],
            "unobserved_development_requirement_ids": [
                row["requirement_id"] for row in unresolved_development
            ],
            "unobserved_final_acceptance_requirement_ids": [
                row["requirement_id"] for row in unresolved_final
            ],
            "counterexample_requirement_ids": [row["requirement_id"] for row in counterexamples],
            "remaining_diagnostic_runs": context["remaining_diagnostic_runs"], "available_routes": routes,
            "scope_limit": "Requirement identity and evidence binding do not prove semantic entailment; final acceptance is separate."}
    if action["reason"] != "unresolved" and (unresolved_development or counterexamples):
        return base | {"accepted": False, "code": "contract_not_ready_for_success"}
    actionable = [row for row in unresolved_development if row["tool_limitation"] is None]
    if action["reason"] == "unresolved" and actionable and capacity:
        return base | {"accepted": False, "code": "diagnostic_capacity_remaining",
                       "actionable_requirements": [row["requirement_id"] for row in actionable]}
    if counterexamples:
        return base | {"accepted": True, "code": "unresolved_with_counterexample"}
    if unresolved:
        if action["reason"] == "unresolved":
            return base | {"accepted": True, "code": "unresolved_with_tool_limit_or_no_diagnostic_route"}
        return base | {"accepted": True, "code": "development_requirements_structurally_supported"}
    return base | {"accepted": True, "code": "all_requirements_structurally_supported"}


def context_from_reference(reference, case, snapshot) -> dict:
    from .execution.postgres import load_postgres_spec
    from .probe_scaffolds import scaffold_for

    path = Path(reference["path"])
    root = path.parent.parent
    task = read(reference, root)
    if (Path(task["task_path"]).parent != root or task["task_id"] != root.name
            or task["case_fingerprint"] != case.fingerprint
            or task["manifest_path"] != str(case.manifest_path)
            or load_candidate(case, task["current_candidate"]).revision != snapshot.revision):
        raise ValueError("Diagnostic context identity changed")
    all_items = [public_observation(task, ref) for ref in task["observations"]]
    display_items = [row for row in all_items if row["id"] not in task.get("hidden_observation_ids", [])]
    reading_items = display_items
    navigation = (task.get("project_context_policy") or {}).get("version") == "project-context-v10"
    access = {}
    if navigation:
        allowed = {row["id"] for row in display_items}
        for event_ref in task.get("navigation_events", []):
            event = read(event_ref, root)
            identity = event.get("observation_id")
            if identity in allowed and not event.get("feedback"):
                access[identity] = event["attempt_index"]
        reading_items = sorted(display_items, key=lambda row: access.get(row["id"], 0))
    latest = task.get("latest_observation")
    visible = reading_items[-3:]
    if latest and latest in {r["id"] for r in display_items}:
        target = next(row for row in display_items if row["id"] == latest)
        visible = [r for r in visible if r["id"] != latest][-2:] + [target]
    elif latest:
        latest = None
    for row in all_items:
        row["stale"] = row["revision"] != snapshot.revision
    output = task.get("latest_observation_output")
    output_page_value = None
    if output and output["observation_id"] == latest and latest in {row["id"] for row in display_items}:
        from .diagnostic_output import output_page

        ref = next(ref for ref in task["observations"] if ref["id"] == output["observation_id"])
        output_page_value = output_page(task, ref, output["cursor"], current_revision=snapshot.revision)
    probes = probe_states([read(ref, root) | {"id": ref["id"]} for ref in task["probes"]],
                          task.get("probe_reviews", []), all_items, snapshot.revision, task["policy"])
    from .finish_evidence import POLICY, validate_policy
    from .finish_evidence import project as finish_projection

    finish_policy = validate_policy(task.get("finish_policy"))
    remaining_runs = task["policy"]["max_runs"] - len(task["diagnostic_runs"])
    progress = task.get("diagnostic_progress", {})
    scaffold = scaffold_for(case)
    from .generation.localization import locations
    navigation_locations = [{"path": path, "line": line, "observation_id": row["id"],
                             "revision": snapshot.revision}
                            for row in all_items if row["revision"] == snapshot.revision and row["kind"] not in {"source", "project_context"}
                            for path, line in locations(row["result"], snapshot.files)][:24]
    result = {"task_id": task["task_id"], "source_state": source_state(snapshot),
            "last_diagnostic_reused": task.get("diagnostic_reused", False),
            "navigation_assistance": task.get("navigation_assistance", "baseline"),
            **({"diagnostic_workset": {
                "hypotheses": task["issues"],
                "observable_results": runtime_findings(all_items),
                "source_locations": navigation_locations,
                "note": "Hypotheses are solver claims, not facts. Compare current evidence and counterevidence; retain unknowns and request the next discriminating observation. Do not treat stale results or setup failures as disproof."
            }} if task.get("navigation_assistance") == "failure_guided" else {}),
            "execution_environment": {"python": case.environment.python_version if case.environment else "3.12", "probe_entry": "pytest test_probe.py",
                "dependencies": "existing locked old/new dependencies only",
                "source_imports": "registered package available on PYTHONPATH",
                "postgres_available": load_postgres_spec(case) is not None,
                "dsn_environment_variable": "UPGRADE_WORKBENCH_PG_DSN" if load_postgres_spec(case) else None,
                "network": "isolated database loopback only; no external network",
                "probe_scaffold": scaffold.public() if scaffold else None},
            "observations": visible,
            "latest_observation_id": latest,
            **({"observation_output": output_page_value} if output_page_value is not None else {}),
            "runtime_findings": runtime_findings(all_items),
            "recurring_public_failures": recurring_public_failures(all_items, snapshot.revision),
            "review_history": task.get("probe_reviews", []),
            "recent_actions": [read(ref, root) for ref in task.get("recent_actions", [])],
            "progress": {key: progress.get(key) for key in ("consecutive_revisits", "warning")},
            "observation_index": [{k: r[k] for k in ("id", "kind", "revision", "stale")} for r in all_items],
            "issues": task["issues"], "probes": probes,
            "probe_budget_note": "remaining_probes limits NEW definitions (root hypotheses), including rejected proposals. "
                                 "revise_probe uses a separate bounded revision allowance for rejected/inconclusive "
                                 "probes; see can_revise and remaining_revisions. Review and execution remain required. "
                                 "run_probe with an existing ID does not consume a definition; it needs diagnostic "
                                 "runs and review for the requested revision. A reusable ID need not be a usable probe.",
            "diagnostic_options": diagnostic_options(
                probes, remaining_runs=remaining_runs, max_probes=task["policy"]["max_probes"]
            ),
            "remaining_diagnostic_runs": remaining_runs,
            "remaining_probes": task["policy"]["max_probes"]
                                - sum(not p.get("parent_probe_id") for p in probes)}
    if task.get("semantic_risk_policy") is not None:
        from .generation.semantic_risk import validate_policy as risk_policy

        result["semantic_risk_policy"] = risk_policy(task["semantic_risk_policy"])
    if task.get("delivery_review_policy") is not None:
        from .generation.delivery_review import EVIDENCE_POLICY, POLICIES, draft, public_outputs

        if task["delivery_review_policy"] not in POLICIES:
            raise ValueError("Unknown delivery review policy")
        result["delivery_review_policy"] = task["delivery_review_policy"]
        if task["delivery_review_policy"] == EVIDENCE_POLICY and task["context_role"] == "solver":
            result["delivery_evidence"] = public_outputs(task, display_items, snapshot.revision)
        if task["context_role"] == "contract_auditor":
            reference = task.get("delivery_draft_reference")
            result["delivery_review"] = {
                "draft": draft(task, reference, snapshot) if reference is not None else None,
                "public_execution": public_outputs(task, display_items, snapshot.revision),
                "scope": "Read-only review of proposed delivery against the original business contract. "
                         "Related effective topics are hypotheses; author confirmations are not evidence.",
            }
    if navigation:
        result["navigation_candidates"] = {
            "index": [{**{key: row[key] for key in ("id", "kind", "revision", "stale")},
                       "action": {key: value for key, value in row["action"].items()
                                  if key in {"type", "path", "start_line", "end_line", "query", "cursor", "view"}},
                       "last_access": access.get(row["id"])} for row in reversed(reading_items)],
            "observations": [row for row in reversed(reading_items)
                             if row["action"]["type"] == "outline_source" and not row["stale"]],
        }
    if finish_policy == POLICY:
        result["finish_requirements"] = finish_projection(
            probes, all_items, snapshot.revision,
            format_limits=(task.get("investigation_policy") or {}).get("finish_limits"))
    if task.get("investigation_policy") is not None:
        from .investigation_policy import policy as investigation_config

        result["investigation_policy"] = investigation_config(task["investigation_policy"])
        result["progress"]["last_retrieval"] = progress.get("last_retrieval")
        result["investigation_note"] = (
            "This frozen policy replaces legacy repeat-stop rules: only repeated unchanged evidence "
            "already visible in the actual request counts toward consecutive_revisits. An omitted item "
            "may be retrieved again. New pages, ranges and revisions reset the count, but are not proof "
            "of repair. All model turns including rejected formats count toward remaining_calls; tool "
            "execution and probe definitions have separate displayed limits. There is no automatic "
            "budget increase. Finish field limits are in finish_requirements.format_limits."
        )
    if task.get("schema_version") == 5:
        result["workflow_profile"] = task.get("workflow_profile", "workbench")
        historical = [
            public_historical_input(task, reference, snapshot=snapshot)
            for reference in task.get("historical_inputs", [])
        ]
        facts = [public_fact(task, reference) for reference in task.get("dependency_facts", [])]
        latest_fact = task.get("latest_fact")
        visible_facts = facts[-3:]
        if latest_fact and latest_fact in {item["id"] for item in facts}:
            latest_row = next(item for item in facts if item["id"] == latest_fact)
            visible_facts = [item for item in visible_facts if item["id"] != latest_fact][-2:] + [
                latest_row
            ]
        queries = []
        for row in task.get("dependency_queries", []):
            request = read(row["request"], root)
            item = {
                "id": row["request"]["id"],
                "action": request["action"],
                "status": row["status"],
                "fact_id": row.get("fact", {}).get("id"),
            }
            if row.get("failure_receipt"):
                failure = read(row["failure_receipt"], root)
                item["failure"] = {
                    "error_type": failure["error_type"],
                    "reason": failure["reason"],
                }
            queries.append(item)
        handoffs = [
            public_investigator_handoff(task, reference, snapshot=snapshot)
            for reference in task.get("investigator_handoffs", [])
        ]
        result.update(
            historical_inputs=historical,
            historical_input_note=(
                "Historical inputs preserve material visible at a prior cutoff. They are not current "
                "executions, facts, budget counters or acceptance evidence."
            ),
            dependency_queries=queries,
            dependency_facts=visible_facts,
            dependency_fact_index=[
                {
                    "id": item["id"],
                    "environment": item["environment"]["role"],
                    "distribution": item["distribution"],
                    "operation": item["operation"],
                    "stale": item["stale"],
                }
                for item in facts
            ],
            dependency_environment_identities=task.get(
                "dependency_environment_identities", {}
            ),
            remaining_dependency_queries=(
                task["dependency_query_policy"]["max_queries"] - len(queries)
            ),
            latest_fact_id=latest_fact,
            latest_dependency_query_id=task.get("latest_dependency_query"),
            dependency_query_feedback=task.get("dependency_query_feedback"),
            investigator_handoffs=handoffs,
            role_budget=task["role_budget"],
            investigator_sessions=[
                {
                    "status": item["status"],
                    "attempts": len(item.get("attempt_indices", [])),
                    "start_revision": item["start_revision"],
                    **({"end_revision": item["end_revision"]} if item.get("end_revision") else {}),
                }
                for item in task.get("investigator_sessions", [])
            ],
            investigator_handoff_note=(
                "Investigator handoffs are advisory summaries. Cite their underlying fact, observation, "
                "source, version, or business_contract references rather than the handoff ID."
            ),
        )
    if task.get("project_context_policy") is not None:
        from .generation.project_context import has_continuity, has_maintenance, is_adaptive
        from .generation.project_context import policy as knowledge_policy

        result["project_context_policy"] = knowledge_policy(task["project_context_policy"])
        reference = task.get("project_knowledge_reference")
        result["project_knowledge"] = public_knowledge(task, reference) if reference else None
        if task.get("knowledge_import") is not None:
            from .knowledge_transfer import public_import

            result["imported_knowledge"] = public_import(task, case, snapshot)
        if is_adaptive(result["project_context_policy"]):
            selections = [row for row in display_items if row["action"]["type"] == "select_investigation"]
            result["investigation_selection"] = (
                {key: value for key, value in selections[-1]["result"].items() if key not in {"read", "excerpts"}} | {"observation_id": selections[-1]["id"],
                                           "current_revision": snapshot.revision} if selections else None)
        if has_continuity(result["project_context_policy"]):
            from .generation.project_context import knowledge_applicability

            result["knowledge_applicability"] = knowledge_applicability(case, snapshot, result["project_knowledge"])
        if has_maintenance(result["project_context_policy"]):
            from .topic_maintenance import view as topic_view

            result["topic_maintenance"] = topic_view(snapshot.revision,
                knowledge=result["project_knowledge"], knowledge_id=reference["id"] if reference else None,
                imported=result.get("imported_knowledge"), observations=all_items)
        from .generation.knowledge_review import enabled as review_enabled
        from .generation.knowledge_review import validate_binding

        if review_enabled(task):
            value = validate_binding(task["knowledge_review_binding"], snapshot)
            if value["task_id"] != task["task_id"] or value["import_id"] != task["knowledge_import_reference"]["id"]:
                raise ValueError("Knowledge review context identity changed")
            result["knowledge_review_binding"] = value
            completion = value["completion_reference"]
            completed = next((row for row in all_items if completion and row["id"] == completion["id"]), None)
            if value["phase"] == "solve" and (completed is None or completed["action"]["type"] != "complete_knowledge_review"):
                raise ValueError("Knowledge review completion is absent from Solver context")
            result["knowledge_review"] = {"phase": value["phase"], "start_revision": value["start_revision"],
                "completion": completed,
                "meaning": "Review dispositions are model interpretations; the next Solver must verify relevant business behavior."}
    if task.get("source_read_retention") is not None:
        if task["source_read_retention"] != "current_revision" or task.get("workflow_profile") != "simple_tools":
            raise ValueError("Unknown source read retention")
        result["source_read_retention"] = task["source_read_retention"]
    if task.get("project_context_policy") is not None or task.get("source_read_retention") is not None:
        # 保存读取范围而非再复制正文；请求装配器从当前快照重新取原文。
        result["retained_source_reads"] = [
            {"observation_id": row["id"], "result": {k: v for k, v in row["result"].items()
             if k in {"path", "revision", "view", "start_line", "end_line"}}}
            for row in reading_items if row["kind"] == "source" and row["action"]["type"] == "read_source"
            and row["revision"] == snapshot.revision and "text" in row["result"]
        ]
        from .generation.project_context import has_continuity

        if has_continuity(task.get("project_context_policy")):
            from .generation.source_context import rebind_source_reads

            result["retained_source_reads"] = rebind_source_reads(snapshot, reading_items)
    if task.get("contract_requirements") is not None:
        result["contract_requirements"] = task["contract_requirements"]
        if task.get("schema_version") == 5:
            options, recommended = candidate_option_state(
                task, case, snapshot.revision, observations=all_items
            )
            result["candidate_options"] = options
            if task.get("workflow_profile", "workbench") != "simple_tools":
                result["recommended_candidate"] = recommended
        else:
            result["restore_candidates"] = restorable_candidates(
                task, case, snapshot.revision, observations=all_items
            )
        audits = []
        for reference in task.get("contract_audits", []):
            value = read(reference, root)
            receipt = Path(value["receipt_path"])
            assert_no_links(receipt)
            if (
                value["task_id"] != task["task_id"]
                or value["case_fingerprint"] != task["case_fingerprint"]
                or value["catalog"] != task["contract_requirements"]
                or not receipt.resolve().is_relative_to(Path(task["work_root"]).resolve())
                or hashlib.sha256(receipt.read_bytes()).hexdigest() != value["receipt_sha256"]
            ):
                raise ValueError("Contract audit identity changed")
            audits.append(
                {
                    "id": reference["id"],
                    "revision": value["revision"],
                    "patch_sha256": value["patch_sha256"],
                    "stale": value["revision"] != snapshot.revision,
                    "summary": value["summary"],
                    "audit": value["audit"],
                }
            )
        result["contract_audits"] = audits
    if task.get("preparation_binding") is not None:
        from .generation.preparation import validate_binding

        value = task["preparation_binding"]
        role, _ = validate_binding(value)
        if task["context_role"] != role or value["task_id"] != task["task_id"]:
            raise ValueError("Frozen preparation task/role changed")
        result["preparation_binding"] = value
        if value["phase"] == "solve":
            public_knowledge(task, value["handoff_reference"])
    if task.get("incomplete_response_scope") is not None:
        result["incomplete_response_scope"] = task["incomplete_response_scope"]
    if task.get("investigator_context_policy") is not None:
        from .generation.investigator_context import RESILIENT_POLICY, policy, source_observation

        result["investigator_context_policy"] = policy(task["investigator_context_policy"])
        result["context_role"] = task["context_role"]
        if task.get("investigator_action_feedback") is not None:
            result["investigator_action_feedback"] = task["investigator_action_feedback"]
        result["investigator_source_observation_ids"] = [row["id"] for row in all_items
                                                         if source_observation(row)]
        if result["investigator_context_policy"] == RESILIENT_POLICY:
            # 执行事实独立于最近阅读窗口；只投影公开观察，不读取独立验收或模型自述。
            executions = [row for row in display_items
                          if row["kind"] == "public_checks" and source_observation(row)]
            result["latest_public_execution"] = executions[-1] if executions else None
            for public, session in zip(result["investigator_sessions"], task["investigator_sessions"], strict=True):
                if session.get("failure") is not None:
                    public["failure"] = session["failure"]
            result["investigator_failure_note"] = (
                "If an Investigator is exhausted, it produced no valid handoff. Its rejected answer is not evidence. "
                "The Solver retains the remaining task allowance and must independently read registered "
                "evidence, maintain relevant explanations and deliver the business result. "
                "latest_public_execution preserves the latest visible public run, even after other reads; "
                "check revision/stale and read output cursors when excerpts are truncated. "
                "A passing check is not proof that an explanation is correct."
            )
        result["investigator_handoff_note"] += (
            " Recent handoffs remain readable after first delivery; delivery is not semantic resolution. "
            "Check stale before relying on a handoff after a source change."
        )
    return result


def remember_action(task, response, case, *, before_revision, delivered_context=None):
    """保存可检查的动作与结果摘要，不把模型summary当作执行证明。"""
    snapshot = load_candidate(case, task["current_candidate"])
    action = response.get("action", {})
    rejected = task.get("edit_feedback") or task.get("protocol_feedback")
    observation_id = None
    if not rejected and action.get("type") in {
        "list_sources", "outline_source", "search_source", "read_source", "get_observation",
        "run_public_checks", "run_probe", "propose_probe", "revise_probe",
        "record_project_knowledge", "read_project_topic", "query_project_relations",
        "read_public_contract",
    } and task["status"] in {"ready", "unresolved"}:
        observation_id = task.get("latest_observation")
    if (not rejected and action.get("type") == "select_investigation" and ("read" in action or "focus" in action)
            and task["status"] == "ready"):
        observation_id = task.get("latest_observation")
    retrieved = None
    if observation_id is not None:
        ref = next(r for r in task["observations"] if r["id"] == observation_id)
        retrieved = public_observation(task, ref)
        if retrieved["action"]["type"] == "select_investigation" and "read" in retrieved["result"]:
            # 只计真实原文；改变范围理由不能制造另一份“新证据”。
            source_read = retrieved["result"]["read"]
            evidence = {key: source_read[key] for key in (
                "view", "revision", "path", "file_sha256", "start_line", "end_line", "text")}
            retrieved = retrieved | {"kind": "source", "action": {"type": "read_source"},
                "result": source_read, "id": digest(["investigation_read", evidence])}
        elif retrieved["action"]["type"] == "select_investigation" and "excerpts" in retrieved["result"]:
            reads = [{key: entry["source"][key] for key in (
                "view", "revision", "path", "file_sha256", "start_line", "end_line", "text")}
                for entry in retrieved["result"]["excerpts"]]
            reads.sort(key=lambda row: (row["path"], row["start_line"], row["end_line"]))
            retrieved = retrieved | {"kind": "source_excerpts", "action": {"type": "read_source_excerpts"},
                "result": {"reads": reads}, "id": digest(["investigation_excerpts", reads])}
        elif retrieved["action"]["type"] in {"select_investigation", "revise_project_topic"}:
            # 重读自己的计划或解释不是新证据，不能借此清空停滞计数。
            observation_id = None
            retrieved = None
    if not rejected and task["protocol"].get("investigation_policy") is not None:
        from .investigation_policy import note_progress as note_visible_progress

        note_visible_progress(task, snapshot.revision,
            observation=retrieved,
            cursor=action.get("cursor") if action.get("type") == "get_observation" else None,
            context=delivered_context,
            changed=snapshot.revision != before_revision or task["status"] == "pending_diagnostic_review")
    elif not rejected:
        note_progress(task, snapshot.revision, observation_id=retrieved["id"] if retrieved else observation_id,
                      output_cursor=action.get("cursor") if action.get("type") == "get_observation" else None,
                      changed=snapshot.revision != before_revision or task["status"] == "pending_diagnostic_review")
    public_action = {key: value for key, value in action.items() if key not in {"code", "edits", "issues", "pages", "page"}}
    if "code" in action:
        public_action["code_sha256"] = hashlib.sha256(action["code"].encode()).hexdigest()
    if "edits" in action:
        public_action["edited_paths"] = sorted({edit["path"] for edit in action["edits"]})
    bound_attempts = [index for index, item in enumerate(task["attempts"], start=1)
                      if item.get("receipt") == response.get("report_path")]
    attempt_index = bound_attempts[0] if len(bound_attempts) == 1 else len(task["attempts"])
    event = {"attempt_index": attempt_index, "revision_before": before_revision,
             "revision_after": snapshot.revision, "action": public_action,
             "model_summary": _clean(response.get("summary", ""))[:800],
             "status": task["status"], "feedback": rejected,
             "observation_id": observation_id, "cached_result": bool(task.get("diagnostic_reused")),
             "stop_reason": task.get("stop_reason")}
    if task["protocol"].get("repository_preparation") is not None:
        from .generation.preparation import verify_response

        value = verify_response(task, response)
        event.update(role=value["role"], preparation_phase=value["phase"])
    ref = store(_root(task) / "action_events", event)
    events = task.setdefault("diagnostic_events", [])
    if ref not in events:
        events.append(ref)


def register_probe(task, action, case, snapshot):
    """新假设与纠错版本分开计数；通过约束后才登记不可变定义。"""
    identity = digest(action)
    existing = next((p for p in task["probes"] if p["id"] == identity), None)
    if existing:
        read(existing, _root(task))
        return identity
    probes = [read(ref, _root(task)) | {"id": ref["id"]} for ref in task["probes"]]
    policy = task["protocol"]["diagnostic_policy"]
    if task.get("schema_version") in {4, 5}:
        required = set(task["contract_requirements"]["requirement_ids"])
        if action.get("requirement_id") not in required:
            raise ValueError("Probe must bind one exact frozen contract requirement ID")
    if len(task["diagnostic_runs"]) >= policy["max_runs"]:
        raise ValueError("Diagnostic execution limit reached; no new probe registered")
    if action["type"] == "revise_probe":
        context = context_from_reference(freeze_context(task), case, snapshot)
        parent = next((p for p in context["probes"] if p["id"] == action["parent_probe_id"]), None)
        if parent is None or not parent["can_revise"]:
            raise ValueError("Probe revision requires an eligible latest rejected/inconclusive parent with revisions remaining")
        from .finish_evidence import POLICY

        if (task["protocol"].get("finish_policy") == POLICY
                and action.get("requirement_id") != parent.get("requirement_id")):
            raise ValueError("Probe revision must preserve requirement_id")
        fixed = ("requirement", "subject", "basis", "operator", "expected")
        if action["oracle"] is not None and (parent.get("oracle") is None or
                any(action["oracle"][key] != parent["oracle"].get(key) for key in fixed)):
            raise ValueError("Probe revision must preserve requirement, subject and predicate; only exercise may change")
        if action["code"] == parent["code"]:
            raise ValueError("Probe revision must correct the code, not merely rename the proposal")
    elif sum(not p.get("parent_probe_id") for p in probes) >= policy["max_probes"]:
        raise ValueError("Probe definition limit reached; use eligible revise_probe, existing evidence, or finish unresolved")
    ref = store(_root(task) / "probes", action)
    task["probes"].append(ref)
    return ref["id"]


def accept_action(task, response, case):
    """诊断协议动作；None 表示仍由原候选处理路径接管。"""
    if task.get("schema_version") == 5:
        from .generation.protocol_v6 import DIAGNOSTIC_ACTIONS
    elif task.get("schema_version") == 4:
        from .generation.protocol_v5 import DIAGNOSTIC_ACTIONS
    else:
        from .generation.protocol_v4 import DIAGNOSTIC_ACTIONS
    from .generation.source_context import NAVIGATION

    action = response["action"]
    if task["protocol"].get("investigator_context_policy") is not None:
        from .generation.investigator_context import require_action

        role = response.get("role") or ("investigator" if response.get("output_format") == "investigator_actions" else "solver")
        require_action(action, task["protocol"]["investigator_context_policy"], role)
    from .generation.knowledge_review import enabled as review_enabled
    from .generation.knowledge_review import verify_response as verify_review

    if review_enabled(task):
        verify_review(task, response, load_candidate(case, task["current_candidate"]))
    preparation = None
    if task["protocol"].get("repository_preparation") is not None:
        from .generation.preparation import verify_response

        preparation = verify_response(task, response)
    # 所有引用先一次核验，拒绝动作时不留下半更新的issues或候选状态。
    refs = list(action.get("evidence_refs", []))
    refs.extend(ref for issue in action.get("issues", []) for ref in issue["evidence_refs"])
    refs.extend(ref for item in action.get("contract_coverage", []) for ref in item["evidence_refs"])
    if action.get("type") == "handoff":
        refs.extend(_handoff_evidence_refs(action))
    resolve_refs(task, refs, response=response)
    fact_reference = None
    if action.get("type") == "get_fact":
        fact_reference = next(
            (item for item in task.get("dependency_facts", []) if item["id"] == action["fact_id"]),
            None,
        )
        if fact_reference is None:
            raise ValueError("Unknown dependency fact")
        public_fact(task, fact_reference)
    task["diagnostic_reused"] = False
    if "issues" in action:
        task["issues"] = action["issues"]
        if preparation is not None:
            task["issues_author"] = {"role": preparation["role"], "phase": preparation["phase"]}
    action = {k: v for k, v in action.items() if k != "issues"}
    kind = action["type"]
    snapshot = load_candidate(case, task["current_candidate"])
    role = response.get("role")
    if role is None:
        role = "investigator" if response.get("output_format") == "investigator_actions" else "solver"
    if role == "investigator" and kind in {"submit_candidate", "restore_candidate", "finish"}:
        raise ValueError("Investigator is read-only and cannot change candidate or terminal state")
    from .generation.project_context import ACTIONS, bind_pages, knowledge_result
    from .generation.project_context import validate_action as validate_knowledge

    if kind in ACTIONS:
        options = {"role": role}
        if preparation is not None:
            from .generation.preparation import action_options

            options = action_options(preparation)
        validate_knowledge(case, snapshot, action, task["protocol"].get("project_context_policy"), **options)
        if kind == "complete_knowledge_review":
            from .generation.knowledge_review import accept_completion

            return accept_completion(task, response | {"action": action}, case, snapshot)
        if kind == "revise_project_topic":
            from .topic_maintenance import revision_result

            receipt = Path(response["report_path"])
            assert_no_links(receipt)
            runtime = context_from_reference(response["diagnostic_context_reference"], case, snapshot)
            result = revision_result(case, snapshot, action, runtime["topic_maintenance"])
            observation = observe(task, case, action, result, kind="project_context", snapshot=snapshot,
                generation_receipt={"path": str(receipt), "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()})
            task["latest_observation"] = observation["id"]
            task["status"] = "ready"
            return True
        reference = task.get("project_knowledge_reference")
        if kind == "record_project_knowledge":
            receipt = Path(response["report_path"])
            assert_no_links(receipt)
            value = {"task_id": task["task_id"], "source_reference": snapshot.reference,
                     "action": action, "knowledge": bind_pages(case, snapshot, action["pages"]),
                     "receipt_path": str(receipt), "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}
            if preparation is not None:
                value.update(author=role, preparation_binding=preparation)
            reference = store(_root(task) / "project_knowledge", value)
        knowledge = public_knowledge(task, reference) if reference else None
        effective_read = (kind == "read_project_topic"
                          and task["protocol"]["project_context_policy"]["version"] in {"project-context-v8", "project-context-v9", "project-context-v10"})
        current_topics = None
        if effective_read:
            runtime = context_from_reference(response["diagnostic_context_reference"], case, snapshot)
            if runtime.get("investigator_context_policy") is not None:
                from .generation.investigator_context import project

                runtime = project(runtime, response["output_format"])
            current_topics = runtime["topic_maintenance"]
        result = knowledge_result(case, snapshot, action, knowledge,
                                  context_policy=task["protocol"].get("project_context_policy"), current_topics=current_topics)
        public_action = {k: v for k, v in action.items() if k != "pages"}
        if len(encoded({"id": "0" * 64, "kind": "project_context", "revision": snapshot.revision,
                        "action": public_action, "result": result})) > 24_000:
            raise ValueError("Knowledge tool result exceeds public observation capacity")
        receipt_binding = None
        if kind == "select_investigation" or effective_read:
            receipt = Path(response["report_path"])
            assert_no_links(receipt)
            receipt_binding = {"path": str(receipt), "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}
        observation = observe(task, case, action, result, kind="project_context", snapshot=snapshot,
                              knowledge_reference=reference, generation_receipt=receipt_binding)
        task["project_knowledge_reference"] = reference
        task["latest_observation"] = observation["id"]
        if preparation is not None and preparation["phase"] == "prepare" and kind == "record_project_knowledge":
            state = task["preparation_state"]
            if state["status"] != "active":
                raise ValueError("Preparation already completed")
            state.update(status="completed", handoff_reference=reference, handoff_receipt=response["report_path"],
                         shared_observation_ids=[observation["id"]])
        task["status"] = "ready"
        return True
    from .generation.project_context import requires_knowledge

    if (kind == "submit_candidate" and requires_knowledge(task["protocol"].get("project_context_policy"))
            and not task.get("project_knowledge_reference")):
        raise ValueError("Record source-grounded project knowledge before the first edit")
    if kind == "handoff":
        if task.get("schema_version") != 5 or role != "investigator":
            raise ValueError("Only a Protocol 6 Investigator may persist a handoff")
        persist_investigator_handoff(task, response, case, action)
        return True
    if kind == "query_dependency":
        if task.get("schema_version") != 5:
            raise ValueError("Dependency queries require Protocol 6")
        return queue_dependency_query(task, response, case, action)
    if kind == "get_fact":
        task["latest_fact"] = fact_reference["id"]
        task["status"] = "ready"
        return True
    if kind == "restore_candidate":
        return restore_candidate(task, case, action, snapshot)
    if kind == "finish":
        context = context_from_reference(freeze_context(task), case, snapshot)
        if task.get("schema_version") in {4, 5}:
            from .cases.requirements import requirements_for_case

            requirements = requirements_for_case(case).contract.requirements
            observations = [public_observation(task, ref) for ref in task["observations"]]
            if task["protocol"].get("workflow_profile", "workbench") == "simple_tools":
                coverage = simple_finish_decision(action, requirements, observations, snapshot.revision)
            else:
                coverage = finish_contract_decision(action, context, requirements, observations, snapshot.revision)
        else:
            coverage = finish_coverage_decision(action, context)
        if not coverage["accepted"]:
            if coverage["code"] in {"unverified_contract_blocks_success", "contract_not_ready_for_success"}:
                if task.get("schema_version") not in {4, 5}:
                    raise ValueError(
                        "candidate_ready/no_change_claimed cannot include an unverified in-contract requirement: "
                        + repr(coverage.get("unverified_in_contract", coverage.get("effective_coverage")))
                    )
                raise ValueError(
                    "candidate_ready/no_change_claimed cannot include an unobserved development-gate "
                    "requirement or any current counterexample; obtain bound current evidence or finish unresolved: " +
                    repr({
                        "unobserved_development_requirement_ids": coverage.get(
                            "unobserved_development_requirement_ids", []
                        ),
                        "counterexample_requirement_ids": coverage.get(
                            "counterexample_requirement_ids", []
                        ),
                    })
                )
            if coverage["code"] == "contract_requirement_set_mismatch":
                raise ValueError("Finish must declare every frozen contract requirement exactly once: " + repr(coverage))
            raise ValueError(
                "Finish blocked: an unverified in-contract requirement still has bounded reviewed diagnostic "
                "capacity. Request one discriminating observation using an available new/revisable/reusable "
                "probe route, or report a concrete tool limitation; do not invent a limitation merely to exit. "
                f"requirements={coverage['actionable_requirements']!r}; "
                f"remaining_diagnostic_runs={coverage['remaining_diagnostic_runs']}; "
                f"available_routes={coverage['available_routes']!r}"
            )
        if action["reason"] != "unresolved":
            passed = [public_observation(task, r) for r in task["observations"]]
            if "finish_requirements" in context:
                from .finish_evidence import require_submission_evidence

                require_submission_evidence(action, context["finish_requirements"])
            else:
                measured = [r for r in passed if r["kind"] == "probe" and "assessment" in r["result"]]
                # 旧策略保留原义；改动源码不会自动消除已观察反例。
                repaired = {r["result"]["assessment"]["probe_id"] for r in measured
                            if r["revision"] == snapshot.revision and r["result"]["status"] == "passed"
                            and r["result"]["assessment"]["conclusion"] == "no_counterexample_observed"}
                counterexamples = [r for r in measured
                                   if r["result"]["assessment"]["conclusion"] == "counterexample_observed"
                                   and (r["revision"] == snapshot.revision
                                        or r["result"]["assessment"]["probe_id"] not in repaired)]
                if counterexamples:
                    raise ValueError("Unresolved measured counterexample blocks finish even if pytest passed or referenced; "
                                     "repair and rerun the same probe on the current revision, or finish unresolved: " +
                                     ", ".join(r["id"] for r in counterexamples))
                failures = [r for r in passed if r["kind"] == "probe" and r["revision"] == snapshot.revision
                            and (r["result"]["status"] != "passed"
                                 or r["result"].get("assessment", {}).get("conclusion") in {"inconclusive", "observation_only"})]
                missing_refs = ["observation:" + r["id"] for r in failures
                                if "observation:" + r["id"] not in action["evidence_refs"]]
                if missing_refs:
                    raise ValueError("Reference and explain current probe failures and observation-only limits in finish, "
                                     "or finish unresolved. Finish not accepted. Missing evidence_refs: " + repr(missing_refs) +
                                     ". This is a citation/scope correction, not a request for another execution.")
            passed = [r for r in passed if r["kind"] == "public_checks" and r["revision"] == snapshot.revision
                      and r["result"]["status"] == "passed"]
            if not passed:
                raise ValueError("Finish requires completed current public checks; use run_public_checks or unresolved")
            if action["reason"] == "candidate_ready" and (
                not snapshot.patch or not task["candidate"] or not task["candidate"]["reviewed"]
            ):
                raise ValueError("candidate_ready requires actual reviewed changes")
            if action["reason"] == "no_change_claimed" and snapshot.patch:
                raise ValueError("no_change_claimed requires unchanged source")
        # 宿主保留范围限制，不让模型的成功措辞覆盖原始未证实要求。
        task["finish"] = action | {"diagnostic_scope": {
            **({"finish_requirements": context["finish_requirements"]}
               if "finish_requirements" in context else {}),
            "runtime_findings": context["runtime_findings"],
            "unestablished_oracles": [item for p in context["probes"] if not p["superseded_by"]
                                       for item in p["unestablished_oracles"]],
            "contract_coverage_gate": coverage,
            "acceptance": "Separate independent final checks required; diagnostics do not establish general correctness."}}
        if task.get("schema_version") == 3:
            from .generation.coverage_evidence import qualify_coverage

            task["finish"]["diagnostic_scope"]["coverage_evidence"] = qualify_coverage(
                action["contract_coverage"],
                [public_observation(task, ref) for ref in task["observations"]],
                snapshot.revision,
            )
        task["verification_subject"] = {"revision": snapshot.revision, "patch_sha256": snapshot.sha256,
                                         "source_reference": snapshot.reference, "kind": "candidate" if snapshot.patch else "original"}
        task["status"] = {"candidate_ready": "submitted", "no_change_claimed": "no_change_claimed",
                          "unresolved": "unresolved"}[action["reason"]]
        return False
    if kind in NAVIGATION:
        result = navigate(case, snapshot, action)
        signature = digest([action, result])
        task["read_repeats"] = task.get("read_repeats", 0) + 1 if signature == task.get("last_read") else 1
        task["last_read"] = signature
        subject = load_candidate(case) if action.get("view") == "original" else snapshot
        ref = observe(task, case, action, result, kind="source", snapshot=subject)
        task["latest_observation"] = ref["id"]
        if task["protocol"].get("investigation_policy") is None and task["read_repeats"] >= 3:
            task.update(status="unresolved", stop_reason="repeated_source_action_no_progress")
            return False
        task["status"] = "ready"
        return True
    if kind == "get_observation":
        ref = next((r for r in task["observations"] if r["id"] == action["observation_id"]), None)
        if ref is None:
            raise ValueError("Unknown public observation")
        frozen = read(freeze_context(task), _root(task))
        if ref["id"] in frozen.get("hidden_observation_ids", []):
            raise ValueError("Observation is not visible in this context")
        public_observation(task, ref)
        if action.get("cursor") is not None:
            from .diagnostic_output import output_page

            output_page(frozen, ref, action["cursor"], current_revision=snapshot.revision)
            task["latest_observation_output"] = {"observation_id": ref["id"], "cursor": action["cursor"]}
        else:
            task.pop("latest_observation_output", None)
        task["latest_observation"] = ref["id"]
        task["status"] = "ready"
        return True
    if kind not in DIAGNOSTIC_ACTIONS:
        return None
    subject = load_candidate(case) if action.get("view") == "original" else snapshot
    if action["revision"] != subject.revision:
        raise ValueError("Stale diagnostic source revision")
    if kind == "run_public_checks":
        for ref in reversed(task["observations"]):
            previous = public_observation(task, ref)
            if (previous["kind"] == "public_checks" and previous["revision"] == subject.revision
                    and previous["result"]["status"] != "execution_incomplete"):
                task.update(status="ready", latest_observation=ref["id"], diagnostic_reused=True)
                return True
    if kind in {"propose_probe", "revise_probe"}:
        probe_id = register_probe(task, action, case, snapshot)
    elif kind == "run_probe":
        probe_id = action["probe_id"]
        if probe_id not in {r["id"] for r in task["probes"]}:
            raise ValueError("Unknown probe")
    else:
        probe_id = None
    execution = task["protocol"].get("execution", {})
    request = {"task_id": task["task_id"], "case_fingerprint": case.fingerprint,
               "revision": subject.revision, "source_reference": subject.reference,
               "patch_sha256": subject.sha256, "probe_id": probe_id,
               "execution": execution, "implementation": task["protocol"]["implementation"],
               "kind": "probe" if probe_id else "public_checks"}
    key = digest(request)
    for run in task["diagnostic_runs"]:
        if run["key"] == key and run.get("result"):
            receipt = read(run["result"], _root(task))
            if receipt["public"]["status"] != "execution_incomplete":
                # 重用的是同一次隔离观察，不宣称重新执行或在新环境上通过。
                task["latest_observation"] = run["observation"]["id"]
                task["diagnostic_reused"] = True
                task["status"] = "ready"
                return True
    if len(task["diagnostic_runs"]) >= task["protocol"]["diagnostic_policy"]["max_runs"]:
        raise ValueError("Diagnostic execution limit reached")
    ref = store(_root(task) / "diagnostic_requests", request)
    task["pending_diagnostic"] = {"request": ref, "key": key, "action": action,
                                   "origin_receipt": response["report_path"]}
    task["status"] = "pending_diagnostic_review"
    return False


def _clean(text):
    # 先完整识别公开 HTTP URI，避免把协议尾部当盘符；中文紧贴的真实盘符仍须脱敏。
    text = re.sub(r"(?i)(https?://[^\s\"']+)|[a-z]:[/\\][^\s\"']+",
                  lambda match: match[1] or "<host-path>", text)
    text = text.replace("/work/source/", "source/").replace("/work/checks/", "public-check/")
    text = re.sub(r"/(?:home|users|tmp|workspace)/[^\s\"']+", "<execution-path>", text)
    return text


def _public_path(value):
    """把 legacy 日志中的绝对位置降为不可回溯的执行路径占位符。"""
    value = _clean(value)
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        return "<execution-path>"
    return value


def _bounded(text, limit):
    """有界保留首尾，明确标注截断，而不是静默丢掉中间证据。"""
    text = str(text or "")
    if len(text) <= limit:
        return text, False
    marker = "\n... <truncated> ...\n"
    width = max(1, limit - len(marker))
    left = max(1, width * 2 // 3)
    return text[:left] + marker + text[-(width - left):], True


def _pytest_failure_blocks(text):
    """从 pytest 终端输出分离失败段；warnings 不再与失败尾部竞争容量。"""
    cleaned = _clean(text)
    lines = cleaned.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.match(r"^=+\s*FAILURES\s*=+\s*$", line)), None)
    if start is None:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^=+\s*(?:warnings summary|short test summary info)\s*=+\s*$", lines[i]):
            end = i
            break
    body = lines[start + 1:end]
    headings = [i for i, line in enumerate(body)
                if re.match(r"^_{5,}.*_{5,}\s*$", line)]
    if not headings:
        value = "\n".join(body).strip()
        return [({"nodeid": None, "when": "call", "message": value, "truncated": False})] if value else []
    blocks = []
    for index, begin in enumerate(headings):
        finish = headings[index + 1] if index + 1 < len(headings) else len(body)
        heading = body[begin].strip("_").strip()
        value = "\n".join(body[begin + 1:finish]).strip()
        if value:
            blocks.append({"nodeid": heading or None, "when": "call", "message": value, "truncated": False})
    return blocks


def _pytest_short_summary(text, known_nodeids=()):
    """读取 pytest 尾部的失败摘要，并只保留可与本阶段关联的身份。"""
    def normal(value):
        value = str(value or "").strip()
        for prefix in ("/work/checks/", "public-check/", "checks/"):
            if value.startswith(prefix):
                return value[len(prefix):]
        return value

    known = {normal(nodeid): nodeid for nodeid in known_nodeids if isinstance(nodeid, str)}
    records = []
    for line in str(text or "").splitlines():
        match = re.match(r"^(FAILED|ERROR)\s+(.+?)(?:\s+-\s+(.*))?$", line.strip())
        if not match:
            continue
        raw_nodeid = match.group(2).strip()
        normalized = normal(raw_nodeid)
        # 阶段带有收集身份时拒绝日志中无法绑定的条目；旧报告没有身份时
        # 仍保留原 nodeid 作为可审阅线索，但不把它当作已收集用例。
        if known and normalized not in known:
            continue
        records.append({
            "nodeid": known.get(normalized, raw_nodeid),
            "when": "collect" if match.group(1) == "ERROR" and normalized not in known else "call",
            "outcome": "error" if match.group(1) == "ERROR" else "failed",
            "message": match.group(3) or line.strip(),
            "truncated": False,
        })
    return records


def public_failure_details(stage, text=None):
    """将执行器失败报告投影成绑定前仍可核验的有限公开详情。"""
    records = stage.get("failure_details")
    if isinstance(records, list) and records:
        details = []
        for record in records[:16]:
            if not isinstance(record, dict) or not isinstance(record.get("message"), str):
                continue
            message, clipped = _bounded(_clean(record["message"]), 3000)
            detail = enrich_failure_detail({
                "nodeid": record.get("nodeid") or None,
                "when": record.get("when") or "call",
                "category": "pytest_error" if record.get("outcome") == "error" else "pytest_failure",
                "message": message,
                "truncated": bool(record.get("truncated")) or clipped,
            }, record["message"])
            location = record.get("location")
            if isinstance(location, dict) and isinstance(location.get("path"), str):
                detail["location"] = {"path": _public_path(location["path"]), "line": location.get("line")}
            details.append(detail)
        if details:
            return details
    raw_text = text or ""
    blocks = _pytest_failure_blocks(raw_text)
    summaries = _pytest_short_summary(raw_text, stage.get("nodeids", []))
    details = []
    for index, block in enumerate(blocks[:16]):
        summary = next((item for item in summaries
                        if item["nodeid"] and str(block.get("nodeid") or "").strip()
                        and str(item["nodeid"]).split("::")[-1].split("[")[0]
                        == str(block["nodeid"]).split("::")[-1].split("[")[0]), None)
        nodeid = summary["nodeid"] if summary else block.get("nodeid")
        message, clipped = _bounded(block["message"], 3000)
        details.append(enrich_failure_detail({
            "nodeid": nodeid, "when": block.get("when", "call"),
            "category": "pytest_failure", "message": message,
            "truncated": bool(block.get("truncated")) or clipped,
        }, block["message"]))
    if not details:
        for summary in summaries[:16]:
            message, clipped = _bounded(summary["message"], 3000)
            details.append(enrich_failure_detail({
                "nodeid": summary["nodeid"], "when": summary["when"],
                "category": "pytest_error" if summary["outcome"] == "error" else "pytest_failure",
                "message": message, "truncated": bool(summary.get("truncated")) or clipped,
            }, summary["message"]))
    return details


def _warning_excerpt(stage, text):
    records = stage.get("warning_details")
    if isinstance(records, list) and records:
        lines = []
        for record in records[:8]:
            if isinstance(record, dict) and isinstance(record.get("message"), str):
                category = record.get("category") or "Warning"
                lines.append(f"{category}: {_clean(record['message'])}")
        value = "\n".join(lines)
    else:
        cleaned = _clean(text)
        lines = cleaned.splitlines()
        start = next((i for i, line in enumerate(lines)
                      if re.match(r"^=+\s*warnings summary\s*=+\s*$", line)), None)
        end = next((i for i in range((start or 0) + 1, len(lines))
                    if re.match(r"^=+\s*short test summary info\s*=+\s*$", lines[i])), len(lines)) \
            if start is not None else None
        value = "\n".join(lines[start + 1:end]).strip() if start is not None else ""
    bounded, truncated = _bounded(value, 1200)
    if not bounded:
        return None
    return {"text": bounded, "truncated": truncated or bool(stage.get("warning_details_truncated"))}


def _diagnostic_excerpt(text, *, failure_details=None, critical_evidence=None):
    """优先保留失败用例的断言和调用位置，warning 单独处理。"""
    from .diagnostic_output import without_warnings

    priority = ""
    if critical_evidence:
        priority = "[critical failure evidence]\n" + "\n".join(
            f"{item['kind']}: {item['text']}" for item in critical_evidence
        ) + "\n\n"
    if failure_details:
        blocks = []
        for detail in failure_details:
            label = detail.get("nodeid") or "pytest failure"
            blocks.append(f"[{label} / {detail.get('when', 'call')}]\n{detail['message']}")
        return _bounded(priority + "\n\n".join(blocks), 2500)
    cleaned = without_warnings(_clean(text))
    failure_blocks = _pytest_failure_blocks(cleaned)
    if failure_blocks:
        return _bounded("\n\n".join(block["message"] for block in failure_blocks), 2500)
    exception_lines = list(dict.fromkeys(re.findall(
        r"^E\s+((?:[A-Za-z_]\w*\.)*[A-Z]\w*:\s*[^\r\n]+)", cleaned, re.MULTILINE)))
    if not exception_lines or len(cleaned) <= 2500:
        return _bounded(cleaned, 2500)
    selected = exception_lines if len(exception_lines) <= 4 else exception_lines[:2] + exception_lines[-2:]
    prefix = "[pytest exception lines, original order]\n" + "\n".join(line[:350] for line in selected) + "\n[output tail]\n"
    return _bounded(prefix + cleaned, 2500)[0], True


def summarize(report, *, probe=False, oracle=None, observation_scope=None):
    from .diagnostic_output import output_cursor
    from .workflow import _fully_passed

    if observation_scope is not None and (not probe or oracle is not None):
        raise ValueError("Observation-only scope cannot be combined with a numeric oracle or public checks")
    stages = {}
    stdout = {}
    dependencies = []
    for name, value in report["stages"].items():
        if value.get("status") == "not_supplied":
            continue
        row = {k: value[k] for k in ("status", "tests", "nodeids", "execution_status", "test_outcome") if k in value}
        row["output_streams"] = []
        combined_output = []
        for field in ("stdout_path", "stderr_path"):
            if value.get(field):
                path = Path(value[field])
                assert_no_links(path)
                data = path.read_bytes()
                sha256 = hashlib.sha256(data).hexdigest()
                stream = field.removesuffix("_path")
                dependencies.append({"path": str(path), "sha256": sha256, "stage": name, "stream": stream})
                row["output_streams"].append({"stream": stream, "sha256": sha256,
                                             "cursor": output_cursor(name, stream, sha256)})
                if field == "stdout_path":
                    stdout[name] = data.decode("utf-8", errors="replace")
                text = "\n".join(line for line in data.decode("utf-8", errors="replace").splitlines()
                                 if "UPGRADE_WORKBENCH_RESULT" not in line)
                combined_output.append(text)
        raw_output = "\n".join(combined_output)
        details = public_failure_details(value, raw_output)
        critical_evidence, failure_clusters = aggregate_failure_evidence(details)
        row["failure_details"] = details
        row["critical_evidence"] = critical_evidence
        row["failure_clusters"] = failure_clusters
        records = value.get("failure_details")
        row["failure_details_total"] = len(records) if isinstance(records, list) and records else len(details)
        row["failure_details_truncated"] = bool(value.get("failure_details_truncated")) or (
            isinstance(records, list) and len(records) > len(details)
        )
        warning = _warning_excerpt(value, raw_output)
        if warning is not None:
            row["warning_summary"] = warning
        row["output_excerpt"], row["output_excerpt_truncated"] = _diagnostic_excerpt(
            raw_output,
            failure_details=details,
            critical_evidence=critical_evidence,
        )
        if not probe:
            row["failed_nodeids"] = failed_public_nodes(row, raw_output)
        stages[name] = row
    target_name = "new_candidate"
    target = report["stages"].get(target_name, {})
    if target.get("status") in {None, "not_supplied"}:
        target_name = "new_original"
        target = report["stages"].get(target_name, {})
    old = report["stages"].get("old_original", {})
    incomplete_stage = any(not _completed_diagnostic_stage(row, probe=probe) for row in report["stages"].values())
    if report["status"] in {"blocked_environment", "execution_error", "execution_incomplete", "interrupted",
                            "baseline_invalid", "test_set_changed"} or incomplete_stage or not old or not target:
        state = "execution_incomplete"
    elif _fully_passed(target) and (probe or (_fully_passed(old) and old.get("nodeids") == target.get("nodeids"))):
        state = "passed"
    else:
        state = "failed"
    result = {"scope": "model_probe_not_acceptance" if probe else "public", "status": state,
              "stages": stages, "coverage": "Only these observations; not independent final acceptance"}
    if probe:
        result["execution_status"] = "incomplete" if state == "execution_incomplete" else "completed"
    invalid_setup = setup_failure(stages) if probe else None
    if invalid_setup:
        result["probe_validity"] = invalid_setup
    # 直接升级无法导入，仍可比较旧环境与候选；只阻断实际被评估对象的启动失败。
    if invalid_setup and any(item["stage"] == target_name for item in invalid_setup["affected_stages"]):
        result["assessment"] = {
            "conclusion": "setup_failed",
            "oracle": oracle,
            **({"declared_scope": observation_scope} if observation_scope is not None else {}),
            "scope_limit": invalid_setup["scope_limit"],
        }
    elif probe and oracle is not None:
        assessed_stages = {name: row | {"status": "passed" if _fully_passed(report["stages"][name]) else "failed"}
                           for name, row in stages.items()}
        result["assessment"] = evaluate(oracle, assessed_stages, stdout)
    elif observation_scope is not None:
        result["assessment"] = {
            "conclusion": "observation_only", "oracle": None,
            "declared_scope": observation_scope,
            "scope_limit": "Local execution output only. Expected observation is a hypothesis, not a measured "
                           "business predicate. Pytest success, matching output or equal values across different "
                           "objects do not establish the broader requirement or clear a prior counterexample."}
    if probe and report.get("probe_scaffold"):
        result["probe_scaffold"] = report["probe_scaffold"]
    # 按整个UTF-8产物预算逐级投影；测试计数和判定来自完整报告，不受摘要裁剪影响。
    for width, count in ((1200, 8), (600, 4), (300, 2), (120, 1), (80, 0)):
        if len(encoded(result)) <= 20_000:
            break
        result["truncated"] = True
        for row in result["stages"].values():
            row["output_excerpt"], clipped = _bounded(row.get("output_excerpt", ""), width)
            row["output_excerpt_truncated"] = row.get("output_excerpt_truncated", False) or clipped
            warning = row.get("warning_summary")
            if warning:
                warning["text"], clipped = _bounded(warning["text"], min(width, 300))
                warning["truncated"] = warning["truncated"] or clipped
            for detail in row.get("failure_details", []):
                detail["message"], clipped = _bounded(detail.get("message", ""), width)
                detail["truncated"] = detail.get("truncated", False) or clipped
            if len(row.get("failure_details", [])) > count:
                row["failure_details"] = row["failure_details"][:count]
                row["failure_details_truncated"] = True
            for field in ("nodeids", "failed_nodeids"):
                values = row.get(field, [])
                if len(values) > count * 4:
                    row.setdefault(field + "_total", len(values))
                    row[field] = values[:count * 4]
                    row[field + "_truncated"] = True
    if len(encoded(result)) > 20_000:
        for row in result["stages"].values():
            for field in ("critical_evidence", "failure_clusters"):
                values = row.get(field, [])
                if len(values) > 4:
                    row[field + "_total"] = len(values)
                    row[field] = values[:4]
                    row[field + "_truncated"] = True
    if len(encoded(result)) > 20_000:
        raise ValueError("Diagnostic metadata exceeds public capacity")
    return result, dependencies


def run_probe(case, snapshot, code: str, directory: Path, execution: dict, *, executor=None):
    """同一探针跨版本执行；只物化探针，不挂载或复制最终检查。"""
    from .execution.docker import DockerExecutor
    from .execution.postgres import load_postgres_spec
    from .probe_scaffolds import scaffold_for
    from .workflow import _check_snapshots, _snapshot_hashes

    report = {"kind": "probe", "check_group": "probe", "case_fingerprint": case.fingerprint,
              "revision": snapshot.revision, "stages": {}, "environments": {}, "status": "running",
              "phase": "started", "current_action": "materialize", "outcome": "unknown"}
    directory.mkdir(parents=True, exist_ok=False)
    report_path = directory / "report.json"
    report["report_path"] = str(report_path)
    _checkpoint_report(report, report_path)
    try:
        stage_source(case, directory / "original")
        if snapshot.patch:
            (directory / "candidate.patch").write_bytes(snapshot.patch)
            stage_source(case, directory / "candidate", directory / "candidate.patch")
        checks = directory / "checks"
        checks.mkdir()
        (checks / "test_probe.py").write_text(code, encoding="utf-8")
        scaffold = scaffold_for(case)
        if scaffold is not None:
            (checks / "conftest.py").write_text(scaffold.code, encoding="utf-8")
            report["probe_scaffold"] = scaffold.public()
        for environment in ("old", "new"):
            name = f"requirements/{environment}.txt"
            (directory / f"{environment}.txt").write_bytes(
                read_verified_file(case.root, name, case.manifest.file_hashes[name])
            )
        environment_options = {}
        if case.environment is not None:
            assets = directory / "environment-assets"
            assets.mkdir()
            for name in case.environment.local_wheels:
                target = assets / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(
                    read_verified_file(case.root, name, case.manifest.file_hashes[name])
                )
            environment_options = {
                "python_version": case.environment.python_version,
                "local_wheels": [assets / name for name in case.environment.local_wheels],
            }
        hashes = {name: _snapshot_hashes(directory / name) for name in ("original", "checks")}
        if snapshot.patch:
            hashes["candidate"] = _snapshot_hashes(directory / "candidate")
        runner = executor or DockerExecutor(directory / "executor")
        pg = load_postgres_spec(case)
        options = {"postgres": pg} if pg else {}
        base = execution.get("base_image")
        if case.environment is not None:
            if base is not None and base != case.environment.base_image:
                raise ValueError("Diagnostic base image conflicts with the case environment")
            base = case.environment.base_image
        for env in ("old", "new"):
            _check_snapshots(directory, hashes)
            report.update(phase="preparing", current_action=f"prepare_{env}")
            _checkpoint_report(report, report_path)
            image = runner.prepare_environment(
                directory / f"{env}.txt",
                timeout_seconds=execution.get("prepare_timeout", 600),
                **environment_options,
                **({"base_image": base} if base else {}),
            )
            report["environments"][env] = image
            _checkpoint_report(report, report_path)
            if env == "old":
                base = image["base_image_digest"]
            elif image["base_image_id"] != report["environments"]["old"]["base_image_id"]:
                raise RuntimeError("Probe environment base image changed")
            for source in (("original", "candidate") if env == "new" and snapshot.patch else ("original",)):
                _check_snapshots(directory, hashes)
                report.update(phase="target_started", current_action=f"verify_{env}_{source}")
                _checkpoint_report(report, report_path)
                report["stages"][f"{env}_{source}"] = runner.verify(image["image_id"], directory / source,
                    checks, timeout_seconds=execution.get("test_timeout", 60), diagnostic=True, **options)
                _checkpoint_report(report, report_path)
                _check_snapshots(directory, hashes)
        report["status"] = "observed"
        report.update(phase="target_completed", current_action="target_completed", outcome="completed")
        if load_case(case.manifest_path).fingerprint != case.fingerprint:
            raise DiagnosticContractError("Case changed during probe execution")
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        report["status"] = "execution_incomplete"
        report["outcome"] = "unknown"
        report["failure"] = _diagnostic_failure(error, report.get("phase", "unknown"))
        report["last_phase"] = report.get("phase", "unknown")
        report["phase"] = "unknown" if report.get("phase") == "target_started" else "failed"
    finally:
        report["last_action"] = report.pop("current_action", "unknown")
        _checkpoint_report(report, report_path)
    return report


def review_diagnostic(task, case, *, request_id, reviewer, note, decision, comparator, probe_runner=run_probe):
    from .tasks import _save

    pending = task["pending_diagnostic"]
    if request_id != pending["request"]["id"] or not reviewer or not note or decision not in {"accept", "reject"}:
        raise ValueError("Diagnostic review must bind the exact pending request")
    request = read(pending["request"], _root(task))
    if (request["task_id"] != task["task_id"] or request["case_fingerprint"] != case.fingerprint
            or digest(request) != pending["key"] or request["execution"] != task["protocol"].get("execution", {})
            or request["implementation"] != task["protocol"]["implementation"]):
        raise ValueError("Diagnostic execution contract changed")
    snapshot = load_candidate(case, request["source_reference"])
    current = load_candidate(case, task["current_candidate"])
    if (request["revision"] != snapshot.revision or
        (pending["action"].get("view") != "original" and current.revision != snapshot.revision)):
        raise ValueError("Stale diagnostic review")
    run = pending | {"review": {"reviewer": reviewer, "note": note, "decision": decision}, "status": "rejected"}
    if decision == "reject":
        task["diagnostic_reviews"].append(run)
        task.update(status="ready", edit_feedback=("diagnostic_contract_rejected: " + _clean(note))[:1000])
        task["pending_diagnostic"] = None
        return
    task["diagnostic_reviews"].append(run)
    run.update(status="running", phase="started", outcome="unknown", revision=snapshot.revision)
    index = len(task["diagnostic_runs"])
    run["directory"] = str(_root(task) / "executions" / f"{index:03d}-{request_id}")
    task["diagnostic_runs"].append(run)
    task["status"] = "diagnosing"
    _save(task, Path(task["task_path"]))
    directory = Path(run["directory"])
    try:
        directory.mkdir(parents=True, exist_ok=False)
        run["last_action"] = "dispatch"
        _save(task, Path(task["task_path"]))
        probe = None
        if request["probe_id"]:
            probe_ref = next((p for p in task["probes"] if p["id"] == request["probe_id"]), None)
            if probe_ref is None:
                raise DiagnosticContractError("Reviewed probe identity is not registered")
            try:
                probe = read(probe_ref, _root(task))
            except ValueError as error:
                raise DiagnosticContractError("Reviewed probe artifact changed") from error
            report = probe_runner(case, snapshot, probe["code"], directory / "probe", request["execution"])
        else:
            patch = directory / "candidate.patch"
            if snapshot.patch:
                patch.write_bytes(snapshot.patch)
            from .generation.investigator_context import FOLLOWUP_POLICY, RESILIENT_POLICY

            # 仅显式新策略保留成功输出；旧请求与独立验收不改变执行模式。
            output_options = ({"diagnostic": True}
                              if task["protocol"].get("investigator_context_policy")
                              in (FOLLOWUP_POLICY, RESILIENT_POLICY) else {})
            report = comparator(case.manifest_path, directory,
                candidate_patch=patch if snapshot.patch else None,
                candidate_origin=snapshot.origin if snapshot.patch else None,
                **({"expected_candidate_sha256": snapshot.sha256} if snapshot.patch else {}),
                check_group="feedback", **output_options, **request["execution"])
            if not isinstance(report, dict):
                raise DiagnosticContractError("Diagnostic runner returned no report object")
            if report.get("check_group") != "feedback" or report.get("case_fingerprint") != case.fingerprint:
                raise DiagnosticContractError("Diagnostic result outside public case")
            if snapshot.patch and report.get("candidate", {}).get("supplied_sha256") != snapshot.sha256:
                raise DiagnosticContractError("Diagnostic result source changed")
        if not isinstance(report, dict) or not isinstance(report.get("report_path"), str):
            raise DiagnosticContractError("Diagnostic report path is missing")
        report_path = Path(report["report_path"])
        assert_no_links(report_path)
        try:
            report_bytes = report_path.read_bytes()
        except OSError:
            raise
        try:
            persisted = json.loads(report_bytes)
        except ValueError as error:
            raise DiagnosticContractError("Diagnostic report cannot be verified") from error
        if persisted != report:
            raise DiagnosticContractError("Diagnostic report differs from persisted receipt")
        run["report"] = {"path": str(report_path), "sha256": hashlib.sha256(report_bytes).hexdigest()}
        run["phase"] = _execution_phase(report)
        run["last_action"] = report.get("last_action", "report_returned")
        _save(task, Path(task["task_path"]))
        try:
            public, dependencies = summarize(report, probe=bool(request["probe_id"]),
                                             oracle=probe.get("oracle") if request["probe_id"] else None,
                                             observation_scope={key: probe[key] for key in ("purpose", "expected_observation")}
                                             if request["probe_id"] and "oracle" in probe and probe["oracle"] is None else None)
        except ValueError as error:
            raise DiagnosticContractError("Diagnostic report cannot produce a bounded public summary") from error
        if "assessment" in public:
            public["assessment"]["probe_id"] = request["probe_id"]
        dependencies.append({"path": str(report_path), "sha256": run["report"]["sha256"]})
        receipt = {"task_id": task["task_id"], "revision": snapshot.revision, "request": pending["request"],
                   "review": run["review"], "public": public, "dependencies": dependencies,
                   "phase": run["phase"], "last_action": run["last_action"],
                   "failure": report.get("failure")}
        try:
            ref = store(directory / "completed", receipt)
        except ValueError as error:
            raise DiagnosticContractError("Diagnostic completion receipt cannot be bound") from error
        # 完成收据独立于任务文件；崩溃后按确定执行目录接纳，不重新派发。
        run["result"] = ref
        try:
            complete_run(task, case, run)
        except ValueError as error:
            raise DiagnosticContractError("Diagnostic completion receipt violates task state") from error
    except Exception as error:
        _fail_run(task, run, error, reconcile=isinstance(error, DiagnosticContractError))
        _save(task, Path(task["task_path"]))
    return task


def complete_run(task, case, run):
    receipt = read(run["result"], _root(task))
    request = read(run["request"], _root(task))
    if receipt["request"] != run["request"] or receipt["review"] != run["review"]:
        raise ValueError("Execution receipt does not match reviewed request")
    snapshot = load_candidate(case, request["source_reference"])
    # 先校验证据，再发布观察，避免坏引用污染后续所有模型上下文。
    _validate_execution_evidence(task, receipt, snapshot)
    observation = run.get("observation") or observe(
        task, case, run["action"], receipt["public"], kind=request["kind"], snapshot=snapshot,
        execution_reference=run["result"], publish=False)
    public_observation(task, observation)
    if observation not in task["observations"]:
        task["observations"].append(observation)
    run["observation"] = observation
    run["phase"] = receipt.get("phase", "unknown")
    run["last_action"] = receipt.get("last_action", "unknown")
    public_status = receipt["public"]["status"]
    if public_status == "execution_incomplete":
        failure = receipt.get("failure") or {
            "category": "infrastructure", "code": "diagnostic_execution_incomplete",
            "phase": run["phase"], "error_type": "ExecutionIncomplete",
            "reason": "The bounded target report did not establish a complete execution."}
        _fail_run(task, run, RuntimeError(), failure=failure, outcome="execution_incomplete")
    else:
        run.update(status="completed", last_phase=run["phase"], phase="completed",
                   outcome="semantic_failed" if public_status == "failed" else "passed")
    task["pending_diagnostic"] = None
    task["latest_observation"] = run["observation"]["id"]
    task["read_repeats"] = 0
    task["status"] = "execution_incomplete" if receipt["public"]["status"] == "execution_incomplete" else "ready"
    current = load_candidate(case, task["current_candidate"])
    note_progress(task, current.revision, observation_id=run["observation"]["id"], changed=True)
    _sync_diagnostic_review(task, run)


def recover_diagnostic(task):
    """恢复也可能遇到损坏/缺失收据；收口并持久化，绝不重放执行。"""
    from .tasks import _save

    try:
        return _recover_diagnostic(task)
    except Exception as error:
        run = task["diagnostic_runs"][-1]
        if isinstance(error, (ValueError, KeyError, TypeError)):
            error = DiagnosticContractError("Diagnostic recovery evidence requires reconciliation")
        _fail_run(task, run, error, reconcile=True)
        _save(task, Path(task["task_path"]))
        return task


def _recover_diagnostic(task):
    case = load_case(Path(task["manifest_path"]))
    run = task["diagnostic_runs"][-1]
    directory = Path(run["directory"]) / "completed"
    assert_no_links(directory)
    paths = list(directory.glob("*.json")) if directory.exists() else []
    failed_paths = []
    for failed in (Path(run["directory"]) / "failed", _root(task) / "diagnostic_failures"):
        if failed.exists():
            failed_paths.extend(failed.glob("*.json"))
    if run.get("reconciliation_required"):
        # 失败收据是停止证据，不允许用另一份completed文件绕过完整性冲突。
        return task
    matching_failures = []
    for path in failed_paths:
        reference = {"id": path.stem, "path": str(path)}
        value = read(reference, _root(task))
        if (value.get("execution_id") == Path(run["directory"]).name
                and value.get("request") == run.get("request")
                and value.get("review") == run.get("review")):
            matching_failures.append((reference, value))
    integrity_failures = [(ref, value) for ref, value in matching_failures if value.get("reconciliation_required")]
    if integrity_failures:
        # 失败收据已落盘而任务游标未更新时，也不能优先采用旧completed收据。
        failure_ref, failure = integrity_failures[-1]
        run.update(status="failed", phase="failed", outcome=failure["outcome"],
                   last_phase=failure["phase"], last_action=failure.get("last_action", "unknown"),
                   failure=failure["failure"], failure_receipt=failure_ref, reconciliation_required=True)
        task.update(status="execution_incomplete", pending_diagnostic=None,
                    stop_reason="diagnostic_integrity_requires_reconciliation")
        _sync_diagnostic_review(task, run)
        return task
    if len(paths) == 1:
        run["result"] = {"id": paths[0].stem, "path": str(paths[0])}
        complete_run(task, case, run)
    else:
        if len(matching_failures) == 1:
            failure_ref, failure = matching_failures[0]
            run.update(status="failed", phase="failed", outcome=failure.get("outcome", "failed"),
                       last_phase=failure.get("phase", "unknown"),
                       last_action=failure.get("last_action", "unknown"),
                       reconciliation_required=failure.get("reconciliation_required", False),
                       failure=failure.get("failure"), failure_receipt=failure_ref)
            run["recovery_cleanup"] = cleanup_run(task, run)
            task.update(status="execution_incomplete", stop_reason="diagnostic_execution_failed",
                        pending_diagnostic=None)
            _sync_diagnostic_review(task, run)
            return task
        last_phase = run.get("phase", "started")
        run.update(status="unknown", phase="unknown", outcome="unknown", last_phase=last_phase)
        run["recovery_cleanup"] = cleanup_run(task, run)
        task.update(status="execution_incomplete", stop_reason="diagnostic_interrupted_explicit_retry_required",
                    pending_diagnostic=None)
        _sync_diagnostic_review(task, run)
    return task


def cleanup_run(task, run):
    """只认本次执行目录在启动前记录的随机资源名，不按项目标签批量删除。"""
    from .execution.docker import DockerExecutor

    directory = Path(run["directory"])
    assert_no_links(directory)
    if not directory.resolve().is_relative_to(_root(task).resolve()):
        raise ValueError("Execution directory outside task")
    results = []
    runner = DockerExecutor(directory / "cleanup")
    for path in directory.rglob("report.json") if directory.exists() else []:
        assert_no_links(path)
        report = json.loads(path.read_bytes())
        operation = path.parent.name
        if not re.fullmatch(r"[0-9a-f]{32}", operation) or "container_name" not in report:
            continue
        names = {"container_name": "upgrade-workbench-verify-" + operation,
                 "materialization_container_name": "upgrade-workbench-materialize-" + operation,
                 "snapshot_image_tag": "upgrade-workbench-snapshot:" + operation}
        if any(report.get(k) != v for k, v in names.items()):
            raise ValueError("Unrecognized interrupted execution resource identity")
        for key, name in names.items():
            command = ["image", "rm", "--force", name] if key == "snapshot_image_tag" else ["rm", "--force", name]
            results.append({"resource": name, **runner._cleanup(command)})
        database = report.get("database", {}).get("container")
        if database:
            if database != "upgrade-workbench-pg-" + operation:
                raise ValueError("Unrecognized diagnostic database identity")
            results.append({"resource": database, **runner._cleanup(["rm", "--force", database])})
    return {"ok": all(row["ok"] for row in results), "resources": results}


def prepare_retry(task, request_id):
    if task["status"] != "execution_incomplete" or not task.get("diagnostic_runs"):
        raise ValueError("Only incomplete diagnostic execution can be retried")
    run = task["diagnostic_runs"][-1]
    if run.get("reconciliation_required"):
        raise ValueError("Diagnostic integrity conflict requires reconciliation before retry")
    if request_id != run["request"]["id"]:
        raise ValueError("Retry must bind the previous diagnostic request")
    if len(task["diagnostic_runs"]) >= task["protocol"]["diagnostic_policy"]["max_runs"]:
        raise ValueError("Diagnostic execution limit reached")
    cleanup = cleanup_run(task, run)
    run["recovery_cleanup"] = cleanup
    if not cleanup["ok"]:
        raise ValueError("Residual diagnostic resources require cleanup before retry")
    task["pending_diagnostic"] = {k: run[k] for k in ("request", "key", "action", "origin_receipt")}
    task.update(status="pending_diagnostic_review", stop_reason="explicit_diagnostic_retry_requires_review")
    return task


def finalize_subject(task, comparator):
    """冻结原始或候选验收对象；结果永远不加入公共观察。"""
    from .tasks import _save
    from .workflow import _fully_passed

    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, task["current_candidate"])
    candidate = task.get("candidate")
    if snapshot.patch:
        if not candidate or not candidate["reviewed"] or not any(
            r.get("revision") == snapshot.revision and r.get("sha256") == snapshot.sha256
            and r.get("reviewer") and r.get("note") and r.get("decision") != "rejected"
            for r in task["review_history"]
        ):
            raise ValueError("Final evaluation requires reviewed current changes")
    elif task["status"] != "no_change_claimed":
        raise ValueError("Unchanged source needs an explicit no_change_claimed exit")
    else:
        if not any(public_observation(task, r)["kind"] == "public_checks"
                   and public_observation(task, r)["revision"] == snapshot.revision
                   and public_observation(task, r)["result"]["status"] == "passed"
                   for r in task["observations"]):
            raise ValueError("No-change subject lacks completed reviewed public execution")
    subject = {"kind": "candidate" if snapshot.patch else "original", "revision": snapshot.revision,
               "patch_sha256": snapshot.sha256, "source_reference": snapshot.reference}
    if task.get("final_result"):
        final = task["final_result"]
        path = Path(final["path"])
        assert_no_links(path)
        if final["subject"] != subject or hashlib.sha256(path.read_bytes()).hexdigest() != final["sha256"]:
            raise ValueError("Final verification subject or result changed")
        return task
    task.update(final_evaluation="running", verification_subject=subject)
    _save(task, Path(task["task_path"]))
    patch = Path(candidate["patch_path"]) if snapshot.patch else None
    report = comparator(case.manifest_path, Path(task["work_root"]), candidate_patch=patch,
                        candidate_origin=snapshot.origin if patch else None,
                        **({"expected_candidate_sha256": snapshot.sha256} if patch else {}),
                        check_group="all", **task["protocol"].get("execution", {}))
    path = Path(report["report_path"])
    assert_no_links(path)
    if (report.get("case_fingerprint") != case.fingerprint or report.get("check_group") != "all"
            or json.loads(path.read_bytes()) != report
            or (patch and report.get("candidate", {}).get("supplied_sha256") != snapshot.sha256)):
        raise ValueError("Independent result subject mismatch")
    conclusion = report["status"]
    if patch is None:
        old = report["stages"].get("old_original", {})
        new = report["stages"].get("new_original", {})
        conclusion = ("no_source_change_needed_for_registered_checks" if _fully_passed(old) and _fully_passed(new)
                      and old.get("nodeids") == new.get("nodeids") else "no_change_claim_not_accepted")
    task["final_result"] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "subject": subject, "status": conclusion,
                            "candidate_revision": snapshot.revision, "candidate_sha256": snapshot.sha256}
    task["final_evaluation"] = "completed"
    _save(task, Path(task["task_path"]))
    return task
