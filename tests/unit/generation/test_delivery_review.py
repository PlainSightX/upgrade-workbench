"""交付复核的真实请求/收据路径；合成回复不算模型能力证据。"""

import hashlib
import json
from pathlib import Path

import pytest
from test_contract_audit_feedback import NO_CONFLICT, audit, finish
from test_contract_coverage import (
    _copy_as_v2_case,
    _ledger,
    _provider_response,
    _review_candidate,
    _submit_candidate,
)

from upgrade_workbench import tasks
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.generation.delivery_review import (
    EVIDENCE_POLICY,
    POLICY,
    draft,
    independent_view,
)
from upgrade_workbench.generation.provider import _verify_payload

from .test_knowledge_transfer import CASE, OWNER, create, response
from .test_knowledge_transfer import exported as exported


@pytest.mark.parametrize("policy", [POLICY, EVIDENCE_POLICY])
def test_actual_draft_review_returns_to_solver_and_replays(tmp_path, policy):
    manifest = _copy_as_v2_case(tmp_path)
    budget = tmp_path / "budget.sqlite"
    _ledger(budget)
    task = tasks.create_operation(manifest, tmp_path / "work", budget_path=budget,
        seed_strategy="none", protocol_revision=6, max_calls=8, contract_audit_policy=policy,
        generation={"model": "owned-model", "endpoint": "https://provider.example/chat/completions",
                    "thinking_mode": "disabled", "max_output_tokens": 1024, "timeout_seconds": 10})
    task = _review_candidate(_submit_candidate(task, "delivery-fixture"), passed=True)
    roles = []

    def transport(request, **_):
        payload = json.loads(request.data)
        context = json.loads(payload["messages"][1]["content"])
        runtime = context["diagnostic_state"]
        if "read-only Contract Auditor" in payload["messages"][0]["content"]:
            roles.append("audit")
            expected = "Public development check passes." if roles.count("audit") == 1 else "The requested business explanation."
            assert runtime["delivery_review"]["draft"]["action"]["explanation"] == expected
            assert runtime["recent_actions"] == []
            assert "FULL business_contract" in payload["messages"][0]["content"]
            result = audit(bad=None) if roles.count("audit") == 1 else NO_CONFLICT
        else:
            roles.append("solver")
            assert "candidate_ready requires actual reviewed changes" in payload["messages"][0]["content"]
            if policy == EVIDENCE_POLICY:
                assert runtime["delivery_evidence"]["observation"]["kind"] == "public_checks"
                assert "complete pages" in payload["messages"][0]["content"]
            else:
                assert "delivery_evidence" not in runtime
            result = finish(task)
            if roles.count("solver") == 2:
                assert runtime["contract_audits"][0]["audit"]["questions"]
                result["action"]["explanation"] = "The requested business explanation."
        return _provider_response(result)

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert roles == ["solver", "audit", "solver", "audit"]
    assert result["status"] == "submitted"
    assert result["final_evaluation"] == "not_run"
    receipts = [json.loads(Path(row["receipt"]).read_bytes()) for row in result["attempts"]]
    for receipt in receipts:
        if receipt["status"] == "pending_review":
            continue  # 夹具的候选生成收据记录了输出候选；这里核对后续交付与审阅请求。
        _verify_payload(receipt, Path(receipt["request_path"]).read_bytes())
    first = Path(next(receipt["report_path"] for receipt in receipts if receipt["status"] == "agent_finished"))
    reference = {"path": str(first), "sha256": hashlib.sha256(first.read_bytes()).hexdigest()}
    snapshot = load_candidate(load_case(manifest), result["current_candidate"])
    assert draft(result, reference, snapshot)["action"]["type"] == "finish"
    with pytest.raises(ValueError, match="receipt changed"):
        draft(result, reference | {"sha256": "0" * 64}, snapshot)
    with pytest.raises(ValueError, match="this task and revision"):
        draft(result | {"task_id": "other-task"}, reference, snapshot)


def test_independent_view_hides_confirmation_but_preserves_claim_identity():
    claim = {"origin": {"kind": "imported"}, "topic_sha256": "hash", "interpretation_revision": "rev",
             "topic": {"explanation": "Claim to check"}, "review": "AUTHOR_CONFIRMED"}
    source = {"action": {"type": "read_source"}}
    runtime = {"delivery_review_policy": POLICY, "delivery_review": {"draft": {"action": "untrusted"}},
               "knowledge_review_binding": {"phase": "solve"}, "knowledge_review": "AUTHOR_CONFIRMED",
               "observations": [source, {"action": {"type": "complete_knowledge_review"}}],
               "topic_maintenance": {"current_revision": "rev", "topics": [claim]}}
    projected = independent_view(runtime)
    assert "AUTHOR_CONFIRMED" not in json.dumps(projected)
    assert projected["topic_maintenance"]["topics"][0]["topic_sha256"] == "hash"
    assert projected["observations"] == [source]
    assert runtime["knowledge_review"] == "AUTHOR_CONFIRMED"
    old = {"observations": []}
    assert independent_view(old) is old


@pytest.mark.parametrize("policy", [POLICY, EVIDENCE_POLICY])
def test_mixed_policy_requests_preserve_role_specific_finish_contract(exported, tmp_path, policy):
    from upgrade_workbench.diagnostics import freeze_context
    from upgrade_workbench.planning import prepare_case_proposal

    from .test_investigator_context import handoff
    from .test_knowledge_review import finish_review

    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v10", max_calls=14,
        contract_audit_policy=policy, investigator_policy="required_once",
        investigator_context_policy={"version": "source-first-v3"},
        investigator_max_calls=6, solver_reserved_calls=4,
        finish_policy="probe-lineage-v1", investigation_policy={
            "version": "investigation-budget-v1",
            "finish_limits": {"explanation_max_characters": 4000, "evidence_refs_max_items": 16}})
    roles = []

    def transport(request, **_):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        role = context["diagnostic_state"]["context_role"]
        roles.append(role)
        if role == "investigator":
            return response(handoff())
        if context["project_context"]["knowledge_review"]["phase"] == "review":
            return response(finish_review(context))
        return response({"type": "run_public_checks", "revision": context["candidate"]["revision"]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport,
                               execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review"
    assert roles == ["solver", "investigator", "solver"]
    # 完成初始复核和真实角色调度后，审阅者使用同一任务的混合配置。
    auditor = prepare_case_proposal(CASE, Path(task["work_root"]), public_source_ack=True,
        output_format="contract_audit", candidate_reference=None,
        task_context={"task_id": task["task_id"], "attempt_index": 4, "remaining_calls": 11},
        source_policy=task["protocol"]["source_policy"],
        diagnostic_context_reference=freeze_context(result, role="contract_auditor"),
        workflow_profile="workbench", **task["protocol"]["generation"])
    receipts = [json.loads(Path(row["receipt"]).read_bytes()) for row in result["attempts"]]
    for prepared in receipts + [auditor]:
        raw = Path(prepared["request_path"]).read_bytes()
        runtime = json.loads(json.loads(raw)["messages"][1]["content"])["diagnostic_state"]
        assert runtime["delivery_review_policy"] == policy
        if runtime["context_role"] == "investigator":
            assert "finish_requirements" not in runtime and "investigation_policy" not in runtime
            assert "finish_limits" not in prepared
            assert "delivery_evidence" not in runtime
        else:
            assert prepared["finish_limits"] == runtime["finish_requirements"]["format_limits"]
            assert prepared["finish_limits"]["explanation_max_characters"] == 4000
        _verify_payload(prepared, raw)
