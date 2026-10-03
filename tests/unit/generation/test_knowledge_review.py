"""普通复核、候选写盘前拒绝与恢复消费；模拟响应不冒充真实理解效果。"""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import (
    accept_action,
    context_from_reference,
    freeze_context,
    public_observation,
    read,
)
from upgrade_workbench.generation.knowledge_review import completion_result, require_action
from upgrade_workbench.generation.project_context import knowledge_result
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.knowledge_transfer import export_bundle, load_bundle
from upgrade_workbench.reporting import export_task_report
from upgrade_workbench.service.recovery import recover_task

from .test_knowledge_maintenance import revision
from .test_knowledge_transfer import CASE, OWNER, create, response
from .test_knowledge_transfer import exported as exported


def finish_review(ctx, disposition="unresolved"):
    entry = ctx["project_context"]["topic_maintenance"]["topics"][0]
    sources = [{key: ref[key] for key in ("path", "start_line", "end_line")}
               for ref in entry["topic"]["sources"]]
    return {"type": "complete_knowledge_review", "revision": ctx["candidate"]["revision"],
        "topics": [{"origin": entry["origin"], "topic_sha256": entry["topic_sha256"],
                    "disposition": disposition, "reason": "The current source has this responsibility; runtime is unobserved.",
                    "sources": sources}],
        "remaining_scope": "Other imported topics are historical; their runtime applicability remains unknown."}


def edit(ctx):
    return {"type": "submit_candidate", "base_revision": ctx["candidate"]["revision"], "edits": [
        {"path": "flaskbb/forum/models.py", "old": "import logging", "new": "import logging  # mechanism test"}]}


def test_review_refuses_candidate_before_writing_then_delivers_maintained_topics(exported, tmp_path):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v8")
    contexts = []

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        if len(contexts) == 1:
            assert ctx["project_context"]["knowledge_review"]["phase"] == "review"
            return response(edit(ctx))
        if len(contexts) == 2:
            assert "read-only" in ctx["task_context"]["edit_feedback"]
            assert not list(Path(task["work_root"]).glob("proposals/*/revision"))
            assert not list(Path(task["work_root"]).glob("proposals/*/candidate.patch"))
            return response(revision(ctx))
        if len(contexts) == 3:
            assert ctx["project_context"]["knowledge_review"]["phase"] == "review"
            return response(finish_review(ctx, "corrected"))
        assert ctx["project_context"]["knowledge_review"]["phase"] == "solve"
        entry = ctx["project_context"]["topic_maintenance"]["topics"][0]
        assert entry["topic"]["explanation"].startswith("The current implementation")
        completed = ctx["project_context"]["knowledge_review"]["completion"]["result"]
        assert completed["topics"][0]["update_observation_id"] == entry["update_observation_id"]
        assert completed["topics"][0]["topic_sha256"] == entry["topic_sha256"]
        return response(edit(ctx))

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_review", result.get("stop_reason")
    assert len(contexts) == 4 and len(result["candidate_history"]) == 1
    state = result["knowledge_review_state"]
    assert state["phase"] == "solve" and state["consumed_by"]["request_id"] == result["attempts"][3]["request_id"]
    # 旧 review 请求按冻结输入重建；后来的 solve 权限不回写旧请求。
    case = load_case(CASE)
    for attempt in result["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_bytes())
        frozen = read(receipt["diagnostic_context_reference"], Path(task["task_path"]).parent)
        base = load_candidate(case, frozen["current_candidate"])
        _verify_payload(receipt | {"candidate_reference": frozen["current_candidate"], "candidate_revision": base.revision},
                        Path(receipt["request_path"]).read_bytes())
    portable = export_bundle(Path(task["task_path"]), tmp_path / "maintained.json")
    bundle = load_bundle({key: portable[key] for key in ("path", "sha256")}, case)
    assert bundle["version"] == "knowledge-import-v2" and len(bundle["topics"][0]["updates"]) == 1
    report = json.loads(Path(export_task_report(Path(task["task_path"]), tmp_path / "report")["json_path"]).read_bytes())
    assert report["knowledge_review"]["consumer"]["attempt_index"] == 4
    assert report["knowledge_review"]["consumer"]["delivery_state"] == "response_received"
    assert report["final_evaluation"] == "not_run"
    assert tasks.inspect_task(Path(task["task_path"]))["knowledge_review_state"] == state
    completed_receipt = json.loads(Path(result["attempts"][2]["receipt"]).read_bytes())
    before = copy.deepcopy(result)
    tasks.accept_response(result, completed_receipt, case)
    assert result == before  # 候选已改变后，原完成回包也只能幂等确认。


@pytest.mark.parametrize("disposition", ["confirmed", "unresolved", "irrelevant"])
def test_review_may_complete_without_topic_updates(exported, tmp_path, disposition):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v8")
    contexts = []

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        if len(contexts) == 1:
            return response(finish_review(ctx, disposition))
        assert ctx["project_context"]["knowledge_review"]["completion"]["result"]["topics"][0]["disposition"] == disposition
        assert all(row["update_observation_id"] is None for row in ctx["project_context"]["topic_maintenance"]["topics"])
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review"
    assert len(result["observations"]) == 1 and len(contexts) == 2


def test_completion_requires_current_topic_identity_and_actual_correction(exported, tmp_path):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v8")
    case, snapshot = load_case(CASE), load_candidate(load_case(CASE))
    runtime = context_from_reference(freeze_context(task), case, snapshot)
    ctx = {"candidate": {"revision": snapshot.revision}, "project_context": runtime}
    action = finish_review(ctx, "corrected")
    with pytest.raises(ValueError, match="persisted topic update"):
        completion_result(case, snapshot, action, runtime["topic_maintenance"])
    action = finish_review(ctx)
    action["topics"][0]["topic_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="supplied explanation"):
        completion_result(case, snapshot, action, runtime["topic_maintenance"])
    with pytest.raises(ValueError, match="source changed"):
        require_action(runtime["knowledge_review_binding"], {"type": "read_source"}, replace(snapshot, revision="a" * 64))
    for kind in ("submit_candidate", "restore_candidate", "finish", "run_public_checks", "propose_probe", "run_probe"):
        with pytest.raises(ValueError, match="read-only"):
            require_action(runtime["knowledge_review_binding"], {"type": kind}, snapshot)


def interrupted_review(task, monkeypatch, action_for):
    complete = tasks.complete_request

    class Crash(BaseException):
        pass

    def crash(prepared, **kwargs):
        complete(prepared, **kwargs)
        raise Crash()

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        return response(action_for(ctx))

    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)


def test_completion_recovery_is_once_and_next_request_consumes_it(exported, tmp_path, monkeypatch):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v8")
    with monkeypatch.context() as patcher:
        interrupted_review(task, patcher, finish_review)
    restored = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    twice = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert restored["knowledge_review_state"]["phase"] == "solve"
    assert len(twice["observations"]) == len(twice["attempts"]) == 1
    assert twice["knowledge_review_state"]["consumed_by"] is None

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        assert ctx["project_context"]["knowledge_review"]["completion"]["id"] == twice["observations"][0]["id"]
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    continued = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert continued["status"] == "pending_diagnostic_review"
    assert continued["knowledge_review_state"]["consumed_by"]["request_id"] == continued["attempts"][1]["request_id"]
    receipt_path = Path(continued["attempts"][0]["receipt"])
    completed = json.loads(receipt_path.read_bytes())
    before = copy.deepcopy(continued)
    tasks.accept_response(continued, completed, load_case(CASE))
    assert continued == before
    forged = copy.deepcopy(completed)
    forged["action"]["topics"][0]["reason"] = "A different completion is not a replay."
    with pytest.raises(ValueError, match="completed phase"):
        tasks.accept_response(continued, forged, load_case(CASE))
    assert continued == before
    receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="receipt changed"):
        tasks.accept_response(continued, completed, load_case(CASE))
    assert continued == before


def test_oversized_completion_does_not_publish_or_leave_review(exported, tmp_path):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v8")
    calls = []

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        calls.append(ctx)
        if len(calls) == 1:
            action = finish_review(ctx)
            action["remaining_scope"] = "待核验" * 666
            sources = [{"path": "flaskbb/forum/models.py", "start_line": i, "end_line": i} for i in range(1, 13)]
            action["topics"] = [{"origin": entry["origin"], "topic_sha256": entry["topic_sha256"],
                "disposition": "unresolved", "reason": "待核验" * 500, "sources": sources}
                for entry in ctx["project_context"]["topic_maintenance"]["topics"]]
            return response(action)
        if len(calls) == 2:
            assert "Public observation exceeds capacity" in ctx["task_context"]["edit_feedback"]
            current = tasks.inspect_task(Path(task["task_path"]))
            assert current["knowledge_review_state"]["phase"] == "review"
            assert current["observations"] == []
            assert not list(Path(task["task_path"]).parent.glob("observations/*.json"))
            return response(finish_review(ctx))
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review" and len(calls) == 3
    completed = public_observation(result, result["knowledge_review_state"]["completion_reference"])
    assert completed["action"] == {"type": "complete_knowledge_review", "revision": completed["revision"]}


def test_v8_reads_imported_and_revised_effective_topics(exported, tmp_path):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v8")
    calls = []

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        calls.append(ctx)
        entry = ctx["project_context"]["topic_maintenance"]["topics"][0]
        if len(calls) in {1, 3}:
            return response({"type": "read_project_topic", "revision": ctx["candidate"]["revision"], "origin": entry["origin"]})
        if len(calls) == 2:
            return response(revision(ctx))
        if len(calls) == 4:
            return response(finish_review(ctx, "corrected"))
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review" and len(calls) == 5
    observations = [public_observation(result, ref) for ref in result["observations"]]
    reads = [row["result"] for row in observations if row["action"]["type"] == "read_project_topic"]
    assert reads[0]["update_observation_id"] is None
    assert reads[1]["update_observation_id"] == observations[1]["id"]
    assert reads[1]["topic_sha256"] != reads[0]["topic_sha256"]
    assert reads[1]["topic"]["explanation"].startswith("The current implementation")
    case = load_case(CASE)
    snapshot = load_candidate(case)
    current = calls[-1]["project_context"]["topic_maintenance"]
    action = {"type": "read_project_topic", "revision": snapshot.revision, "topic_id": reads[0]["origin"]["topic_id"]}
    assert knowledge_result(case, snapshot, action, context_policy={"version": "project-context-v8", "context_tokens": 1_000_000,
        "framing_reserve_tokens": 4096}, current_topics=current)["topic_sha256"] == reads[1]["topic_sha256"]
    duplicate = copy.deepcopy(current)
    duplicate["topics"].append(copy.deepcopy(duplicate["topics"][0]))
    duplicate["topics"][-1]["origin"]["sha256"] = "a" * 64
    with pytest.raises(ValueError, match="ambiguous"):
        knowledge_result(case, snapshot, action, context_policy=result["protocol"]["project_context_policy"], current_topics=duplicate)
    with pytest.raises(ValueError, match="No recorded project knowledge"):
        knowledge_result(case, snapshot, action, context_policy={"version": "project-context-v7"})


def test_solve_investigator_recovery_precedes_actual_solver_consumption(exported, tmp_path, monkeypatch):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v8", investigator_policy="required_once")
    complete = tasks.complete_request

    class Crash(BaseException):
        pass

    def crash(prepared, **kwargs):
        report = complete(prepared, **kwargs)
        if prepared["output_format"] == "investigator_actions":
            raise Crash()
        return report

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        if ctx["project_context"]["knowledge_review"]["phase"] == "review":
            return response(finish_review(ctx))
        return response({"type": "handoff", "observed_facts": [], "scope_limits": ["Source review only."],
            "remaining_hypotheses": [], "conflicts": [], "next_discriminating_action": None})

    with monkeypatch.context() as patcher:
        patcher.setattr(tasks, "complete_request", crash)
        with pytest.raises(Crash):
            tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    restored = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert [item["role"] for item in restored["attempts"]] == ["solver", "investigator"]
    assert restored["knowledge_review_state"]["consumed_by"] is None

    def solve(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=solve, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review"
    assert result["knowledge_review_state"]["consumed_by"]["request_id"] == result["attempts"][2]["request_id"]


def test_solve_auditor_attempt_can_recover_without_becoming_solver_consumer(exported, tmp_path, monkeypatch):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v8", contract_audit_policy="bounded")
    with monkeypatch.context() as patcher:
        interrupted_review(task, patcher, finish_review)
    task = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    complete = tasks.complete_request

    class Crash(BaseException):
        pass

    def crash(prepared, **kwargs):
        complete(prepared, **kwargs)
        raise Crash()

    def audit(*_args, **_kwargs):
        return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps({"summary": "Offline audit.",
                "audit": {"verdict": "no_specific_conflict", "questions": []}})}}]}).encode()

    with monkeypatch.context() as patcher:
        patcher.setattr(tasks, "complete_request", crash)
        with pytest.raises(Crash):
            tasks._run_contract_audit(task, load_case(CASE), BudgetLedger(Path(task["budget_path"])),
                api_key="unused", transport=audit)
    restored = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert restored["status"] == "ready"
    assert restored["attempts"][-1]["role"] == "contract_auditor"
    assert restored["attempts"][-1]["knowledge_review_binding"]["phase"] == "solve"
    assert restored["knowledge_review_state"]["consumed_by"] is None


def test_direct_acceptance_and_recovery_reject_forged_candidate_in_review(exported, tmp_path, monkeypatch):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v8")
    with monkeypatch.context() as patcher:
        interrupted_review(task, patcher, lambda ctx: {"type": "read_source", "revision": ctx["candidate"]["revision"],
            "path": "flaskbb/forum/models.py", "start_line": 1, "end_line": 12})
    current = tasks.inspect_task(Path(task["task_path"]))
    receipt_path = Path(current["attempts"][0]["receipt"])
    receipt = json.loads(receipt_path.read_bytes())
    ctx = json.loads(json.loads(Path(receipt["request_path"]).read_bytes())["messages"][1]["content"])
    forged = receipt | {"action": edit(ctx), "status": "pending_review"}
    before = copy.deepcopy(current)
    with pytest.raises(ValueError, match="read-only"):
        accept_action(current, forged, load_case(CASE))
    assert current == before
    raw = response(forged["action"])
    Path(forged["response_path"]).write_bytes(raw)
    forged["response_sha256"] = hashlib.sha256(raw).hexdigest()
    receipt_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(ValueError, match="read-only"):
        recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert not list(Path(task["work_root"]).glob("proposals/*/revision"))
    assert not list(Path(task["work_root"]).glob("proposals/*/candidate.patch"))


def test_v8_without_import_has_no_review_state_or_extra_phase(tmp_path):
    task = create(tmp_path, version="project-context-v8")
    assert "knowledge_review_state" not in task

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        assert "knowledge_review" not in ctx["project_context"]
        assert "knowledge_review_binding" not in ctx["diagnostic_state"]
        return response(edit(ctx))

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_review" and len(result["attempts"]) == 1
