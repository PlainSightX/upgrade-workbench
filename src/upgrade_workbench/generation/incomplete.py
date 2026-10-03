"""P4已知截断的有界纠正；未知远端结果不进入此路径。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .preparation import digest


def enabled(task):
    return (task["protocol"]["protocol_revision"] == 4
            and task["protocol"]["generation"].get("runtime_capacity") is not None)


def observation_identity(row):
    if row["kind"] not in {"public_checks", "probe"}:
        return None
    result = row["result"]
    if result.get("status") not in {"passed", "failed"}:
        return None
    stages = {}
    for name, stage in result.get("stages", {}).items():
        stages[name] = {k: stage[k] for k in ("status", "tests", "execution_status", "test_outcome") if k in stage}
        for key in ("nodeids", "failed_nodeids"):
            stages[name][key] = sorted(set(stage.get(key, [])))
        stages[name]["failures"] = sorted({
            tuple(str(item.get(k) or "") for k in ("nodeid", "when", "category", "failure_fingerprint"))
            for item in stage.get("failure_details", [])})
    assessment = result.get("assessment", {})
    oracle = assessment.get("oracle") or {}
    measurements = {name: {k: value[k] for k in ("status", "before", "after", "path_completed",
                    "actual", "predicate_satisfied", "conclusion") if k in value}
                    for name, value in assessment.get("measurements", {}).items()}
    return {"kind": row["kind"], "revision": row["revision"], "status": result["status"],
            "stages": stages, "assessment": {"conclusion": assessment.get("conclusion"),
            "oracle": {k: oracle[k] for k in ("basis", "operator", "expected") if k in oracle},
            "measurements": measurements}}


def scope(task, role, revision):
    from ..diagnostics import _root, public_observation, read
    from .preparation import phase

    evidence = set()
    for reference in task.get("observations", []):
        value = read(reference, _root(task))
        if value.get("kind") not in {"public_checks", "probe"} or value.get("revision") != revision:
            continue
        if not value.get("execution_reference"):
            raise ValueError("Runtime observation lacks execution evidence")
        identity = observation_identity(public_observation(task, reference))
        if identity is not None:
            evidence.add(digest(identity))
    target = phase(task) if task["protocol"].get("repository_preparation") else "solve"
    return {"role": role, "candidate_revision": revision, "decision_target": target,
            "evidence_epoch": digest(sorted(evidence))}


def verify_length(report, *, root):
    from ..service.recovery import read_bound
    from .capacity import from_record
    from .provider import _decode_json, _usage, completion_metadata

    limits = from_record(report)
    receipt = Path(report["report_path"])
    path = receipt.with_name("response.sanitized.json")
    raw = read_bound(path, root, limits["max_response_bytes"])
    if (report.get("status") != "provider_response_rejected" or report.get("reason") != "response_not_complete"
            or report.get("response_storage") != "sanitized_provider_json"
            or report.get("response_redacted") is not False or report.get("response_path") != str(path)
            or hashlib.sha256(raw).hexdigest() != report.get("response_sha256")
            or report.get("response_bytes") != len(raw)):
        raise ValueError("Length response identity changed")
    response = _decode_json(raw)
    usage = _usage(response)
    metadata = completion_metadata(response, usage)
    if (not metadata["known_length"] or metadata != report.get("response_completion")
            or usage != report.get("model_usage") or usage["output_tokens"] > limits["max_output_tokens"]
            or {"action", "audit", "candidate_patch", "candidate_sha256", "base_revision"} & set(report)):
        raise ValueError("Length response is not a verified incomplete result")
    return usage


def accept_length(task, report):
    from ..diagnostics import _root, read
    from ..service.recovery import read_bound
    from .provider import _verify_payload
    from .request import MAX_REQUEST_CAPACITY

    if (not enabled(task) or report.get("status") != "provider_response_rejected"
            or not report.get("response_completion", {}).get("known_length")):
        return None
    root = Path(task["work_root"])
    verify_length(report, root=root)
    matches = [a for a in task["attempts"] if a["receipt"] == report["report_path"]]
    if len(matches) != 1:
        raise ValueError("Length response requires one frozen attempt")
    attempt = matches[0]
    request = read_bound(Path(report["request_path"]), root, MAX_REQUEST_CAPACITY)
    if hashlib.sha256(request).hexdigest() != report["request_sha256"] or attempt.get("request_sha256") != report["request_sha256"]:
        raise ValueError("Length request identity changed")
    _verify_payload(report, request)
    frozen = read(report["diagnostic_context_reference"], _root(task))
    value = report.get("incomplete_response_scope")
    if (not isinstance(value, dict) or value != attempt.get("incomplete_response_scope")
            or value != frozen.get("incomplete_response_scope") or frozen["task_id"] != task["task_id"]):
        raise ValueError("Length scope changed")
    state = task.setdefault("incomplete_response_scopes", {})
    entry = state.setdefault(digest(value), {"scope": value, "receipts": []})
    identity = {"request_id": attempt["request_id"], "request_sha256": report["request_sha256"],
                "response_sha256": report["response_sha256"]}
    existing = next((row for row in entry["receipts"] if row["request_id"] == identity["request_id"]), None)
    if existing is not None and existing != identity:
        raise ValueError("Already counted length receipt changed")
    if existing is None:
        entry["receipts"].append(identity)
    if len(entry["receipts"]) >= 2:
        task.update(status="unresolved", stop_reason="repeated_incomplete_response_no_progress")
        return False
    task["protocol_feedback"] = {"code": "response_not_complete", "attempt_index": len(task["attempts"])}
    task.update(status="ready", edit_feedback="The preceding output reached its frozen length limit; no partial action executed. Produce one complete grounded action or use an available investigation tool. One corrective call is allowed for this same decision and evidence scope.")
    return True
