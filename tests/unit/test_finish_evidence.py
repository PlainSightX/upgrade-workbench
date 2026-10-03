"""提交修订链与真实调用边界；合成执行不计为模型或迁移成绩。"""

import copy
import json
from pathlib import Path

import pytest
from test_contract_coverage import (
    _copy_as_v2_case,
    _create_protocol_5_task,
    _create_protocol_6_task,
    _replace_catalog,
)
from test_diagnostic_operations import measured_run, public_run, review, run_action

from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.cases.requirements import ContractRequirement
from upgrade_workbench.diagnostic_state import probe_states
from upgrade_workbench.diagnostics import (
    _contract_observation_states,
    accept_action,
    context_from_reference,
    freeze_context,
)
from upgrade_workbench.finish_evidence import POLICY, project, require_submission_evidence
from upgrade_workbench.generation import protocol_v5, protocol_v6
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.reporting import export_task_report
from upgrade_workbench.tasks import finalize_task, inspect_task

ORACLE = {"requirement": "resources returned", "subject": "count", "basis": "absolute",
          "operator": "eq", "expected": 0, "exercise": "close"}


def chain():
    probes = [{"id": identity, "requirement_id": "resource.count", "oracle": dict(ORACLE),
               **({"parent_probe_id": parent} if parent else {})}
              for identity, parent in (("a", None), ("b", "a"), ("c", "b"))]
    reviews = [{"probe_id": probe["id"], "revision": "current", "decision": "accept"} for probe in probes]
    rows = [{"id": str(index), "kind": "probe", "revision": "current", "result": {
        "status": "passed" if index == 3 else "failed", "assessment": {
            "probe_id": probe["id"], "conclusion": "no_counterexample_observed" if index == 3 else "inconclusive"}}}
        for index, probe in enumerate(probes, 1)]
    return probes, reviews, rows


def view(probes, reviews, rows):
    return project(probe_states(probes, reviews, rows, "current", {"max_revisions_per_probe": 2}), rows, "current")


def test_valid_revision_chain_retires_only_separate_history_citations():
    probes, reviews, rows = chain()
    before = copy.deepcopy((probes, reviews, rows))
    result = view(probes, reviews, rows)
    assert result["required_evidence_refs"] == result["blocking_counterexample_refs"] == []
    assert [row["resolved_by"] for row in result["probe_history"]] == ["observation:3", "observation:3", None]
    assert result["probe_history"][0]["conclusion"] == "inconclusive"
    assert (probes, reviews, rows) == before
    require_submission_evidence({"reason": "candidate_ready", "evidence_refs": []}, result)


@pytest.mark.parametrize("defect", ["unrun", "unreviewed", "rejected", "stale_review", "stale_run",
                                     "failed", "inconclusive", "narrowed", "changed_predicate",
                                     "changed_requirement", "changed_middle_requirement"])
def test_revision_does_not_erase_unresolved_scope(defect):
    probes, reviews, rows = chain()
    if defect == "unrun":
        rows.pop()
    elif defect == "unreviewed":
        reviews.pop()
    elif defect == "rejected":
        reviews[-1]["decision"] = "reject"
    elif defect == "stale_review":
        reviews[-1]["revision"] = "old"
    elif defect == "stale_run":
        rows[-1]["revision"] = "old"
    elif defect == "failed":
        rows[-1]["result"]["status"] = "failed"
    elif defect == "inconclusive":
        rows[-1]["result"]["assessment"]["conclusion"] = "inconclusive"
    elif defect == "narrowed":
        probes[-1]["oracle"] = None
        rows[-1]["result"]["assessment"]["conclusion"] = "observation_only"
    elif defect == "changed_predicate":
        probes[-1]["oracle"]["expected"] = 10
    elif defect == "changed_requirement":
        probes[-1]["requirement_id"] = "other.contract"
    else:
        probes[1]["requirement_id"] = "other.contract"
    result = view(probes, reviews, rows)
    assert "observation:1" in result["required_evidence_refs"]
    with pytest.raises(ValueError, match="Missing evidence_refs"):
        require_submission_evidence({"reason": "candidate_ready", "evidence_refs": []}, result)


@pytest.mark.parametrize("revision", ["old", "current"])
def test_successful_child_cannot_clear_measured_counterexample(revision):
    probes, reviews, rows = chain()
    rows[0]["revision"] = revision
    rows[0]["result"]["assessment"]["conclusion"] = "counterexample_observed"
    result = view(probes, reviews, rows)
    assert result["blocking_counterexample_refs"] == ["observation:1"]
    with pytest.raises(ValueError, match="counterexample"):
        require_submission_evidence({"reason": "candidate_ready", "evidence_refs": ["observation:1"]}, result)


def test_same_probe_current_remeasurement_clears_only_stale_counterexample():
    probes, reviews, rows = chain()
    rows[0]["revision"] = "old"
    rows[0]["result"]["assessment"]["conclusion"] = "counterexample_observed"
    rows.append({"id": "4", "revision": "current", "kind": "probe", "result": {
        "status": "passed", "assessment": {"probe_id": "a", "conclusion": "no_counterexample_observed"}}})
    assert not view(probes, reviews, rows)["blocking_counterexample_refs"]
    rows[0]["revision"] = "current"
    assert view(probes, reviews, rows)["blocking_counterexample_refs"]


@pytest.mark.parametrize("profile", ["p5", "workbench", "simple_tools"])
def test_exact_limits_match_all_new_solver_parsers(profile):
    action = {"type": "finish", "reason": "unresolved", "explanation": "x" * 2000, "evidence_refs": []}
    if profile != "simple_tools":
        action["contract_coverage"] = [{"requirement_id": "resource.count", "state": "unobserved",
                                        "evidence_refs": [], "tool_limitation": None}]
    validate = protocol_v5.validate if profile == "p5" else lambda *args: protocol_v6.validate(*args, workflow_profile=profile)
    assert validate(None, action, None) == action
    with pytest.raises(ValueError, match="maximum 2000 characters"):
        validate(None, action | {"explanation": "x" * 2001}, None)


@pytest.mark.parametrize("profile", ["p5", "workbench", "simple_tools"])
def test_ordinary_request_discloses_limits_and_provider_rebuilds_them(tmp_path, profile):
    manifest = _copy_as_v2_case(tmp_path)
    task = (_create_protocol_5_task(manifest, tmp_path, contract_audit_policy="disabled") if profile == "p5"
            else _create_protocol_6_task(manifest, tmp_path, workflow_profile=profile))
    snapshot = load_candidate(load_case(manifest))
    task = run_action(task, {"type": "run_public_checks", "revision": snapshot.revision})
    assert task["status"] == "pending_diagnostic_review"
    receipt = json.loads(Path(task["attempts"][0]["receipt"]).read_bytes())
    body = Path(receipt["request_path"]).read_bytes()
    context = json.loads(json.loads(body)["messages"][1]["content"])
    required = context["diagnostic_state"]["finish_requirements"]
    assert required["policy"] == POLICY
    assert "at most 2000 characters" in required["instructions"]
    _verify_payload(receipt, body)
    restored = inspect_task(Path(task["task_path"]))
    rebuilt = context_from_reference(freeze_context(restored), load_case(manifest), snapshot)
    assert rebuilt["finish_requirements"] == required


def test_successful_revision_consumed_by_finish_and_export_not_final_acceptance(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    case = load_case(manifest)
    task = _create_protocol_5_task(manifest, tmp_path, contract_audit_policy="disabled")
    snapshot = load_candidate(case)
    requirement = task["contract_requirements"]["requirement_ids"][0]
    action = {"type": "propose_probe", "revision": snapshot.revision, "requirement_id": requirement,
              "code": "def test_resource():\n    pass\n", "purpose": "offline fixture",
              "expected_observation": "zero", "evidence_refs": ["business_contract"], "oracle": ORACLE}
    task = review(run_action(task, action), probe_runner=lambda *args: measured_run(*args, completed=False))
    parent = task["probes"][0]
    original = Path(parent["path"]).read_bytes()
    revision = action | {"type": "revise_probe", "parent_probe_id": parent["id"],
                        "revision_reason": "correct setup", "code": "def test_resource():\n    assert True\n"}
    task = review(run_action(task, revision), probe_runner=measured_run)

    def public(manifest, directory, **kwargs):
        report = public_run(manifest, directory, **kwargs)
        for stage in ("old_original", "new_original"):
            report["stages"][stage]["nodeids"] = ["feedback/test_protocol_v5.py::test_registered_behavior"]
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    task = review(run_action(task, {"type": "run_public_checks", "revision": snapshot.revision}), comparator=public)
    public_ref = "observation:" + task["latest_observation"]
    task = run_action(task, {"type": "finish", "reason": "no_change_claimed", "explanation": "Bounded fixture only.",
                            "evidence_refs": [public_ref], "contract_coverage": [{
                                "requirement_id": requirement, "state": "supported",
                                "evidence_refs": [public_ref], "tool_limitation": None}]})
    assert task["status"] == "no_change_claimed" and task["final_evaluation"] == "not_run"
    scope = task["finish"]["diagnostic_scope"]
    assert scope["finish_requirements"]["probe_history"][0]["disposition"] == "resolved_setup_history"
    assert Path(parent["path"]).read_bytes() == original
    exported = export_task_report(Path(task["task_path"]), tmp_path / "export")
    assert json.loads(Path(exported["json_path"]).read_bytes())["finish"]["diagnostic_scope"] == scope

    def reject(manifest, directory, **kwargs):
        report = public(manifest, directory, **kwargs)
        report["stages"]["new_original"]["status"] = "failed"
        report["status"] = "migration_required"
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    final = finalize_task(Path(task["task_path"]), comparator=reject)
    assert final["final_result"]["status"] == "no_change_claim_not_accepted"


def test_registered_probe_revision_cannot_switch_between_valid_contract_ids(tmp_path):
    manifest = _copy_as_v2_case(tmp_path)
    _replace_catalog(manifest, lambda catalog: catalog["requirements"].append({
        "id": "input.tuple", "statement": "Tuple path remains valid.", "public_check_nodeids": [],
    }))
    case = load_case(manifest)
    task = _create_protocol_5_task(manifest, tmp_path, contract_audit_policy="disabled")
    action = {"type": "propose_probe", "revision": load_candidate(case).revision,
              "requirement_id": "release.registered-behavior", "code": "def test_value():\n    pass\n",
              "purpose": "offline", "expected_observation": "zero",
              "evidence_refs": ["business_contract"], "oracle": ORACLE}
    task = review(run_action(task, action), probe_runner=lambda *args: measured_run(*args, completed=False))
    with pytest.raises(ValueError, match="preserve requirement_id"):
        accept_action(task, {"action": action | {"type": "revise_probe", "requirement_id": "input.tuple",
            "parent_probe_id": task["probes"][0]["id"], "revision_reason": "different input",
            "code": "def test_value():\n    assert True\n"}}, case)
    assert len(task["probes"]) == 1


@pytest.mark.parametrize("candidate_status,original_status", [("failed", "passed"), (None, "failed")])
def test_no_change_stage_fallback_never_hides_a_failure(candidate_status, original_status):
    node = "feedback/test_contract.py::test_value"
    stages = {"new_original": {"status": original_status, "nodeids": [node]}}
    if candidate_status is not None:
        stages["new_candidate"] = {"status": candidate_status, "nodeids": [node]}
    requirement = ContractRequirement(id="input.value", statement="Value is preserved.", public_check_nodeids=[node])
    rows = [{"id": "observation", "revision": "current", "kind": "public_checks",
             "result": {"status": "passed", "stages": stages}}]
    assert not _contract_observation_states(rows, [requirement], "current")[requirement.id]["support"]
