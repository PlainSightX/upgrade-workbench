"""连接生命周期线索必须随当前源码重算，且保持消融和原文边界。"""

import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench.analysis.lifecycle import RULE
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.diagnostics import freeze_context
from upgrade_workbench.generation.provider import _load_prepared
from upgrade_workbench.generation.request import _located_findings
from upgrade_workbench.planning import prepare_case_proposal
from upgrade_workbench.tasks import create_operation

CASE = Path(__file__).resolve().parents[3] / "cases/optuna-3.1.0-pg/manifest.json"


def prepare(folder, arm):
    return prepare_case_proposal(CASE, folder, model="test-model",
        endpoint="https://example.test/chat/completions", max_output_tokens=1024,
        timeout_seconds=10, public_source_ack=True, output_format="candidate_actions",
        experiment_arm=arm, max_source_bytes=1600000, max_request_bytes=2400000)


def content(receipt):
    return json.loads(json.loads(Path(receipt["request_path"]).read_bytes())["messages"][1]["content"])


def test_real_snapshot_risks_reach_request_with_scoped_ownership_and_bound_original(tmp_path):
    receipt = prepare(tmp_path / "full", "full")
    _load_prepared(receipt)
    context = content(receipt)
    findings = [row for row in context["potential_impacts"] if row["rule_id"] == RULE]
    assert len(findings) == 3
    assert {row["lifecycle"]["cleanup"] for row in findings} == {"not_established", "transaction_scope_only"}
    assert all(row["lifecycle"]["engine_basis"] == "member_annotation" for row in findings)
    assert all("not a proven leak" in row["review_question"] for row in findings)
    assert all(row["path"] == "optuna/storages/_rdb/storage.py" for row in findings)
    assert {row["line"] for row in findings} == {1084, 1101, 1141}
    anchors = {entry["evidence_key"] for entry in context["version_evidence"]}
    assert {key for row in findings for key in row["evidence_keys"]} <= anchors
    assert not any("checks/" in row["path"] for row in context["source_files"])
    assert "test_constraint_failure" not in json.dumps(context)
    assert receipt["calls"] == 0 and receipt["analysis_findings_dropped"] == 0
    assert not Path(receipt["report_path"]).with_name("attempt.json").exists()
    without_ast = prepare(tmp_path / "without-ast", "no_ast")
    assert content(without_ast)["potential_impacts"] == []
    assert content(without_ast)["version_evidence"] == context["version_evidence"]


@pytest.mark.parametrize("replacement", [{"engine_basis": "invented"}, {"engine_line": 9999},
    {"engine_line": True}, {"ownership": "proven_leak"}, {"cleanup": "passed"}, {"scope": "all_paths"}])
def test_projection_rejects_unvalidated_lifecycle_fields(replacement):
    finding = {"file": "source/app.py", "line": 2, "symbol": "engine.connect", "rule": RULE,
        "status": "potential_impact", "evidence_key": "sqlalchemy-v2-connectionless",
        "lifecycle": {"engine_basis": "declared_engine", "engine_line": 1, "ownership": "local",
                      "cleanup": "not_established", "scope": "local_syntax_not_all_paths_or_proven_version_regression"}}
    source = "def work(engine):\n    engine.connect()\n"
    blocks = [{"path": "app.py", "text": source, "sha256": hashlib.sha256(source.encode()).hexdigest()}]
    evidence = [{"evidence_key": "sqlalchemy-v2-connectionless"}]
    assert _located_findings({"findings": [finding]}, blocks, evidence)[1] == 0
    finding["lifecycle"].update(replacement)
    assert _located_findings({"findings": [finding]}, blocks, evidence) == ([], 1)


def test_protocol4_focused_request_keeps_risk_source_and_uses_no_provider(tmp_path):
    ledger = tmp_path / "budget.sqlite3"
    BudgetLedger(ledger, {"mode": "user_managed", "limit_usd": None, "model": "test-model",
        "input_per_million": "1", "output_per_million": "1", "pricing_source": "https://example.test/pricing",
        "pricing_checked_at": "2026-09-18"})
    task = create_operation(CASE, tmp_path / "work", budget_path=ledger, seed_strategy="none",
        protocol_revision=4, generation={"model": "test-model", "endpoint": "https://example.test/chat/completions",
        "thinking_mode": "disabled", "max_output_tokens": 1024, "timeout_seconds": 10,
        "max_source_bytes": 1600000, "max_request_bytes": 384000},
        source_policy={"mode": "focused", "body_bytes": 96000, "inventory_bytes": 64000})
    receipt = prepare_case_proposal(CASE, tmp_path / "work", public_source_ack=True,
        output_format="diagnostic_actions", candidate_reference=task["current_candidate"],
        diagnostic_context_reference=freeze_context(task), source_policy=task["protocol"]["source_policy"],
        **task["protocol"]["generation"])
    _load_prepared(receipt)
    context = content(receipt)
    for finding in context["potential_impacts"]:
        assert any(row["path"] == finding["path"] and row["start_line"] <= finding["line"] <= row["end_line"]
                   for row in context["source_selection"]["coverage"])
    assert len(context["potential_impacts"]) == 3
    assert BudgetLedger(ledger).snapshot()["calls"] == 0
    assert task["attempts"] == []
