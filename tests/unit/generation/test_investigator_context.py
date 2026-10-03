"""来源优先视图的普通入口和恢复边界；假响应只验证机制，不证明模型理解。"""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from upgrade_workbench import cli, tasks
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import (
    context_from_reference,
    freeze_context,
    public_investigator_handoff,
    read,
)
from upgrade_workbench.generation import investigator_context
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.generation.request import _json_bytes
from upgrade_workbench.reporting import _public_protocol
from upgrade_workbench.service.recovery import recover_task

from .test_knowledge_maintenance import revision
from .test_knowledge_review import finish_review
from .test_knowledge_transfer import CASE, OWNER, create, response
from .test_knowledge_transfer import exported as exported

AUTHOR = "AUTHOR_CONFIRMATION_SENTINEL_DO_NOT_SEND"
CONFIG = investigator_context.POLICY


def operation(root, spec=None, **changes):
    options = {"version": "project-context-v10", "max_calls": 14,
               "contract_audit_policy": "disabled", "investigator_policy": "required_once",
               "investigator_context_policy": CONFIG, "investigator_max_calls": 6,
               "solver_reserved_calls": 4}
    return create(root, spec, **(options | changes))


def handoff():
    return {"type": "handoff", "observed_facts": [], "scope_limits": ["Source-only review."],
            "remaining_hypotheses": [{"hypothesis": "Runtime behavior remains unknown.",
                                      "evidence_refs": [], "next_check": "Run the relevant public checks."}],
            "conflicts": [], "next_discriminating_action": None}


def context(request):
    return json.loads(json.loads(request.data)["messages"][1]["content"])


@pytest.mark.parametrize("config", [CONFIG, investigator_context.FOLLOWUP_POLICY, investigator_context.RESILIENT_POLICY])
def test_cli_freezes_opt_in_and_exports_policy_without_calling_provider(tmp_path, capsys, config):
    template = operation(tmp_path / "template", investigator_context_policy=config)
    options = {key: template["protocol"][key] for key in (
        "generation", "execution", "max_calls", "protocol_revision", "project_context_policy",
        "contract_audit_policy", "investigator_policy", "investigator_context_policy")}
    options.update(investigator_max_calls=6, solver_reserved_calls=4)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(options), encoding="utf-8")
    assert cli.main(["--work-root", str(tmp_path / "cli"), "task-create", str(CASE),
                     "--config", str(path), "--budget", template["budget_path"], "--seed", "none"]) == 0
    task = tasks.inspect_task(Path(json.loads(capsys.readouterr().out)["task_path"]))
    assert task["protocol"]["investigator_context_policy"] == config
    assert task["protocol"]["role_budget"] == {"investigator_max_calls": 6, "solver_reserved_calls": 4}
    assert task["protocol"]["max_calls"] == 14 and task["attempts"] == []
    assert _public_protocol(task["protocol"])["investigator_context_policy"] == config


@pytest.mark.parametrize("changes", [
    {"investigator_policy": "disabled"}, {"version": "project-context-v9"},
    {"investigator_max_calls": 31}, {"investigator_max_calls": True},
    {"solver_reserved_calls": 31}, {"max_calls": 9},
    {"investigator_context_policy": {"version": "unknown"}},
])
def test_new_budget_requires_valid_explicit_policy(tmp_path, changes):
    with pytest.raises(ValueError):
        operation(tmp_path, **changes)


def test_legacy_budget_and_projection_remain_unchanged(tmp_path):
    task = create(tmp_path / "old", version="project-context-v10", investigator_policy="required_once")
    assert "investigator_context_policy" not in task["protocol"]
    assert task["protocol"]["role_budget"] == {"investigator_max_calls": 3, "solver_reserved_calls": 2}
    runtime = {"observations": [], "issues": [AUTHOR]}
    assert investigator_context.project(runtime, "investigator_actions") is runtime
    assert investigator_context.task_view({"edit_feedback": AUTHOR}, runtime) == {"edit_feedback": AUTHOR}
    with pytest.raises(ValueError, match="bounded Investigator"):
        create(tmp_path / "invalid", version="project-context-v10", max_calls=14,
               investigator_policy="required_once", investigator_max_calls=6, solver_reserved_calls=4)
    protocol = copy.deepcopy(operation(tmp_path / "new")["protocol"])
    protocol["decision_objective"] = {"schema_version": 1}
    with pytest.raises(ValueError, match="ordinary P6/v10"):
        tasks._require_current_protocol(protocol)
    configurable = operation(tmp_path / "larger", investigator_max_calls=7, solver_reserved_calls=5)
    assert configurable["protocol"]["role_budget"] == {"investigator_max_calls": 7, "solver_reserved_calls": 5}
    assert configurable["protocol"]["max_calls"] == 14


@pytest.mark.parametrize("config", [CONFIG, investigator_context.FOLLOWUP_POLICY])
def test_author_conclusions_hidden_across_rereads_then_solver_keeps_handoff(exported, tmp_path, monkeypatch, config):
    spec, _ = exported
    task = operation(tmp_path, spec, investigator_context_policy=config)
    prepare = tasks.prepare_case_proposal
    frozen_before = {}

    def injected(*args, **kwargs):
        if kwargs["output_format"] == "investigator_actions":
            kwargs["task_context"]["edit_feedback"] = AUTHOR
            ref = kwargs["diagnostic_context_reference"]
            frozen_before[ref["path"]] = Path(ref["path"]).read_bytes()
        return prepare(*args, **kwargs)

    monkeypatch.setattr(tasks, "prepare_case_proposal", injected)
    seen = []
    investigator_calls = []
    solver_handoffs = []
    completion_id = None

    def transport(request, **_):
        nonlocal completion_id
        ctx = context(request)
        seen.append(ctx)
        runtime = ctx["diagnostic_state"]
        rev = ctx["candidate"]["revision"]
        if runtime.get("context_role") == "investigator":
            investigator_calls.append(ctx)
            assert AUTHOR not in json.dumps(ctx)
            assert "knowledge_review" not in ctx["project_context"]
            assert "issues" not in runtime and "recent_actions" not in runtime
            assert any("class User" in row["text"] for row in ctx["source_files"])
            assert runtime["withheld_observations"]["count"] >= 1
            assert "INVESTIGATOR_CALL_BUDGET" in ctx["task_context"]["edit_feedback"]
            number = len(investigator_calls)
            if number == 1:
                completion_id = runtime["knowledge_review_binding"]["completion_reference"]["id"]
                return response({"type": "get_observation", "observation_id": completion_id})
            if number == 2:
                assert runtime["withheld_observations"]["requested_latest_id"] == completion_id
                entry = ctx["project_context"]["topic_maintenance"]["topics"][0]
                assert "status" not in entry and "update_observation_id" not in entry
                return response({"type": "read_project_topic", "revision": rev, "origin": entry["origin"]})
            if number == 3:
                assert runtime["withheld_observations"]["requested_latest_id"] is None
                assert any(row["action"]["type"] == "read_project_topic" for row in runtime["observations"])
                return response({"type": "outline_source", "revision": rev,
                                 "path": "flaskbb/user/models.py", "cursor": None})
            assert any(row["action"]["type"] == "outline_source" for row in runtime["observations"])
            return response(handoff())
        if ctx["project_context"]["knowledge_review"]["phase"] == "review":
            action = finish_review(ctx, "confirmed")
            action["topics"][0]["reason"] = AUTHOR
            raw = json.loads(response(action))
            body = json.loads(raw["choices"][0]["message"]["content"])
            body["summary"] = AUTHOR
            raw["choices"][0]["message"]["content"] = json.dumps(body)
            return json.dumps(raw).encode()
        solver_handoffs.append(runtime["investigator_handoffs"])
        assert runtime["investigator_handoffs"][0]["remaining_hypotheses"]
        if len(solver_handoffs) == 1:
            return response({"type": "read_source", "revision": rev,
                             "path": "flaskbb/user/models.py", "start_line": 90, "end_line": 110})
        if len(solver_handoffs) == 2:
            return response(revision(ctx))
        return response({"type": "run_public_checks", "revision": rev})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review", result.get("stop_reason")
    assert len(investigator_calls) == 4 and len(solver_handoffs) == 3
    assert all(items == solver_handoffs[0] for items in solver_handoffs)
    assert len(result["consumed_investigator_handoffs"]) == 1
    assert result["knowledge_review_state"]["phase"] == "solve"
    for name, contents in frozen_before.items():
        assert Path(name).read_bytes() == contents
    completion = next(ref for ref in result["observations"] if ref["id"] == completion_id)
    assert AUTHOR in Path(completion["path"]).read_text(encoding="utf-8")
    case = load_case(CASE)
    snapshot = load_candidate(case, result["current_candidate"])
    stale = public_investigator_handoff(result, result["investigator_handoffs"][0],
                                       snapshot=replace(snapshot, revision="f" * 64))
    assert stale["stale"] is True
    for attempt in result["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_bytes())
        frozen = read(receipt["diagnostic_context_reference"], Path(result["task_path"]).parent)
        base = load_candidate(case, frozen["current_candidate"])
        _verify_payload(receipt | {"candidate_reference": frozen["current_candidate"], "candidate_revision": base.revision},
                        Path(receipt["request_path"]).read_bytes())

    investigator = next(row for row in result["attempts"] if row["role"] == "investigator")
    receipt = json.loads(Path(investigator["receipt"]).read_bytes())
    for location in ("task", "claim", "role", "policy"):
        forged = copy.deepcopy(receipt)
        payload = json.loads(Path(receipt["request_path"]).read_bytes())
        ctx = json.loads(payload["messages"][1]["content"])
        if location == "task":
            ctx["task_context"]["edit_feedback"] = AUTHOR
        elif location == "claim":
            ctx["project_context"]["topic_maintenance"]["topics"][0]["topic"]["explanation"] = AUTHOR
        elif location == "role":
            forged["context_role"] = "solver"
        else:
            forged.pop("investigator_context_policy")
        payload["messages"][1]["content"] = _json_bytes(ctx).decode()
        data = _json_bytes(payload)
        forged["request_sha256"] = hashlib.sha256(data).hexdigest()
        forged["public_context_sha256"] = hashlib.sha256(_json_bytes(ctx)).hexdigest()
        forged["task_context_sha256"] = hashlib.sha256(_json_bytes(ctx["task_context"])).hexdigest()
        with pytest.raises(ValueError):
            _verify_payload(forged, data)


def test_handoff_crash_recovers_without_duplicate_request_or_solver_consumption(exported, tmp_path, monkeypatch):
    spec, _ = exported
    task = operation(tmp_path, spec)
    complete = tasks.complete_request

    class Crash(BaseException):
        pass

    def crash(prepared, **kwargs):
        result = complete(prepared, **kwargs)
        if prepared["output_format"] == "investigator_actions":
            raise Crash()
        return result

    calls = []

    def transport(request, **_):
        ctx = context(request)
        calls.append(ctx)
        return response(handoff() if ctx["diagnostic_state"].get("context_role") == "investigator"
                        else finish_review(ctx, "unresolved"))

    with monkeypatch.context() as patcher:
        patcher.setattr(tasks, "complete_request", crash)
        with pytest.raises(Crash):
            tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    restored = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert len(calls) == 2
    assert [row["role"] for row in restored["attempts"]] == ["solver", "investigator"]
    assert restored["knowledge_review_state"]["consumed_by"] is None

    def solve(request, **_):
        ctx = context(request)
        assert ctx["diagnostic_state"]["investigator_handoffs"]
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=solve, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review"
    assert len(result["attempts"]) == 3
    assert result["knowledge_review_state"]["consumed_by"]["request_id"] == result["attempts"][2]["request_id"]
    frozen = freeze_context(result, role="solver")
    runtime = context_from_reference(frozen, load_case(CASE), load_candidate(load_case(CASE)))
    assert runtime["investigator_handoffs"] and not runtime["investigator_handoffs"][0]["stale"]


def test_projection_is_copy_and_preserves_sources_and_locked_facts():
    source = {"id": "source", "kind": "source", "revision": "r", "stale": False,
              "action": {"type": "read_source"}, "result": {"text": "original source bytes"}}
    authored = {"id": "old", "kind": "project_context", "revision": "r", "stale": False,
                "action": {"type": "complete_knowledge_review"}, "result": {"reason": AUTHOR}}
    runtime = {"investigator_context_policy": CONFIG, "context_role": "investigator", "task_id": "t",
               "source_state": {"revision": "r"}, "observations": [source, authored],
               "investigator_source_observation_ids": ["source"],
               "observation_index": [{key: row[key] for key in ("id", "kind", "revision", "stale")}
                                     for row in (source, authored)],
               "latest_observation_id": "old", "observation_output": {"text": AUTHOR},
               "recent_actions": [{"model_summary": AUTHOR}], "issues": [AUTHOR],
               "diagnostic_workset": {"hypotheses": [AUTHOR]}, "contract_audits": [AUTHOR],
               "review_history": [AUTHOR], "historical_inputs": [AUTHOR],
               "knowledge_review": {"completion": AUTHOR},
               "dependency_facts": [{"result": "locked dependency source", "stale": False}],
               "dependency_query_feedback": {"code": "dependency_query_failed_no_automatic_replay", "query_id": "q"}}
    original = copy.deepcopy(runtime)
    projected = investigator_context.project(runtime, "investigator_actions")
    assert runtime == original and AUTHOR not in json.dumps(projected)
    assert projected["observations"] == [source]
    assert projected["dependency_facts"] == runtime["dependency_facts"]
    assert projected["dependency_query_feedback"] == runtime["dependency_query_feedback"]
    projected["observations"][0]["result"]["text"] = "changed view"
    assert runtime == original


@pytest.mark.parametrize("action", [None, [], "run_probe"])
def test_malformed_actions_defer_to_existing_protocol_validation(action):
    from upgrade_workbench.generation.actions import AgentActionError
    from upgrade_workbench.generation.protocol_v6 import validate

    investigator_context.require_action(action, CONFIG, "investigator")
    with pytest.raises(AgentActionError):
        validate(None, action, None, role="investigator")


@pytest.mark.parametrize("config", [CONFIG, investigator_context.FOLLOWUP_POLICY])
def test_current_role_tool_errors_remain_visible_and_probes_do_not_execute(exported, tmp_path, config):
    spec, _ = exported
    task = operation(tmp_path, spec, investigator_context_policy=config)
    calls = []

    def transport(request, **_):
        ctx = context(request)
        runtime = ctx["diagnostic_state"]
        if runtime.get("context_role") != "investigator":
            if ctx["project_context"]["knowledge_review"]["phase"] == "review":
                return response(finish_review(ctx, "unresolved"))
            return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})
        calls.append(ctx)
        if len(calls) == 1:
            assert "investigator_action_feedback" not in runtime
            return response({"type": "get_observation", "observation_id": "e" * 64})
        error = runtime["investigator_action_feedback"]
        assert error["attempt_index"] == ctx["task_context"]["attempt_index"] - 1
        if len(calls) == 2:
            assert error["action"] == "get_observation" and "Unknown" in error["feedback"]
            return response({"type": "run_probe"})
        assert "cannot author or run probes" in error["feedback"]
        return response(handoff())

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review", result.get("stop_reason")
    assert len(calls) == 3 and result["probes"] == []
    assert result["diagnostic_runs"] == []
    assert result["attempts"][2]["role"] == "investigator"
    investigator_context.require_action({"type": "run_probe"}, None, "investigator")
    investigator_context.require_action({"type": "run_probe"}, CONFIG, "solver")


@pytest.mark.parametrize("config", [CONFIG, investigator_context.FOLLOWUP_POLICY])
def test_source_first_report_preserves_claim_delivery_without_import_metadata(exported, tmp_path, config):
    from upgrade_workbench.reporting import export_task_report

    spec, _ = exported
    task = operation(tmp_path / "task", spec, investigator_context_policy=config)

    def transport(request, **_):
        ctx = context(request)
        if ctx["diagnostic_state"].get("context_role") == "investigator":
            return response(handoff())
        if ctx["project_context"]["knowledge_review"]["phase"] == "review":
            return response(finish_review(ctx, "unresolved"))
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    report = export_task_report(Path(result["task_path"]), tmp_path / "report")
    rows = json.loads(Path(report["json_path"]).read_bytes())["knowledge_import_consumption"]
    assert len(rows) == 3
    assert rows[0]["bundle_sha256"] == rows[2]["bundle_sha256"] == spec["sha256"]
    claims = rows[1]
    assert claims["delivery_view"] == "source_first_claims"
    assert set(claims["topic_ids"]) == {"worker", "answers", "contract"}
    assert len(claims["topic_claims"]) == 3
    assert all(entry["topic_sha256"] and entry["origin"] for entry in claims["topic_claims"])
    assert not {"bundle_sha256", "source_task_id", "exporting_task_id"}.intersection(claims)
    assert claims["delivery_state"] == "response_received"


@pytest.mark.parametrize("unknown", ["task_policy", "receipt_policy", "format", "role", "view_policy"])
def test_missing_import_is_not_silently_reported_as_claims(tmp_path, unknown):
    from upgrade_workbench.reporting import _knowledge_import_consumption

    task = {"protocol": {"investigator_context_policy": CONFIG}}
    receipt = {"investigator_context_policy": CONFIG, "output_format": "investigator_actions"}
    ctx = {"diagnostic_state": {"context_role": "investigator"},
           "project_context": {"investigator_context_policy": CONFIG}}
    if unknown == "task_policy":
        task["protocol"].clear()
    elif unknown == "receipt_policy":
        receipt.pop("investigator_context_policy")
    elif unknown == "format":
        receipt["output_format"] = "protocol_v6_actions"
    elif unknown == "role":
        ctx["diagnostic_state"]["context_role"] = "solver"
    else:
        ctx["project_context"].clear()
    with pytest.raises(ValueError, match="unrecognized request view"):
        _knowledge_import_consumption(task, tmp_path, [(1, receipt, tmp_path / "unused", ctx)])


def test_v2_followup_derives_only_post_review_evidence_and_keeps_stale_boundaries():
    def row(identity, kind, *, stale=False):
        return {"id": identity, "kind": kind, "revision": "r", "stale": stale}

    runtime = {"investigator_context_policy": investigator_context.FOLLOWUP_POLICY,
        "context_role": "solver", "source_state": {"revision": "r2"},
        "knowledge_review_binding": {"phase": "solve", "completion_reference": {"id": "review"}},
        "observation_index": [row("old-check", "public_checks"), row("review", "project_context"),
            row("check", "public_checks", stale=True), row("probe", "probe"), row("latest", "source")],
        "investigator_handoffs": [{"id": "handoff", "revision": "r", "stale": True,
                                    "observed_facts": [{"statement": "Not copied into the index."}]}]}
    before = copy.deepcopy(runtime)
    projected = investigator_context.solver_followup(runtime)
    assert [item["id"] for item in projected["subsequent_execution_observations"]] == ["check", "probe"]
    assert projected["subsequent_execution_observations"][0]["stale"]
    assert projected["available_handoffs"] == [{"id": "handoff", "revision": "r", "stale": True}]
    assert runtime == before
    projected["subsequent_execution_observations"][0]["stale"] = False
    assert runtime == before
    for changes in ({"investigator_context_policy": CONFIG}, {"context_role": "investigator"},
                    {"knowledge_review_binding": {"phase": "review"}}):
        assert investigator_context.solver_followup(runtime | changes) is None
    with pytest.raises(ValueError, match="one frozen observation"):
        investigator_context.solver_followup(runtime | {"observation_index": []})


@pytest.mark.parametrize("config", [CONFIG, investigator_context.FOLLOWUP_POLICY, investigator_context.RESILIENT_POLICY])
def test_success_output_pages_and_maintained_topic_reach_following_solver(exported, tmp_path, config):
    from test_diagnostic_operations import public_run

    from upgrade_workbench.diagnostics import public_observation, store

    spec, _ = exported
    task = operation(tmp_path, spec, investigator_context_policy=config)
    initial_contexts = []

    def initial(request, **_):
        ctx = context(request)
        initial_contexts.append(ctx)
        if ctx["diagnostic_state"].get("context_role") == "investigator":
            return response(handoff())
        if ctx["project_context"]["knowledge_review"]["phase"] == "review":
            assert "knowledge_review_followup" not in ctx["project_context"]
            return response(finish_review(ctx, "unresolved"))
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    task = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=initial, execution_owner=OWNER)
    assert task["status"] == "pending_diagnostic_review"
    diagnostic = config in (investigator_context.FOLLOWUP_POLICY, investigator_context.RESILIENT_POLICY)
    first_solver = initial_contexts[-1]["project_context"]
    assert ("knowledge_review_followup" in first_solver) is diagnostic
    if diagnostic:
        assert first_solver["knowledge_review_followup"]["subsequent_execution_observations"] == []
    else:
        assert " Knowledge review is complete. Use its topic dispositions and remaining uncertainty in the business task; review completion is not behavioral evidence." in first_solver["instructions"]

    output = "owned successful measurement\n" * 360 + "TAIL_MEASUREMENT: scope is this member only\n"

    def comparator(manifest, directory, **kwargs):
        assert kwargs["check_group"] == "feedback"
        assert kwargs.get("diagnostic", False) is diagnostic
        if not diagnostic:
            assert "diagnostic" not in kwargs
        report = public_run(manifest, directory, **kwargs)
        for name in ("old_original", "new_original"):
            path = directory / (name + ".log")
            path.write_text(output if diagnostic else "1 passed\n", encoding="utf-8")
            report["stages"][name] = report["stages"][name] | {"stdout_path": str(path)}
        Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        return report

    task = tasks.advance_task(Path(task["task_path"]), execution_owner=OWNER, review_only=True,
        diagnostic_request_id=task["pending_diagnostic"]["request"]["id"], reviewer="test",
        review_note="Owned public-output fixture only", comparator=comparator)
    assert task["status"] == "ready"
    observed = public_observation(task, task["observations"][-1])
    assert observed["result"]["status"] == "passed"
    if not diagnostic:
        assert "TAIL_MEASUREMENT" not in json.dumps(observed)
        return
    cursor = observed["result"]["stages"]["new_original"]["output_streams"][0]["cursor"]
    assert observed["result"]["stages"]["new_original"]["output_excerpt_truncated"]
    consumed = []
    pages = []
    maintained = "Owned measurement read in full; its recorded scope applies to this member only."

    def continue_solver(request, **_):
        ctx = context(request)
        consumed.append(ctx)
        if config == investigator_context.RESILIENT_POLICY:
            assert ctx["diagnostic_state"]["latest_public_execution"] == observed | {"stale": False}
            if len(consumed) <= 3:
                start = (1, 10, 20)[len(consumed) - 1]
                return response({"type": "read_source", "revision": ctx["candidate"]["revision"],
                    "path": "flaskbb/user/models.py", "start_line": start, "end_line": start + 3})
            if len(consumed) == 4:
                assert observed["id"] not in {item["id"] for item in ctx["diagnostic_state"]["observations"]}
        followup = ctx["project_context"]["knowledge_review_followup"]
        assert followup["subsequent_execution_observations"] == [{
            "id": observed["id"], "kind": "public_checks", "revision": observed["revision"], "stale": False}]
        assert followup["available_handoffs"] and "observed_facts" not in followup["available_handoffs"][0]
        page = ctx["diagnostic_state"].get("observation_output")
        if page is None and len(consumed) == (4 if config == investigator_context.RESILIENT_POLICY else 1):
            return response({"type": "get_observation", "observation_id": observed["id"], "cursor": cursor})
        if page is not None:
            pages.append(page["text"])
            if not page["eof"]:
                return response({"type": "get_observation", "observation_id": observed["id"], "cursor": page["next_cursor"]})
            assert "TAIL_MEASUREMENT" in "".join(pages)
            action = revision(ctx)
            action["page"]["explanation"] = maintained
            return response(action)
        entry = ctx["project_context"]["topic_maintenance"]["topics"][0]
        assert entry["topic"]["explanation"] == maintained and entry["update_observation_id"]
        return response({"type": "submit_candidate", "base_revision": ctx["candidate"]["revision"], "edits": [
            {"path": "flaskbb/forum/models.py", "old": "import logging", "new": "import logging  # owned fixture"}]})

    task = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=continue_solver, execution_owner=OWNER)
    assert task["status"] == "pending_review", task.get("stop_reason")
    assert len(pages) >= 2 and "".join(pages) == output.rstrip("\n")
    assert len(task["diagnostic_runs"]) == 1 and task["final_evaluation"] == "not_run"
    # 当前任务未做独立验收；派生视图也不得从任何隐藏验收结果读取内容。
    for attempt in task["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_bytes())
        frozen = read(receipt["diagnostic_context_reference"], Path(task["task_path"]).parent)
        base = load_candidate(load_case(CASE), frozen["current_candidate"])
        _verify_payload(receipt | {"candidate_reference": frozen["current_candidate"], "candidate_revision": base.revision},
                        Path(receipt["request_path"]).read_bytes())
    receipt = json.loads(Path(task["attempts"][-1]["receipt"]).read_bytes())
    frozen = read(receipt["diagnostic_context_reference"], Path(task["task_path"]).parent)
    base = load_candidate(load_case(CASE), frozen["current_candidate"])
    if config == investigator_context.RESILIENT_POLICY:
        runtime = context_from_reference(receipt["diagnostic_context_reference"], load_case(CASE), base)
        isolated = investigator_context.project(runtime | {"context_role": "investigator"}, "investigator_actions")
        assert isolated["latest_public_execution"] == observed | {"stale": False}
        # 隐藏规则由冻结视图拥有；不扩大普通task的隐藏权限来满足夹具。
        hidden = store(Path(task["task_path"]).parent / "test-hidden-context", frozen | {
            "hidden_observation_ids": [observed["id"]]})
        assert context_from_reference(hidden, load_case(CASE), base)["latest_public_execution"] is None
    receipt.update(candidate_reference=frozen["current_candidate"], candidate_revision=base.revision)
    payload = json.loads(Path(receipt["request_path"]).read_bytes())
    ctx = json.loads(payload["messages"][1]["content"])
    ctx["project_context"]["knowledge_review_followup"]["subsequent_execution_observations"] = []
    payload["messages"][1]["content"] = _json_bytes(ctx).decode()
    data = _json_bytes(payload)
    receipt.update(request_sha256=hashlib.sha256(data).hexdigest(),
                   public_context_sha256=hashlib.sha256(_json_bytes(ctx)).hexdigest())
    with pytest.raises(ValueError, match="source selection or diagnostic observation changed"):
        _verify_payload(receipt, data)


@pytest.mark.parametrize("config", [investigator_context.FOLLOWUP_POLICY, investigator_context.RESILIENT_POLICY])
def test_exhausted_invalid_handoff_returns_only_v3_to_solver(exported, tmp_path, config):
    spec, _ = exported
    task = operation(tmp_path, spec, investigator_context_policy=config, investigator_max_calls=1,
                     solver_reserved_calls=2, max_calls=8)
    seen = []

    def transport(request, **_):
        ctx = context(request)
        seen.append(ctx)
        runtime = ctx["diagnostic_state"]
        if runtime.get("context_role") == "investigator":
            action = handoff()
            action["next_discriminating_action"] = {
                "kind": "request_observation", "description": "x" * 776, "evidence_refs": []}
            return response(action)
        if ctx["project_context"]["knowledge_review"]["phase"] == "review":
            return response(finish_review(ctx, "unresolved"))
        assert config == investigator_context.RESILIENT_POLICY
        session = runtime["investigator_sessions"][0]
        assert session["status"] == "exhausted_without_handoff"
        assert "next_discriminating_action.description" in session["failure"]["last_edit_feedback"]
        assert not ctx["task_context"].get("edit_feedback") and not ctx["task_context"].get("protocol_feedback")
        assert runtime["investigator_handoffs"] == []
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert len([item for item in result["attempts"] if item["role"] == "investigator"]) == 1
    assert result["investigator_handoffs"] == [] and result["consumed_investigator_handoffs"] == []
    assert result["final_evaluation"] == "not_run" and result["protocol"]["max_calls"] == 8
    if config == investigator_context.RESILIENT_POLICY:
        assert result["status"] == "pending_diagnostic_review"
        assert len(result["attempts"]) == 3
        # 主Solver仍须通过公开运行审阅及原有终态门禁，转交本身不构成业务成功。
        assert result["diagnostic_runs"] == []
    else:
        assert result["status"] == "unresolved" and len(result["attempts"]) == 2
        assert result["stop_reason"] == "required_investigator_handoff_missing"
    for attempt in result["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_bytes())
        _verify_payload(receipt, Path(receipt["request_path"]).read_bytes())






