"""用真实请求收据和合成供应商回复验证审阅纠错；不调用外部模型或目标环境。"""

import json
from pathlib import Path

import pytest
from test_contract_coverage import (
    _copy_as_v2_case,
    _create_protocol_5_task,
    _ledger,
    _provider_response,
    _review_candidate,
    _submit_candidate,
)

from upgrade_workbench import tasks
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.service.recovery import recover_task


def ready_task(tmp_path, *, protocol=6, owner=None, max_calls=8):
    manifest = _copy_as_v2_case(tmp_path)
    ledger = tmp_path / "ledger.sqlite"
    _ledger(ledger)
    task = tasks.create_operation(
        manifest, tmp_path / "work", budget_path=ledger, seed_strategy="none",
        protocol_revision=protocol, max_calls=max_calls, contract_audit_policy="bounded",
        generation={"model": "owned-model", "endpoint": "https://provider.example/chat/completions",
                    "thinking_mode": "disabled", "max_output_tokens": 1024, "timeout_seconds": 10},
    )
    task = _review_candidate(_submit_candidate(task, "audit-fixture"), passed=True)
    if owner is not None:
        task["service_owner"] = owner
        tasks._save(task, Path(task["task_path"]))
    return task


def finish(task):
    reference = "observation:" + task["latest_observation"]
    return {"summary": "Reviewed candidate is ready.", "action": {
        "type": "finish", "reason": "candidate_ready", "explanation": "Public development check passes.",
        "evidence_refs": [reference], "contract_coverage": [{
            "requirement_id": "release.registered-behavior", "state": "supported",
            "evidence_refs": [reference], "tool_limitation": None,
        }],
    }}


def audit(*, bad="reference", reference="business_contract"):
    if bad == "schema":
        return {"summary": "The required top-level audit object is missing."}
    return {"summary": "Bounded audit question.", "audit": {
        "verdict": "specific_questions", "questions": [{
            "requirement_id": "release.unknown" if bad == "requirement" else "release.registered-behavior",
            "evidence_refs": ["observation:" + "1" * 64 if bad == "reference" else
                              "evidence:invalid" if bad == "syntax" else reference],
            "specific_conflict": "The current source requires one bounded observation.",
            "suggested_observation": "Inspect the registered public behavior.",
        }],
    }}


NO_CONFLICT = {"summary": "No specific conflict.", "audit": {"verdict": "no_specific_conflict", "questions": []}}


@pytest.mark.parametrize("protocol", [5, 6])
@pytest.mark.parametrize("bad", ["reference", "requirement", "schema"])
def test_known_binding_rejection_can_correct_and_return_to_solver(tmp_path, protocol, bad):
    task = ready_task(tmp_path, protocol=protocol)
    roles = []
    rejection_code = "invalid_proposal_schema" if bad == "schema" else "contract_audit_invalid_" + bad

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        context = json.loads(payload["messages"][1]["content"])
        if "read-only Contract Auditor" in payload["messages"][0]["content"]:
            roles.append("auditor")
            count = roles.count("auditor")
            if count == 1:
                output = audit(bad=bad)
            elif count == 2:
                assert context["task_context"]["protocol_feedback"] == {
                    "code": rejection_code, "attempt_index": 3,
                }
                output = audit(bad=None)
            else:
                output = NO_CONFLICT
        else:
            roles.append("solver")
            if roles.count("solver") == 2:
                assert context["diagnostic_state"]["contract_audits"][0]["audit"]["questions"]
            output = finish(task)
        return _provider_response(output)

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert roles == ["solver", "auditor", "auditor", "solver", "auditor"]
    assert result["status"] == "submitted", result.get("stop_reason")
    assert result["attempts"][2]["status"] == ("provider_response_rejected" if bad == "schema" else "audit_ready")
    assert result["attempts"][2]["audit_rejection_code"] == rejection_code
    assert "pending_contract_audit" not in result


@pytest.mark.parametrize("bad", ["reference", "schema"])
def test_two_known_binding_rejections_exhaust_only_existing_audit_retry_budget(tmp_path, bad):
    task = ready_task(tmp_path)
    roles = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        is_auditor = "read-only Contract Auditor" in payload["messages"][0]["content"]
        roles.append("auditor" if is_auditor else "solver")
        return _provider_response(audit(bad=bad) if is_auditor else finish(task))

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert roles == ["solver", "auditor", "auditor"]
    assert result["status"] == "unresolved"
    assert result["stop_reason"] == "required_contract_audit_not_accepted"
    assert result["contract_audit_failure"]["reason"] == (
        "invalid_proposal_schema" if bad == "schema" else "contract_audit_invalid_reference"
    )
    assert result["contract_audits"] == []
    assert result["budget"]["calls"] == 4


def test_changed_version_receipt_is_execution_failure_not_retryable_model_error(tmp_path, monkeypatch):
    task = ready_task(tmp_path)
    complete = tasks.complete_request
    roles = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        context = json.loads(payload["messages"][1]["content"])
        is_auditor = "read-only Contract Auditor" in payload["messages"][0]["content"]
        roles.append("auditor" if is_auditor else "solver")
        output = audit(bad=None, reference="version:" + context["version_evidence"][0]["evidence_key"]) if is_auditor else finish(task)
        return _provider_response(output)

    def corrupt_after_completed_response(prepared, **kwargs):
        response = complete(prepared, **kwargs)
        if prepared["output_format"] == "contract_audit":
            request_path = Path(prepared["request_path"])
            request_path.write_bytes(request_path.read_bytes() + b" ")
        return response

    monkeypatch.setattr(tasks, "complete_request", corrupt_after_completed_response)
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert roles == ["solver", "auditor"]
    assert result["status"] == "execution_incomplete"
    assert "RequestEvidenceError" in result["stop_reason"]
    assert result["attempts"][-1]["status"] == "audit_ready"
    assert "audit_rejection_code" not in result["attempts"][-1]
    assert result["pending_contract_audit"]["rejected_codes"] == []
    assert all(group["state"] == "settled" for group in result["budget"]["groups"])


@pytest.mark.parametrize("interrupt_after_rejection", [False, True])
@pytest.mark.parametrize("bad", ["reference", "syntax", "schema"])
def test_completed_bad_reference_recovers_without_resending_response_or_resetting_retry_budget(
    tmp_path, monkeypatch, interrupt_after_rejection, bad,
):
    class Crash(BaseException):
        pass

    owner = "a" * 32
    task = ready_task(tmp_path, owner=owner)
    process = tasks._process_contract_audit_response
    roles = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        is_auditor = "read-only Contract Auditor" in payload["messages"][0]["content"]
        roles.append("auditor" if is_auditor else "solver")
        output = (audit(bad=bad) if roles.count("auditor") == 1 else NO_CONFLICT) if is_auditor else finish(task)
        return _provider_response(output)

    def crash(task, response, case):
        if interrupt_after_rejection:
            result = process(task, response, case)
            tasks._save(task, Path(task["task_path"]))
            assert result["status"] == "retry_pending"
        raise Crash()

    monkeypatch.setattr(tasks, "_process_contract_audit_response", crash)
    with pytest.raises(Crash):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=owner)
    interrupted = tasks.inspect_task(Path(task["task_path"]))
    assert interrupted["status"] == ("ready" if interrupt_after_rejection else "processing_response")
    assert interrupted["budget"]["calls"] == 3
    recovered = recover_task(Path(task["task_path"]), Path(task["work_root"]), owner)
    assert roles == ["solver", "auditor"]
    assert recovered["status"] == "ready"
    assert recovered["budget"]["calls"] == 3
    assert recovered["pending_contract_audit"]["rejected_codes"] == [
        "invalid_proposal_schema" if bad == "schema" else "contract_audit_invalid_reference"
    ]
    assert recovered["contract_audits"] == []
    monkeypatch.setattr(tasks, "_process_contract_audit_response", process)
    completed = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=owner)
    assert roles == ["solver", "auditor", "auditor"]
    assert completed["status"] == "submitted", completed.get("stop_reason")
    assert completed["budget"]["calls"] == 4
    assert "pending_contract_audit" not in completed


@pytest.mark.parametrize("remaining_calls", [2, 3])
def test_stagnation_audit_rejections_preserve_one_solver_call_within_total_budget(tmp_path, remaining_calls):
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path, max_calls=3 + remaining_calls)
    for index in range(3):
        task = _review_candidate(_submit_candidate(task, f"failed-revision-{index}"), passed=False)
    case = load_case(manifest)
    snapshot = load_candidate(case, task["current_candidate"])
    name = case.manifest.allowed_changes[0]
    old = snapshot.files[name].decode()
    roles = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        context = json.loads(payload["messages"][1]["content"])
        if "read-only Contract Auditor" in payload["messages"][0]["content"]:
            roles.append("auditor")
            assert context["task_context"]["remaining_calls"] >= 2
            return _provider_response(audit())
        roles.append("solver")
        assert context["task_context"]["remaining_calls"] == 1
        assert context["diagnostic_state"]["contract_audits"] == []
        return _provider_response({"summary": "Candidate after unsuccessful bounded audit", "action": {
            "type": "submit_candidate", "base_revision": snapshot.revision,
            "edits": [{"path": name, "old": old, "new": "# next-candidate\n" + old}],
        }})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert roles == ["auditor"] * (remaining_calls - 1) + ["solver"]
    assert result["status"] == "pending_review"
    assert len(result["attempts"]) == result["budget"]["calls"] == task["protocol"]["max_calls"]
    assert result["contract_audits"] == []
    assert all(item["audit_rejection_code"] == "contract_audit_invalid_reference"
               for item in result["attempts"][3:-1])


@pytest.mark.parametrize("protocol", [5, 6])
def test_corrected_solver_finish_retires_feedback_before_audit_questions(tmp_path, monkeypatch, protocol):
    """复现 JWT 的第12至16次调用；此前拒绝只能交给下一次Solver消费一次。"""
    task = ready_task(tmp_path, protocol=protocol, max_calls=18)
    for index in range(10):
        task = _review_candidate(_submit_candidate(task, f"prior-candidate-{index}"), passed=True)
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, task["current_candidate"])
    name = case.manifest.allowed_changes[0]
    old = snapshot.files[name].decode()
    roles = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        context = json.loads(payload["messages"][1]["content"])
        attempt = context["task_context"]["attempt_index"]
        role = "auditor" if "read-only Contract Auditor" in payload["messages"][0]["content"] else "solver"
        roles.append((attempt, role))
        if attempt == 12:
            output = finish(task)
            output["action"]["issues"] = []
        elif attempt == 13:
            assert context["task_context"]["protocol_feedback"] == {
                "code": "action_invalid_fields", "attempt_index": 12,
            }
            output = finish(task)
        elif attempt == 14:
            assert "protocol_feedback" not in context["task_context"]
            output = audit(bad=None)
            output["audit"]["questions"][0]["specific_conflict"] = "x" * 2000
        elif attempt == 15:
            assert context["task_context"]["protocol_feedback"] == {
                "code": "contract_audit_text_too_long", "attempt_index": 14,
            }
            output = audit(bad=None)
        else:
            assert attempt == 16
            assert "protocol_feedback" not in context["task_context"]
            assert context["diagnostic_state"]["contract_audits"][0]["audit"]["questions"]
            assert "Contract Auditor" in context["task_context"]["edit_feedback"]
            output = {"summary": "Solver received the new advisory question.", "action": {
                "type": "submit_candidate", "base_revision": snapshot.revision,
                "edits": [{"path": name, "old": old, "new": "# audit-response\n" + old}],
            }}
        return _provider_response(output)

    if protocol == 6:
        class Crash(BaseException):
            pass

        continuation = tasks._continue_after_contract_audit

        def crash_after_completed_audit(*_args, **_kwargs):
            raise Crash()

        monkeypatch.setattr(tasks, "_continue_after_contract_audit", crash_after_completed_audit)
        with pytest.raises(Crash):
            tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
        interrupted = tasks.inspect_task(Path(task["task_path"]))
        assert interrupted["protocol_feedback"] is None
        assert interrupted["pending_contract_audit"]["result"]["status"] == "accepted"
        assert len(interrupted["attempts"]) == 15
        monkeypatch.setattr(tasks, "_continue_after_contract_audit", continuation)
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert roles == [(12, "solver"), (13, "solver"), (14, "auditor"), (15, "auditor"), (16, "solver")]
    assert result["status"] == "pending_review", result.get("stop_reason")
    assert result["protocol_feedback"] is None
    assert result["attempts"][13]["audit_rejection_code"] == "contract_audit_text_too_long"
    assert result["budget"]["calls"] == 16
    assert len(result["contract_audits"]) == 1


def test_rejected_stagnation_audit_does_not_preempt_pending_solver_correction(tmp_path):
    """审阅拒绝未生成审阅产物，仍须先让Solver收到自身的格式反馈。"""
    manifest = _copy_as_v2_case(tmp_path)
    task = _create_protocol_5_task(manifest, tmp_path, max_calls=10)
    for index in range(3):
        task = _review_candidate(_submit_candidate(task, f"failed-revision-{index}"), passed=False)
    case = load_case(manifest)
    snapshot = load_candidate(case, task["current_candidate"])
    name = case.manifest.allowed_changes[0]
    old = snapshot.files[name].decode()
    roles = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        context = json.loads(payload["messages"][1]["content"])
        if "read-only Contract Auditor" in payload["messages"][0]["content"]:
            roles.append("auditor")
            return _provider_response(audit())
        roles.append("solver")
        if roles.count("solver") == 1:
            output = finish(task)
            output["action"]["issues"] = []
        else:
            assert context["task_context"]["attempt_index"] == 7
            assert context["task_context"]["protocol_feedback"] == {
                "code": "action_invalid_fields", "attempt_index": 6,
            }
            output = {"summary": "Correct the rejected Solver response.", "action": {
                "type": "submit_candidate", "base_revision": snapshot.revision,
                "edits": [{"path": name, "old": old, "new": "# corrected-response\n" + old}],
            }}
        return _provider_response(output)

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert roles == ["auditor", "auditor", "solver", "solver"]
    assert result["status"] == "pending_review", result.get("stop_reason")
    assert result["protocol_feedback"] is None
    assert len(result["attempts"]) == result["budget"]["calls"] == 7


def test_top_level_audit_rejection_recovery_rechecks_reason_against_raw_response(tmp_path, monkeypatch):
    class Crash(BaseException):
        pass

    owner = "a" * 32
    task = ready_task(tmp_path, owner=owner)
    calls = []

    def transport(request, **_kwargs):
        payload = json.loads(request.data)
        auditor = "read-only Contract Auditor" in payload["messages"][0]["content"]
        calls.append("auditor" if auditor else "solver")
        return _provider_response(audit(bad="schema") if auditor else finish(task))

    def crash(*_args):
        raise Crash()

    monkeypatch.setattr(tasks, "_process_contract_audit_response", crash)
    with pytest.raises(Crash):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=owner)
    interrupted = tasks.inspect_task(Path(task["task_path"]))
    receipt = Path(interrupted["attempts"][-1]["receipt"])
    report = json.loads(receipt.read_bytes())
    assert report["reason"] == "invalid_proposal_schema"
    report["reason"] = "contract_audit_invalid_schema"
    receipt.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="recovery_contract_audit_rejection_changed"):
        recover_task(Path(task["task_path"]), Path(task["work_root"]), owner)
    assert calls == ["solver", "auditor"]
    after = tasks.inspect_task(Path(task["task_path"]))
    assert after["pending_contract_audit"]["rejected_codes"] == []
    assert after["contract_audits"] == []
