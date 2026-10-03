"""离线故障只验证恢复机制，不作为迁移成功率或模型成绩。"""

import json
from pathlib import Path

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.service.recovery import (
    _attempt_contract,
    _read_only_state,
    _require_read_only_report,
    _require_read_only_state,
    clear_stale_core_lock,
    recover_task,
)

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "cases/bump-my-version-0.5.0-r2/manifest.json"
OWNER = "a" * 32


class Crash(BaseException):
    pass


def response(action):
    return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5},
                       "choices": [{"finish_reason": "stop", "message": {
                           "content": json.dumps({"summary": "offline recovery test", "action": action})}}]}).encode()


def edit(request, **_kwargs):
    context = json.loads(json.loads(request.data)["messages"][1]["content"])
    name = context["allowed_changes"][0]
    text = next(item["text"] for item in context["source_files"] if item["path"] == name)
    return response({"type": "submit_candidate", "base_revision": context["candidate"]["revision"],
                     "edits": [{"path": name, "old": text, "new": "# offline test\n" + text}]})


@pytest.fixture
def task(tmp_path):
    ledger = tmp_path / "budget.sqlite"
    BudgetLedger(ledger, {"mode": "user_managed", "limit_usd": None, "model": "owned-model",
                          "input_per_million": "1", "output_per_million": "1",
                          "pricing_source": "https://example.test/pricing", "pricing_checked_at": "2026-09-17"})
    return tasks.create_operation(CASE, tmp_path / "work", budget_path=ledger, service_owner=OWNER,
                                  seed_strategy="none", max_calls=3,
                                  generation={"model": "owned-model", "endpoint": "https://example.test/chat/completions",
                                              "thinking_mode": "disabled", "max_output_tokens": 1024, "timeout_seconds": 10})


def crash_after_receipt(task, monkeypatch, transport=edit, settled=False):
    complete = tasks.complete_request

    def crash(prepared, **kwargs):
        result = complete(prepared, **kwargs)
        if settled:
            BudgetLedger(Path(task["budget_path"])).settle(Path(result["report_path"]).parent.name, result["model_usage"])
        raise Crash()

    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)


@pytest.mark.parametrize("settled", [False, True])
def test_completed_receipt_recovered_without_second_request(task, monkeypatch, settled):
    crash_after_receipt(task, monkeypatch, settled=settled)
    result = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert result["status"] == "pending_review" and len(result["attempts"]) == 1
    assert result["budget"]["calls"] == 1 and result["budget"]["groups"][0]["state"] == "settled"
    assert recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER) == result


@pytest.mark.parametrize("action, status", [({"type": "finish"}, "no_candidate"),
    ({"type": "read_source", "path": "bumpversion/config.py", "start_line": 1, "end_line": 3}, "ready")])
def test_non_candidate_actions_recovered(task, monkeypatch, action, status):
    crash_after_receipt(task, monkeypatch, lambda *_a, **_kw: response(action))
    result = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert result["status"] == status and len(result["attempts"]) == 1


def test_unknown_attempt_is_never_replayed(task, monkeypatch):
    def unknown(*_a, **_kw):
        raise TimeoutError("injected")
    crash_after_receipt(task, monkeypatch, unknown)
    result = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert result["status"] == "outcome_unknown"
    assert result["budget"]["groups"][0]["state"] == "unknown"
    assert result["budget"]["estimated_or_reserved_usd"] > 0


@pytest.mark.parametrize(
    "role,output_format,statuses",
    [
        ("solver", "protocol_v6_actions", {"pending_review", "agent_finished", "action_ready"}),
        ("investigator", "investigator_actions", {"action_ready", "investigation_ready"}),
        ("contract_auditor", "contract_audit", {"audit_ready"}),
    ],
)
def test_schema_5_recovery_binds_role_format_and_status(role, output_format, statuses):
    spec, expected = _attempt_contract(
        {"schema_version": 5},
        {"role": role, "output_format": output_format},
        {"output_format": output_format},
    )

    assert spec.role == role
    assert expected == output_format
    assert spec.recoverable_statuses == frozenset(statuses)


@pytest.mark.parametrize(
    "attempt,report",
    [
        ({}, {"output_format": "protocol_v6_actions"}),
        (
            {"role": "solver", "output_format": "protocol_v6_actions"},
            {"output_format": "investigator_actions"},
        ),
        (
            {"role": "investigator", "output_format": "protocol_v6_actions"},
            {"output_format": "protocol_v6_actions"},
        ),
    ],
)
def test_schema_5_recovery_rejects_implicit_or_changed_role_format(attempt, report):
    with pytest.raises(ValueError, match="recovery_(attempt_role|output_format)_changed"):
        _attempt_contract({"schema_version": 5}, attempt, report)


def test_read_only_recovery_rejects_candidate_output_and_state_mutation():
    with pytest.raises(ValueError, match="returned_candidate_output"):
        _require_read_only_report(
            {"candidate_patch": "forbidden.patch"},
            "investigator",
        )

    task = {
        "candidate": {"revision": "r1"},
        "current_candidate": {"revision": "r1"},
        "candidate_history": [{"revision": "r1"}],
        "review_history": [],
        "final_evaluation": "not_run",
    }
    before = _read_only_state(task)
    task["candidate"]["revision"] = "r2"
    with pytest.raises(ValueError, match="changed_candidate_or_terminal_state"):
        _require_read_only_state(task, before)


@pytest.mark.parametrize("artifact", ["request.json", "response.sanitized.json", "attempt.json", "revision/candidate.patch"])
def test_tampered_artifact_not_accepted(task, monkeypatch, artifact):
    crash_after_receipt(task, monkeypatch)
    path = Path(task["task_path"])
    pending = tasks.inspect_task(path)
    target = Path(pending["attempts"][-1]["receipt"]).parent / artifact
    target.write_bytes(target.read_bytes() + b" ")
    if artifact == "attempt.json":
        target.write_text('{"request_sha256":"wrong","calls":1}')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        recover_task(path, Path(task["work_root"]), OWNER)
    assert path.read_bytes() == before


def test_cli_cannot_operate_service_task_or_remove_its_lock(task):
    path = Path(task["task_path"])
    lock = path.with_suffix(".lock")
    lock.write_text(task["task_id"])
    with pytest.raises(ValueError, match="ownership"):
        tasks.advance_task(path)
    assert lock.exists()
    with pytest.raises(ValueError, match="ownership"):
        clear_stale_core_lock(path, "b" * 32, Path(task["work_root"]))
    assert lock.exists()
    clear_stale_core_lock(path, OWNER, Path(task["work_root"]))
    assert not lock.exists()


def test_equal_price_different_tokens_cannot_replace_settlement(task, monkeypatch):
    crash_after_receipt(task, monkeypatch, settled=True)
    pending = tasks.inspect_task(Path(task["task_path"]))
    receipt = json.loads(Path(pending["attempts"][-1]["receipt"]).read_bytes())
    wrong = dict(receipt["model_usage"], input_tokens=5, output_tokens=10)
    ledger = BudgetLedger(Path(task["budget_path"]))
    with pytest.raises(ValueError, match="replace settled usage"):
        ledger.reconcile_receipt(pending["attempts"][-1]["request_id"], receipt["request_sha256"], wrong)


def test_verification_interruption_requires_explicit_command(task):
    path = Path(task["task_path"])
    task["status"] = "verifying"
    tasks._save(task, path)
    result = recover_task(path, Path(task["work_root"]), OWNER)
    assert result["status"] == "pending_review"
    assert result["stop_reason"] == "verification_interrupted_explicit_retry_required"
