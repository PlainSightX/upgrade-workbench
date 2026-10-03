"""核对真实请求消费、默认行为和发送前身份边界，不把提示存在当作质量收益。"""

import json
from pathlib import Path

import pytest

from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import context_from_reference, freeze_context
from upgrade_workbench.generation.provider import _load_prepared
from upgrade_workbench.generation.request import (
    ProposalInputError,
    _digest,
    _instructions,
    _json_bytes,
)
from upgrade_workbench.generation.semantic_risk import INSTRUCTIONS, POLICY
from upgrade_workbench.planning import prepare_case_proposal
from upgrade_workbench.reporting import _public_protocol
from upgrade_workbench.tasks import (
    _operation_configuration,
    _require_current_protocol,
    create_operation,
)

CASE = Path(__file__).resolve().parents[3] / "cases/csvkit-csvsql-sqlalchemy-2-a3/manifest.json"


def profile(**updates):
    return {"generation": {"model": "owned-model", "endpoint": "https://example.test/chat/completions",
                           "max_output_tokens": 1024, "timeout_seconds": 10, "thinking_mode": "disabled"},
            "seed_strategy": "none", "protocol_revision": 4, **updates}


@pytest.mark.parametrize("revision,format_name", [(4, "diagnostic_actions"), (5, "contract_actions"),
                                                 (6, "protocol_v6_actions")])
def test_policy_is_consumed_from_frozen_task_through_request(tmp_path, revision, format_name):
    BudgetLedger(tmp_path / "ledger.sqlite", {
        "mode": "user_managed", "limit_usd": None, "model": "owned-model",
        "input_per_million": "1", "output_per_million": "1",
        "pricing_source": "https://example.test/pricing", "pricing_checked_at": "2026-10-03",
    })
    task = create_operation(CASE, tmp_path / "work", budget_path=tmp_path / "ledger.sqlite",
                            **profile(semantic_risk_policy=POLICY, protocol_revision=revision))
    case = load_case(CASE)
    snapshot = load_candidate(case)
    reference = freeze_context(task)
    assert context_from_reference(reference, case, snapshot)["semantic_risk_policy"] == POLICY
    receipt = prepare_case_proposal(CASE, tmp_path / "request", public_source_ack=True,
                                   output_format=format_name, diagnostic_context_reference=reference,
                                   candidate_reference=snapshot.reference, max_source_bytes=1600000,
                                   max_request_bytes=850000, source_policy=task["protocol"]["source_policy"],
                                   **profile()["generation"])
    _load_prepared(receipt)
    payload = json.loads(Path(receipt["request_path"]).read_bytes())
    assert INSTRUCTIONS in payload["messages"][0]["content"]
    context = json.loads(payload["messages"][1]["content"])
    assert context["diagnostic_state"]["semantic_risk_policy"] == POLICY
    assert receipt["semantic_risk_policy"] == POLICY
    assert "checks/acceptance" not in json.dumps(context)
    # 改写请求收据不能从已冻结的策略中移除自查。
    receipt.pop("semantic_risk_policy")
    Path(receipt["report_path"]).write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ProposalInputError, match="frozen generation protocol"):
        _load_prepared(receipt)
    payload["messages"][0]["content"] = _instructions(format_name)
    forged = _json_bytes(payload)
    Path(receipt["request_path"]).write_bytes(forged)
    receipt["request_sha256"] = _digest(forged)
    Path(receipt["report_path"]).write_bytes(_json_bytes(receipt))
    with pytest.raises(ProposalInputError, match="Semantic risk policy differs"):
        _load_prepared(receipt)
    assert _public_protocol(task["protocol"])["semantic_risk_policy"] == POLICY


def test_default_has_no_policy_and_role_permissions_are_unchanged():
    protocol, _ = _operation_configuration(CASE, **profile())
    assert "semantic_risk_policy" not in protocol
    assert "semantic_risk_policy" not in _public_protocol(protocol)
    _require_current_protocol(protocol)
    assert INSTRUCTIONS not in _instructions("diagnostic_actions")
    assert INSTRUCTIONS not in _instructions("investigator_actions", semantic_risk_policy=POLICY)
    assert INSTRUCTIONS not in _instructions("contract_audit", semantic_risk_policy=POLICY)
    assert "exec_driver_sql" not in INSTRUCTIONS and "colon" not in INSTRUCTIONS


def test_isolated_investigator_keeps_policy_identity_without_solver_instructions():
    from upgrade_workbench.generation.investigator_context import POLICY as SOURCE_POLICY
    from upgrade_workbench.generation.investigator_context import project

    runtime = {"investigator_context_policy": SOURCE_POLICY, "context_role": "investigator",
               "semantic_risk_policy": POLICY, "observations": [], "observation_index": [],
               "investigator_source_observation_ids": [], "source_state": {"revision": "a" * 64}}
    view = project(runtime, "investigator_actions")
    assert view["semantic_risk_policy"] == POLICY
    assert INSTRUCTIONS not in _instructions("investigator_actions", semantic_risk_policy=view["semantic_risk_policy"])


@pytest.mark.parametrize("value,revision", [("unknown", 4), (POLICY, 3)])
def test_invalid_configuration_is_rejected(value, revision):
    with pytest.raises(ValueError):
        _operation_configuration(CASE, **profile(semantic_risk_policy=value, protocol_revision=revision))
