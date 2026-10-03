"""协议提示、动作校验与事实运行时使用同一有界接口，不执行目标代码。"""

import copy
import json
from types import SimpleNamespace

import pytest

from upgrade_workbench import diagnostics
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.generation import protocol_v4, protocol_v5, protocol_v6, provider
from upgrade_workbench.generation.actions import AgentActionError

FACT = "fact:" + "f" * 64


def _issue(reference=FACT):
    return {
        "hypothesis": "The locked dependency may differ.", "evidence_refs": [reference],
        "unknown": "Application behavior remains unobserved.",
        "next_observation": "Inspect the registered dependency.",
    }


def _query(operation="read_file"):
    fields = {
        "list_files": {"prefix": "owned_dependency"},
        "read_file": {"path": "owned_dependency/api.py", "start_line": 1, "end_line": 200},
        "search_text": {"query": "Thing", "paths": ["owned_dependency/api.py"], "max_results": 5},
        "inspect_symbol": {"module": "owned_dependency.api", "qualname": "Thing"},
    }
    return {
        "type": "query_dependency", "environment": "new", "operation": operation,
        "distribution": "owned-dependency", **fields[operation],
    }


def _finish(workflow_profile):
    action = {
        "type": "finish", "reason": "candidate_ready", "explanation": "Observed limits.",
        "evidence_refs": [FACT],
    }
    if workflow_profile == "workbench":
        action["contract_coverage"] = [{
            "requirement_id": "label.default", "state": "supported",
            "evidence_refs": [FACT], "tool_limitation": None,
        }]
    return action


def _handoff():
    return {
        "type": "handoff", "observed_facts": [], "scope_limits": ["Dependency facts only."],
        "remaining_hypotheses": [], "conflicts": [], "next_discriminating_action": None,
    }


def _tool_actions(snapshot):
    revision = snapshot.revision
    probe = {
        "type": "propose_probe", "revision": revision, "requirement_id": "label.default",
        "code": "def test_owned():\n    assert True\n", "purpose": "Observe the public path.",
        "expected_observation": "The public behavior is observable.",
        "evidence_refs": [FACT], "oracle": None,
    }
    return [
        {"type": "list_sources", "revision": revision},
        {"type": "outline_source", "revision": revision, "path": "model.py"},
        {"type": "search_source", "revision": revision, "query": "Profile", "paths": ["model.py"]},
        {"type": "read_source", "revision": revision, "path": "model.py", "start_line": 1, "end_line": 3},
        {"type": "get_observation", "observation_id": "a" * 64},
        {"type": "run_public_checks", "revision": revision},
        probe,
        probe | {"type": "revise_probe", "parent_probe_id": "a" * 64, "revision_reason": "Clarify observation."},
        {"type": "run_probe", "revision": revision, "probe_id": "a" * 64},
        {"type": "restore_candidate", "revision": revision, "reason": "Return to a reviewed revision.",
         "evidence_refs": [FACT]},
        {"type": "submit_candidate", "base_revision": revision, "edits": [{
            "path": "model.py", "old": "label: str | None", "new": "label: str | None = None",
        }]},
        {"type": "get_fact", "fact_id": "f" * 64},
        *[_query(operation) for operation in ("list_files", "read_file", "search_text", "inspect_symbol")],
    ]


@pytest.mark.parametrize("workflow_profile", ["simple_tools", "workbench"])
def test_all_advertised_tools_accept_bounded_issues_and_fact_references(owned_case, workflow_profile):
    snapshot = load_candidate(owned_case)
    for action in _tool_actions(snapshot):
        action["issues"] = [_issue()]
        before = copy.deepcopy(action)
        assert protocol_v6.validate(owned_case, action, snapshot, workflow_profile=workflow_profile) == before
        assert action == before


def test_investigator_tool_metadata_keeps_read_only_role(owned_case):
    snapshot = load_candidate(owned_case)
    for action in _tool_actions(snapshot):
        action["issues"] = [_issue()]
        if action["type"] in {"submit_candidate", "restore_candidate"}:
            with pytest.raises(AgentActionError) as failure:
                protocol_v6.validate(owned_case, action, snapshot, role="investigator")
            assert failure.value.code == "role_write_forbidden"
        else:
            assert protocol_v6.validate(owned_case, action, snapshot, role="investigator") == action


@pytest.mark.parametrize("workflow_profile", ["simple_tools", "workbench"])
def test_finish_allows_fact_citation_but_never_issue_metadata(workflow_profile):
    action = _finish(workflow_profile)
    assert protocol_v6.validate(None, action, None, workflow_profile=workflow_profile) == action
    with pytest.raises(AgentActionError) as failure:
        protocol_v6.validate(None, action | {"issues": [_issue()]}, None, workflow_profile=workflow_profile)
    assert "extra=['issues']" in str(failure.value)
    assert "allowed=" in str(failure.value)
    assert "finish does not accept issues" in str(failure.value)


@pytest.mark.parametrize("invalid", ["", "Public checks only", "null"])
def test_finish_feedback_identifies_exact_nullable_field_and_accepts_correction(invalid):
    action = _finish("workbench")
    action["contract_coverage"][0]["tool_limitation"] = invalid
    with pytest.raises(AgentActionError, match=r"contract_coverage\[0\].tool_limitation must be JSON null"):
        protocol_v6.validate(None, action, None, workflow_profile="workbench")
    action["contract_coverage"][0]["tool_limitation"] = None
    assert protocol_v6.validate(None, action, None, workflow_profile="workbench") == action


def test_handoff_never_accepts_issue_metadata():
    action = _handoff()
    assert protocol_v6.validate(None, action, None, role="investigator") == action
    with pytest.raises(AgentActionError) as failure:
        protocol_v6.validate(None, action | {"issues": [_issue()]}, None, role="investigator")
    assert "extra=['issues']" in str(failure.value)
    assert "allowed=" in str(failure.value)
    assert "handoff does not accept issues" in str(failure.value)


@pytest.mark.parametrize("legacy", [protocol_v4, protocol_v5])
def test_legacy_protocols_still_reject_fact_references(legacy):
    action = {"type": "run_public_checks", "revision": "a" * 64, "issues": [_issue()]}
    with pytest.raises(AgentActionError):
        legacy.validate(None, action, None)
    protocol_v4.evidence_refs(["business_contract", "source:model.py", "observation:" + "a" * 64])
    with pytest.raises(ValueError):
        protocol_v4.evidence_refs([FACT])


@pytest.mark.parametrize("action", [_query(), {"type": "get_fact", "fact_id": "f" * 64}])
def test_extra_action_fields_have_deterministic_correction_details(action):
    malformed = action | {"surprise": "do not execute"}
    with pytest.raises(AgentActionError) as failure:
        protocol_v6.validate(None, malformed, None)
    message = str(failure.value)
    assert "missing=[]" in message
    assert "extra=['surprise']" in message
    assert "allowed=" in message and "issues" in message
    assert failure.value.code == "action_invalid_fields"


@pytest.mark.parametrize("operation", [{"type": "read_file"}, ["read_file"], "unknown"])
def test_dependency_operation_feedback_teaches_flat_shape(operation):
    with pytest.raises(AgentActionError) as failure:
        protocol_v6.validate(None, {"type": "query_dependency", "environment": "new", "operation": operation}, None)
    message = str(failure.value)
    assert "operation must be a string" in message or "Unknown query_dependency operation" in message
    assert "list_files" in message and "inspect_symbol" in message
    assert "action level" in message


@pytest.mark.parametrize("role,workflow", [("solver", "simple_tools"), ("solver", "workbench"), ("investigator", "workbench")])
def test_prompt_examples_validate_and_name_metadata_exceptions(role, workflow):
    prompt = protocol_v6.instructions(role=role, workflow_profile=workflow)
    assert "Any action may include issues" not in prompt
    assert "finish and handoff do not accept issues" in prompt
    assert "issues:[{hypothesis,evidence_refs,unknown,next_observation}]" in prompt
    assert "At most 4 issues, 8 references per issue and 1000 characters" in prompt
    examples = [json.loads(line) for line in prompt.splitlines() if line.startswith('{"summary":')]
    query_examples = [item["action"] for item in examples if item["action"]["type"] == "query_dependency"]
    assert {item["operation"] for item in query_examples} == {"list_files", "search_text", "read_file", "inspect_symbol"}
    for action in query_examples:
        assert protocol_v6.validate(None, action, None, role=role, workflow_profile=workflow) == action


@pytest.mark.parametrize("kind", ["finish", "handoff"])
def test_top_level_issues_feedback_does_not_move_metadata_into_terminal_action(kind):
    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
        "summary": "Preserve the terminal shape.", "action": {"type": kind}, "issues": [_issue()],
    })}}]}
    with pytest.raises(provider._ProviderBoundaryError) as failure:
        provider._proposal(response, "protocol_v6_actions")
    assert failure.value.code == "diagnostic_issues_outside_action"
    assert "do not move issues" in failure.value.feedback


def _runtime_task(owned_case, tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    task = {
        "schema_version": 5, "task_id": "owned-task", "task_path": str(root / "task.json"),
        "manifest_path": str(owned_case.manifest_path), "case_fingerprint": owned_case.fingerprint,
        "current_candidate": None, "observations": [], "historical_inputs": [], "issues": [],
        "dependency_queries": [], "dependency_facts": [], "dependency_environment_identities": {},
        "protocol": {"dependency_query_policy": {"max_queries": 8, "max_result_bytes": 24000}},
        "status": "ready",
    }
    action = _query()
    response = {"action": action, "report_path": str(root / "owned-receipt.json")}
    assert diagnostics.accept_action(task, response, owned_case) is False
    row = task["dependency_queries"][0]
    request = diagnostics.read(row["request"], root)
    # 注入自有依赖事实，不导入依赖包，也不执行查询或业务代码。
    result = {
        "status": "observed", "environment": {"role": "new"},
        "distribution": "owned-dependency", "installed_version": "1.0",
        "operation": "read_file", "result": {"lines": ["class Thing: pass"]},
        "limitations": ["Synthetic fixture, no target execution."],
    }
    diagnostics._register_dependency_query_result(task, row, request, result, None)
    return task


def test_valid_issues_do_not_change_query_identity_and_facts_remain_registered(owned_case, tmp_path):
    task = _runtime_task(owned_case, tmp_path)
    fact = task["dependency_facts"][0]
    query = copy.deepcopy(task["dependency_queries"][0])
    action = _query() | {"issues": [_issue("fact:" + fact["id"])]}
    protocol_v6.validate(None, action, None)
    assert diagnostics.accept_action(task, {"action": action, "report_path": "owned"}, owned_case) is True
    assert task["issues"] == action["issues"]
    assert task["dependency_queries"] == [query]
    assert diagnostics.public_fact(task, fact)["id"] == fact["id"]
    get_fact = {"type": "get_fact", "fact_id": fact["id"], "issues": action["issues"]}
    protocol_v6.validate(None, get_fact, None)
    assert diagnostics.accept_action(task, {"action": get_fact}, owned_case) is True
    assert task["latest_fact"] == fact["id"]


def test_unknown_fact_rejects_metadata_atomically_before_queue_or_candidate_mutation(owned_case, tmp_path):
    task = _runtime_task(owned_case, tmp_path)
    action = _query("list_files") | {"issues": [_issue()]}
    protocol_v6.validate(None, action, None)
    before = copy.deepcopy(task)
    paths = sorted(str(path) for path in tmp_path.rglob("*"))
    with pytest.raises(ValueError, match="Unknown public evidence"):
        diagnostics.accept_action(task, {"action": action}, owned_case)
    assert task == before
    assert sorted(str(path) for path in tmp_path.rglob("*")) == paths


@pytest.mark.parametrize("damage", ["unknown_target", "tampered_target"])
def test_get_fact_target_is_checked_before_any_metadata_mutation(owned_case, tmp_path, damage):
    task = _runtime_task(owned_case, tmp_path)
    reference = task["dependency_facts"][0]
    fact_id = "0" * 64 if damage == "unknown_target" else reference["id"]
    if damage == "tampered_target":
        from pathlib import Path

        Path(reference["path"]).write_text("{}", encoding="utf-8")
    before = copy.deepcopy(task)
    action = {"type": "get_fact", "fact_id": fact_id, "issues": [_issue("business_contract")]}
    # 独立检查实际运行时，不能让上游语法拒绝掩盖目标查验前的状态变更。
    with pytest.raises(ValueError):
        diagnostics.accept_action(task, {"action": action}, owned_case)
    assert task == before


@pytest.mark.parametrize("issues", [None, {}, "text", [None], [{"unexpected": True}]])
def test_malformed_issue_metadata_is_rejected_without_runtime_mutation(owned_case, tmp_path, issues):
    task = _runtime_task(owned_case, tmp_path)
    before = copy.deepcopy(task)
    action = {"type": "run_public_checks", "revision": "a" * 64, "issues": issues}
    with pytest.raises(AgentActionError) as failure:
        protocol_v6.validate(None, action, None)
    assert failure.value.code == "action_invalid_fields"
    assert task == before


def test_registered_fact_is_not_behavioral_observation_support(owned_case, tmp_path):
    task = _runtime_task(owned_case, tmp_path)
    reference = "fact:" + task["dependency_facts"][0]["id"]
    diagnostics.resolve_refs(task, [reference])
    action = _finish("workbench")
    action["evidence_refs"] = [reference]
    action["contract_coverage"][0]["evidence_refs"] = [reference]
    protocol_v6.validate(None, action, None)
    requirements = [SimpleNamespace(id="label.default", public_check_nodeids=["feedback/test_label.py::test_default"])]
    context = {
        "contract_requirements": {"sha256": "a" * 64}, "remaining_diagnostic_runs": 0,
        "diagnostic_options": {"new_probe_definition_available": False, "revisable_probe_ids": [], "reusable_probe_ids": []},
    }
    decision = diagnostics.finish_contract_decision(action, context, requirements, [], "a" * 64)
    assert decision["accepted"] is False
    assert decision["effective_coverage"][0]["effective_state"] == "unobserved"
    assert diagnostics.simple_finish_decision(action, requirements, [], "a" * 64)["accepted"] is False
