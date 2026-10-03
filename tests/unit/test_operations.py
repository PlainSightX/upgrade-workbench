"""正常任务复用推进核心：候选、审阅、反馈、计量与实验隔离。"""

import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench.budget import BudgetExceeded, BudgetLedger
from upgrade_workbench.candidates import load_candidate, publish_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.cli import _task_summary, main
from upgrade_workbench.codemod import collect_output_patch
from upgrade_workbench.reporting import export_task_report
from upgrade_workbench.tasks import advance_task, create_operation, finalize_task, inspect_task

CASE = Path(__file__).resolve().parents[2] / "cases/bump-my-version-0.5.0-r2/manifest.json"


@pytest.fixture
def operation(tmp_path):
    budget = tmp_path / "ledger.sqlite"
    BudgetLedger(budget, {"mode": "user_managed", "limit_usd": None, "model": "owned-model",
                         "input_per_million": "1", "output_per_million": "1",
                         "pricing_source": "https://example.test/pricing", "pricing_checked_at": "2026-09-16"})
    def create(seed="none", max_calls=3, execution=None):
        return create_operation(CASE, tmp_path / "work", budget_path=budget, seed_strategy=seed,
                                generation={"model": "owned-model", "endpoint": "https://provider.example/chat/completions",
                                            "thinking_mode": "disabled", "max_output_tokens": 1024, "timeout_seconds": 10},
                                max_calls=max_calls, execution=execution)
    return create


def response(action):
    return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5},
                       "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"summary": "offline action", "action": action})}}]}).encode()


def edit_transport(request, **kwargs):
    context = json.loads(json.loads(request.data)["messages"][1]["content"])
    assert context["potential_impacts"], "Normal operations must not inherit the no_ast experiment arm"
    name = context["allowed_changes"][0]
    text = next(block["text"] for block in context["source_files"] if block["path"] == name)
    return response({"type": "submit_candidate", "base_revision": context["candidate"]["revision"],
                     "edits": [{"path": name, "old": text, "new": "# offline revision\n" + text}]})


def review(task):
    return {"reviewed_sha256": task["candidate"]["sha256"], "reviewed_revision": task["candidate"]["revision"],
            "reviewer": "offline-review", "review_note": "only boundary check, not migration evidence"}


def comparison(manifest, work_root, **kwargs):
    result = {"check_group": kwargs["check_group"], "case_fingerprint": load_case(manifest).fingerprint,
              "status": "candidate_verified", "candidate": {"supplied_sha256": hashlib.sha256(kwargs["candidate_patch"].read_bytes()).hexdigest()},
              "stages": {"new_candidate": {"status": "passed"}}, "report_path": str(work_root / (kwargs["check_group"] + "-test-result.json"))}
    Path(result["report_path"]).write_text(json.dumps(result))
    return result


def test_normal_task_read_review_continue_finalize_export(operation, tmp_path):
    task = operation()
    path = Path(task["task_path"])
    assert task["kind"] == "operation" and task["phase"] == "operation"
    pending = advance_task(path, api_key="unused", transport=edit_transport)
    assert pending["status"] == "pending_review"
    assert inspect_task(path)["candidate"]["revision"] == pending["candidate"]["revision"]
    with pytest.raises(ValueError, match="revision"):
        advance_task(path, reviewed_sha256=pending["candidate"]["sha256"], reviewer="x", review_note="x", review_only=True)
    checked = advance_task(path, **review(pending), review_only=True, comparator=comparison)
    assert checked["status"] == "ready" and len(checked["attempts"]) == 1
    assert checked["feedback_revision"] == pending["candidate"]["revision"]
    final = advance_task(path, api_key="unused", transport=lambda *_a, **_kw: response({"type": "finish"}))
    assert final["status"] == "submitted" and final["final_evaluation"] == "not_run"
    final = finalize_task(path, comparator=comparison)
    assert final["final_evaluation"] == "completed"
    exported = export_task_report(path, tmp_path / "export")
    assert exported["result"] == "candidate_verified"
    assert "provider.example" in Path(exported["json_path"]).read_text()
    assert "unused" not in Path(exported["json_path"]).read_text()
    assert finalize_task(path, comparator=lambda *_a, **_kw: pytest.fail("no repeat"))["final_result"] == final["final_result"]


def test_new_revision_invalidates_feedback_and_read_results(operation):
    task = operation()
    path = Path(task["task_path"])
    first = advance_task(path, api_key="unused", transport=edit_transport)
    advance_task(path, **review(first), review_only=True, comparator=comparison)
    second = advance_task(path, api_key="unused", transport=edit_transport)
    assert second["candidate"]["revision"] != first["candidate"]["revision"]
    assert second["feedback"] is None and second["tool_results"] == []
    assert "feedback_revision" not in second and not second["candidate"]["reviewed"]
    assert len(second["candidate_history"]) == 2
    previous = second["candidate_history"][0]
    assert previous["reviewed"] is True
    assert previous["feedback_report"] == str(Path(task["work_root"]) / "feedback-test-result.json")
    assert second["candidate_history"][1]["reviewed"] is False


def test_finalized_cli_points_to_export_and_report_preserves_execution(operation, tmp_path):
    execution = {"base_image": "python@sha256:" + "a" * 64, "prepare_timeout": 90, "test_timeout": 75}
    task = operation(execution=execution)
    path = Path(task["task_path"])
    pending = advance_task(path, api_key="unused", transport=edit_transport)
    advance_task(path, **review(pending), review_only=True, comparator=comparison)
    advance_task(path, api_key="unused", transport=lambda *_a, **_kw: response({"type": "finish"}))
    final = finalize_task(path, comparator=comparison)
    assert "task-export" in _task_summary(final)["next_action"]
    assert "task-finalize" not in _task_summary(final)["next_action"]
    exported = export_task_report(path, tmp_path / "execution-export")
    markdown = Path(exported["markdown_path"]).read_text(encoding="utf-8")
    assert "--base-image '" + execution["base_image"] + "'" in markdown
    assert "--prepare-timeout 90 --test-timeout 75" in markdown
    # 普通复现不能隐式重建共享环境；镜像、超时和候选身份仍来自原任务。
    commands = [line for line in markdown.splitlines() if line.startswith("uv ")]
    assert len(commands) == 1
    assert commands[0].startswith("uv run --no-sync upgrade-workbench verify ")
    assert "--candidate '<导出包>/candidate.patch'" in commands[0]
    assert "--candidate-origin agent_candidate --check-group all" in commands[0]


def test_invalid_submit_then_finish_preserves_current_candidate(operation):
    task = operation()
    path = Path(task["task_path"])
    first = advance_task(path, api_key="unused", transport=edit_transport)
    advance_task(path, **review(first), review_only=True, comparator=comparison)
    contexts = []
    def model(request, **kwargs):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(context)
        if len(contexts) == 1:
            return response({"type": "submit_candidate", "base_revision": "0" * 64, "edits": []})
        assert context["task_context"]["edit_feedback"] == "Stale base_revision"
        return response({"type": "finish"})
    final = advance_task(path, api_key="unused", transport=model)
    assert final["candidate"] == first["candidate"] | {"reviewed": True, "feedback_report": str(Path(task["work_root"]) / "feedback-test-result.json")}
    assert len(final["attempts"]) == 3


def test_unknown_request_never_replayed(operation):
    task = operation()
    calls = []
    def fail(*_a, **_kw):
        calls.append(1)
        raise TimeoutError("unknown")
    result = advance_task(Path(task["task_path"]), api_key="unused", transport=fail)
    assert result["status"] == "outcome_unknown"
    advance_task(Path(task["task_path"]), api_key="unused", transport=fail)
    assert len(calls) == 1 and result["budget"]["groups"][0]["state"] == "unknown"
    assert result["stop_reason"] == "provider_timeout_no_retry"
    assert _task_summary(result)["transport_diagnostic"]["category"] == "timeout"


def test_official_seed_imports_without_api_and_no_silent_fallback(operation, tmp_path):
    task = operation("official")
    path = Path(task["task_path"])
    assert advance_task(path)["stop_reason"] == "static_tool_review_required"
    case = load_case(CASE)
    base = load_candidate(case)
    name = case.manifest.allowed_changes[0]
    seed = publish_candidate(case, base, {name: b"# official fixture\n" + base.files[name]}, tmp_path / "seed", origin="official_tool")
    def runner(*_a, **_kw):
        return {"status": "pending_candidate_safety_review", "case_fingerprint": case.fingerprint,
                "candidate_path": str(tmp_path / "seed/candidate.patch"), "candidate_sha256": seed.sha256,
                "baseline_path": str(tmp_path / "seed/baseline.json")}
    pending = advance_task(path, reviewed_tool=True, seed_runner=runner)
    assert pending["status"] == "pending_review" and pending["attempts"] == []
    assert pending["candidate"]["origin"] == "official_tool"
    failed = operation("official")
    result = advance_task(Path(failed["task_path"]), reviewed_tool=True, seed_runner=lambda *_a, **_kw: {"status": "failed"})
    assert result["status"] == "seed_failed" and result["attempts"] == []


def test_official_no_change_is_explicit(tmp_path):
    (tmp_path / "model.py").write_bytes(b"x = 1\n")
    patch, changed, _ = collect_output_patch(tmp_path, {"model.py": b"x = 1\n"}, ["model.py"], allow_empty=True)
    assert patch == "" and changed == []


def test_cli_normal_create_status_and_diff_need_no_api_key(operation, tmp_path, monkeypatch, capsys):
    task = operation()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = task["task_path"]
    assert main(["task-status", path]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert main(["task-diff", path]) == 0
    assert "尚无候选" in capsys.readouterr().out
    config = tmp_path / "config.json"
    config.write_text(json.dumps({key: task["protocol"][key] for key in ("generation", "execution", "max_calls")}))
    assert main(["--work-root", str(tmp_path / "normal"), "task-create", str(CASE), "--config", str(config),
                 "--budget", task["budget_path"], "--seed", "none"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert "arm" not in created and "phase" not in created and created["calls"] == 0
    assert main(["--work-root", str(tmp_path / "analysis"), "task-analyze", path]) == 0
    analyzed = json.loads(capsys.readouterr().out)
    assert analyzed["source_binding"]["revision"] == load_candidate(load_case(CASE)).revision
    assert analyzed["retrieval"]["entries"] and analyzed["target_code_executed"] is False


def test_source_no_progress_stops_within_task_bound(operation):
    task = operation(max_calls=10)
    read = {"type": "read_source", "path": "bumpversion/config.py", "start_line": 1, "end_line": 3}
    result = advance_task(Path(task["task_path"]), api_key="unused", transport=lambda *_a, **_kw: response(read))
    assert result["stop_reason"] == "repeated_source_action_no_progress"
    assert len(result["attempts"]) == 3


def test_wrong_feedback_identity_never_reaches_another_model(operation):
    task = operation()
    path = Path(task["task_path"])
    pending = advance_task(path, api_key="unused", transport=edit_transport)
    def wrong(*args, **kwargs):
        value = comparison(*args, **kwargs)
        value["candidate"]["supplied_sha256"] = "0" * 64
        return value
    result = advance_task(path, **review(pending), review_only=True, comparator=wrong)
    assert result["status"] == "execution_incomplete" and result["feedback"] is None
    assert len(result["attempts"]) == 1


def test_user_managed_ledger_preserves_history_and_optional_cap(operation, tmp_path):
    task = operation()
    advance_task(Path(task["task_path"]), api_key="unused", transport=lambda *_a, **_kw: response({"type": "finish"}))
    ledger = BudgetLedger(Path(task["budget_path"]))
    before = ledger.snapshot()
    ledger.amend_policy(mode="capped", limit_usd="0.0001", expected_configuration_sha256=before["configuration_sha256"], reason="offline policy test")
    assert ledger.snapshot()["calls"] == before["calls"] == 1
    other = operation()
    halted = advance_task(Path(other["task_path"]), api_key="unused", transport=lambda *_a, **_kw: pytest.fail("cap blocks"))
    assert halted["status"] == "budget_exhausted"
    with pytest.raises(BudgetExceeded):
        ledger.amend_policy(mode="user_managed", expected_configuration_sha256=before["configuration_sha256"], reason="stale")
    ledger.amend_policy(mode="user_managed", expected_configuration_sha256=ledger.snapshot()["configuration_sha256"], reason="user manages bill")
    assert ledger.snapshot()["calls"] == 1 and ledger.snapshot()["limit_usd"] is None
