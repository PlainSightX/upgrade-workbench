"""证据分类仅说明实际引用种类，不替模型或独立验收判断语义。"""

import json
from copy import deepcopy

import pytest

from upgrade_workbench.generation.coverage_evidence import qualify_coverage


def declaration(refs):
    return {"requirement": "Preserve output behavior", "scope": "in_contract",
            "status": "verified", "evidence_refs": refs, "tool_limitation": None}


def observation(kind="probe", revision="current", status="passed", conclusion="no_counterexample_observed"):
    return {"id": "a", "revision": revision, "kind": kind,
            "result": {"status": status, "assessment": {"conclusion": conclusion,
                "scope_limit": "one path only"}, "stages": {
                    "new_candidate": {"status": status, "nodeids": ["test_one_path"],
                                      "tests": {"passed": 1}, "stdout": "not copied"}}}}


def test_verified_source_claim_does_not_become_execution():
    declarations = [declaration(["source:package/module.py", "business_contract"])]
    original = deepcopy(declarations)
    result = qualify_coverage(declarations, [observation()], "current")
    item = result["items"][0]
    assert item["evidence_basis"] == "static_evidence_only"
    assert item["model_declared_status"] == "verified"
    assert item["execution_references"] == []  # 不借用没有引用的成功观察。
    assert item["semantic_requirement_match"] == "not_established_by_host"
    assert "verified_declaration_without_current_execution" in item["warnings"]
    assert declarations == original


@pytest.mark.parametrize("kind", ["source", "project_context"])
def test_observation_namespace_does_not_imply_execution(kind):
    result = qualify_coverage([declaration(["observation:a"])], [observation(kind=kind)], "current")
    assert result["items"][0]["evidence_basis"] == "static_evidence_only"
    assert result["items"][0]["execution_references"] == []


def test_stale_pass_is_not_current_execution():
    item = qualify_coverage([declaration(["observation:a"])],
                            [observation(revision="old")], "current")["items"][0]
    assert item["evidence_basis"] == "no_current_completed_execution"
    assert "stale_execution_not_current_proof" in item["warnings"]


@pytest.mark.parametrize("status,conclusion,warning", [
    ("passed", "observation_only", "observation_only_not_business_acceptance"),
    ("passed", "counterexample_observed", "measured_counterexample"),
    ("passed", "inconclusive", "inconclusive_not_business_support"),
    ("execution_incomplete", "inconclusive", "execution_failed_or_incomplete"),
    ("failed", "counterexample_observed", "execution_failed_or_incomplete"),
])
def test_preserves_negative_and_observation_only_limits(status, conclusion, warning):
    item = qualify_coverage([declaration(["observation:a"])],
                            [observation(status=status, conclusion=conclusion)], "current")["items"][0]
    assert warning in item["warnings"]
    reference = item["execution_references"][0]
    assert reference["assessment"]["scope_limit"] == "one path only"
    assert reference["assessment"]["conclusion"] == conclusion
    assert reference["stages"]["new_candidate"]["nodeids"] == ["test_one_path"]
    assert "stdout" not in reference["stages"]["new_candidate"]
    assert item["semantic_requirement_match"] == "not_established_by_host"


def test_items_do_not_borrow_each_others_runtime_evidence():
    result = qualify_coverage([declaration(["source:module.py"]), declaration(["observation:a"])],
                              [observation(kind="public_checks")], "current")
    assert [row["evidence_basis"] for row in result["items"]] == [
        "static_evidence_only", "current_execution_cited"]


def test_unknown_reference_never_becomes_support():
    item = qualify_coverage([declaration(["observation:missing"])], [], "current")["items"][0]
    assert item["evidence_basis"] == "no_current_completed_execution"
    assert item["unavailable_references"] == ["observation:missing"]


def test_current_read_of_old_knowledge_stays_stale():
    row = observation(kind="project_context")
    row["result"] = {"knowledge_revision": "old", "current_revision": "current", "stale": True}
    item = qualify_coverage([declaration(["observation:a"])], [row], "current")["items"][0]
    ref = item["static_references"][0]
    assert ref["observation_revision"] == "current" and ref["knowledge_revision"] == "old"
    assert ref["freshness"] == "stale" and ref["knowledge_stale"] is True
    assert "stale_static_interpretation" in item["warnings"]


@pytest.mark.parametrize("nodes", [[], ["first-node"]])
def test_partial_execution_list_cannot_look_complete(nodes):
    row = observation(kind="public_checks")
    row["result"]["truncated"] = True
    row["result"]["stages"]["new_candidate"].update(
        nodeids=nodes, nodeids_total=200, nodeids_truncated=True)
    result = qualify_coverage([declaration(["observation:a"])], [row], "current")
    runtime = result["items"][0]["execution_references"][0]
    assert runtime["truncated"] is True
    assert runtime["stages"]["new_candidate"]["nodeids_total"] == 200
    assert runtime["stages"]["new_candidate"]["nodeids_truncated"] is True
    assert runtime["stages"]["new_candidate"]["nodeids"] == nodes


def test_repeated_maximum_references_fit_task_storage():
    rows = [observation() | {"id": str(i)} for i in range(8)]
    for row in rows:
        row["result"]["stages"]["new_candidate"]["nodeids"] = ["test_" + "x" * 195] * 40
        row["result"]["assessment"]["scope_limit"] = "x" * 1000
    refs = ["observation:" + row["id"] for row in rows]
    result = qualify_coverage([declaration(refs) for _ in range(8)], rows, "current")
    assert len(json.dumps(result).encode()) < 1_000_000
