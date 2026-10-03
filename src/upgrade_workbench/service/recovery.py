"""恢复只消费可验证的本地结果，从不再次发送供应商请求。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..budget import BudgetLedger
from ..candidates import load_candidate
from ..cases import load_case
from ..cases.manifest import assert_no_links
from ..generation.actions import validate_action
from ..generation.contract_auditor import CONTRACT_AUDIT_RETRY_CODES, ContractAuditError
from ..generation.edits import edited_sources
from ..generation.provider import (
    _decode_json,
    _proposal,
    _ProviderBoundaryError,
    _usage,
    _verify_inputs,
    _verify_payload,
)
from ..generation.roles import RoleContractError, recoverable_attempt_contract
from ..tasks import (
    _accept_contract_audit,
    _process_contract_audit_response,
    _require_current_protocol,
    _save,
    accept_response,
    inspect_task,
    require_owner,
)


def version(path: Path) -> str:
    assert_no_links(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_bound(path: Path, root: Path, limit: int = 2_000_000) -> bytes:
    assert_no_links(path)
    if not path.resolve().is_relative_to(root.resolve()) or path.stat().st_size > limit:
        raise ValueError("service_artifact_outside_job")
    return path.read_bytes()


def owned_task(path: Path, root: Path, owner: str) -> dict:
    read_bound(path, root)
    task = inspect_task(path)
    require_owner(task, owner)
    if Path(task["work_root"]).resolve() != root.resolve():
        raise ValueError("task_work_root_changed")
    for item in task.get("candidate_history", []):
        read_bound(Path(item["patch_path"]), root)
    return task


def clear_stale_core_lock(path: Path, owner: str, root: Path) -> None:
    """调用者必须已经持有作业OS排他锁；不是按锁文件年龄猜测进程死亡。"""
    owned_task(path, root, owner)
    lock = path.with_suffix(".lock")
    assert_no_links(lock)
    if lock.exists():
        if lock.read_text(encoding="utf-8") != path.parent.name:
            raise ValueError("unrecognized_core_lock")
        lock.unlink()


def _attempt_contract(task: dict, attempt: dict, report: dict):
    """恢复只信任冻结 attempt 的角色与格式；旧 schema 保留原推断方式。"""
    schema_version = task.get("schema_version")
    if schema_version == 5 and (
        report.get("workflow_profile", "workbench")
        != task.get("protocol", {}).get("workflow_profile", "workbench")
    ):
        raise ValueError("recovery_workflow_profile_changed")
    if schema_version in {3, 4, 5}:
        if task.get("protocol", {}).get("repository_preparation") is not None:
            from ..generation.preparation import verify_response

            verify_response(task, report)
        try:
            spec, expected_output = recoverable_attempt_contract(task, attempt)
        except RoleContractError as error:
            raise ValueError("recovery_attempt_role_changed") from error
        reported_output = (
            report.get("output_format")
            if schema_version == 5
            else report.get("output_format", expected_output)
        )
        if reported_output != expected_output:
            raise ValueError("recovery_output_format_changed")
        return spec, expected_output
    expected_output = "candidate_actions"
    if report.get("output_format", expected_output) != expected_output:
        raise ValueError("recovery_output_format_changed")
    return None, expected_output


def _read_only_state(task: dict, *, preparation: bool = False) -> dict:
    """捕获只读角色绝不能改写的候选和终态字段。"""
    keys = (
        "candidate",
        "current_candidate",
        "candidate_history",
        "review_history",
        "final_evaluation",
        "final_candidate",
        "final_result",
        "finish",
        "verification_subject",
    )
    if preparation:
        keys += ("probes", "diagnostic_runs", "diagnostic_reviews", "pending_diagnostic")
    return {key: json.loads(json.dumps(task[key])) for key in keys if key in task}


def _require_read_only_state(task: dict, before: dict, *, preparation: bool = False) -> None:
    after = _read_only_state(task, preparation=preparation)
    if after != before:
        raise ValueError("recovery_read_only_role_changed_candidate_or_terminal_state")


def _require_read_only_report(report: dict, role: str) -> None:
    candidate_outputs = {
        "base_revision",
        "candidate_patch",
        "candidate_sha256",
        "changed_files",
        "edit_count",
        "increment_files",
        "patch_compilation",
    }
    if candidate_outputs.intersection(report):
        raise ValueError("recovery_read_only_role_returned_candidate_output")
    if role == "contract_auditor" and ("action" in report or "handoff" in report):
        raise ValueError("recovery_contract_auditor_returned_action")
    if role == "investigator" and "audit" in report:
        raise ValueError("recovery_investigator_returned_contract_audit")


def recover_task(path: Path, root: Path, owner: str, *, adopt_dependency_report: bool = False) -> dict:
    task = owned_task(path, root, owner)
    status = task["status"]
    if status == "pending_dependency_query":
        # 模型刚登记查询时还没有执行报告；只有原查询执行命令的恢复才接纳报告。
        if not adopt_dependency_report:
            return task
        from ..diagnostics import recover_pending_dependency_query

        _require_current_protocol(task["protocol"])
        task = recover_pending_dependency_query(task, load_case(Path(task["manifest_path"])))
        _save(task, path)
        return task
    if status == "diagnosing":
        from ..diagnostics import recover_diagnostic

        task = recover_diagnostic(task)
        _save(task, path)
        return task
    if status == "seeding":
        raise ValueError("seed_interrupted_requires_reconciliation")
    if status == "verifying":
        # 审阅记录保留；只有新的显式review命令才重新运行隔离检查。
        task.update(status="pending_review", stop_reason="verification_interrupted_explicit_retry_required")
        _save(task, path)
        return task
    if task["final_evaluation"] == "running":
        task["final_evaluation"] = "interrupted"
        _save(task, path)
        return task
    if status not in {"calling_model", "processing_response"}:
        return task
    _require_current_protocol(task["protocol"])
    if not task["attempts"]:
        raise ValueError("missing_attempt_at_recovery_boundary")
    attempt = task["attempts"][-1]
    receipt_path = Path(attempt["receipt"])
    report = _decode_json(read_bound(receipt_path, root))
    if (report["report_path"] != str(receipt_path) or receipt_path.parent.name != attempt["request_id"]
            or report["case_fingerprint"] != task["case_fingerprint"]
            or report["manifest_path"] != task["manifest_path"]):
        raise ValueError("recovery_receipt_identity_changed")
    role, expected_output = _attempt_contract(task, attempt, report)
    from ..generation.capacity import from_record
    from ..generation.request import MAX_REQUEST_CAPACITY

    envelope = from_record(report)
    request_path = receipt_path.with_name("request.json")
    body = read_bound(request_path, root, min(report.get("max_request_bytes", 192_000), MAX_REQUEST_CAPACITY))
    request_hash = hashlib.sha256(body).hexdigest()
    if str(request_path) != report["request_path"] or request_hash != report["request_sha256"]:
        raise ValueError("recovery_request_changed")
    # 回复中的candidate_reference指向新版本，发送前核对必须改用任务仍持有的旧版本。
    case = load_case(Path(task["manifest_path"]))
    base = load_candidate(case, task["current_candidate"])
    prepared = dict(report, candidate_reference=task["current_candidate"], candidate_revision=base.revision)
    _verify_inputs(prepared)
    _verify_payload(prepared, body)
    from ..generation.knowledge_review import enabled as review_enabled
    from ..generation.knowledge_review import verify_response as verify_review

    review_binding = verify_review(task, report, base) if review_enabled(task) else None
    review_read_only = review_binding is not None and review_binding["phase"] == "review"
    if review_read_only:
        _require_read_only_report(report, "solver")
    context = json.loads(json.loads(body)["messages"][1]["content"])["task_context"]
    if context["task_id"] != task["task_id"] or context["attempt_index"] != len(task["attempts"]):
        raise ValueError("recovery_request_task_changed")
    ledger = BudgetLedger(Path(task["budget_path"]))
    marker = receipt_path.with_name("attempt.json")
    if not marker.exists():
        if report["status"] != "request_prepared":
            raise ValueError("missing_provider_attempt_marker")
        ledger.reconcile_receipt(attempt["request_id"], request_hash, {"input_tokens": 0, "output_tokens": 0})
        attempt["status"] = "not_sent"
        task.update(status="generation_failed", stop_reason="interrupted_before_send_no_replay")
    else:
        if _decode_json(read_bound(marker, root)) != {"request_sha256": request_hash, "calls": 1}:
            raise ValueError("provider_attempt_marker_changed")
        if report["status"] in {"calling_provider", "request_prepared", "outcome_unknown"}:
            ledger.reconcile_receipt(attempt["request_id"], request_hash, {})
            attempt["status"] = "outcome_unknown"
            task.update(status="outcome_unknown", stop_reason="recovered_unknown_request_never_replayed")
        elif (report["status"] == "provider_response_rejected"
                and report.get("reason") == "response_not_complete"
                and report.get("response_completion", {}).get("known_length")):
            from ..generation.incomplete import enabled, verify_length

            if not enabled(task):
                raise ValueError("non_success_receipt_requires_reconciliation")
            usage = verify_length(report, root=root)
            if attempt.get("incomplete_response_scope") != report.get("incomplete_response_scope"):
                raise ValueError("recovery_incomplete_scope_changed")
            before_read_only = _read_only_state(task, preparation=True)
            ledger.reconcile_receipt(attempt["request_id"], request_hash, usage)
            attempt["status"] = report["status"]
            accept_response(task, report, case)
            _require_read_only_state(task, before_read_only, preparation=True)
        elif report["status"] in (
            role.recoverable_statuses
            if role is not None
            else {"pending_review", "agent_finished", "action_ready"}
        ) or (
            role is not None and role.role == "contract_auditor"
            and task.get("pending_contract_audit") is not None
            and report["status"] == "provider_response_rejected"
            and report.get("reason") in CONTRACT_AUDIT_RETRY_CODES
        ):
            response_path = receipt_path.with_name("response.sanitized.json")
            raw = read_bound(response_path, root, envelope["max_response_bytes"])
            if (report.get("response_storage") != "sanitized_provider_json"
                    or report.get("response_redacted") is not False
                    or str(response_path) != report["response_path"]
                    or hashlib.sha256(raw).hexdigest() != report["response_sha256"]):
                raise ValueError("recovery_response_changed")
            response = _decode_json(raw)
            usage = _usage(response)
            if usage.get("output_tokens") is not None and usage["output_tokens"] > envelope["max_output_tokens"]:
                raise ValueError("recovery_output_budget_exceeded")
            preparation = report.get("preparation_binding")
            preparation_read_only = preparation is not None and preparation["phase"] == "prepare"
            if review_read_only:
                before_review = _read_only_state(task, preparation=True)
            if preparation_read_only:
                _require_read_only_report(report, role.role)
                before_preparation = _read_only_state(task, preparation=True)
            output_format = expected_output
            if role is not None and role.role == "contract_auditor":
                from ..generation.contract_auditor import validate as validate_audit

                _require_read_only_report(report, role.role)
                before_read_only = _read_only_state(task)
                try:
                    proposal = _proposal(response, output_format)
                    audit = validate_audit(proposal["audit"])
                except (ContractAuditError, _ProviderBoundaryError) as error:
                    if (error.code not in CONTRACT_AUDIT_RETRY_CODES
                            or report["status"] != "provider_response_rejected"
                            or report.get("reason") != error.code):
                        raise ValueError("recovery_contract_audit_rejection_changed") from error
                else:
                    if (report.get("status") != "audit_ready" or report.get("audit") != audit
                            or report.get("summary") != proposal["summary"]):
                        raise ValueError("recovery_contract_audit_changed")
                if usage != report.get("model_usage"):
                    raise ValueError("recovery_contract_audit_changed")
                ledger.reconcile_receipt(attempt["request_id"], request_hash, usage)
                attempt["status"] = report["status"]
                if task.get("pending_contract_audit") is not None:
                    _process_contract_audit_response(task, report, case)
                else:
                    _accept_contract_audit(task, report, case)
                task["status"] = "ready"
                _require_read_only_state(task, before_read_only)
                task["budget"] = ledger.snapshot()
                _save(task, path)
                return task
            proposal = _proposal(response, output_format)
            if output_format in {
                "diagnostic_actions",
                "contract_actions",
                "protocol_v6_actions",
                "investigator_actions",
                "repository_context_actions",
            }:
                if output_format in {"diagnostic_actions", "repository_context_actions"}:
                    from ..generation.preparation import action_options
                    from ..generation.protocol_v4 import validate

                    action = validate(case, proposal["action"], base,
                        project_context_policy=task["protocol"].get("project_context_policy"),
                        **(action_options(preparation) if preparation is not None else {}))
                elif output_format == "contract_actions":
                    from ..generation.protocol_v5 import validate
                    action = validate(case, proposal["action"], base, finish_limits=report.get("finish_limits"))
                else:
                    from ..generation.protocol_v6 import validate

                    action = validate(
                        case,
                        proposal["action"],
                        base,
                        role=role.role,
                        workflow_profile=task["protocol"].get("workflow_profile", "workbench"),
                        project_context_policy=task["protocol"].get("project_context_policy"),
                        knowledge_review_binding=report.get("knowledge_review_binding"),
                        finish_limits=report.get("finish_limits"),
                    )
            else:
                action = validate_action(case, proposal["action"], candidate=base)
            if action != report["action"] or usage != report["model_usage"] or proposal["summary"] != report["summary"]:
                raise ValueError("recovery_action_or_usage_changed")
            if role is not None and action["type"] == "submit_candidate":
                role.require_candidate_write()
            if role is not None and action["type"] == "finish" and not role.terminal_write_allowed:
                raise ValueError("recovery_read_only_role_requested_terminal_state")
            if role is not None and role.role == "investigator":
                _require_read_only_report(report, role.role)
            expected_status = (
                "investigation_ready"
                if role is not None and role.role == "investigator" and action["type"] == "handoff"
                else {"finish": "agent_finished", "submit_candidate": "pending_review"}.get(
                    action["type"], "action_ready"
                )
            )
            if report["status"] != expected_status:
                raise ValueError("recovery_action_status_changed")
            if expected_status == "pending_review":
                read_bound(Path(report["candidate_reference"]["path"]), root)
                new = load_candidate(case, report["candidate_reference"])
                expected_files = {**base.files, **edited_sources(case, action["edits"], base.files)}
                if (new.parent != base.revision or new.files != expected_files or new.origin != "agent_candidate"
                        or new.sha256 != report["candidate_sha256"]
                        or new.revision != report["candidate_revision"]
                        or report["base_revision"] != base.revision
                        or read_bound(Path(report["candidate_patch"]), root) != new.patch):
                    raise ValueError("recovery_candidate_changed")
            before_read_only = (
                _read_only_state(task)
                if role is not None and role.role == "investigator"
                else None
            )
            ledger.reconcile_receipt(attempt["request_id"], request_hash, usage)
            attempt["status"] = report["status"]
            accept_response(task, report, case)
            if review_read_only:
                _require_read_only_state(task, before_review, preparation=True)
            if preparation_read_only:
                _require_read_only_state(task, before_preparation, preparation=True)
            if before_read_only is not None:
                _require_read_only_state(task, before_read_only)
                if task["status"] not in {
                    "ready", "pending_dependency_query", "pending_diagnostic_review"
                }:
                    raise ValueError("recovery_investigator_handoff_changed_task_status")
        else:
            # 不能只相信状态文本来接纳拒绝/格式异常的回复；保留原记录供核对。
            raise ValueError("non_success_receipt_requires_reconciliation")
    task["budget"] = ledger.snapshot()
    _save(task, path)
    return task
