"""诊断协议的任务边界；假运行结果只证明编排，绝不计为应用迁移成绩。"""

import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import (
    accept_action,
    context_from_reference,
    finish_coverage_decision,
    freeze_context,
    read,
    recover_diagnostic,
    resolve_refs,
    summarize,
)
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.planning import prepare_case_proposal
from upgrade_workbench.reporting import export_task_report
from upgrade_workbench.tasks import advance_task, create_operation, finalize_task, inspect_task

CASE = Path(__file__).resolve().parents[2] / "cases/bump-my-version-0.5.0-r2/manifest.json"


@pytest.fixture
def task(tmp_path):
    budget = tmp_path / "ledger.sqlite"
    BudgetLedger(budget, {"mode": "user_managed", "limit_usd": None, "model": "owned-model",
                         "input_per_million": "1", "output_per_million": "1",
                         "pricing_source": "https://example.test/pricing", "pricing_checked_at": "2026-09-17"})
    return create_operation(CASE, tmp_path / "work", budget_path=budget, seed_strategy="none", protocol_revision=4,
        generation={"model": "owned-model", "endpoint": "https://provider.example/chat/completions",
                    "thinking_mode": "disabled", "max_output_tokens": 1024, "timeout_seconds": 10,
                    "max_request_bytes": 384000}, max_calls=8)


def response(action):
    return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5},
                       "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"summary": "offline action", "action": action})}}]}).encode()


def run_action(task, action):
    return advance_task(Path(task["task_path"]), api_key="unused", transport=lambda *_a, **_k: response(action))


def public_run(manifest, directory, **kwargs):
    directory.mkdir(parents=True, exist_ok=True)
    stage = {"status": "passed", "exit_code": 0, "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0}, "nodeids": ["test_public"]}
    report = {"case_fingerprint": load_case(manifest).fingerprint, "check_group": kwargs["check_group"],
              "status": "no_regression_observed", "stages": {"old_original": stage, "new_original": stage,
                                                            "new_candidate": {"status": "not_supplied"}},
              "report_path": str(directory / "result.json")}
    if kwargs.get("candidate_patch"):
        report.update(status="candidate_verified", candidate={"supplied_sha256": hashlib.sha256(kwargs["candidate_patch"].read_bytes()).hexdigest()})
        report["stages"]["new_candidate"] = stage
    Path(report["report_path"]).write_text(json.dumps(report))
    return report


def request_public(task):
    revision = load_candidate(load_case(CASE), task["current_candidate"]).revision
    return run_action(task, {"type": "run_public_checks", "revision": revision})


def review(task, **options):
    comparator = options.pop("comparator", public_run)
    return advance_task(Path(task["task_path"]), diagnostic_request_id=task["pending_diagnostic"]["request"]["id"],
        reviewer="test", review_note="offline execution contract", review_only=True, comparator=comparator, **options)


def coverage(requirement="Declared local business contract", *, scope="in_contract", status="verified",
             evidence=None, tool_limitation=None):
    return [{"requirement": requirement, "scope": scope, "status": status,
             "evidence_refs": ["business_contract"] if evidence is None else evidence,
             "tool_limitation": tool_limitation}]


def finish(task, reason="no_change_claimed"):
    return run_action(task, {"type": "finish", "reason": reason, "explanation": "bounded observation",
                             "evidence_refs": ["business_contract"], "contract_coverage": coverage()})


def test_misplaced_issues_rejected_with_actionable_feedback_then_corrected(task):
    revision = load_candidate(load_case(CASE)).revision
    issue = {"hypothesis": "public behavior may differ", "evidence_refs": ["business_contract"],
             "unknown": "execution result", "next_observation": "public checks"}
    action = {"type": "run_public_checks", "revision": revision}
    seen = []

    def transport(request, **kwargs):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        seen.append(context)
        if len(seen) == 1:
            body = json.loads(response(action))
            content = json.loads(body["choices"][0]["message"]["content"])
            content["issues"] = [issue]
            body["choices"][0]["message"]["content"] = json.dumps(content)
            return json.dumps(body).encode()
        assert context["task_context"]["protocol_feedback"]["code"] == "diagnostic_issues_outside_action"
        return response(action | {"issues": [issue]})

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert len(seen) == 2 and len(result["attempts"]) == 2
    assert result["status"] == "pending_diagnostic_review"
    assert result["diagnostic_runs"] == [] and result["candidate"] is None
    assert result["issues"] == [issue]


def test_finish_and_export_keep_claims_separate_from_execution(task, tmp_path):
    task = review(request_public(task))
    reference = "observation:" + task["observations"][-1]["id"]
    result = run_action(task, {
        "type": "finish", "reason": "no_change_claimed", "explanation": "Static inference plus local execution",
        "evidence_refs": [reference], "contract_coverage": [
            *coverage("Unexecuted requirement inferred from contract"),
            *coverage("Local public path", evidence=[reference]),
        ],
    })
    assert result["status"] == "no_change_claimed"  # 没有新增会造成无尽补测的门禁。
    assert len(result["attempts"]) == 2 and len(result["diagnostic_runs"]) == 1
    evidence = result["finish"]["diagnostic_scope"]["coverage_evidence"]
    assert [item["evidence_basis"] for item in evidence["items"]] == [
        "static_evidence_only", "current_execution_cited"]
    exported = export_task_report(Path(result["task_path"]), tmp_path / "export")
    report = json.loads(Path(exported["json_path"]).read_text(encoding="utf-8"))
    assert report["finish"]["diagnostic_scope"]["coverage_evidence"] == evidence
    assert report["result"] == "not_evaluated"  # 这次宿主回归没有做真实独立验收。
    markdown = Path(exported["markdown_path"]).read_text(encoding="utf-8")
    assert "仅静态依据 1 项" in markdown and "引用当前已完成执行 1 项" in markdown
    assert "源码未改不证明行为未变" in markdown


def test_call11_to_13_feedback_replay_reaches_legal_same_base_edit(task):
    revision = load_candidate(load_case(CASE), task["current_candidate"]).revision
    issue = {
        "hypothesis": "The current compatibility edit may be incomplete.",
        "evidence_refs": ["business_contract"],
        "unknown": "Whether the candidate preserves the registered behavior.",
        "next_observation": "Review the corrected candidate with public checks.",
    }
    valid_action = {
        "type": "submit_candidate",
        "base_revision": revision,
        "edits": [{
            "path": "bumpversion/config.py",
            "old": "import itertools",
            "new": "import itertools\nimport json",
        }],
        "issues": [issue],
    }
    chained_action = {
        **valid_action,
        "edits": [
            valid_action["edits"][0],
            {
                "path": "bumpversion/config.py",
                "old": "import itertools\nimport json",
                "new": "import itertools\nimport json\nimport os",
            },
        ],
    }
    seen = []

    def encoded(proposal):
        return json.dumps({
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(proposal)},
            }],
        }).encode()

    def transport(request, **_kwargs):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        seen.append(context)
        if len(seen) == 1:
            return encoded({
                "type": "submit_candidate",
                "summary": "Misplaced root fields",
                "action": valid_action,
                "issues": [issue],
            })
        if len(seen) == 2:
            feedback = context["task_context"]["edit_feedback"]
            assert context["task_context"]["protocol_feedback"]["code"] == "diagnostic_issues_outside_action"
            assert "Top-level keys must be exactly summary and action" in feedback
            assert "Move optional issues inside action" in feedback
            return encoded({"summary": "Chained exact edits", "action": chained_action})
        if len(seen) == 3:
            feedback = context["task_context"]["edit_feedback"]
            assert context["task_context"]["protocol_feedback"]["code"] == "edit_old_text_not_found"
            assert "Exact edit #2" in feedback and "same base" in feedback
            assert "Probe oracle" not in feedback
            return encoded({
                "summary": "Unexpected candidate field",
                "action": valid_action | {"issues_note": "keep investigating"},
            })
        feedback = context["task_context"]["edit_feedback"]
        assert context["task_context"]["protocol_feedback"]["code"] == "action_invalid_fields"
        assert "issues_note" in feedback
        assert "Probe oracle" not in feedback
        return encoded({"summary": "Corrected same-base edit", "action": valid_action})

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)

    assert len(seen) == 4 and len(result["attempts"]) == 4
    assert result["status"] == "pending_review"
    assert result["candidate"]["revision"] != revision
    assert result["diagnostic_runs"] == []


def test_original_public_checks_then_false_no_change_is_rejected_by_final(task, tmp_path):
    task = request_public(task)
    assert task["status"] == "pending_diagnostic_review" and task["candidate"] is None
    task = review(task)
    assert task["status"] == "ready" and len(task["observations"]) == 1
    task = finish(task)
    assert task["status"] == "no_change_claimed" and task["final_evaluation"] == "not_run"

    def reject(manifest, directory, **kwargs):
        report = public_run(manifest, directory, **kwargs)
        report["stages"]["new_original"] = {"status": "failed", "nodeids": ["test_public", "test_final"],
                                               "tests": {"collected": 2, "passed": 1, "failed": 1, "errors": 0, "skipped": 0}}
        report["status"] = "migration_required"
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    final = finalize_task(Path(task["task_path"]), comparator=reject)
    assert final["final_result"]["status"] == "no_change_claim_not_accepted"
    assert final["candidate_history"] == [] and len(final["observations"]) == 1
    result = export_task_report(Path(task["task_path"]), tmp_path / "export")
    assert result["result"] == "no_change_claim_not_accepted"


def test_completed_diagnostic_recovery_does_not_execute_again(task):
    task = request_public(task)
    before = json.loads(Path(task["task_path"]).read_bytes())
    done = review(task)
    # 重现收据已完成而任务游标仍在running的断点。
    run = dict(done["diagnostic_runs"][0])
    run.pop("result")
    run.pop("observation")
    run["status"] = "running"
    before.update(status="diagnosing", diagnostic_runs=[run], diagnostic_reviews=done["diagnostic_reviews"])
    recovered = recover_diagnostic(before)
    assert recovered["status"] == "ready"
    assert recovered["observations"] == done["observations"]
    assert len(recovered["attempts"]) == 1
    assert recovered["diagnostic_reviews"][-1]["status"] == "completed"


def test_missing_diagnostic_receipt_recovers_unknown_without_stale_pending_review(task):
    task = request_public(task)
    before = json.loads(Path(task["task_path"]).read_bytes())
    done = review(task)
    run = dict(done["diagnostic_runs"][0])
    run.pop("result")
    run.pop("observation")
    run.update(status="running", phase="target_started", outcome="unknown")
    for path in (Path(run["directory"]) / "completed").glob("*.json"):
        path.unlink()
    before.update(status="diagnosing", diagnostic_runs=[run], diagnostic_reviews=done["diagnostic_reviews"])

    recovered = recover_diagnostic(before)

    assert recovered["status"] == "execution_incomplete"
    assert recovered["pending_diagnostic"] is None
    assert recovered["diagnostic_runs"][-1]["status"] == "unknown"
    assert recovered["diagnostic_runs"][-1]["last_phase"] == "target_started"
    assert recovered["diagnostic_reviews"][-1]["status"] == "unknown"


def test_diagnostic_exception_closes_run_with_bounded_failure_receipt(task):
    task = request_public(task)

    def explode(*_args, **_kwargs):
        # comparator边界的普通ValueError也可能来自Docker/PG运行完整性，不能默认算输入合同错误。
        raise ValueError("C:\\private\\secret\\worker.sock")

    result = review(task, comparator=explode)
    run = result["diagnostic_runs"][-1]
    assert result["status"] == "execution_incomplete"
    assert result["pending_diagnostic"] is None
    assert run["status"] == "failed" and run["phase"] == "failed"
    assert run["failure"]["category"] == "infrastructure"
    assert run["failure_receipt"]["id"]
    assert "private" not in json.dumps(result, ensure_ascii=False)

    recovered = recover_diagnostic(json.loads(json.dumps(result)))
    assert recovered["diagnostic_runs"][-1]["status"] == "failed"
    assert recovered["status"] == "execution_incomplete"
    assert recovered["diagnostic_reviews"][-1]["status"] == "failed"


def test_execution_incomplete_report_is_not_marked_completed(task):
    task = request_public(task)

    def incomplete(manifest, directory, **kwargs):
        report = public_run(manifest, directory, **kwargs)
        report["status"] = "execution_error"
        Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        return report

    result = review(task, comparator=incomplete)
    run = result["diagnostic_runs"][-1]
    assert result["status"] == "execution_incomplete"
    assert run["status"] == "failed"
    assert run["outcome"] == "execution_incomplete"
    assert run["phase"] == "failed"


def test_completed_public_failure_is_semantic_outcome_not_lifecycle_failure(task):
    task = request_public(task)

    def failed(manifest, directory, **kwargs):
        report = public_run(manifest, directory, **kwargs)
        report["status"] = "migration_required"
        report["stages"]["new_original"] = {
            "status": "failed", "exit_code": 1,
            "tests": {"collected": 1, "passed": 0, "failed": 1, "errors": 0, "skipped": 0},
            "nodeids": ["test_public"],
        }
        Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        return report

    result = review(task, comparator=failed)
    run = result["diagnostic_runs"][-1]
    assert result["status"] == "ready"
    assert run["status"] == "completed" and run["phase"] == "completed"
    assert run["outcome"] == "semantic_failed"


@pytest.mark.parametrize("damaged", ["report", "receipt"])
def test_recovery_integrity_failure_closes_persisted_run_without_replay(task, monkeypatch, damaged):
    from upgrade_workbench import tasks
    from upgrade_workbench.service.recovery import recover_task

    task = request_public(task)
    checkpoints = []
    save = tasks._save

    def capture(value, path):
        if value["status"] == "diagnosing":
            checkpoints.append(json.loads(json.dumps(value)))
        save(value, path)

    monkeypatch.setattr(tasks, "_save", capture)
    done = review(task)
    run = done["diagnostic_runs"][-1]
    target = Path(run["report" if damaged == "report" else "result"]["path"])
    target.write_bytes(target.read_bytes() + b" ")
    path = Path(task["task_path"])
    path.write_text(json.dumps(checkpoints[-1]), encoding="utf-8")

    recovered = recover_task(path, Path(task["work_root"]), None)
    assert recovered["status"] == "execution_incomplete"
    assert recovered["pending_diagnostic"] is None
    assert recovered["diagnostic_runs"][-1]["status"] == "failed"
    assert recovered["diagnostic_runs"][-1]["failure"]["category"] == "contract"
    assert recovered["diagnostic_runs"][-1]["failure_receipt"]["id"]
    assert recovered["diagnostic_reviews"][-1]["status"] == "failed"
    assert recovered["observations"] == []
    assert inspect_task(path) == recovered
    assert recover_task(path, Path(task["work_root"]), None) == recovered


def test_probe_materialization_failure_preserves_phase_and_receipt(task, monkeypatch):
    from upgrade_workbench import diagnostics
    from upgrade_workbench.cases import PatchInfrastructureError

    task = run_action(task, probe_action(task))

    def unavailable(*_args, **_kwargs):
        raise PatchInfrastructureError("controlled materialization failure")

    monkeypatch.setattr(diagnostics, "stage_source", unavailable)
    result = review(task)
    run = result["diagnostic_runs"][-1]
    report = json.loads(Path(run["report"]["path"]).read_bytes())
    assert report["stages"] == {}
    assert run["status"] == "failed"
    assert run["last_phase"] == "started"
    assert run["last_action"] == "materialize"
    assert run["failure"]["error_type"] == "PatchInfrastructureError"
    assert run["failure_receipt"]["id"]
    assert recover_diagnostic(json.loads(json.dumps(result)))["diagnostic_runs"] == result["diagnostic_runs"]


def test_recovery_revalidates_evidence_even_with_saved_observation_pointer(task):
    task = review(request_public(task))
    report = Path(task["diagnostic_runs"][-1]["report"]["path"])
    report.write_bytes(report.read_bytes() + b" ")
    task["status"] = "diagnosing"

    recovered = recover_diagnostic(task)

    assert recovered["status"] == "execution_incomplete"
    assert recovered["diagnostic_runs"][-1]["reconciliation_required"]
    assert recovered["diagnostic_reviews"][-1]["status"] == "failed"
    assert inspect_task(Path(task["task_path"])) == recovered


def test_recovery_honors_failure_receipt_even_before_failed_task_was_saved(task, monkeypatch):
    from upgrade_workbench import diagnostics, tasks
    from upgrade_workbench.service.recovery import recover_task

    task = request_public(task)
    checkpoints = []
    save = tasks._save

    def capture(value, path):
        if value["status"] == "diagnosing":
            checkpoints.append(json.loads(json.dumps(value)))
        save(value, path)

    monkeypatch.setattr(tasks, "_save", capture)
    done = review(task)
    path = Path(task["task_path"])
    damaged = Path(done["diagnostic_runs"][-1]["report"]["path"])
    original = damaged.read_bytes()
    damaged.write_bytes(original + b" ")
    path.write_text(json.dumps(checkpoints[-1]), encoding="utf-8")
    failed = recover_task(path, Path(task["work_root"]), None)
    failure_id = failed["diagnostic_runs"][-1]["failure_receipt"]["id"]
    # 即使文件被还原，已落盘的完整性故障也不能被旧completed收据覆盖。
    damaged.write_bytes(original)
    path.write_text(json.dumps(checkpoints[-1]), encoding="utf-8")
    recovered = recover_task(path, Path(task["work_root"]), None)
    assert recovered["status"] == "execution_incomplete"
    assert recovered["diagnostic_runs"][-1]["reconciliation_required"]
    assert recovered["diagnostic_runs"][-1]["failure_receipt"]["id"] == failure_id
    assert recovered["observations"] == []
    with pytest.raises(ValueError, match="reconciliation"):
        diagnostics.prepare_retry(recovered, recovered["diagnostic_runs"][-1]["request"]["id"])


def test_service_and_export_expose_diagnostic_failure_without_paths(task, tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from upgrade_workbench.service.api import create_app
    from upgrade_workbench.service.engine import Engine

    task = request_public(task)

    def explode(*_args, **_kwargs):
        raise OSError("C:/private/secret/worker.sock")

    result = review(task, comparator=explode)
    job = {"id": "a" * 32, "paused": False, "blocked": None, "created_at": "offline",
           "task_path": result["task_path"], "request": {"case_id": result["case_id"], "profile": "offline"}}
    engine = object.__new__(Engine)
    engine.store = SimpleNamespace(job=lambda _id: job)
    engine.task = lambda _id: result
    settings = SimpleNamespace(token="t" * 32)
    with TestClient(create_app(settings, engine=engine)) as client:
        headers = {"Authorization": "Bearer " + settings.token}
        state = client.get(f"/jobs/{job['id']}", headers=headers)
        detail = client.get(f"/jobs/{job['id']}/diagnostic", headers=headers)
    assert state.status_code == detail.status_code == 200
    diagnostics = state.json()["diagnostics"]
    assert diagnostics["counts"]["registered"] == 1
    assert diagnostics["counts"]["completed"] == 0
    assert diagnostics["counts"]["failed"] == 1
    assert diagnostics["latest"]["failure"]["category"] == "infrastructure"
    assert diagnostics["latest"]["failure_receipt_id"]
    assert detail.json()["diagnostics"] == diagnostics
    exported = export_task_report(Path(result["task_path"]), tmp_path / "failure-export")
    record = json.loads(Path(exported["json_path"]).read_bytes())
    assert record["diagnostics"] == diagnostics
    assert "诊断登记1次、完成0次、失败1次" in Path(exported["markdown_path"]).read_text(encoding="utf-8")
    assert "private" not in json.dumps(diagnostics)
    assert str(tmp_path) not in json.dumps(diagnostics)


def test_stale_review_rejected_and_missing_receipt_not_replayed(task):
    task = request_public(task)
    with pytest.raises(ValueError, match="exact pending"):
        advance_task(Path(task["task_path"]), diagnostic_request_id="0" * 64,
                     reviewer="test", review_note="stale", review_only=True, comparator=lambda *_a, **_k: pytest.fail("must not execute"))
    unchanged = inspect_task(Path(task["task_path"]))
    assert unchanged["status"] == "pending_diagnostic_review" and unchanged["diagnostic_runs"] == []


def test_prepared_selection_rebuilt_before_send(task):
    prepared = prepare_case_proposal(CASE, Path(task["work_root"]), public_source_ack=True,
        output_format="diagnostic_actions", source_policy=task["protocol"]["source_policy"],
        diagnostic_context_reference=freeze_context(task), **task["protocol"]["generation"])
    body = Path(prepared["request_path"]).read_bytes()
    _verify_payload(prepared, body)
    payload = json.loads(body)
    context = json.loads(payload["messages"][1]["content"])
    assert context["source_selection"]["kind"] == "selected" and context["source_state"]["state"] == "original"
    context["source_files"][0]["text"] += "# tampered"
    payload["messages"][1]["content"] = json.dumps(context)
    with pytest.raises(ValueError, match="selection"):
        _verify_payload(prepared, json.dumps(payload).encode())


def test_priority_failure_evidence_reaches_generated_diagnostic_request(task):
    constraint = "NOT NULL constraint failed: topics.username"
    autoflush = "Query-invoked autoflush; consider using a session.no_autoflush block"

    def failing_public(manifest, directory, **kwargs):
        report = public_run(manifest, directory, **kwargs)
        node = "test_public"
        message = "head\n" * 400 + f"E sqlalchemy.exc.IntegrityError: {autoflush}\n" \
            + f"E sqlite3.IntegrityError: {constraint}\n" + "tail\n" * 400
        failed = {
            "status": "failed",
            "tests": {"collected": 1, "passed": 0, "failed": 1, "errors": 0, "skipped": 0},
            "nodeids": [node],
            "failure_details": [{"nodeid": node, "when": "call", "outcome": "failed",
                                 "message": message}],
        }
        report.update(status="migration_required", stages={
            "old_original": report["stages"]["old_original"],
            "new_original": failed,
            "new_candidate": {"status": "not_supplied"},
        })
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    task = review(request_public(task), comparator=failing_public)
    prepared = prepare_case_proposal(CASE, Path(task["work_root"]), public_source_ack=True,
        output_format="diagnostic_actions", source_policy=task["protocol"]["source_policy"],
        diagnostic_context_reference=freeze_context(task), **task["protocol"]["generation"])
    body = Path(prepared["request_path"]).read_bytes()
    _verify_payload(prepared, body)
    public_context = json.loads(json.loads(body)["messages"][1]["content"])
    serialized = json.dumps(public_context["diagnostic_state"], ensure_ascii=False)
    assert constraint in serialized and "Query-invoked autoflush" in serialized
    assert "acceptance/" not in serialized and "private/" not in serialized


def test_unresolved_finish_is_not_success(task):
    task = finish(task, "unresolved")
    assert task["status"] == "unresolved" and task["final_evaluation"] == "not_run"
    with pytest.raises(ValueError, match="no_change"):
        finalize_task(Path(task["task_path"]), comparator=public_run)


def test_unverified_in_contract_finish_is_blocked_while_diagnostic_route_remains(task):
    action = {"type": "finish", "reason": "unresolved", "explanation": "Known clause not observed",
              "evidence_refs": ["business_contract"],
              "contract_coverage": coverage(
                  "Default queries must exclude hidden records", status="unverified", evidence=[]
              )}
    with pytest.raises(ValueError, match="diagnostic capacity") as error:
        accept_action(task, {"action": action}, load_case(CASE))
    assert "new_probe_definition_available" in str(error.value)
    assert task.get("finish") is None and task["status"] == "ready"


def test_outside_scope_and_verified_coverage_do_not_force_more_diagnostics(task):
    context = {
        "remaining_diagnostic_runs": 3,
        "diagnostic_options": {
            "new_probe_definition_available": True,
            "revisable_probe_ids": [],
            "reusable_probe_ids": [],
        },
    }
    action = {"reason": "candidate_ready", "contract_coverage": coverage(
        "Remote VCS workflows", scope="outside_contract", status="unverified", evidence=[]
    )}
    decision = finish_coverage_decision(action, context)
    assert decision["accepted"] and decision["code"] == "no_unverified_in_contract_requirement"
    verified = finish_coverage_decision(
        {"reason": "unresolved", "contract_coverage": coverage()}, context
    )
    assert verified["accepted"] and verified["code"] == "no_unverified_in_contract_requirement"


@pytest.mark.parametrize("mode", ["exhausted", "tool_limit"])
def test_unresolved_finish_is_allowed_only_when_route_is_exhausted_or_tool_limited(task, mode):
    if mode == "exhausted":
        task["diagnostic_runs"] = [{"key": str(index)} for index in range(4)]
        limitation = None
    else:
        limitation = "Registered runner cannot construct the required external service boundary"
    action = {"type": "finish", "reason": "unresolved", "explanation": "Bounded unresolved result",
              "evidence_refs": ["business_contract"],
              "contract_coverage": coverage(
                  "External service behavior remains unverified", status="unverified", evidence=[],
                  tool_limitation=limitation
              )}
    accept_action(task, {"action": action}, load_case(CASE))
    gate = task["finish"]["diagnostic_scope"]["contract_coverage_gate"]
    assert task["status"] == "unresolved" and gate["accepted"]
    assert gate["code"] == "unresolved_with_tool_limit_or_no_diagnostic_route"


def test_success_finish_cannot_hide_unverified_contract_when_diagnostics_are_exhausted(task):
    task["diagnostic_runs"] = [{"key": str(index)} for index in range(4)]
    action = {"type": "finish", "reason": "no_change_claimed", "explanation": "Still unknown",
              "evidence_refs": ["business_contract"],
              "contract_coverage": coverage(
                  "A required behavior remains unknown", status="unverified", evidence=[]
              )}
    with pytest.raises(ValueError, match="cannot include an unverified in-contract requirement"):
        accept_action(task, {"action": action}, load_case(CASE))


def test_probe_is_separate_from_candidate_and_acceptance(task):
    revision = load_candidate(load_case(CASE)).revision
    task = run_action(task, {"type": "propose_probe", "revision": revision,
        "code": "def test_example():\n    assert 1 == 1\n", "purpose": "observe",
        "expected_observation": "comparison", "evidence_refs": ["business_contract"],
        "oracle": {"requirement": "nothing outstanding", "subject": "count", "exercise": "cleanup",
                   "basis": "absolute", "operator": "eq", "expected": 0}})
    assert task["status"] == "pending_diagnostic_review" and task["candidate"] is None
    pending = read(task["pending_diagnostic"]["request"], Path(task["task_path"]).parent)
    assert pending["kind"] == "probe" and pending["probe_id"] == task["probes"][0]["id"]
    rejected = review(task, diagnostic_decision="reject")
    assert rejected["status"] == "ready" and not rejected["diagnostic_runs"] and not rejected["observations"]
    assert inspect_task(Path(task["task_path"]))["candidate_history"] == []
    context = context_from_reference(freeze_context(rejected), load_case(CASE), load_candidate(load_case(CASE)))
    assert context["probes"][0]["last_review_decision"] == "reject"


def test_existing_probe_can_request_review_when_definition_budget_is_exhausted(task):
    case = load_case(CASE)
    action = {"type": "propose_probe", "revision": load_candidate(case).revision,
              "code": "def test_example():\n    pass\n", "purpose": "offline probe", "expected_observation": "zero",
              "evidence_refs": ["business_contract"],
              "oracle": {"requirement": "return resources", "subject": "count", "exercise": "close",
                         "basis": "absolute", "operator": "eq", "expected": 0}}
    for index in range(2):
        task = review(run_action(task, action | {"purpose": f"offline probe {index}"}), diagnostic_decision="reject")
    context = context_from_reference(freeze_context(task), case, load_candidate(case))
    assert context["remaining_probes"] == 0 and context["remaining_diagnostic_runs"] == 4
    probe_id = task["probes"][0]["id"]
    task = run_action(task, {"type": "run_probe", "revision": action["revision"], "probe_id": probe_id})
    assert task["status"] == "pending_diagnostic_review"
    assert len(task["probes"]) == 2 and task["diagnostic_runs"] == []
    # 重用请求不能绕过已拒绝探针的再次审阅，更不自动执行。
    assert read(task["pending_diagnostic"]["request"], Path(task["task_path"]).parent)["probe_id"] == probe_id


def test_measured_counterexample_blocks_finish_survives_recovery_and_revision(task):
    from upgrade_workbench.diagnostic_oracles import MARKER

    task = review(request_public(task))
    revision = load_candidate(load_case(CASE)).revision
    action = {"type": "propose_probe", "revision": revision, "code": "def test_resource():\n    pass\n",
              "purpose": "offline resource observation", "expected_observation": "no outstanding resources",
              "evidence_refs": ["business_contract"],
              "oracle": {"requirement": "return resources", "subject": "outstanding count", "exercise": "close",
                         "basis": "absolute", "operator": "eq", "expected": 0}}
    task = run_action(task, action)

    def runner(case, snapshot, code, directory, execution):
        report = public_run(case.manifest_path, directory, check_group="feedback")
        stdout = directory / "stdout.txt"
        stdout.write_text(MARKER + json.dumps({"before": 4, "after": 4, "path_completed": True}))
        report["stages"]["new_original"] = report["stages"]["new_original"] | {"stdout_path": str(stdout)}
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    task = review(task, probe_runner=runner)
    context = context_from_reference(freeze_context(task), load_case(CASE), load_candidate(load_case(CASE)))
    assert context["probes"][0]["code"] == action["code"]
    assert context["probes"][0]["last_review_decision"] == "accept"
    assert "NEW definitions" in context["probe_budget_note"]
    recovered = recover_diagnostic(json.loads(json.dumps(task)))
    assert recovered["observations"] == task["observations"]
    finish_action = {"type": "finish", "reason": "no_change_claimed", "explanation": "pytest passed",
                     "evidence_refs": ["observation:" + task["observations"][-1]["id"]],
                     "contract_coverage": coverage()}
    with pytest.raises(ValueError, match="measured counterexample"):
        accept_action(recovered, {"action": finish_action}, load_case(CASE))
    original = load_candidate(load_case(CASE))
    name = load_case(CASE).manifest.allowed_changes[0]
    old = original.files[name].decode()
    task = run_action(task, {"type": "submit_candidate", "base_revision": revision,
        "edits": [{"path": name, "old": old, "new": "# revision alone cannot repair an observation\n" + old}]})
    candidate = task["candidate"]
    task = advance_task(Path(task["task_path"]), reviewed_revision=candidate["revision"],
        reviewed_sha256=candidate["sha256"], reviewer="test", review_note="offline contract",
        review_only=True, comparator=public_run)
    with pytest.raises(ValueError, match="rerun the same probe"):
        accept_action(task, {"action": finish_action | {"reason": "candidate_ready"}}, load_case(CASE))
    task = run_action(task, {"type": "run_probe", "revision": candidate["revision"], "probe_id": task["probes"][0]["id"]})

    def repaired_runner(case, snapshot, code, directory, execution):
        report = runner(case, snapshot, code, directory, execution)
        stdout = directory / "candidate.txt"
        stdout.write_text(MARKER + json.dumps({"before": 0, "after": 0, "path_completed": True}))
        report["stages"]["new_candidate"] = report["stages"]["new_original"] | {"stdout_path": str(stdout)}
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    task = review(task, probe_runner=repaired_runner)
    accept_action(task, {"action": finish_action | {"reason": "candidate_ready"}}, load_case(CASE))
    assert task["status"] == "submitted"
    assert task["final_evaluation"] == "not_run"


def test_public_observation_becomes_stale_after_increment_and_candidate_can_finish(task):
    task = review(request_public(task))
    original = load_candidate(load_case(CASE))
    name = load_case(CASE).manifest.allowed_changes[0]
    old = original.files[name].decode()
    task = run_action(task, {"type": "submit_candidate", "base_revision": original.revision,
        "edits": [{"path": name, "old": old, "new": "# offline boundary test\n" + old}]})
    assert task["status"] == "pending_review"
    snapshot = load_candidate(load_case(CASE), task["current_candidate"])
    context = context_from_reference(freeze_context(task), load_case(CASE), snapshot)
    assert context["observation_index"][0]["stale"] is True
    candidate = task["candidate"]
    task = advance_task(Path(task["task_path"]), reviewed_revision=candidate["revision"],
        reviewed_sha256=candidate["sha256"], reviewer="test", review_note="offline contract",
        review_only=True, comparator=public_run)
    task = finish(task, "candidate_ready")
    assert task["status"] == "submitted" and task["final_evaluation"] == "not_run"


def test_recurring_failures_reach_verified_request_after_reload_and_candidate_changes(task):
    node = "feedback/test_behavior.py::test_defaults"

    def failing_public(manifest, directory, **kwargs):
        directory = directory / hashlib.sha256(kwargs["candidate_patch"].read_bytes()).hexdigest()[:16]
        report = public_run(manifest, directory, **kwargs)
        stdout = directory / "public-failure.txt"
        stdout.write_text(f"FAILED checks/{node} - AssertionError\n")
        old = report["stages"]["old_original"] | {"nodeids": [node]}
        failed = old | {"status": "failed", "exit_code": 1,
                        "tests": {"collected": 1, "passed": 0, "failed": 1, "errors": 0, "skipped": 0},
                        "stdout_path": str(stdout)}
        report.update(status="candidate_not_accepted",
                      stages={"old_original": old, "new_original": failed, "new_candidate": failed})
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    for number in range(2):
        snapshot = load_candidate(load_case(CASE), task["current_candidate"])
        name = load_case(CASE).manifest.allowed_changes[0]
        old = snapshot.files[name].decode()
        task = run_action(task, {"type": "submit_candidate", "base_revision": snapshot.revision,
            "edits": [{"path": name, "old": old, "new": f"# offline candidate {number}\n" + old}]})
        candidate = task["candidate"]
        task = advance_task(Path(task["task_path"]), reviewed_revision=candidate["revision"],
            reviewed_sha256=candidate["sha256"], reviewer="test", review_note="offline public failure",
            review_only=True, comparator=failing_public)
    task = inspect_task(Path(task["task_path"]))
    snapshot = load_candidate(load_case(CASE), task["current_candidate"])
    prepared = prepare_case_proposal(CASE, Path(task["work_root"]), public_source_ack=True,
        output_format="diagnostic_actions", source_policy=task["protocol"]["source_policy"],
        candidate_reference=task["current_candidate"], diagnostic_context_reference=freeze_context(task),
        **task["protocol"]["generation"])
    body = Path(prepared["request_path"]).read_bytes()
    _verify_payload(prepared, body)
    context = json.loads(json.loads(body)["messages"][1]["content"])
    repeated = context["diagnostic_state"]["recurring_public_failures"]
    assert repeated["items"][0]["distinct_failed_revisions"] == 2
    assert repeated["items"][0]["latest_failed_revision"] == snapshot.revision
    assert repeated["items"][0]["nodeid"] == node
    assert task["status"] == "ready" and task["final_evaluation"] == "not_run"


def test_false_candidate_ready_does_not_create_candidate(task):
    calls = []
    def transport(*_a, **_k):
        calls.append(1)
        return response({"type": "finish", "reason": "candidate_ready" if len(calls) == 1 else "unresolved",
                         "explanation": "no candidate", "evidence_refs": [],
                         "contract_coverage": coverage()})
    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert result["status"] == "unresolved" and result["candidate"] is None and len(calls) == 2


def test_corrupt_observation_stops_before_network(task):
    task = review(request_public(task))
    path = Path(task["observations"][0]["path"])
    path.write_bytes(path.read_bytes() + b" ")
    result = advance_task(Path(task["task_path"]), api_key="unused",
                         transport=lambda *_a, **_k: pytest.fail("must not send"))
    assert result["status"] == "execution_incomplete" and len(result["attempts"]) == 1


def test_probe_infrastructure_error_is_not_application_failure():
    report = {"status": "observed", "stages": {"old_original": {"status": "error"},
                                                 "new_original": {"status": "timeout"}}}
    assert summarize(report, probe=True)[0]["status"] == "execution_incomplete"


def test_full_and_focused_preserve_analysis(task):
    kwargs = dict(public_source_ack=True, output_format="diagnostic_actions",
                  diagnostic_context_reference=freeze_context(task), **task["protocol"]["generation"])
    contexts = []
    for mode in ("full", "focused"):
        prepared = prepare_case_proposal(CASE, Path(task["work_root"]),
            source_policy=task["protocol"]["source_policy"] | {"mode": mode}, **kwargs)
        _verify_payload(prepared, Path(prepared["request_path"]).read_bytes())
        contexts.append(json.loads(json.loads(Path(prepared["request_path"]).read_bytes())["messages"][1]["content"]))
    assert contexts[0]["potential_impacts"] == contexts[1]["potential_impacts"]
    assert contexts[0]["version_evidence"] == contexts[1]["version_evidence"]
    assert contexts[0]["source_inventory"] == contexts[1]["source_inventory"]


def test_invalid_reference_explains_exact_namespace_without_accepting_guess(task):
    with pytest.raises(ValueError, match="without an added prefix"):
        resolve_refs(task, ["source:registered/bumpversion/config.py"])
    resolve_refs(task, ["source:bumpversion/config.py", "business_contract"])


def probe_action(task, **changes):
    return {"type": "propose_probe", "revision": load_candidate(load_case(CASE), task["current_candidate"]).revision,
            "code": "def test_resource():\n    pass\n", "purpose": "offline resource observation",
            "expected_observation": "no outstanding resources", "evidence_refs": ["business_contract"],
            "oracle": {"requirement": "return resources", "subject": "outstanding count", "exercise": "close",
                       "basis": "absolute", "operator": "eq", "expected": 0}, **changes}


def revision_action(task, parent, **changes):
    return probe_action(task, type="revise_probe", parent_probe_id=parent,
        revision_reason="correct setup before taking the same measurement",
        code="def test_resource():\n    value = 0\n    assert value == 0\n", **changes)


def measured_run(case, snapshot, code, directory, execution, *, value=0, completed=True):
    from upgrade_workbench.diagnostic_oracles import MARKER

    report = public_run(case.manifest_path, directory, check_group="feedback")
    stdout = directory / "stdout.txt"
    stdout.write_text(MARKER + json.dumps({"before": 0, "after": value, "path_completed": completed}))
    for name in ("old_original", "new_original"):
        report["stages"][name] = report["stages"][name] | {"stdout_path": str(stdout)}
    Path(report["report_path"]).write_text(json.dumps(report))
    return report


def test_rejection_persists_in_actual_requests_across_navigation_and_reload(task):
    task = review(run_action(task, probe_action(task)), diagnostic_decision="reject")
    note = task["diagnostic_reviews"][-1]["review"]["note"]
    seen = []
    actions = iter([
        {"type": "read_source", "revision": probe_action(task)["revision"],
         "path": "bumpversion/config.py", "start_line": 1, "end_line": 5},
        {"type": "search_source", "revision": probe_action(task)["revision"],
         "scope": "repository", "query": "import"},
        {"type": "finish", "reason": "unresolved", "explanation": "offline boundary", "evidence_refs": [],
         "contract_coverage": coverage()},
    ])

    def transport(request, **kwargs):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        seen.append(ctx)
        return response(next(actions))

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert len(seen) == 3 and result["status"] == "unresolved"
    assert "edit_feedback" in seen[0]["task_context"] and "edit_feedback" not in seen[-1]["task_context"]
    for ctx in seen:
        diagnostic = ctx["diagnostic_state"]
        assert diagnostic["review_history"][0]["note"] == note
        assert diagnostic["probes"][0]["review_history"][0]["decision"] == "reject"
    assert seen[-1]["diagnostic_state"]["recent_actions"][-1]["action"]["type"] == "search_source"
    assert seen[-1]["diagnostic_state"]["observations"][-1]["action"]["type"] == "search_source"
    reloaded = inspect_task(Path(task["task_path"]))
    context = context_from_reference(freeze_context(reloaded), load_case(CASE), load_candidate(load_case(CASE)))
    assert context["review_history"][0]["note"] == note
    assert context["recent_actions"][-1]["status"] == "unresolved"


def test_revision_after_exhausted_definitions_requires_review_preserves_parent(task):
    for index in range(2):
        task = review(run_action(task, probe_action(task, purpose=f"root {index}")), diagnostic_decision="reject")
    parent = task["probes"][0]
    original = Path(parent["path"]).read_bytes()
    task = run_action(task, revision_action(task, parent["id"]))
    assert task["status"] == "pending_diagnostic_review" and task["diagnostic_runs"] == []
    assert len(task["probes"]) == 3 and task["candidate"] is None
    assert read(task["probes"][-1], Path(task["task_path"]).parent)["parent_probe_id"] == parent["id"]
    task = review(task, probe_runner=measured_run)
    context = context_from_reference(freeze_context(task), load_case(CASE), load_candidate(load_case(CASE)))
    assert context["remaining_probes"] == 0 and context["remaining_diagnostic_runs"] == 3
    assert context["probes"][-1]["last_conclusion"] == "no_counterexample_observed"
    assert not context["probes"][0]["can_revise"] and not context["probes"][-1]["can_revise"]
    assert Path(parent["path"]).read_bytes() == original
    assert task["final_evaluation"] == "not_run"


@pytest.mark.parametrize("field,value", [("requirement", "weaker"), ("subject", "different"),
    ("basis", "delta"), ("operator", "le"), ("expected", 2)])
def test_probe_revision_cannot_weaken_registered_predicate(task, field, value):
    task = review(run_action(task, probe_action(task)), diagnostic_decision="reject")
    action = revision_action(task, task["probes"][0]["id"])
    action["oracle"][field] = value
    before = list(task["probes"])
    with pytest.raises(ValueError, match="preserve requirement"):
        accept_action(task, {"action": action}, load_case(CASE))
    assert task["probes"] == before and task["pending_diagnostic"] is None


def test_revision_lineage_is_bounded_and_cannot_branch_from_old_parent(task):
    task = review(run_action(task, probe_action(task)), diagnostic_decision="reject")
    root = task["probes"][0]["id"]
    task = review(run_action(task, revision_action(task, root)), diagnostic_decision="reject")
    with pytest.raises(ValueError, match="eligible latest"):
        accept_action(task, {"action": revision_action(task, root, purpose="branch")}, load_case(CASE))
    action = revision_action(task, task["probes"][-1]["id"])
    action["code"] = "def test_resource():\n    assert 2 == 2\n"
    task = review(run_action(task, action), diagnostic_decision="reject")
    context = context_from_reference(freeze_context(task), load_case(CASE), load_candidate(load_case(CASE)))
    assert all(p["remaining_revisions"] == 0 and not p["can_revise"] for p in context["probes"])
    with pytest.raises(ValueError, match="eligible latest"):
        accept_action(task, {"action": revision_action(task, task["probes"][-1]["id"])}, load_case(CASE))
    assert len(task["probes"]) == 3


def test_inconclusive_probe_is_revisable_but_counterexample_is_not(task):
    def incomplete(*args):
        return measured_run(*args, completed=False)

    task = review(run_action(task, probe_action(task)), probe_runner=incomplete)
    context = context_from_reference(freeze_context(task), load_case(CASE), load_candidate(load_case(CASE)))
    assert context["probes"][0]["can_revise"]
    action = revision_action(task, task["probes"][0]["id"])
    task = run_action(task, action)
    task = review(task, probe_runner=lambda *args: measured_run(*args, value=3))
    context = context_from_reference(freeze_context(task), load_case(CASE), load_candidate(load_case(CASE)))
    assert context["probes"][-1]["last_conclusion"] == "counterexample_observed"
    assert not context["probes"][-1]["can_revise"]
    with pytest.raises(ValueError, match="eligible latest"):
        accept_action(task, {"action": revision_action(task, task["probes"][-1]["id"])}, load_case(CASE))
    with pytest.raises(ValueError, match="counterexample"):
        accept_action(task, {"action": {"type": "finish", "reason": "no_change_claimed",
            "explanation": "not allowed", "evidence_refs": [],
            "contract_coverage": coverage()}}, load_case(CASE))


def test_alternating_cached_diagnostics_warn_then_stop_without_new_execution(task):
    task = review(request_public(task))
    task = review(run_action(task, probe_action(task)), probe_runner=measured_run)
    revision = probe_action(task)["revision"]
    actions = [{"type": "run_public_checks", "revision": revision},
               {"type": "run_probe", "revision": revision, "probe_id": task["probes"][0]["id"]}]
    observed = []

    def transport(request, **kwargs):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        observed.append(ctx["diagnostic_state"])
        return response(actions[(len(observed) - 1) % 2])

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport,
                         comparator=lambda *_a, **_k: pytest.fail("cached execution must not run"))
    assert len(observed) == 4 and result["stop_reason"] == "repeated_observation_cycle_no_progress"
    assert result["status"] == "unresolved" and len(result["diagnostic_runs"]) == 2
    assert observed[2]["progress"]["warning"]
    assert len(result["attempts"]) == 6 and result["final_evaluation"] == "not_run"
    context = context_from_reference(freeze_context(inspect_task(Path(task["task_path"]))),
                                     load_case(CASE), load_candidate(load_case(CASE)))
    assert context["progress"]["consecutive_revisits"] == 4
    assert context["recent_actions"][-1]["cached_result"] is True


def test_runtime_findings_survive_source_window_and_feedback_tamper_is_rejected(task):
    from upgrade_workbench.tasks import accept_response

    task = review(run_action(task, probe_action(task)), probe_runner=lambda *args: measured_run(*args, completed=False))
    observation = task["observations"][0]["id"]
    for line in (1, 2, 3, 4):
        accept_response(task, {"status": "action_ready", "summary": "read new lines", "action": {
            "type": "read_source", "revision": probe_action(task)["revision"], "path": "bumpversion/config.py",
            "start_line": line, "end_line": line}}, load_case(CASE))
    context = context_from_reference(freeze_context(task), load_case(CASE), load_candidate(load_case(CASE)))
    assert all(r["kind"] == "source" for r in context["observations"])
    assert context["runtime_findings"][0]["observation_id"] == observation
    assert context["runtime_findings"][0]["conclusion"] == "inconclusive"
    prepared = prepare_case_proposal(CASE, Path(task["work_root"]), public_source_ack=True,
        output_format="diagnostic_actions", source_policy=task["protocol"]["source_policy"],
        diagnostic_context_reference=freeze_context(task), **task["protocol"]["generation"])
    payload = json.loads(Path(prepared["request_path"]).read_bytes())
    body = json.loads(payload["messages"][1]["content"])
    body["diagnostic_state"]["review_history"][0]["note"] = "tampered review"
    payload["messages"][1]["content"] = json.dumps(body)
    with pytest.raises(ValueError, match="diagnostic observation changed"):
        _verify_payload(prepared, json.dumps(payload).encode())


def test_probe_revision_response_recovers_once_without_provider_replay(task, monkeypatch):
    from upgrade_workbench import tasks
    from upgrade_workbench.service.recovery import recover_task

    task = review(run_action(task, probe_action(task)), diagnostic_decision="reject")
    action = revision_action(task, task["probes"][0]["id"])
    complete = tasks.complete_request
    calls = []

    class Crash(BaseException):
        pass

    def crash_after_receipt(prepared, **kwargs):
        complete(prepared, **kwargs)
        calls.append(1)
        raise Crash()

    monkeypatch.setattr(tasks, "complete_request", crash_after_receipt)
    with pytest.raises(Crash):
        run_action(task, action)
    path = Path(task["task_path"])
    recovered = recover_task(path, Path(task["work_root"]), None)
    assert recovered["status"] == "pending_diagnostic_review"
    assert len(recovered["probes"]) == 2 and len(recovered["diagnostic_events"]) == 2
    assert recovered["budget"]["calls"] == 2 and len(calls) == 1
    assert recover_task(path, Path(task["work_root"]), None) == recovered
    assert recovered["diagnostic_runs"] == []


def test_exhausted_execution_budget_does_not_register_an_unusable_definition(task):
    task["diagnostic_runs"] = [{"key": str(i)} for i in range(4)]
    with pytest.raises(ValueError, match="no new probe registered"):
        accept_action(task, {"action": probe_action(task)}, load_case(CASE))
    assert task["probes"] == [] and task["pending_diagnostic"] is None


def test_exhausted_probe_budget_still_allows_reviewed_candidate_feedback_and_finish(task):
    task = review(request_public(task))
    # 只模拟计数耗尽；补丁内容和比较器也是夹具，不构成应用修复证据。
    task["diagnostic_runs"] = [{"key": f"offline-used-{i}"} for i in range(4)]
    Path(task["task_path"]).write_text(json.dumps(task), encoding="utf-8")
    case = load_case(CASE)
    original = load_candidate(case)
    name = case.manifest.allowed_changes[0]
    old = original.files[name].decode()
    task = run_action(task, {"type": "submit_candidate", "base_revision": original.revision,
        "edits": [{"path": name, "old": old, "new": "# offline candidate boundary\n" + old}]})
    assert task["status"] == "pending_review"
    candidate = task["candidate"]
    task = advance_task(Path(task["task_path"]), reviewed_revision=candidate["revision"],
        reviewed_sha256=candidate["sha256"], reviewer="test", review_note="offline contract",
        review_only=True, comparator=public_run)
    assert task["status"] == "ready" and len(task["diagnostic_runs"]) == 4
    assert task["feedback"]["status"] != "execution_incomplete"
    task = finish(task, "candidate_ready")
    assert task["status"] == "submitted" and task["final_evaluation"] == "not_run"
