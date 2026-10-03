"""冻结预算和真实请求可见性；合成模型/执行仅核验工作流，不计迁移成绩。"""

import copy
import json
from pathlib import Path

import pytest
from test_contract_coverage import _copy_as_v2_case, _ledger
from test_diagnostic_operations import public_run, response, review, run_action

from upgrade_workbench import tasks
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostic_output import output_cursor
from upgrade_workbench.diagnostics import context_from_reference, freeze_context
from upgrade_workbench.finish_evidence import project, require_submission_evidence
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.investigation_policy import (
    VERSION,
    note_progress,
    observation_visible,
    policy,
)
from upgrade_workbench.reporting import export_task_report


def create(tmp_path, *, profile="simple_tools", selected=None, max_calls=8, service_owner=None):
    manifest = _copy_as_v2_case(tmp_path)
    budget = tmp_path / "ledger.sqlite"
    _ledger(budget)
    kwargs = {"workflow_profile": profile}
    if profile == "p5":
        kwargs = {}
    elif profile == "simple_tools":
        kwargs.update(context_assistance="version_only", navigation_assistance="baseline")
    return tasks.create_operation(manifest, tmp_path / "work", budget_path=budget,
        seed_strategy="none", protocol_revision=5 if profile == "p5" else 6,
        generation={"model": "owned-model", "endpoint": "https://provider.example/chat/completions",
                    "thinking_mode": "disabled", "max_output_tokens": 1024, "timeout_seconds": 10},
        contract_audit_policy="disabled", max_calls=max_calls,
        investigation_policy=selected or {"version": VERSION}, service_owner=service_owner, **kwargs)


def unresolved(task, explanation="Offline fixture ends without target execution."):
    action = {"type": "finish", "reason": "unresolved", "explanation": explanation, "evidence_refs": []}
    if task["protocol"].get("workflow_profile") != "simple_tools":
        action["contract_coverage"] = [{"requirement_id": name, "state": "unobserved",
                                       "evidence_refs": [], "tool_limitation": None}
                                      for name in task["contract_requirements"]["requirement_ids"]]
    return action


def fake_observation(identity="a", *, revision="r", text="one\n", start=1):
    return {"id": identity, "kind": "source", "revision": revision,
            "action": {"type": "read_source"}, "result": {"path": "a.py", "revision": revision,
            "start_line": start, "end_line": start, "text": text}}


def progress_task(limit=4):
    return {"status": "ready", "protocol": {"diagnostic_policy": {"max_observation_revisits": limit}}}


@pytest.mark.parametrize("invalid", [False, {}, {"version": "unknown"},
    {"version": VERSION, "diagnostic_limits": {"max_runs": 0}},
    {"version": VERSION, "diagnostic_limits": {"max_probes": True}},
    {"version": VERSION, "diagnostic_limits": {"max_revisions_per_probe": -1}},
    {"version": VERSION, "diagnostic_limits": {"max_observation_revisits": 1}},
    {"version": VERSION, "finish_limits": {"evidence_refs_max_items": 0}},
    {"version": VERSION, "finish_notice_calls": False}])
def test_invalid_budgets_are_rejected(invalid):
    with pytest.raises(ValueError):
        policy(invalid)


def test_visible_loop_stops_but_evicted_evidence_can_be_retrieved():
    item = fake_observation()
    task = progress_task()
    present = {"diagnostic_state": {"observations": [item]}}
    note_progress(task, "r", observation=item, context=present)
    for _ in range(3):
        note_progress(task, "r", observation=item, context=present)
    assert task["status"] == "ready" and task["diagnostic_progress"]["warning"]
    # 本次请求漏掉原文，曾见过不等于现在仍能看见。
    note_progress(task, "r", observation=item, context={})
    assert task["diagnostic_progress"]["consecutive_revisits"] == 0
    restored = json.loads(json.dumps(task))
    for _ in range(4):
        note_progress(restored, "r", observation=item, context=present)
    assert restored["stop_reason"] == "repeated_visible_evidence_no_progress"
    note_progress(restored, "new", observation=fake_observation(revision="new"), context={})
    assert restored["diagnostic_progress"]["consecutive_revisits"] == 0


def test_action_without_new_observation_cannot_wash_out_repeated_reads():
    task = progress_task(2)
    item = fake_observation()
    present = {"diagnostic_state": {"observations": [item]}}
    note_progress(task, "r", observation=item, context=present)
    for _ in range(2):
        note_progress(task, "r", context=present)
        note_progress(task, "r", observation=item, context=present)
    assert task["stop_reason"] == "repeated_visible_evidence_no_progress"


def test_complete_current_source_counts_but_stale_or_partial_source_does_not():
    item = fake_observation()
    block = item["result"] | {"end_line": 2, "text": "one\ntwo\n"}
    assert observation_visible({"source_files": [block]}, item)
    assert not observation_visible({"source_files": [block | {"revision": "old"}]}, item)
    assert not observation_visible({"source_files": [block | {"text": "different\ntwo\n"}]}, item)
    assert not observation_visible({"source_files": [block | {"start_line": 2}]}, item)


def test_new_output_pages_and_new_ranges_reset_only_information_repeats():
    item = fake_observation()
    page = {"observation_id": "a", "stage": "new_original", "stream": "stdout", "sha256": "f" * 64, "offset": 0}
    cursor = output_cursor("new_original", "stdout", "f" * 64)
    context = {"diagnostic_state": {"observation_output": page}}
    task = progress_task()
    for _ in range(3):
        note_progress(task, "r", observation=item, cursor=cursor, context=context)
    assert task["diagnostic_progress"]["consecutive_revisits"] == 2
    note_progress(task, "r", observation=item, cursor=output_cursor("new_original", "stdout", "f" * 64, 8000), context=context)
    assert task["diagnostic_progress"]["consecutive_revisits"] == 0
    note_progress(task, "r", observation=fake_observation("b", start=2), context={})
    assert not task["diagnostic_progress"]["last_retrieval"]["counted_as_repeat"]


def test_new_task_freezes_configurable_limits_without_changing_legacy(tmp_path):
    task = create(tmp_path, selected={"version": VERSION, "diagnostic_limits": {
        "max_runs": 12, "max_probes": 5, "max_revisions_per_probe": 4}}, max_calls=60)
    assert task["protocol"]["max_calls"] == 60
    assert task["protocol"]["diagnostic_policy"]["max_runs"] == 12
    tasks._require_current_protocol(task["protocol"])
    with pytest.raises(ValueError, match="Diagnostic limits"):
        tasks._require_current_protocol(task["protocol"] | {"diagnostic_policy": {}})
    legacy = copy.deepcopy(task["protocol"])
    legacy.pop("investigation_policy")
    legacy["diagnostic_policy"] = policy({"version": VERSION})["diagnostic_limits"]
    with pytest.raises(ValueError, match="call limit"):
        tasks.create_task(Path(task["manifest_path"]), tmp_path / "legacy", legacy,
                          arm="direct_repair", phase="operation", budget_path=tmp_path / "ledger.sqlite", kind="operation")
    assert tasks.inspect_task(Path(task["task_path"]))["protocol"] == task["protocol"]


@pytest.mark.parametrize("max_calls", [0, -1, True])
def test_invalid_total_limits_rejected_at_creation(tmp_path, max_calls):
    with pytest.raises(ValueError, match="call limit"):
        create(tmp_path, max_calls=max_calls)


def test_actual_loop_counts_format_failures_and_stops_before_extra_request(tmp_path):
    task = create(tmp_path, max_calls=3)
    contexts = []

    def transport(request, **kwargs):
        contexts.append(json.loads(json.loads(request.data)["messages"][1]["content"]))
        return response({"type": "unsupported"})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert result["status"] == "budget_exhausted" and result["stop_reason"] == "call_limit"
    assert len(contexts) == len(result["attempts"]) == 3
    assert [c["task_context"]["remaining_calls"] for c in contexts] == [3, 2, 1]
    assert "CALL_BUDGET_NOTICE" in contexts[1]["task_context"]["edit_feedback"]
    assert result["final_evaluation"] == "not_run"
    again = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert len(contexts) == 3 and again["status"] == "budget_exhausted"
    exported = export_task_report(Path(task["task_path"]), tmp_path / "export")
    report = json.loads(Path(exported["json_path"]).read_bytes())
    assert report["protocol"]["investigation_policy"] == task["protocol"]["investigation_policy"]


def test_ordinary_source_loop_uses_sent_context_and_survives_inspection(tmp_path):
    task = create(tmp_path)
    case = load_case(Path(task["manifest_path"]))
    snapshot = load_candidate(case)
    name = next(iter(snapshot.files))
    action = {"type": "read_source", "revision": snapshot.revision, "path": name, "start_line": 1, "end_line": 1}
    result = run_action(task, action)
    assert result["status"] == "unresolved"
    assert result["stop_reason"] == "repeated_visible_evidence_no_progress"
    assert len(result["attempts"]) == 5  # 首次读取加四次真实可见的重复，不走旧三次硬停。
    restored = tasks.inspect_task(Path(task["task_path"]))
    runtime = context_from_reference(freeze_context(restored), case, snapshot)
    assert runtime["progress"]["last_retrieval"]["already_visible_in_request"] is True
    receipt = json.loads(Path(result["attempts"][-1]["receipt"]).read_bytes())
    _verify_payload(receipt, Path(receipt["request_path"]).read_bytes())


def test_real_observation_window_eviction_does_not_count_as_stagnation(tmp_path):
    task = create(tmp_path, max_calls=7)
    snapshot = load_candidate(load_case(Path(task["manifest_path"])))
    names = [n for n in snapshot.files if n.endswith(".py")][:4]
    assert len(names) == 4
    contexts = []

    def transport(request, **kwargs):
        contexts.append(json.loads(json.loads(request.data)["messages"][1]["content"]))
        index = len(contexts) - 1
        return response({"type": "outline_source", "revision": snapshot.revision,
                         "path": names[index] if index < 4 else names[0]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert result["status"] == "budget_exhausted" and len(contexts) == 7
    assert result["diagnostic_progress"]["consecutive_revisits"] == 2
    assert result["diagnostic_progress"]["last_retrieval"]["already_visible_in_request"]
    assert all(row.get("action", {}).get("path") != names[0]
               for row in contexts[4]["diagnostic_state"]["observations"])
    assert contexts[5]["diagnostic_state"]["progress"]["consecutive_revisits"] == 0
    for context in contexts[5:]:
        assert any(row.get("action", {}).get("path") == names[0]
                   for row in context["diagnostic_state"]["observations"])


def test_custom_diagnostic_limit_is_enforced_after_reviewed_execution(tmp_path):
    task = create(tmp_path, max_calls=2, selected={"version": VERSION,
        "diagnostic_limits": {"max_runs": 1}})
    snapshot = load_candidate(load_case(Path(task["manifest_path"])))
    task = review(run_action(task, {"type": "run_public_checks", "revision": snapshot.revision}))
    assert len(task["diagnostic_runs"]) == 1
    runtime = context_from_reference(freeze_context(task), load_case(Path(task["manifest_path"])), snapshot)
    assert runtime["remaining_diagnostic_runs"] == 0
    action = {"type": "propose_probe", "revision": snapshot.revision,
              "requirement_id": task["contract_requirements"]["requirement_ids"][0],
              "code": "def test_value():\n    assert True\n", "purpose": "offline",
              "expected_observation": "pass", "evidence_refs": ["business_contract"], "oracle": None}
    result = run_action(task, action)
    assert result["status"] == "budget_exhausted" and not result["probes"]
    assert "Diagnostic execution limit reached" in result["edit_feedback"]


@pytest.mark.parametrize("profile", ["p5", "workbench", "simple_tools"])
def test_configured_finish_limits_reach_real_parser_and_export(tmp_path, profile):
    task = create(tmp_path, profile=profile, selected={"version": VERSION, "finish_limits": {
        "explanation_max_characters": 4000, "evidence_refs_max_items": 12}})
    explanation = "x" * 2766
    snapshot = load_candidate(load_case(Path(task["manifest_path"])))

    def public(manifest, directory, **kwargs):
        report = public_run(manifest, directory, **kwargs)
        for stage in ("old_original", "new_original"):
            report["stages"][stage]["nodeids"] = ["feedback/test_protocol_v5.py::test_registered_behavior"]
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    task = review(run_action(task, {"type": "run_public_checks", "revision": snapshot.revision}), comparator=public)
    ref = "observation:" + task["latest_observation"]
    action = unresolved(task, explanation) | {"reason": "no_change_claimed", "evidence_refs": [ref]}
    if "contract_coverage" in action:
        for row in action["contract_coverage"]:
            row.update(state="supported", evidence_refs=[ref])
    result = run_action(task, action)
    assert result["status"] == "no_change_claimed" and result["final_evaluation"] == "not_run"
    assert result["finish"]["explanation"] == explanation
    assert len(result["attempts"]) == 2
    receipt = json.loads(Path(result["attempts"][-1]["receipt"]).read_bytes())
    body = Path(receipt["request_path"]).read_bytes()
    _verify_payload(receipt, body)
    with pytest.raises(ValueError, match="Finish limits"):
        _verify_payload(receipt | {"finish_limits": {"explanation_max_characters": 9999}}, body)
    exported = export_task_report(Path(result["task_path"]), tmp_path / "export")
    assert json.loads(Path(exported["json_path"]).read_bytes())["finish"]["explanation"] == explanation

    def reject(manifest, directory, **kwargs):
        report = public(manifest, directory, **kwargs)
        report["stages"]["new_original"]["status"] = "failed"
        report["status"] = "migration_required"
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    final = tasks.finalize_task(Path(task["task_path"]), comparator=reject)
    assert final["final_result"]["status"] == "no_change_claim_not_accepted"


def test_cli_creation_consumes_selected_policy(tmp_path, capsys):
    from upgrade_workbench.cli import main

    source = create(tmp_path, max_calls=60)
    config = {key: source["protocol"][key] for key in (
        "generation", "execution", "max_calls", "protocol_revision", "investigation_policy",
        "workflow_profile", "context_assistance", "navigation_assistance", "contract_audit_policy")}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    root = tmp_path / "cli-work"
    assert main(["--work-root", str(root), "task-create", source["manifest_path"],
                 "--config", str(path), "--budget", str(tmp_path / "ledger.sqlite"), "--seed", "none"]) == 0
    capsys.readouterr()
    task = tasks.inspect_task(next(root.glob("tasks/*/task.json")))
    assert task["protocol"]["max_calls"] == 60
    assert task["protocol"]["investigation_policy"] == config["investigation_policy"]


def test_completed_candidate_recovery_keeps_budget_and_input_identity(tmp_path, monkeypatch):
    from upgrade_workbench.service.recovery import recover_task
    owner = "a" * 32
    task = create(tmp_path, service_owner=owner, max_calls=2)
    path = Path(task["task_path"])
    snapshot = load_candidate(load_case(Path(task["manifest_path"])))
    name = load_case(Path(task["manifest_path"])).manifest.allowed_changes[0]
    action = {"type": "submit_candidate", "base_revision": snapshot.revision,
              "edits": [{"path": name, "old": snapshot.files[name].decode(),
                         "new": "# offline recovery fixture\n" + snapshot.files[name].decode()}]}
    complete = tasks.complete_request

    class Crash(BaseException):
        pass

    def crash(*args, **kwargs):
        complete(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        tasks.advance_task(path, api_key="unused", execution_owner=owner,
                           transport=lambda *_a, **_k: response(action))
    monkeypatch.setattr(tasks, "complete_request", complete)
    result = recover_task(path, Path(task["work_root"]), owner)
    assert result["status"] == "pending_review" and len(result["attempts"]) == 1
    assert result["budget"]["calls"] == 1
    assert result["candidate"]["revision"] != snapshot.revision
    assert recover_task(path, Path(task["work_root"]), owner) == result
    result = tasks.advance_task(path, execution_owner=owner,
        reviewed_revision=result["candidate"]["revision"], reviewed_sha256=result["candidate"]["sha256"],
        reviewer="offline", review_note="Synthetic public check only", review_only=True, comparator=public_run)
    assert result["status"] == "ready"
    contexts = []

    def transport(request, **kwargs):
        contexts.append(json.loads(json.loads(request.data)["messages"][1]["content"]))
        return response({"type": "unsupported"})

    result = tasks.advance_task(path, execution_owner=owner, api_key="unused", transport=transport)
    assert result["status"] == "budget_exhausted" and len(result["attempts"]) == 2
    assert contexts[0]["task_context"]["remaining_calls"] == 1
    assert contexts[0]["candidate"]["revision"] != snapshot.revision


@pytest.mark.parametrize("profile", ["p5", "workbench", "simple_tools"])
def test_recovery_reuses_frozen_finish_limits_after_receipt(tmp_path, monkeypatch, profile):
    from upgrade_workbench.service.recovery import recover_task

    owner = "b" * 32
    task = create(tmp_path, profile=profile, service_owner=owner, max_calls=1,
                  selected={"version": VERSION, "finish_limits": {"explanation_max_characters": 4000}})
    complete = tasks.complete_request

    class Crash(BaseException):
        pass

    def crash(*args, **kwargs):
        result = complete(*args, **kwargs)
        assert result["status"] == "agent_finished"
        raise Crash()

    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", execution_owner=owner,
                           transport=lambda *_a, **_k: response(unresolved(task, "x" * 2766)))
    result = recover_task(Path(task["task_path"]), Path(task["work_root"]), owner)
    assert len(result["attempts"]) == result["budget"]["calls"] == 1
    assert result["final_evaluation"] == "not_run"
    if profile == "simple_tools":
        assert len(result["finish"]["explanation"]) == 2766
    else:
        # 格式恢复通过，仍由正常合同门禁阻止尚有诊断空间的提前退出。
        assert result["status"] == "ready" and "Finish blocked" in result["edit_feedback"]


def test_required_evidence_can_never_exceed_its_effective_format_allowance():
    rows = [{"id": str(i), "kind": "probe", "revision": "r", "result": {"status": "failed",
             "assessment": {"probe_id": str(i), "conclusion": "setup_failed"}}} for i in range(10)]
    limits = policy({"version": VERSION})["finish_limits"]
    legacy = project([], rows, "r")
    current = project([], rows, "r", format_limits=limits)
    assert legacy["format_limits"]["evidence_refs_max_items"] == 8
    assert current["format_limits"]["evidence_refs_max_items"] == 10
    require_submission_evidence({"reason": "candidate_ready", "evidence_refs": current["required_evidence_refs"]}, current)
    with pytest.raises(ValueError, match="Missing"):
        require_submission_evidence({"reason": "candidate_ready", "evidence_refs": []}, current)
    from upgrade_workbench.generation.protocol_v6 import validate

    action = {"type": "finish", "reason": "unresolved", "explanation": "All observations retained.",
              "evidence_refs": current["required_evidence_refs"]}
    # 真实校验器同时接受派生额度；未启用的旧校验器仍保留八条限制。
    assert validate(None, action, None, workflow_profile="simple_tools",
                    finish_limits=current["format_limits"]) == action
    with pytest.raises(ValueError, match="eight"):
        validate(None, action, None, workflow_profile="simple_tools")
