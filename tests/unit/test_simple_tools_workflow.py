"""强工具基线复用真实证据门禁；不执行目标代码或真实模型。"""

import copy
import json
from pathlib import Path

import pytest
from test_contract_coverage import (
    _copy_as_v2_case,
    _create_protocol_6_task,
    _dependency_action,
    _dependency_runner,
    _feedback_comparator,
    _provider_response,
    _review_candidate,
    _submit_candidate,
)

from upgrade_workbench import tasks
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import accept_action, context_from_reference, freeze_context
from upgrade_workbench.generation import ProposalInputError
from upgrade_workbench.generation.actions import AgentActionError
from upgrade_workbench.generation.protocol_v6 import instructions, validate
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.service.recovery import _attempt_contract, recover_task


def simple_task(tmp_path, **kwargs):
    return _create_protocol_6_task(
        _copy_as_v2_case(tmp_path), tmp_path, workflow_profile="simple_tools", **kwargs
    )


def finish(task):
    return {"type": "finish", "reason": "candidate_ready", "explanation": "Current public behavior passed.",
            "evidence_refs": ["observation:" + task["latest_observation"]]}


def test_simple_finish_is_distinct_from_default_workbench_contract():
    action = {"type": "finish", "reason": "unresolved", "explanation": "No safe repair identified.",
              "evidence_refs": []}
    assert validate(None, action, None, workflow_profile="simple_tools") == action
    with pytest.raises(AgentActionError, match="contract_coverage"):
        validate(None, action, None)
    with pytest.raises(AgentActionError, match="exactly"):
        validate(None, action | {"contract_coverage": []}, None, workflow_profile="simple_tools")
    assert "Finish with exactly {type:finish,reason,explanation,evidence_refs}" in instructions(workflow_profile="simple_tools")
    assert "contract_coverage must contain every ID" in instructions()
    with pytest.raises(ValueError, match="only a Solver"):
        instructions(role="investigator", workflow_profile="simple_tools")


@pytest.mark.parametrize("change", [
    {"contract_audit_policy": "bounded"}, {"investigator_policy": "required_once"},
    {"navigation_assistance": "failure_guided"}, {"context_assistance": "source_only"},
    {"workflow_profile": "unknown"},
])
def test_simple_profile_rejects_hidden_assistance_or_missing_documents(tmp_path, change):
    task = simple_task(tmp_path)
    with pytest.raises(ValueError):
        tasks._require_current_protocol(task["protocol"] | change)


def test_simple_request_keeps_official_documents_and_binds_profile(tmp_path):
    task = _submit_candidate(simple_task(tmp_path), "offline-simple")
    report = json.loads(Path(task["attempts"][0]["receipt"]).read_text(encoding="utf-8"))
    body = Path(report["request_path"]).read_bytes()
    context = json.loads(json.loads(body)["messages"][1]["content"])
    assert report["experiment_arm"] == "no_ast"
    assert report["workflow_profile"] == "simple_tools"
    assert context["version_evidence"] and not context["potential_impacts"]
    assert context["business_contract"]["text"] and context["contract_requirements"]
    assert context["diagnostic_state"]["workflow_profile"] == "simple_tools"
    assert "diagnostic_workset" not in context["diagnostic_state"]
    assert "recommended_candidate" not in context["diagnostic_state"]
    assert "retained_source_reads" not in context["diagnostic_state"]
    assert "source_read_retention" not in report
    # 完成收据指向新候选；发送边界仍按请求中的原始候选核对。
    report = report | {"candidate_reference": None, "candidate_revision": context["candidate"]["revision"]}
    _verify_payload(report, body)
    with pytest.raises(ProposalInputError):
        _verify_payload(report | {"workflow_profile": "workbench"}, body)
    with pytest.raises(ProposalInputError, match="without AST"):
        _verify_payload(report | {"experiment_arm": "full"}, body)
    # 同时改提示和收据仍不能改变已经冻结的任务上下文。
    payload = json.loads(body)
    from upgrade_workbench.generation.request import _instructions

    payload["messages"][0]["content"] = _instructions("protocol_v6_actions")
    with pytest.raises(ProposalInputError, match="workflow"):
        _verify_payload(report | {"workflow_profile": "workbench"}, json.dumps(payload).encode())


def test_simple_opt_in_retains_reads_beyond_observation_window_and_binds_revision(tmp_path):
    original = simple_task(tmp_path)
    task = tasks.create_operation(
        Path(original["manifest_path"]), tmp_path / "retained",
        budget_path=tmp_path / "ledger-v6.sqlite", seed_strategy="none", protocol_revision=6,
        generation=original["protocol"]["generation"], max_calls=8,
        workflow_profile="simple_tools", context_assistance="version_only",
        navigation_assistance="baseline", contract_audit_policy="disabled",
        investigator_policy="disabled", source_read_retention="current_revision",
    )
    contexts = []
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, task["current_candidate"])
    name = next(n for n in case.manifest.allowed_changes if len(snapshot.files[n].splitlines()) >= 10)

    def transport(request, **_kwargs):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(context)
        if len(contexts) <= 4:
            start = len(contexts) * 2 - 1
            return _provider_response({"summary": "Read a distinct exact range", "action": {
                "type": "read_source", "revision": snapshot.revision, "path": name,
                "start_line": start, "end_line": start + 1,
            }})
        return _provider_response({"summary": "End offline fixture", "action": {
            "type": "finish", "reason": "unresolved", "explanation": "Fixture stops without target execution.",
            "evidence_refs": [],
        }})

    task = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    context = contexts[-1]
    runtime = context["diagnostic_state"]
    assert len(contexts) == 5 and len(runtime["observations"]) == 3
    assert len(runtime["retained_source_reads"]) == 4
    assert context["source_selection"]["read_retention"] == {
        "requested_lines": 8, "included_lines": 8, "complete": True, "omitted_ranges": [],
    }
    assert not context["potential_impacts"] and context["version_evidence"]
    assert "project_context" not in context and "project_knowledge" not in runtime
    assert "diagnostic_workset" not in runtime and "recommended_candidate" not in runtime
    receipt = json.loads(Path(task["attempts"][-1]["receipt"]).read_bytes())
    body = Path(receipt["request_path"]).read_bytes()
    _verify_payload(receipt, body)
    from upgrade_workbench.reporting import export_task_report

    exported = export_task_report(Path(task["task_path"]), tmp_path / "retained-export")
    public = json.loads(Path(exported["json_path"]).read_bytes())
    assert public["protocol"]["source_read_retention"] == "current_revision"
    with pytest.raises(ProposalInputError, match="retention"):
        _verify_payload(receipt | {"source_read_retention": None}, body)
    for invalid in ("all_revisions", True, {}):
        with pytest.raises(ValueError, match="retention"):
            tasks._require_current_protocol(task["protocol"] | {"source_read_retention": invalid})

    from upgrade_workbench.candidates import apply_increment

    changed = apply_increment(case, snapshot, {"type": "submit_candidate", "base_revision": snapshot.revision,
        "edits": [{"path": name, "old": snapshot.files[name].decode(),
                   "new": "# changed revision\n" + snapshot.files[name].decode()}]}, tmp_path / "changed")
    task["current_candidate"] = changed.reference
    new_runtime = context_from_reference(freeze_context(task), case, changed)
    assert not new_runtime["retained_source_reads"]


def test_simple_flow_submits_without_self_report_and_can_export(tmp_path):
    from upgrade_workbench.reporting import export_task_report

    task = _review_candidate(_submit_candidate(simple_task(tmp_path, max_calls=2), "simple"), passed=True)
    contexts = []

    def transport(request, **_kwargs):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(context)
        assert "Do not include contract_coverage" in context["task_context"]["edit_feedback"]
        return _provider_response({"summary": "Deliver reviewed result", "action": finish(task)})

    completed = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert completed["status"] == "submitted" and len(completed["attempts"]) == 2
    assert "contract_coverage" not in completed["finish"]
    assert completed["finish"]["diagnostic_scope"]["contract_coverage_gate"]["accepted"]
    assert completed["final_evaluation"] == "not_run"
    assert len(contexts) == 1
    exported = export_task_report(Path(task["task_path"]), tmp_path / "export")
    public = json.loads(Path(exported["json_path"]).read_text(encoding="utf-8"))
    assert public["protocol"]["workflow_profile"] == "simple_tools"


@pytest.mark.parametrize("problem", ["failed_public", "unreviewed", "counterexample"])
def test_simple_finish_preserves_observed_behavior_and_review_gates(tmp_path, monkeypatch, problem):
    from upgrade_workbench import diagnostics

    task = _submit_candidate(simple_task(tmp_path), "simple")
    task = _review_candidate(task, passed=problem != "failed_public")
    case = load_case(Path(task["manifest_path"]))
    if problem == "unreviewed":
        task["candidate"]["reviewed"] = False
    if problem == "counterexample":
        original = diagnostics.public_observation

        def observed(owner, reference):
            row = original(owner, reference)
            return row | {
                "kind": "probe", "action": {"requirement_id": "release.registered-behavior"},
                "result": {"status": "passed", "assessment": {
                    "probe_id": "test-probe", "conclusion": "counterexample_observed"}},
            }

        monkeypatch.setattr(diagnostics, "public_observation", observed)
    with pytest.raises(ValueError):
        accept_action(task, {"action": finish(task), "role": "solver"}, case)
    assert task["status"] != "submitted"


def test_simple_unresolved_does_not_require_exhausting_diagnostic_budget(tmp_path):
    task = simple_task(tmp_path)
    completed = tasks.advance_task(
        Path(task["task_path"]), api_key="unused",
        transport=lambda *_a, **_k: _provider_response({"summary": "Stop without a repair", "action": {
            "type": "finish", "reason": "unresolved", "explanation": "No defensible repair available.",
            "evidence_refs": [],
        }}),
    )
    assert completed["status"] == "unresolved" and len(completed["attempts"]) == 1
    assert not completed["diagnostic_runs"] and not completed["contract_audits"]


def test_simple_dependency_tool_and_fact_remain_available(tmp_path):
    task = simple_task(tmp_path)
    queried = tasks.advance_task(
        Path(task["task_path"]), api_key="unused",
        transport=lambda *_a, **_k: _provider_response({"summary": "Inspect locked dependency", "action": _dependency_action()}),
    )
    assert queried["status"] == "pending_dependency_query"
    observed = tasks.advance_task(Path(task["task_path"]), dependency_query_runner=_dependency_runner("a"))
    assert len(observed["dependency_facts"]) == 1 and len(observed["attempts"]) == 1
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, observed["current_candidate"])
    context = context_from_reference(freeze_context(observed), case, snapshot)
    assert context["dependency_facts"] and context["workflow_profile"] == "simple_tools"


def test_simple_history_is_manual_and_not_ranked_by_pass_count(tmp_path):
    task = simple_task(tmp_path)
    first = _review_candidate(_submit_candidate(task, "first"), passed=True)
    second = _review_candidate(_submit_candidate(first, "second"), passed=False)
    case = load_case(Path(second["manifest_path"]))
    snapshot = load_candidate(case, second["current_candidate"])
    context = context_from_reference(freeze_context(second), case, snapshot)
    assert "recommended_candidate" not in context
    assert [row["revision"] for row in context["candidate_options"]] == [
        second["candidate"]["revision"], first["candidate"]["revision"],
    ]
    target = context["candidate_options"][1]
    assert accept_action(second, {"action": {
        "type": "restore_candidate", "revision": target["revision"], "reason": "Return to reviewed history.",
        "evidence_refs": [target["public_observation_ref"]],
    }}, case)
    assert second["candidate"]["revision"] == target["revision"]


def test_simple_completed_finish_receipt_recovers_without_new_request(tmp_path, monkeypatch):
    owner = "a" * 32
    task = simple_task(tmp_path, service_owner=owner)
    path = Path(task["task_path"])
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case, task["current_candidate"])
    name = case.manifest.allowed_changes[0]
    old = snapshot.files[name].decode()
    task = tasks.advance_task(path, api_key="unused", execution_owner=owner,
        transport=lambda *_a, **_k: _provider_response({"summary": "Edit for recovery test", "action": {
            "type": "submit_candidate", "base_revision": snapshot.revision,
            "edits": [{"path": name, "old": old, "new": "# recovery test\n" + old}],
        }}))
    task = tasks.advance_task(path, execution_owner=owner, reviewed_revision=task["candidate"]["revision"],
        reviewed_sha256=task["candidate"]["sha256"], reviewer="offline", review_note="Injected check only.",
        review_only=True, comparator=_feedback_comparator(passed=True))
    original = tasks.complete_request

    class Crash(BaseException):
        pass

    def crash_after_receipt(prepared, **kwargs):
        original(prepared, **kwargs)
        raise Crash()

    monkeypatch.setattr(tasks, "complete_request", crash_after_receipt)
    with pytest.raises(Crash):
        tasks.advance_task(path, api_key="unused", execution_owner=owner,
            transport=lambda *_a, **_k: _provider_response({"summary": "Finish", "action": finish(task)}))
    recovered = recover_task(path, Path(task["work_root"]), owner)
    assert recovered["status"] == "submitted" and len(recovered["attempts"]) == 2
    assert recover_task(path, Path(task["work_root"]), owner) == recovered
    changed = copy.deepcopy(recovered)
    changed["protocol"]["workflow_profile"] = "workbench"
    receipt = json.loads(Path(recovered["attempts"][-1]["receipt"]).read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="workflow_profile_changed"):
        _attempt_contract(changed, changed["attempts"][-1], receipt)
