"""离线检查先冻结全矩阵再揭示结果；伪执行器不产生迁移效果证据。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench.batches import create_batch, finalize_batch
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.cases import load_case
from upgrade_workbench.tasks import create_task, implementation_identity

CASE = Path(__file__).parents[2] / "cases/bump-my-version-0.5.0-r2/manifest.json"


def save(task):
    Path(task["task_path"]).write_text(json.dumps(task), encoding="utf-8")


@pytest.fixture
def batch_factory(tmp_path):
    budget = tmp_path / "budget.sqlite"
    BudgetLedger(budget, {
        "limit_usd": "1", "checkpoint_usd": "0.5", "calibration_usd": "0.2",
        "model": "offline-model", "input_per_million": "1", "output_per_million": "1",
        "pricing_source": "https://example.test/prices", "pricing_checked_at": "2026-09-16",
        "quotas": {"calibration": 20, "development": 20, "holdout": 20},
        "holdout_reserve_usd": "0.2",
    })
    protocol = {"status": "frozen", "protocol_revision": 2, "implementation": implementation_identity(), "max_calls": 3, "generation": {
        "model": "offline-model", "endpoint": "https://example.test/chat/completions",
        "thinking_mode": "disabled", "max_output_tokens": 100, "timeout_seconds": 10,
    }}

    def create(*, repetitions=1, phase="development"):
        tasks = [create_task(CASE, tmp_path / "work", protocol, arm=arm, phase=phase,
                             budget_path=budget, repetition=repetition)
                 for arm in ("generic", "full") for repetition in range(1, repetitions + 1)]
        path = tmp_path / "batch.json"
        batch = create_batch(tasks, protocol, path)
        return tasks, batch, path

    return create


def terminal(task, *, candidate=True, reviewed=True, status="submitted"):
    task["status"] = status
    if candidate:
        patch = Path(task["task_path"]).with_name("candidate.patch")
        patch.write_bytes(b"offline candidate, never execute\n")
        digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        task["candidate"] = {"patch_path": str(patch), "sha256": digest, "reviewed": reviewed}
        task["review_history"] = [{"sha256": digest, "reviewer": "offline-tester", "note": "fixture only"}]
    save(task)


class Comparator:
    def __init__(self, batch_path, statuses=None, interrupt_call=None):
        self.batch_path = batch_path
        self.calls = []
        self.statuses = list(statuses or [])
        self.interrupt_call = interrupt_call

    def __call__(self, manifest, work_root, **kwargs):
        self.calls.append(kwargs)
        batch = json.loads(self.batch_path.read_text())
        frozen = json.loads(Path(batch["candidates_path"]).read_text())
        assert len(frozen["tasks"]) == len(batch["registration"]["tasks"])
        assert batch["final_evaluation_disclosed"] is False
        assert kwargs["check_group"] == "all"
        for task in frozen["tasks"]:
            if task["candidate"]:
                assert Path(task["candidate"]["patch_path"]).is_file()
        if self.interrupt_call == len(self.calls):
            raise KeyboardInterrupt()
        status = self.statuses.pop(0) if self.statuses else "candidate_verified"
        directory = work_root / str(len(self.calls))
        directory.mkdir(parents=True, exist_ok=True)
        report = {"status": status, "check_group": "all", "case_fingerprint": load_case(manifest).fingerprint,
                  "candidate": {"supplied_sha256": hashlib.sha256(kwargs["candidate_patch"].read_bytes()).hexdigest()},
                  "report_path": str(directory / "report.json")}
        Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        return report


@pytest.mark.parametrize("pending", ["ready", "pending_review", "calling_model"])
def test_one_pending_task_blocks_all_final_evaluation(batch_factory, pending):
    tasks, _, path = batch_factory()
    terminal(tasks[0])
    tasks[1]["status"] = pending
    save(tasks[1])
    comparator = Comparator(path)
    with pytest.raises(ValueError, match="Every preregistered task"):
        finalize_batch(path, comparator=comparator)
    assert not comparator.calls
    assert not path.with_name("batch-artifacts").exists()


def test_unreviewed_last_candidate_blocks_all_scoring(batch_factory):
    tasks, _, path = batch_factory()
    terminal(tasks[0])
    terminal(tasks[1], reviewed=False)
    comparator = Comparator(path)
    with pytest.raises(ValueError, match="hash-bound review"):
        finalize_batch(path, comparator=comparator)
    assert not comparator.calls


def test_terminal_string_cannot_override_active_or_unreconciled_task_lock(batch_factory):
    tasks, _, path = batch_factory()
    for task in tasks:
        terminal(task)
    Path(tasks[1]["task_path"]).with_suffix(".lock").write_text(tasks[1]["task_id"])
    comparator = Comparator(path)
    with pytest.raises(ValueError, match="execution lock"):
        finalize_batch(path, comparator=comparator)
    assert not comparator.calls
    assert not path.with_name("batch-artifacts").exists()
    assert json.loads(path.read_text())["final_evaluation_disclosed"] is False


def test_candidate_freeze_rechecks_tasks_before_revealing_any_score(batch_factory, monkeypatch):
    from upgrade_workbench import batches

    tasks, _, path = batch_factory()
    for task in tasks:
        terminal(task)
    original = batches._save

    def change_during_snapshot(value, target):
        original(value, target)
        if target.name == "task-0000.json":
            tasks[1]["status"] = "ready"
            save(tasks[1])

    monkeypatch.setattr(batches, "_save", change_during_snapshot)
    comparator = Comparator(path)
    with pytest.raises(ValueError, match="changed while freezing"):
        finalize_batch(path, comparator=comparator)
    assert not comparator.calls
    assert json.loads(path.read_text())["final_evaluation_disclosed"] is False


def test_freezes_all_candidates_before_first_comparison_and_keeps_failures(batch_factory):
    tasks, batch, path = batch_factory(repetitions=2)
    terminal(tasks[0])
    terminal(tasks[1])
    terminal(tasks[2], candidate=False, status="budget_exhausted")
    terminal(tasks[3], candidate=False, status="no_candidate")
    comparator = Comparator(path, ["candidate_verified", "candidate_not_accepted"])
    report = finalize_batch(path, comparator=comparator)
    assert len(comparator.calls) == 2
    assert report["registered_tasks"] == 4
    assert report["succeeded_tasks"] == 1
    assert report["projects"] == 1
    assert report["groups"][0]["registered"] == 2
    assert {item["failure_category"] for item in report["results"]} >= {"budget_exhausted", "no_candidate"}
    assert report["solver_feedback"] is False
    assert report["actual_billing"] == "not_claimed"
    assert len(report["budgets"]) == 1
    assert len(report["results"]) == len(batch["registration"]["tasks"])
    markdown = Path(report["markdown_path"]).read_text(encoding="utf-8")
    assert "项目数与测试数不同" in markdown
    assert "batch-finalize" in markdown
    assert all(json.loads(Path(task["task_path"]).read_text())["final_evaluation"] == "not_run" for task in tasks)
    again = finalize_batch(path, comparator=lambda *a, **kw: pytest.fail("A completed report must not rerun"))
    assert again == report


def test_terminal_failure_with_last_reviewed_candidate_is_evaluated_and_status_retained(batch_factory):
    tasks, _, path = batch_factory()
    terminal(tasks[0], status="budget_exhausted")
    terminal(tasks[1], candidate=False, status="execution_incomplete")
    report = finalize_batch(path, comparator=Comparator(path))
    assert report["succeeded_tasks"] == 1
    assert report["results"][0]["task_status"] == "budget_exhausted"
    assert report["registered_tasks"] == 2


def test_interrupted_evaluation_resumes_only_the_frozen_remaining_tasks(batch_factory):
    tasks, _, path = batch_factory()
    for task in tasks:
        terminal(task)
    comparator = Comparator(path, interrupt_call=2)
    with pytest.raises(KeyboardInterrupt):
        finalize_batch(path, comparator=comparator)
    batch = json.loads(path.read_text())
    assert batch["status"] == "candidates_frozen"
    assert batch["final_evaluation_disclosed"] is False
    assert not path.with_suffix(".lock").exists()
    comparator.interrupt_call = None
    report = finalize_batch(path, comparator=comparator)
    assert len(comparator.calls) == 3
    assert report["succeeded_tasks"] == 2


@pytest.mark.parametrize("change", ["identity", "candidate", "review", "protocol"])
def test_changed_frozen_inputs_cannot_be_substituted(batch_factory, change):
    tasks, _, path = batch_factory()
    for task in tasks:
        terminal(task)
    if change == "identity":
        tasks[1]["repetition"] = 99
    elif change == "candidate":
        Path(tasks[1]["candidate"]["patch_path"]).write_bytes(b"different patch")
    elif change == "review":
        tasks[1]["review_history"] = []
    else:
        tasks[1]["protocol_sha256"] = "0" * 64
    save(tasks[1])
    comparator = Comparator(path)
    with pytest.raises(ValueError):
        finalize_batch(path, comparator=comparator)
    assert not comparator.calls


def test_duplicate_task_and_posthoc_registration_are_rejected(batch_factory, tmp_path):
    tasks, _, _ = batch_factory()
    with pytest.raises(ValueError, match="Duplicate"):
        create_batch([tasks[0], tasks[0]], tasks[0]["protocol"], tmp_path / "duplicate.json")
    tasks[0]["attempts"] = [{"receipt": "already-called"}]
    save(tasks[0])
    with pytest.raises(ValueError, match="without attempts"):
        create_batch([tasks[0]], tasks[0]["protocol"], tmp_path / "posthoc.json")


def test_calibration_report_is_not_a_formal_matrix(batch_factory):
    tasks, batch, path = batch_factory(phase="calibration")
    for task in tasks:
        terminal(task, candidate=False, status="no_candidate")
    assert batch["formal_matrix"] is False
    report = finalize_batch(path, comparator=lambda *a, **kw: pytest.fail("No candidates"))
    assert report["formal_matrix"] is False
    assert report["registered_tasks"] == 2


def test_receipt_usage_is_reported_without_exporting_provider_content(batch_factory):
    tasks, _, path = batch_factory()
    for task in tasks:
        terminal(task, candidate=False, status="no_candidate")
    receipt_path = Path(tasks[0]["task_path"]).with_name("proposal.json")
    receipt_path.write_text(json.dumps({
        "case_fingerprint": tasks[0]["case_fingerprint"], "model": "offline-model",
        "model_usage": {"input_tokens": 20, "output_tokens": 10}, "duration_seconds": 1.5,
        "raw_content": "RAW_PROVIDER_NOT_FOR_EXPORT",
    }), encoding="utf-8")
    tasks[0]["attempts"] = [{"receipt": str(receipt_path)}]
    save(tasks[0])
    report = finalize_batch(path, comparator=lambda *a, **kw: pytest.fail("No candidate"))
    assert report["results"][0]["usage"]["reported_input_tokens"] == 20
    assert report["results"][0]["usage"]["provider_duration_seconds"] == 1.5
    assert "RAW_PROVIDER_NOT_FOR_EXPORT" not in json.dumps(report)


def test_modified_final_result_is_not_trusted_without_original_evidence(batch_factory):
    tasks, _, path = batch_factory()
    for task in tasks:
        terminal(task)
    report = finalize_batch(path, comparator=Comparator(path, ["candidate_not_accepted", "candidate_verified"]))
    result_path = Path(report["report_path"]).with_name("result-0000.json")
    result = json.loads(result_path.read_text())
    result["result"] = "solved"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="comparison evidence changed"):
        finalize_batch(path, comparator=lambda *a, **kw: pytest.fail("No rerun"))


def test_rejected_candidate_keeps_failure_denominator_without_running(batch_factory):
    tasks, _, path = batch_factory()
    terminal(tasks[0], candidate=False, status="no_candidate")
    terminal(tasks[1], reviewed=False, status="candidate_rejected")
    tasks[1]["candidate"]["review_decision"] = "rejected"
    tasks[1]["stop_reason"] = "candidate_contract_rejected"
    tasks[1]["review_history"][0].update(decision="rejected", reason="candidate_contract_rejected")
    save(tasks[1])
    report = finalize_batch(path, comparator=lambda *a, **kw: pytest.fail("Rejected candidate must not execute"))
    assert report["registered_tasks"] == 2
    assert report["succeeded_tasks"] == 0
    assert report["results"][1]["failure_category"] == "candidate_contract_rejected"
    frozen = json.loads(Path(json.loads(path.read_text())["candidates_path"]).read_text())
    assert frozen["tasks"][1]["candidate"] is None
    assert Path(frozen["tasks"][1]["rejected_candidate"]["patch_path"]).exists()
