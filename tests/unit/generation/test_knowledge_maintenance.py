"""通过普通任务验证局部刷新、来源隔离和中断恢复；这些假响应不是实跑成果。"""

import copy
import json
from pathlib import Path

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import (
    context_from_reference,
    freeze_context,
    public_observation,
    read,
    store,
)
from upgrade_workbench.generation.project_context import validate_action
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.knowledge_transfer import export_bundle, load_bundle, validate_bundle
from upgrade_workbench.reporting import export_task_report
from upgrade_workbench.service.recovery import recover_task
from upgrade_workbench.topic_maintenance import revision_result, view

from .test_knowledge_transfer import CASE, OWNER, POLICY, create, response
from .test_knowledge_transfer import exported as exported


def revision(ctx):
    entry = ctx["project_context"]["topic_maintenance"]["topics"][0]
    page = copy.deepcopy(entry["topic"])
    page["explanation"] = "The current implementation has been reread; runtime behavior still requires observation."
    page["sources"] = [{key: value for key, value in ref.items()
                        if key in {"path", "start_line", "end_line"}} for ref in page["sources"]]
    return {"type": "revise_project_topic", "revision": ctx["candidate"]["revision"],
            "origin": entry["origin"], "previous_topic_sha256": entry["topic_sha256"], "page": page}


def test_v7_selection_reads_source_then_maintains_and_rebinds(exported, tmp_path):
    """普通入口同时消费范围与正文，随后维护解释；旧策略仍拒绝新字段。"""
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v7")
    case = load_case(CASE)
    initial = load_candidate(case)
    contexts = []

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        rev = ctx["candidate"]["revision"]
        if len(contexts) == 1:
            action = {"type": "select_investigation", "revision": rev, "mode": "deep",
                "reason": "Read this implementation and keep its static consumers available.",
                "focus_paths": ["flaskbb/forum/models.py"],
                "read": {"path": "flaskbb/forum/models.py", "start_line": 50, "end_line": 80}}
            for version in ("project-context-v4", "project-context-v5", "project-context-v6"):
                with pytest.raises(ValueError, match="fields"):
                    validate_action(case, initial, action, {**POLICY, "version": version})
            for scope in ({"path": "flaskbb/user/models.py", "start_line": 1, "end_line": 3},
                          {"path": "flaskbb/forum/models.py", "start_line": 1, "end_line": 201}):
                with pytest.raises(ValueError, match="Investigation read"):
                    validate_action(case, initial, {**action, "read": scope}, task["protocol"]["project_context_policy"])
            return response(action)
        if len(contexts) == 2:
            scope = ctx["project_context"]["investigation_selection"]
            assert scope["mode"] == "deep" and "read" not in scope
            observation = ctx["diagnostic_state"]["observations"][-1]
            assert "text" in observation["result"]["read"]
            assert ctx["source_selection"]["read_retention"]["included_lines"] == 31
            assert any(row["path"] == "flaskbb/forum/models.py" and row["start_line"] <= 50
                       and row["end_line"] >= 80 for row in ctx["source_files"])
            return response(revision(ctx))
        assert ctx["project_context"]["topic_maintenance"]["topics"][0]["update_observation_id"]
        assert len(ctx["project_context"]["retained_reads"]) == 1
        return response({"type": "submit_candidate", "base_revision": rev, "edits": [{
            "path": "flaskbb/forum/models.py", "old": "import logging", "new": "import logging  # separate edit"}]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_review", result.get("stop_reason")
    current = context_from_reference(freeze_context(result), case, load_candidate(case, result["current_candidate"]))
    assert current["retained_source_reads"][0]["continuity"]["status"] == "unique_excerpt_relocated"
    for attempt in result["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_bytes())
        frozen = read(receipt["diagnostic_context_reference"], Path(task["task_path"]).parent)
        base = load_candidate(case, frozen["current_candidate"])
        _verify_payload(receipt | {"candidate_reference": frozen["current_candidate"], "candidate_revision": base.revision},
                        Path(receipt["request_path"]).read_bytes())
    # 再取相同正文只计同一证据，不因选择理由改变而清空重复计数。
    from upgrade_workbench.diagnostics import remember_action
    from upgrade_workbench.investigation_policy import policy
    latest = result["observations"][0]
    before_edit = copy.deepcopy(result)
    before_edit["current_candidate"] = None
    before_edit["status"] = "ready"
    before_edit["protocol"]["investigation_policy"] = policy({"version": "investigation-budget-v1"})
    repeated = {"action": {"type": "get_observation", "observation_id": latest["id"], "cursor": None}}
    before_edit["latest_observation"] = latest["id"]
    for _ in range(2):
        remember_action(before_edit, repeated, case, before_revision=initial.revision, delivered_context=contexts[1])
    assert before_edit["diagnostic_progress"]["consecutive_revisits"] == 1


@pytest.mark.parametrize("use_visibility_policy", [False, True])
def test_v7_same_returned_source_is_not_new_progress(exported, monkeypatch, use_visibility_policy):
    """请求终点和理由变化不等于取得新原文；覆盖两种既有计数路径。"""
    from upgrade_workbench import diagnostics
    from upgrade_workbench.generation.source_context import navigate
    from upgrade_workbench.investigation_policy import policy

    _, original = exported
    task = copy.deepcopy(original)
    task.update(status="ready", diagnostic_progress={}, observations=[])
    if use_visibility_policy:
        task["protocol"]["investigation_policy"] = policy({"version": "investigation-budget-v1"})
    case = load_case(CASE)
    snapshot = load_candidate(case)
    path = "flaskbb/utils/database.py"
    end = len(snapshot.files[path].decode().splitlines())
    result = navigate(case, snapshot, {"type": "read_source", "revision": snapshot.revision,
        "path": path, "start_line": end - 2, "end_line": end})
    context = {"source_files": [result]}
    for index in range(2):
        action = {"type": "select_investigation", "revision": snapshot.revision, "mode": "direct",
            "reason": f"reason {index}", "focus_paths": [path],
            "read": {"path": path, "start_line": end - 2, "end_line": end + index}}
        observation = {"id": str(index) * 64, "kind": "project_context", "revision": snapshot.revision,
            "action": action, "result": {"read": result | {"requested_end_line": end + index}}}
        task["observations"].append({"id": observation["id"]})
        task["latest_observation"] = observation["id"]
        monkeypatch.setattr(diagnostics, "public_observation", lambda *_: observation)
        diagnostics.remember_action(task, {"action": action}, case, before_revision=snapshot.revision,
                                    delivered_context=context)
    assert task["diagnostic_progress"]["consecutive_revisits"] == 1


def test_imported_topic_refresh_reaches_next_request_without_relabelling_other_topics(exported, tmp_path):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v5")
    before = Path(task["knowledge_import_reference"]["path"]).read_bytes()
    contexts = []

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        rev = ctx["candidate"]["revision"]
        if len(contexts) == 1:
            return response({"type": "read_source", "revision": rev,
                "path": "flaskbb/forum/models.py", "start_line": 1, "end_line": 12})
        if len(contexts) == 2:
            return response(revision(ctx))
        entries = ctx["project_context"]["topic_maintenance"]["topics"]
        assert entries[0]["update_observation_id"] is not None
        assert entries[0]["status"] == "model_interpretation_unverified"
        assert entries[0]["topic"]["explanation"].startswith("The current implementation")
        assert [row["status"] for row in entries[1:]] == ["reference_only", "reference_only"]
        assert all(row["update_observation_id"] is None for row in entries[1:])
        assert ctx["project_context"]["imported_knowledge"]["topics"][0]["explanation"].startswith("Historical")
        return response({"type": "submit_candidate", "base_revision": rev, "edits": [{
            "path": "flaskbb/forum/models.py", "old": "import logging", "new": "import logging  # mechanism test"}]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_review", result.get("stop_reason")
    assert len(contexts) == 3 and result.get("project_knowledge_reference") is None
    assert Path(result["knowledge_import_reference"]["path"]).read_bytes() == before
    case = load_case(CASE)
    current = context_from_reference(freeze_context(result), case, load_candidate(case, result["current_candidate"]))
    assert all(row["status"] == "needs_review" for row in current["topic_maintenance"]["topics"])
    # 原请求按其冻结知识和候选重建，后续刷新不回写旧请求。
    for attempt in result["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_bytes())
        frozen = read(receipt["diagnostic_context_reference"], Path(task["task_path"]).parent)
        base = load_candidate(case, frozen["current_candidate"])
        _verify_payload(receipt | {"candidate_reference": frozen["current_candidate"], "candidate_revision": base.revision},
                        Path(receipt["request_path"]).read_bytes())
    report = json.loads(Path(export_task_report(Path(task["task_path"]), tmp_path / "export")["json_path"]).read_bytes())
    assert len(report["topic_revisions"]) == 1 and report["final_evaluation"] == "not_run"
    portable = export_bundle(Path(task["task_path"]), tmp_path / "maintained.json")
    bundle = load_bundle({key: portable[key] for key in ("path", "sha256")}, case)
    assert bundle["version"] == "knowledge-import-v2"
    assert [len(topic["updates"]) for topic in bundle["topics"]] == [1, 0, 0]
    assert bundle["topics"][0]["updates"][0]["bundle"]["knowledge"]["pages"][0]["explanation"].startswith("The current implementation")
    # 即使重签内部观察摘要，也不能替换模型实际返回的解释。
    ref = next(ref for ref in result["observations"] if read(ref, Path(task["task_path"]).parent)["action"]["type"] == "revise_project_topic")
    forged = read(ref, Path(task["task_path"]).parent)
    forged["action"]["page"]["explanation"] = "Forged explanation"
    with pytest.raises(ValueError, match="identity changed"):
        public_observation(result, store(Path(task["task_path"]).parent / "observations", forged))


def test_refresh_receipt_recovery_applies_once(exported, tmp_path, monkeypatch):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v5")
    calls = []
    original = tasks.complete_request

    class Crash(BaseException):
        pass

    def interrupt(prepared, **kwargs):
        original(prepared, **kwargs)
        raise Crash()

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        calls.append(ctx)
        return response(revision(ctx))

    monkeypatch.setattr(tasks, "complete_request", interrupt)
    with pytest.raises(Crash):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    recovered = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    twice = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert recovered["status"] == "ready" and len(twice["observations"]) == len(calls) == 1
    case = load_case(CASE)
    runtime = context_from_reference(freeze_context(twice), case, load_candidate(case))
    assert runtime["topic_maintenance"]["topics"][0]["update_observation_id"] == twice["observations"][0]["id"]
    # 改写和重读自己的解释均不意味着取得新证据。
    from upgrade_workbench.diagnostics import remember_action
    from upgrade_workbench.investigation_policy import policy
    for budget in (None, policy({"version": "investigation-budget-v1"})):
        current = copy.deepcopy(twice)
        if budget:
            current["protocol"]["investigation_policy"] = budget
        current["diagnostic_progress"] = {"revision": load_candidate(case).revision,
            "consecutive_revisits": 2, "seen_observations": [], "seen_output_pages": []}
        remember_action(current, {"action": {"type": "get_observation", "observation_id": twice["observations"][0]["id"], "cursor": None}},
                        case, before_revision=load_candidate(case).revision)
        assert current["diagnostic_progress"]["consecutive_revisits"] == 2


def test_same_id_origins_and_update_chain_remain_separate(exported, tmp_path):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v5")
    case = load_case(CASE)
    snapshot = load_candidate(case)
    runtime = context_from_reference(freeze_context(task), case, snapshot)
    imported = runtime["imported_knowledge"]
    original = json.loads(Path(spec["path"]).read_bytes())["knowledge"]
    combined = view(snapshot.revision, knowledge=original, knowledge_id="a" * 64, imported=imported)
    assert len(combined["topics"]) == 6
    ctx = {"candidate": {"revision": snapshot.revision}, "project_context": {"topic_maintenance": runtime["topic_maintenance"]}}
    action = revision(ctx)
    update = revision_result(case, snapshot, action, combined)
    row = {"id": "1" * 64, "action": {"type": "revise_project_topic"}, "result": update}
    revised = view(snapshot.revision, knowledge=original, knowledge_id="a" * 64, imported=imported, observations=[row])
    assert revised["topics"][0]["update_observation_id"] is None
    assert revised["topics"][3]["update_observation_id"] == row["id"]
    with pytest.raises(ValueError, match="chain"):
        view(snapshot.revision, imported=imported, observations=[row, row])
    bad = copy.deepcopy(action)
    bad["previous_topic_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="prior explanation"):
        revision_result(case, snapshot, bad, combined)
    bad["origin"]["namespace"] = "unknown"
    with pytest.raises(ValueError, match="origin"):
        revision_result(case, snapshot, bad, combined)
    with pytest.raises(ValueError, match="maintenance Solver"):
        validate_action(case, snapshot, action, POLICY)


def test_v6_public_check_and_maintained_knowledge_survive_three_task_chain(exported, tmp_path):
    """检查正文、维护消费及再次导出走普通调用；假模型结果不冒充业务验收。"""
    spec, donor = exported
    task = create(tmp_path / "middle", spec, version="project-context-v6")
    contexts = []

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        rev = ctx["candidate"]["revision"]
        if len(contexts) == 1:
            checks = ctx["project_context"]["feedback_checks"]
            assert all(row["path"].startswith("feedback/") for row in checks)
            assert all("acceptance" not in row["path"] for row in checks)
            assert "explanation" not in ctx["project_context"]["imported_knowledge"]["topics"][0]
            applicability = ctx["project_context"]["imported_knowledge"]["topics"][0]["applicability"]
            assert applicability["sources"][0]["current_location"] is not None
            assert "topics" not in ctx["diagnostic_state"]["imported_knowledge"]
            return response({"type": "read_public_check", "revision": rev,
                "path": checks[0]["path"], "start_line": 1, "end_line": 80})
        if len(contexts) == 2:
            reading = next(row for row in ctx["diagnostic_state"]["observations"]
                           if row["action"]["type"] == "read_public_check")
            assert reading["result"]["group"] == "feedback"
            assert "def test_" in reading["result"]["text"]
            return response(revision(ctx))
        text = json.dumps(ctx)
        assert text.count("The current implementation has been reread") == 1
        assert text.count("Historical source explanation; no behavior claim.") == 2
        return response({"type": "run_public_checks", "revision": rev})

    middle = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert middle["status"] == "pending_diagnostic_review", middle.get("stop_reason")
    out = export_bundle(Path(task["task_path"]), tmp_path / "portable.json")
    portable = {key: out[key] for key in ("path", "sha256")}
    bundle = json.loads(Path(out["path"]).read_bytes())
    assert bundle["topics"][0]["updates"][0]["bundle"]["origin"]["task_id"] == middle["task_id"]
    assert next(iter(bundle["roots"].values()))["origin"]["task_id"] == donor["task_id"]
    child = create(tmp_path / "child", portable, version="project-context-v6")
    Path(out["path"]).rename(tmp_path / "no-longer-at-input.json")
    seen = []

    def consume(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        seen.append(ctx)
        entry = ctx["project_context"]["topic_maintenance"]["topics"][0]
        if len(seen) == 1:
            assert entry["topic"]["explanation"].startswith("The current implementation")
            action = revision(ctx)
            action["page"]["explanation"] = "The third task confirms this source interpretation; behavior remains separately measured."
            return response(action)
        assert entry["topic"]["explanation"].startswith("The third task confirms")
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    last = tasks.advance_task(Path(child["task_path"]), api_key="unused", transport=consume, execution_owner=OWNER)
    assert last["status"] == "pending_diagnostic_review", last.get("stop_reason")
    again = export_bundle(Path(child["task_path"]), tmp_path / "twice-maintained.json")
    final = json.loads(Path(again["path"]).read_bytes())
    assert [len(topic["updates"]) for topic in final["topics"]] == [2, 0, 0]
    assert final["topics"][0]["root_sha256"] == bundle["topics"][0]["root_sha256"]
    forged = copy.deepcopy(final)
    forged["topics"][0]["updates"][1]["previous_topic_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="chain"):
        validate_bundle(forged)
    # 所有层级的损坏输入都应是可处理的格式错误，不能穿透到CLI traceback。
    for location in ("root", "update"):
        for field in ("origin", "inputs", "knowledge", "excerpts"):
            malformed = copy.deepcopy(final)
            nested = (next(iter(malformed["roots"].values())) if location == "root"
                      else malformed["topics"][0]["updates"][0]["bundle"])
            nested[field] = None
            with pytest.raises(ValueError):
                validate_bundle(malformed)
    forged = copy.deepcopy(final)
    version = forged["topics"][0]["updates"][0]["bundle"]
    excerpt = next(iter(version["excerpts"]))
    version["excerpts"][excerpt] += "tampered"
    with pytest.raises(ValueError, match="excerpt"):
        validate_bundle(forged)
    forged = copy.deepcopy(final)
    forged["topics"][0]["updates"][0]["hidden_acceptance"] = "not public"
    with pytest.raises(ValueError, match="chain"):
        validate_bundle(forged)
    report = json.loads(Path(export_task_report(Path(child["task_path"]), tmp_path / "report")["json_path"]).read_bytes())
    row = report["knowledge_import_consumption"][0]
    assert "source_task_id" not in row and row["exporting_task_id"] == middle["task_id"]
    assert row["topic_origins"][0]["lineage"]["root"]["task_id"] == donor["task_id"]
    for current in (middle, last):
        for attempt in current["attempts"]:
            receipt = json.loads(Path(attempt["receipt"]).read_bytes())
            _verify_payload(receipt, Path(receipt["request_path"]).read_bytes())


def test_public_check_read_refuses_acceptance_and_legacy_policy():
    case = load_case(CASE)
    snapshot = load_candidate(case)
    action = {"type": "read_public_check", "revision": snapshot.revision,
              "path": "acceptance/test_hidden.py", "start_line": 1, "end_line": 20}
    for version in ("project-context-v1", "project-context-v4", "project-context-v5"):
        with pytest.raises(ValueError, match="consumption Solver"):
            validate_action(case, snapshot, action, POLICY | {"version": version})
    for name in ("acceptance/test_hidden.py", "../business-contract.md", "tests/conftest.py"):
        with pytest.raises(ValueError):
            validate_action(case, snapshot, action | {"path": name}, POLICY | {"version": "project-context-v6"})
