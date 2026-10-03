"""报告必须来自完整冻结矩阵，不能导出密钥载荷或把全局费用冒充本批成本。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench import cli
from upgrade_workbench.batches import create_batch, finalize_batch
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.cases import load_case
from upgrade_workbench.reporting import export_batch_report
from upgrade_workbench.tasks import create_task, implementation_identity

CASE = Path(__file__).parents[2] / "cases/bump-my-version-0.5.0-r2/manifest.json"


@pytest.fixture
def completed_batch(tmp_path):
    budget = tmp_path / "budget.sqlite"
    ledger = BudgetLedger(budget, {
        "limit_usd": "1", "checkpoint_usd": "0.5", "calibration_usd": "0.2",
        "model": "offline-model", "input_per_million": "1", "output_per_million": "1",
        "pricing_source": "https://example.test/prices?private=DO_NOT_EXPORT", "pricing_checked_at": "2026-09-16",
        "quotas": {"calibration": 20, "development": 20, "holdout": 20}, "holdout_reserve_usd": "0.2",
    })
    protocol = {"protocol_revision": 2, "implementation": implementation_identity(), "max_calls": 3,
                "generation": {"model": "offline-model", "endpoint": "https://example.test/chat/completions",
                               "thinking_mode": "disabled", "max_output_tokens": 100, "timeout_seconds": 10},
                "internal_note": "DO_NOT_EXPORT"}
    tasks = [create_task(CASE, tmp_path / "work", protocol, arm=arm, phase="calibration", budget_path=budget)
             for arm in ("full", "generic", "no_ast")]
    batch_path = tmp_path / "batch.json"
    create_batch(tasks, protocol, batch_path)
    request_dir = tmp_path / "one-request"
    request_dir.mkdir()
    (request_dir / "request.json").write_bytes(b"{}")
    receipt = {
        "request_path": str(request_dir / "request.json"), "report_path": str(request_dir / "proposal.json"),
        "request_sha256": hashlib.sha256(b"{}").hexdigest(), "model": "offline-model", "max_output_tokens": 100,
        "case_fingerprint": tasks[0]["case_fingerprint"], "calls": 1, "status": "pending_review",
        "model_usage": {"input_tokens": 100, "output_tokens": 10}, "duration_seconds": 2.5,
        "raw_response": "DO_NOT_EXPORT", "api_key": "DO_NOT_EXPORT", "summary": "DO_NOT_EXPORT",
    }
    reservation = ledger.reserve(receipt, "calibration")
    ledger.settle(reservation["request_id"], receipt["model_usage"])
    Path(receipt["report_path"]).write_text(json.dumps(receipt), encoding="utf-8")
    tasks[0]["attempts"] = [{"receipt": receipt["report_path"], **reservation}]
    for index, task in enumerate(tasks):
        task["status"] = "submitted" if index == 0 else "no_candidate"
        if index in (0, 2):
            patch = Path(task["task_path"]).with_name("candidate.patch")
            patch.write_bytes(b"offline reviewed patch\n")
            digest = hashlib.sha256(patch.read_bytes()).hexdigest()
            task["candidate"] = {"patch_path": str(patch), "sha256": digest, "reviewed": index == 0}
            task["review_history"] = [{"sha256": digest, "reviewer": "offline-review", "note": "DO_NOT_EXPORT"}]
            if index == 2:
                task["status"] = "candidate_rejected"
                task["stop_reason"] = "candidate_contract_rejected"
                task["candidate"]["review_decision"] = "rejected"
                task["review_history"][0].update(decision="rejected", reason="candidate_contract_rejected")
        Path(task["task_path"]).write_text(json.dumps(task), encoding="utf-8")

    def comparator(manifest, work_root, **kwargs):
        directory = work_root / "comparison"
        directory.mkdir(parents=True)
        report = {"status": "candidate_verified", "check_group": "all", "duration_seconds": 3.5,
                  "case_fingerprint": load_case(manifest).fingerprint,
                  "candidate": {"supplied_sha256": hashlib.sha256(kwargs["candidate_patch"].read_bytes()).hexdigest()},
                  "report_path": str(directory / "report.json"), "raw_logs": "DO_NOT_EXPORT"}
        Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        return report

    finalize_batch(batch_path, comparator=comparator)
    # 后续无关账本费用不得污染已冻结批次的归因。
    other_dir = tmp_path / "another-request"
    other_dir.mkdir()
    (other_dir / "request.json").write_bytes(b"{}")
    other = receipt | {"request_path": str(other_dir / "request.json"), "report_path": str(other_dir / "proposal.json")}
    row = ledger.reserve(other, "calibration")
    ledger.settle(row["request_id"], {"input_tokens": 1000, "output_tokens": 50})
    return batch_path, tasks, tmp_path


def test_export_preserves_all_results_cost_scope_and_reproducible_reviewed_patch(completed_batch):
    path, tasks, root = completed_batch
    result = export_batch_report(path, root / "export", evidence_root=root)
    report = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    assert report["formal_matrix"] is False
    assert report["totals"]["registered_tasks"] == 3
    assert report["totals"]["succeeded_tasks"] == 1
    assert report["totals"]["estimated_or_reserved_usd"] == pytest.approx(0.000110)
    assert report["totals"]["input_tokens"] == 100
    assert report["totals"]["model_calls_attempted"] == 1
    assert report["totals"]["provider_duration_seconds"] == 2.5
    assert report["totals"]["verification_duration_seconds"] == 3.5
    assert report["failure_groups"] == {"no_candidate": 1, "candidate_contract_rejected": 1}
    assert result["reviewed_patches"] == 1
    copied = root / "export" / report["tasks"][0]["candidate"]["exported_path"]
    assert copied.read_bytes() == Path(tasks[0]["candidate"]["patch_path"]).read_bytes()
    assert report["tasks"][2]["candidate"] is None
    assert report["tasks"][2]["rejected_candidate"]
    assert not any("DO_NOT_EXPORT" in p.read_text(encoding="utf-8") for p in (root / "export").rglob("*") if p.is_file())
    markdown = Path(result["markdown_path"]).read_text(encoding="utf-8")
    assert "--candidate '<导出包>/candidates/" in markdown
    assert "不是供应商账单" in markdown
    commands = [line for line in markdown.splitlines() if line.startswith("uv ")]
    assert len(commands) == result["reviewed_patches"]
    assert all(line.startswith("uv run --no-sync upgrade-workbench verify ") for line in commands)
    assert all("--candidate-origin agent_candidate --check-group all" in line for line in commands)


def test_report_only_export_does_not_copy_patch(completed_batch):
    path, _, root = completed_batch
    result = export_batch_report(path, root / "export", evidence_root=root, include_reviewed_patches=False)
    assert result["reviewed_patches"] == 0
    assert sorted(p.name for p in (root / "export").iterdir()) == ["report.json", "report.md"]


@pytest.mark.parametrize("tamper", ["protocol", "candidate", "receipt", "phase", "missing_task"])
def test_tampered_or_incomplete_evidence_is_rejected_before_export(completed_batch, tamper):
    path, tasks, root = completed_batch
    if tamper == "candidate":
        Path(tasks[0]["candidate"]["patch_path"]).write_bytes(b"changed")
    elif tamper == "receipt":
        Path(tasks[0]["attempts"][0]["receipt"]).write_text("{}")
    else:
        batch = json.loads(path.read_text())
        if tamper == "protocol":
            batch["registration"]["protocol"]["max_calls"] = 30
        elif tamper == "phase":
            batch["phase"] = "holdout"
        else:
            batch["registration"]["tasks"].pop()
        path.write_text(json.dumps(batch), encoding="utf-8")
    with pytest.raises(ValueError):
        export_batch_report(path, root / "export", evidence_root=root)
    assert not (root / "export").exists()


def test_cli_export_is_read_only_and_does_not_initialize_provider(completed_batch, monkeypatch, capsys):
    path, _, root = completed_batch
    monkeypatch.setattr(cli, "advance_task", lambda *a, **kw: pytest.fail("No paid task"))
    monkeypatch.setattr(cli, "finalize_batch", lambda *a, **kw: pytest.fail("No scoring"))
    assert cli.main(["report-export", str(path), "--output", str(root / "export")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "report_exported"


def test_cli_forwards_explicit_candidate_rejection(monkeypatch, capsys):
    captured = {}

    def reject(path, **kwargs):
        captured.update(kwargs)
        return {"task_id": "task1", "task_path": str(path), "case_id": "case1", "arm": "full",
                "phase": "calibration", "status": "candidate_rejected", "attempts": []}

    monkeypatch.setattr(cli, "advance_task", reject)
    assert cli.main(["continue-task", "task.json", "--review-sha256", "a" * 64,
                     "--reviewer", "reviewer", "--review-note", "rejected",
                     "--reject-reason", "candidate_contract_rejected"]) == 2
    assert captured["reject_reason"] == "candidate_contract_rejected"
    assert json.loads(capsys.readouterr().out)["status"] == "candidate_rejected"
