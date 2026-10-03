"""离线验证审阅、调用上限和中断边界；mock结果不是迁移效果证据。"""

from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.budget import BudgetLedger

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "cases/bump-my-version-0.5.0-r2/manifest.json"
READ_ACTION = {
    "type": "read_source", "path": "bumpversion/config.py", "start_line": 1, "end_line": 3,
}


@pytest.fixture
def task_factory(tmp_path):
    budget_path = tmp_path / "budget.sqlite"
    BudgetLedger(budget_path, {
        "limit_usd": "1", "checkpoint_usd": "0.5", "calibration_usd": "0.2",
        "model": "owned-model", "input_per_million": "1", "output_per_million": "1",
        "pricing_source": "https://example.test/prices", "pricing_checked_at": "2026-09-16",
        "quotas": {"calibration": 20, "development": 20, "holdout": 20},
        "holdout_reserve_usd": "0.2",
    })
    protocol = {
        "max_calls": 3, "status": "frozen", "protocol_revision": 2,
        "generation": {"model": "owned-model", "endpoint": "https://example.test/completions",
                       "thinking_mode": "disabled", "max_output_tokens": 100,
                       "timeout_seconds": 10},
    }

    def create(*, arm="full", max_calls=3, manifest=CASE):
        selected = deepcopy(protocol)
        selected["max_calls"] = max_calls
        selected["implementation"] = tasks.implementation_identity()
        return tasks.create_task(manifest, tmp_path / "work", selected, arm=arm,
                                 phase="development", budget_path=budget_path)

    return create


class LocalModel:
    """模拟供应商结构输出并留下真实本地请求字节供账本核验。"""

    def __init__(self, root, responses):
        self.root = root
        self.responses = list(responses)
        self.contexts = []
        self.calls = 0

    def prepare(self, manifest, work_root, **kwargs):
        self.contexts.append(deepcopy(kwargs["task_context"]))
        directory = self.root / f"request-{len(self.contexts)}"
        directory.mkdir()
        path = directory / "request.json"
        path.write_text(json.dumps(kwargs), encoding="utf-8")
        return {
            "request_path": str(path), "report_path": str(directory / "proposal.json"),
            "request_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "model": kwargs["model"], "max_output_tokens": kwargs["max_output_tokens"],
        }

    def complete(self, prepared, **kwargs):
        self.calls += 1
        Path(prepared["report_path"]).with_name("attempt.json").write_text(
            '{"offline_owned_attempt": true}', encoding="utf-8",
        )
        reply = self.responses.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, dict):
            response = deepcopy(reply)
        elif reply == "read":
            response = {"status": "action_ready", "action": READ_ACTION}
        elif reply == "candidate":
            patch = Path(prepared["report_path"]).parent / "candidate.patch"
            patch.write_bytes((CASE.parent / "diagnostics/alias-glob-rule.patch").read_bytes())
            response = {
                "status": "pending_review", "candidate_patch": str(patch),
                "candidate_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
                "action": {"type": "submit_candidate", "edits": [{
                    "path": "bumpversion/files.py", "old": "new_file_cfg = file_cfg.copy()",
                    "new": "new_file_cfg = file_cfg",
                }]},
            }
        else:
            response = {"status": "agent_finished", "action": {"type": "finish"}}
        response.update(report_path=prepared["report_path"],
                        model_usage={"input_tokens": 10, "output_tokens": 5})
        return response


def install_model(monkeypatch, tmp_path, responses):
    model = LocalModel(tmp_path, responses)
    monkeypatch.setattr(tasks, "prepare_case_proposal", model.prepare)
    monkeypatch.setattr(tasks, "complete_request", model.complete)
    return model


def never_compare(*args, **kwargs):
    pytest.fail("No comparator or target execution was authorized at this boundary")


def approved(task):
    return {"reviewed_sha256": task["candidate"]["sha256"], "reviewer": "offline-test-reviewer",
            "review_note": "Only a test of review gating, not approval of behavior"}


def test_candidate_requires_review_before_feedback_or_another_call(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, ["candidate", "finish"])
    task = task_factory()
    path = Path(task["task_path"])
    pending = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    assert pending["status"] == "pending_review"
    assert pending["candidate"]["reviewed"] is False
    assert model.calls == 1
    untouched = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    assert untouched["status"] == "pending_review"
    assert model.calls == 1
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Review"):
        tasks.advance_task(path, api_key="unused", comparator=never_compare,
                           reviewed_sha256="0" * 64, reviewer="reviewer", review_note="wrong hash")
    assert path.read_bytes() == before
    assert model.calls == 1


def test_review_binds_patch_bytes_before_comparator(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, ["candidate"])
    task = task_factory()
    path = Path(task["task_path"])
    pending = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    Path(pending["candidate"]["patch_path"]).write_bytes(b"changed after review\n")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="changed"):
        tasks.advance_task(path, api_key="unused", comparator=never_compare, **approved(pending))
    assert path.read_bytes() == before
    assert model.calls == 1


def test_only_reviewed_public_feedback_is_given_to_next_model_call(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, ["candidate", "finish"])
    task = task_factory()
    path = Path(task["task_path"])
    pending = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    invoked = []

    def comparator(*args, **kwargs):
        invoked.append(kwargs)
        assert json.loads(path.read_text())["candidate"]["reviewed"] is True
        return {"check_group": "feedback", "status": "candidate_verified",
                "report_path": str(tmp_path / "fake-report.json"),
                "stages": {"new_candidate": {"status": "passed"}}}

    final = tasks.advance_task(path, api_key="unused", comparator=comparator, **approved(pending))
    assert final["status"] == "submitted"
    assert len(invoked) == 1 and invoked[0]["check_group"] == "feedback"
    assert model.contexts[1]["feedback"]["scope"] == "public"
    assert model.contexts[1]["feedback"]["status"] == "passed"
    assert final["final_evaluation"] == "not_run"


def test_no_feedback_arm_never_calls_comparator_or_sends_results(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, ["candidate", "finish"])
    task = task_factory(arm="no_feedback")
    path = Path(task["task_path"])
    pending = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    final = tasks.advance_task(path, api_key="unused", comparator=never_compare, **approved(pending))
    assert final["status"] == "submitted"
    assert model.calls == 2
    assert all("feedback" not in context for context in model.contexts)
    assert final["candidate"]["reviewed"] is True
    assert final["final_evaluation"] == "not_run"
    assert tasks.advance_task(path, api_key="unused", comparator=never_compare)["status"] == "submitted"
    assert model.calls == 2


def test_each_tool_continuation_consumes_same_total_call_limit(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, ["read"] * 3)
    task = task_factory(max_calls=3)
    final = tasks.advance_task(Path(task["task_path"]), api_key="unused", comparator=never_compare)
    assert final["status"] == "no_candidate"
    assert final["stop_reason"] == "call_limit"
    assert model.calls == len(final["attempts"]) == 3
    assert final["budget"]["calls"] == 3
    assert len(final["tool_results"]) == 3
    assert "tool_results" not in model.contexts[0]
    assert model.contexts[1]["tool_results"][0]["result"]["path"] == "bumpversion/config.py"


def test_protocol_hash_change_prevents_api_call(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, [])
    task = task_factory()
    path = Path(task["task_path"])
    task["protocol"]["max_calls"] += 1
    path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValueError, match="protocol"):
        tasks.advance_task(path, api_key="unused", comparator=never_compare)
    assert model.calls == 0


def test_case_identity_change_prevents_api_call(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, [])
    copied = tmp_path / "copied-case"
    shutil.copytree(CASE.parent, copied)
    manifest = copied / "manifest.json"
    task = task_factory(manifest=manifest)
    changed = json.loads(manifest.read_text(encoding="utf-8"))
    changed["title"] = "Changed case identity"
    manifest.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="case has changed"):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", comparator=never_compare)
    assert model.calls == 0


def test_calling_model_checkpoint_requires_reconciliation_before_new_call(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, [])
    task = task_factory()
    path = Path(task["task_path"])
    task["status"] = "calling_model"
    path.write_text(json.dumps(task), encoding="utf-8")
    recovered = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    assert recovered["status"] in {"outcome_unknown", "interrupted"}
    assert model.calls == 0


def test_provider_exception_does_not_allow_new_paid_call_on_resume(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, [RuntimeError("transport outcome uncertain")])
    task = task_factory()
    path = Path(task["task_path"])
    try:
        first = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    except RuntimeError:
        first = json.loads(path.read_text(encoding="utf-8"))
    assert first["status"] in {"outcome_unknown", "interrupted", "calling_model"}
    resumed = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    assert resumed["status"] in {"outcome_unknown", "interrupted"}
    assert model.calls == 1
    assert BudgetLedger(Path(task["budget_path"])).snapshot()["calls"] == 1


def test_feedback_extractor_rejects_final_results_even_when_successful():
    with pytest.raises(ValueError, match="public feedback"):
        tasks._feedback({"check_group": "all", "status": "candidate_verified"}, "a" * 64)


def test_active_call_lock_cannot_be_reclassified_by_second_process(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, [])
    task = task_factory()
    task["status"] = "calling_model"
    path = Path(task["task_path"])
    path.write_text(json.dumps(task), encoding="utf-8")
    lock = path.with_suffix(".lock")
    lock.write_text(task["task_id"], encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        tasks.advance_task(path, api_key="unused", comparator=never_compare)
    assert path.read_bytes() == before
    assert lock.read_text(encoding="utf-8") == task["task_id"]
    assert model.calls == 0


def test_rejected_format_receives_fixed_feedback_with_new_counted_request(task_factory, monkeypatch, tmp_path):
    rejected = {"status": "provider_response_rejected", "reason": "invalid_json"}
    model = install_model(monkeypatch, tmp_path, [rejected, "candidate"])
    task = task_factory()
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", comparator=never_compare)
    assert result["status"] == "pending_review"
    assert model.calls == result["budget"]["calls"] == 2
    assert model.contexts[1]["protocol_feedback"] == {"code": "invalid_json", "attempt_index": 1}
    assert model.contexts[1]["remaining_calls"] == 2
    assert result["attempts"][0]["request_id"] != result["attempts"][1]["request_id"]
    assert result["attempts"][0]["status"] == "provider_response_rejected"
    assert rejected == {"status": "provider_response_rejected", "reason": "invalid_json"}


def test_rejected_formats_cannot_exceed_total_call_limit(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, [
        {"status": "provider_response_rejected", "reason": "invalid_json"} for _ in range(3)
    ])
    task = task_factory(max_calls=3)
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", comparator=never_compare)
    assert result["status"] == "no_candidate"
    assert result["stop_reason"] == "call_limit"
    assert model.calls == result["budget"]["calls"] == 3


@pytest.mark.parametrize("reason", ["provider_secret_echo", "provider_exceeded_output_budget"])
def test_nonrecoverable_provider_boundary_is_not_auto_retried(task_factory, monkeypatch, tmp_path, reason):
    model = install_model(monkeypatch, tmp_path, [{"status": "provider_response_rejected", "reason": reason}])
    task = task_factory()
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", comparator=never_compare)
    assert result["status"] == "generation_failed"
    assert result["stop_reason"] == reason
    assert model.calls == 1


@pytest.mark.parametrize("arm", ["full", "generic", "no_ast", "no_evidence", "no_feedback"])
def test_repeated_reviewed_candidate_stops_without_repeated_docker(task_factory, monkeypatch, tmp_path, arm):
    model = install_model(monkeypatch, tmp_path, ["candidate", "candidate"])
    task = task_factory(arm=arm)
    path = Path(task["task_path"])
    pending = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    comparisons = []

    def compare(*args, **kwargs):
        comparisons.append(kwargs)
        return {"check_group": "feedback", "status": "candidate_verified",
                "report_path": str(tmp_path / "comparison.json"),
                "stages": {"new_candidate": {"status": "passed"}}}

    result = tasks.advance_task(path, api_key="unused", comparator=compare, **approved(pending))
    assert result["status"] == "submitted"
    assert result["stop_reason"] == "repeated_candidate_no_change"
    assert result["candidate"]["reviewed"] is True
    assert result["duplicate_submission"]["sha256"] == result["candidate"]["sha256"]
    assert len(comparisons) == (0 if arm == "no_feedback" else 1)
    assert model.calls == 2
    assert result["final_evaluation"] == "not_run"


def test_out_of_range_read_is_recoverable_and_still_costs_a_call(task_factory, monkeypatch, tmp_path):
    invalid_range = {"type": "read_source", "path": "bumpversion/config.py",
                     "start_line": 1000, "end_line": 1001}
    model = install_model(monkeypatch, tmp_path, [
        {"status": "action_ready", "action": invalid_range}, "read", "finish",
    ])
    task = task_factory()
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", comparator=never_compare)
    assert result["status"] == "no_candidate"
    assert model.calls == result["budget"]["calls"] == 3
    error = model.contexts[1]["tool_results"][0]["result"]["error"]
    assert error["code"] == "read_range_out_of_bounds"
    assert error["available_lines"] > 0
    assert result["tool_results"][1]["result"]["path"] == "bumpversion/config.py"


def test_changed_implementation_refuses_resume_without_state_write(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, [])
    task = task_factory()
    path = Path(task["task_path"])
    before = path.read_bytes()
    changed = deepcopy(task["protocol"]["implementation"])
    changed["prompt_sha256"] = "0" * 64
    monkeypatch.setattr(tasks, "implementation_identity", lambda: changed)
    with pytest.raises(ValueError, match="implementation or prompt"):
        tasks.advance_task(path, api_key="unused", comparator=never_compare)
    assert path.read_bytes() == before
    assert not path.with_suffix(".lock").exists()
    assert model.calls == 0


def test_formal_task_rejects_unfrozen_protocol(task_factory):
    task = task_factory()
    protocol = deepcopy(task["protocol"])
    protocol["status"] = "development_calibration"
    with pytest.raises(ValueError, match="frozen protocol"):
        tasks.create_task(CASE, Path(task["work_root"]), protocol, arm="full", phase="development",
                          budget_path=Path(task["budget_path"]))


@pytest.mark.parametrize("key", ["", " \t", "secret\nvalue"])
def test_invalid_key_creates_no_reservation_or_attempt(task_factory, monkeypatch, tmp_path, key):
    model = install_model(monkeypatch, tmp_path, [])
    task = task_factory()
    path = Path(task["task_path"])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="provider key"):
        tasks.advance_task(path, api_key=key, comparator=never_compare)
    assert path.read_bytes() == before
    assert BudgetLedger(Path(task["budget_path"])).snapshot()["calls"] == 0
    assert model.calls == 0


def test_known_unsent_provider_preflight_failure_has_no_unknown_charge(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, [])
    task = task_factory()

    def reject_before_attempt(*args, **kwargs):
        raise ValueError("Frozen request rejected before provider attempt marker")

    monkeypatch.setattr(tasks, "complete_request", reject_before_attempt)
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", comparator=never_compare)
    assert result["status"] == "generation_failed"
    assert result["attempts"][0]["status"] == "not_sent"
    assert result["budget"]["estimated_or_reserved_usd"] == 0
    assert all(group["state"] != "unknown" for group in result["budget"]["groups"])
    assert model.calls == 0


def test_no_regression_still_reports_valid_public_feedback_without_claiming_repair():
    passing = {
        "status": "passed", "exit_code": 0, "nodeids": ["test_public.py::test_one"],
        "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
    }
    report = {"check_group": "feedback", "status": "no_regression_observed",
              "stages": {"old_original": deepcopy(passing), "new_candidate": deepcopy(passing)}}
    assert tasks._feedback(report, "a" * 64)["status"] == "passed"
    assert report["status"] == "no_regression_observed"
    report["stages"]["new_candidate"]["nodeids"] = ["test_public.py::different"]
    assert tasks._feedback(report, "a" * 64)["status"] == "failed"


def test_contract_rejection_is_recorded_without_docker_or_automatic_repair(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, ["candidate"])
    task = task_factory()
    path = Path(task["task_path"])
    pending = tasks.advance_task(path, api_key="unused", comparator=never_compare)
    rejected = tasks.advance_task(path, api_key="unused", comparator=never_compare,
                                  reject_reason="candidate_contract_rejected", **approved(pending))
    assert rejected["status"] == "candidate_rejected"
    assert rejected["candidate"]["reviewed"] is False
    assert rejected["review_history"][-1]["decision"] == "rejected"
    assert rejected["final_evaluation"] == "not_run"
    assert tasks.advance_task(path, api_key="unused", comparator=never_compare)["status"] == "candidate_rejected"
    assert model.calls == 1


def test_rejection_requires_exact_review_identity(task_factory, monkeypatch, tmp_path):
    model = install_model(monkeypatch, tmp_path, ["candidate"])
    task = task_factory()
    path = Path(task["task_path"])
    tasks.advance_task(path, api_key="unused", comparator=never_compare)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="exact-hash"):
        tasks.advance_task(path, api_key="unused", comparator=never_compare,
                           reject_reason="candidate_contract_rejected")
    assert path.read_bytes() == before
    assert model.calls == 1
