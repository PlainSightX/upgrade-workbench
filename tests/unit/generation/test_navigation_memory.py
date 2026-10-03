"""验证真实请求的导航保留；元数据和模拟回答不证明模型理解收益。"""

import copy
import json
from pathlib import Path

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.generation.project_context import assemble, attach_navigation, payload_for
from upgrade_workbench.investigation_policy import observation_visible

from .test_investigation_excerpts import synthetic, verify_requests
from .test_knowledge_review import finish_review
from .test_knowledge_transfer import OWNER, create, response
from .test_knowledge_transfer import exported as exported


def navigation_input():
    rows = [{"id": str(i) * 64, "revision": "a" * 64, "kind": "source", "stale": False,
             "action": {"type": "outline_source", "path": f"module{i}.py"},
             "result": {"path": f"module{i}.py", "revision": "a" * 64,
                        "items": [{"name": "run", "start_line": 2, "end_line": 9}],
                        "offset": 0, "total": 2, "next_cursor": "next-page"}}
            for i in range(5)]
    runtime = {"navigation_candidates": {
        "index": [{"id": row["id"], "action": row["action"], "last_access": i}
                  for i, row in enumerate(rows)], "observations": rows}}
    context = {"source_files": [{"path": "active.py", "text": "unchanged\n"}],
               "diagnostic_state": {}, "context_plan": {}}
    opts = {"model": "test", "system": "test", "output_tokens": 1000, "thinking_mode": "enabled"}
    return rows, runtime, context, opts


def test_navigation_keeps_read_pages_and_source_without_claiming_unread_page():
    rows, runtime, context, opts = navigation_input()
    original = copy.deepcopy(context["source_files"])
    result = attach_navigation(context, runtime, ceiling=20_000, opts=opts)
    memory = result["diagnostic_state"]["navigation_memory"]
    assert memory["observations"] == rows
    assert memory["observations"][0]["result"]["next_cursor"] == "next-page"
    assert result["source_files"] == original
    assert result["context_plan"]["navigation_delivery"]["omitted_results"] == 0
    assert len(payload_for(result, **opts)) <= 20_000
    assert observation_visible(result, rows[0])


def test_navigation_budget_omission_is_not_visible_and_never_removes_source():
    rows, runtime, context, opts = navigation_input()
    # 一页超过余量，只保留可定位入口，不能据此惩罚必要重取。
    rows[0]["result"]["items"] = [{"name": "x" * 10_000, "start_line": 1, "end_line": 2}]
    original = copy.deepcopy(context["source_files"])
    result = attach_navigation(context, runtime, ceiling=3800, opts=opts)
    assert result["source_files"] == original
    assert len(payload_for(result, **opts)) <= 3800
    assert result["context_plan"]["navigation_delivery"]["omitted_results"] >= 1
    assert not observation_visible(result, rows[0])
    assert result["diagnostic_state"]["navigation_memory"]["index"]


def test_only_exact_current_navigation_result_counts_as_visible():
    rows, runtime, context, opts = navigation_input()
    result = attach_navigation(context, runtime, ceiling=20_000, opts=opts)
    target = copy.deepcopy(rows[0])
    target["result"]["offset"] = 1
    assert not observation_visible(result, target)
    target = copy.deepcopy(rows[0])
    target["revision"] = "b" * 64
    assert not observation_visible(result, target)
    result["diagnostic_state"]["navigation_memory"]["observations"] = []
    assert not observation_visible(result, rows[0])


def test_assembly_trades_automatic_source_for_navigation_without_losing_active_reads(monkeypatch):
    from upgrade_workbench import evaluation
    from upgrade_workbench.generation import project_context
    from upgrade_workbench.generation.source_context import POLICY, navigate

    case, snapshot = synthetic({"active.py": "value = 1\n", "auto.py": "value = 2\n" * 8000})
    monkeypatch.setattr(project_context, "project_view", lambda *_: {})
    monkeypatch.setattr(evaluation, "load_evaluation", lambda *_: {"groups": {"feedback": {"paths": [], "nodeids": []}}})
    _, runtime, _, opts = navigation_input()
    source = navigate(case, snapshot, {"type": "read_source", "revision": snapshot.revision,
                      "path": "active.py", "start_line": 1, "end_line": 1})
    runtime.update(project_context_policy={"version": "project-context-v10", "context_tokens": 1000000,
                                          "framing_reserve_tokens": 4096},
                   observations=[], retained_source_reads=[{"observation_id": "c" * 64, "result": source}])
    context = {"potential_impacts": [], "business_contract": {"path": "business-contract.md", "sha256": "d" * 64, "text": "Contract\n"}}
    no_navigation = assemble(case, snapshot, context, runtime | {"navigation_candidates": {"index": [], "observations": []}},
                             POLICY, **opts, request_limit=18000)
    result = assemble(case, snapshot, context, runtime, POLICY, **opts, request_limit=18000)
    assert result["source_selection"]["read_retention"] == no_navigation["source_selection"]["read_retention"]
    assert result["source_selection"]["read_retention"]["included_lines"] == 1
    assert result["context_plan"]["navigation_delivery"]["omitted_results"] == 0
    assert result["source_selection"]["body_bytes"] < no_navigation["source_selection"]["body_bytes"]
    assert len(payload_for(result, **opts)) <= 18000


def test_ordinary_v10_navigation_recency_review_and_frozen_replay(exported, tmp_path, monkeypatch):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v10", max_calls=7)
    contexts = []
    first_id = None
    paths = ["flaskbb/utils/database.py", "flaskbb/user/models.py", "flaskbb/forum/models.py", "flaskbb/app.py"]

    def transport(request, **_):
        nonlocal first_id
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        index = len(contexts)
        revision = ctx["candidate"]["revision"]
        if index <= 4:
            if index == 2:
                first_id = ctx["diagnostic_state"]["latest_observation_id"]
            return response({"type": "outline_source", "revision": revision, "path": paths[index - 1]})
        if index == 5:
            memory = ctx["diagnostic_state"]["navigation_memory"]
            assert first_id in {row["id"] for row in memory["observations"]}
            return response({"type": "get_observation", "observation_id": first_id})
        if index == 6:
            memory = ctx["diagnostic_state"]["navigation_memory"]
            assert memory["index"][0]["id"] == first_id
            assert memory["index"][0]["last_access"] == 5
            assert ctx["diagnostic_state"]["progress"]["consecutive_revisits"] == 1
            assert "claim" in ctx["project_context"]["instructions"]
            return response(finish_review(ctx))
        assert ctx["project_context"]["knowledge_review"]["phase"] == "solve"
        return response({"type": "run_public_checks", "revision": revision})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review", result.get("stop_reason")
    assert len(contexts) == 7
    verify_requests(result)
    from upgrade_workbench import reporting
    from upgrade_workbench.generation import provider

    verify = provider._verify_payload
    calls = []

    def counted(*args):
        calls.append(args[0]["request_path"])
        return verify(*args)

    monkeypatch.setattr(provider, "_verify_payload", counted)
    exported_report = reporting.export_task_report(Path(task["task_path"]), tmp_path / "report")
    report = json.loads(Path(exported_report["json_path"]).read_bytes())
    assert len(calls) == len(set(calls)) == 7
    assert len(report["knowledge_import_consumption"]) == 7
    assert report["investigation_consumption"] == []
    first_request = Path(calls[0])
    first_request.write_bytes(b"{}")
    with pytest.raises(ValueError):
        reporting.export_task_report(Path(task["task_path"]), tmp_path / "corrupted-report")
