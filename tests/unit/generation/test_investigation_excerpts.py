"""多文件取文的来源、容量、恢复和实际送达；模拟响应不证明模型调查收益。"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from upgrade_workbench import diagnostics, tasks
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import (
    context_from_reference,
    freeze_context,
    observation_projection,
    public_observation,
    read,
)
from upgrade_workbench.generation.project_context import investigation_excerpts, validate_action
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.generation.source_context import (
    build_context,
    encoded,
    excerpt_delivery,
    navigate,
    rebind_source_reads,
)
from upgrade_workbench.investigation_policy import policy
from upgrade_workbench.reporting import export_task_report
from upgrade_workbench.service.recovery import recover_task

from .test_knowledge_review import edit, interrupted_review
from .test_knowledge_transfer import CASE, OWNER, POLICY, create, response
from .test_knowledge_transfer import exported as exported

V9 = POLICY | {"version": "project-context-v9"}


def synthetic(files):
    data = {name: content.encode("utf-8") for name, content in files.items()}
    snapshot = SimpleNamespace(files=data, original=data, revision="a" * 64, origin="original", sha256="b" * 64)
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=[name for name in data if name.endswith(".py")]))
    return case, snapshot


def selection(snapshot, *, mode="direct", focus=None, reason="Inspect the relevant implementations."):
    return {"type": "select_investigation", "revision": snapshot.revision, "mode": mode, "reason": reason,
            "focus": focus or [{"path": "models.py", "responsibility": "Read behavior", "symbol": "Store.read"}]}


def observe(case, snapshot, action):
    validate_action(case, snapshot, action, V9)
    return {"id": "f" * 64, "kind": "project_context", "revision": snapshot.revision,
            "action": action, "result": investigation_excerpts(case, snapshot, action)}


def test_direct_deep_and_explicit_template_return_implementation_not_import_only():
    case, snapshot = synthetic({
        "models.py": "class Store:\n    def read(self):\n        return 7\n",
        "consumer.py": "from models import Store\n\ndef render():\n    return Store().read()\n",
        "imports.py": "from models import Store\n\ndef unrelated():\n    return 9\n",
        "unrelated.py": "def read():\n    return 42\n",
        "detail.html": "<main>\n{{ store.read() }}\n</main>\n",
    })
    direct = observe(case, snapshot, selection(snapshot))["result"]
    assert [row["source"]["path"] for row in direct["excerpts"]] == ["models.py"]
    assert direct["excerpts"][0]["source"]["text"] == "    def read(self):\n        return 7\n"
    action = selection(snapshot, mode="deep")
    action["focus"].append({"path": "detail.html", "responsibility": "Template consumer", "start_line": 1, "end_line": 3})
    deep = observe(case, snapshot, action)["result"]
    assert [row["source"]["path"] for row in deep["excerpts"]] == ["models.py", "detail.html", "consumer.py"]
    consumer = deep["excerpts"][-1]
    assert consumer["source"]["text"] == "def render():\n    return Store().read()\n"
    assert consumer["basis"]["kind"] == "static_neighbor_lexical_use"
    assert "not resolved calls" in deep["meaning"]
    assert any(row["reason"] == "no_static_symbol_for_anchor" for row in deep["omitted"])


def test_syntax_error_allows_exact_anchor_and_deep_lexical_fallback():
    case, snapshot = synthetic({
        "models.py": "import broken\n\nclass Store:\n    def read(self):\n        return 7\n",
        "broken.py": "def render(:\n    return store.read()\n",
    })
    exact = selection(snapshot, focus=[{"path": "broken.py", "responsibility": "Inspect incomplete source", "start_line": 1, "end_line": 2}])
    assert observe(case, snapshot, exact)["result"]["excerpts"][0]["source"]["text"].startswith("def render(:")
    deep = observe(case, snapshot, selection(snapshot, mode="deep"))["result"]
    assert any(row["reason"] == "neighbor_parse_error" for row in deep["omitted"])
    assert any(row["source"]["path"] == "broken.py" and "store.read()" in row["source"]["text"] for row in deep["excerpts"])


def test_deep_truncation_keeps_actual_match_inside_returned_span():
    body = "".join("    # " + "长" * 300 + "\n" for _ in range(120))
    case, snapshot = synthetic({
        "models.py": "class Store:\n    def read(self):\n        return 7\n",
        "consumer.py": "from models import Store\n\ndef render():\n" + body + "    return Store().read()\n",
    })
    row = observe(case, snapshot, selection(snapshot, mode="deep"))
    consumer = next(item for item in row["result"]["excerpts"] if item["source"]["path"] == "consumer.py")
    source = consumer["source"]
    assert source["start_line"] <= consumer["basis"]["match_line"] <= source["end_line"]
    assert "Store().read()" in source["text"]
    assert len(encoded(observation_projection(row, row["id"]))) <= 24_000
    assert any(item["reason"] == "bounded_implementation_prefix" for item in row["result"]["omitted"])


def test_unicode_capacity_accounts_for_action_wrapper_and_reports_omissions():
    case, snapshot = synthetic({f"part{i}.py": "".join("# " + "文" * 140 + "\n" for _ in range(150)) for i in range(4)})
    focus = [{"path": name, "responsibility": "查" * 160, "start_line": 1, "end_line": 150} for name in snapshot.files]
    row = observe(case, snapshot, selection(snapshot, focus=focus, reason="因" * 2000))
    assert row["result"]["excerpts"]
    assert row["result"]["omitted"] or row["result"]["additional_omissions"]
    assert len(encoded(observation_projection(row, row["id"]))) <= 24_000
    assert sum(item["source"]["end_line"] - item["source"]["start_line"] + 1 for item in row["result"]["excerpts"]) <= 400
    huge_case, huge = synthetic({"huge.html": "文" * 9000 + "\n"})
    with pytest.raises(ValueError, match="complete source line"):
        observe(huge_case, huge, selection(huge, focus=[{"path": "huge.html", "responsibility": "Read", "start_line": 1, "end_line": 1}]))


def test_deep_file_and_line_limits_are_explicit():
    files = {"models.py": "class Store:\n    def read(self):\n        return 7\n"}
    files.update({f"use{i:02}.py": "from models import Store\n\ndef render():\n    value = Store().read()\n" + "    pass\n" * 178 for i in range(18)})
    case, snapshot = synthetic(files)
    result = observe(case, snapshot, selection(snapshot, mode="deep"))["result"]
    assert len({item["source"]["path"] for item in result["excerpts"]}) <= 6
    assert len(result["excerpts"]) <= 8
    assert sum(item["source"]["end_line"] - item["source"]["start_line"] + 1 for item in result["excerpts"]) == 400
    assert result["limits"]["discovery"].startswith("all registered Python bindings")
    assert any(row["reason"] == "excerpt_budget" for row in result["omitted"])


def test_deep_discovers_same_file_and_reexport_alias_beyond_sixteen_neighbors():
    files = {
        "pkg/app.py": "def create_app():\n    return configure_extensions()\n\ndef configure_extensions():\n    return 7\n",
        "pkg/__init__.py": "from .app import create_app as exported_app\n",
        "zz_cli.py": "from pkg import exported_app as factory\n\ndef make_app():\n    return factory()\n",
    }
    files.update({f"a{i:02}.py": "from pkg.app import create_app\n\ndef unrelated():\n    return 7\n" for i in range(25)})
    case, snapshot = synthetic(files)
    action = selection(snapshot, mode="deep", focus=[{"path": "pkg/app.py", "symbol": "create_app", "responsibility": "Factory consumer"}])
    result = observe(case, snapshot, action)["result"]
    caller = next(row for row in result["excerpts"] if row["source"]["path"] == "zz_cli.py")
    assert "return factory()" in caller["source"]["text"]
    assert [(edge["path"], edge["local_name"]) for edge in caller["basis"]["import_chain"]] == [("zz_cli.py", "factory"), ("pkg/__init__.py", "exported_app")]
    assert not any(row["source"]["path"].startswith("a0") for row in result["excerpts"])
    action["focus"][0]["symbol"] = "configure_extensions"
    result = observe(case, snapshot, action)["result"]
    assert any(row["basis"]["kind"] == "same_file_use" and "return configure_extensions()" in row["source"]["text"] for row in result["excerpts"])


def test_used_local_import_bindings_return_assignment_definitions_and_skip_shadowed_names():
    case, snapshot = synthetic({
        "extensions.py": "db = Database()\nalembic: object = Alembic()\nunused = Cache()\n",
        "app.py": "from extensions import db as store, alembic, unused\n\ndef setup():\n    store.init_app()\n    alembic.init_app()\n\ndef shadowed(store):\n    return store.query()\n",
        "local.py": "def setup():\n    from extensions import db as local_db\n    return local_db.query()\n",
        "class_body.py": "from extensions import db\nclass C:\n    db = other\n    result = db.query()\n    def method(self):\n        return db.query()\n",
    })
    action = selection(snapshot, mode="deep", focus=[{"path": "app.py", "symbol": "setup", "responsibility": "Extension initialization"}])
    result = observe(case, snapshot, action)["result"]
    definitions = next(row for row in result["excerpts"] if row["source"]["path"] == "extensions.py")
    assert "db = Database()" in definitions["source"]["text"] and "alembic: object" in definitions["source"]["text"]
    assert "unused = Cache()" not in definitions["source"]["text"]
    assert {item["binding_name"] for item in definitions["basis"]["bindings"]} == {"store", "alembic"}
    direct = observe(case, snapshot, selection(snapshot, focus=[{"path": "extensions.py", "symbol": "db", "responsibility": "Database identity"}]))
    assert direct["result"]["excerpts"][0]["source"]["text"] == "db = Database()\n"
    action = selection(snapshot, mode="deep", focus=[{"path": "extensions.py", "symbol": "db", "responsibility": "Database consumers"}])
    result = observe(case, snapshot, action)["result"]
    assert any(row["source"]["path"] == "local.py" and "local_db.query()" in row["source"]["text"] for row in result["excerpts"])
    assert not any("def shadowed" in row["source"]["text"] for row in result["excerpts"])
    class_uses = [row for row in result["excerpts"] if row["source"]["path"] == "class_body.py"]
    assert len(class_uses) == 1 and class_uses[0]["source"]["start_line"] == 5
    assert class_uses[0]["basis"]["match_line"] == 6


def test_reexport_cycle_depth_rebinding_and_star_do_not_claim_transparent_binding():
    case, snapshot = synthetic({
        "original.py": "def make():\n    return 7\n",
        "one.py": "from original import make\n",
        "two.py": "from one import make\n",
        "too_deep.py": "from two import make\n\ndef use():\n    return make()\n",
        "rebound.py": "from original import make\nmake = other\n\ndef use():\n    return make()\n",
        "star.py": "from original import make\nfrom other import *\n\ndef use():\n    return make()\n",
        "cycle1.py": "from cycle2 import make\n",
        "cycle2.py": "from cycle1 import make\n\ndef use():\n    return make()\n",
        "conditional.py": "if enabled:\n    from original import make\n\ndef use():\n    return make()\n",
        "exception.py": "from original import make\n\ndef use():\n    try:\n        pass\n    except Exception as make:\n        return make()\n",
        "walrus.py": "from original import make\n\ndef use():\n    other = (make := replacement)\n    return make()\n",
    })
    result = observe(case, snapshot, selection(snapshot, mode="deep", focus=[{"path": "original.py", "symbol": "make", "responsibility": "Callers"}]))["result"]
    assert [row["source"]["path"] for row in result["excerpts"]] == ["original.py"]
    reasons = {row["reason"] for row in result["omitted"]}
    assert {"reexport_depth_limit", "reexport_cycle", "ambiguous_or_conditional_binding", "wildcard_binding_unknown"} <= reasons


def test_real_flaskbb_factory_and_extensions_reach_retained_source_context():
    # 这里只验证跨文件导航，使用同修订的原字节片段，不假装运行完整 HTTP 案例。
    root = Path(__file__).resolve().parents[2] / "fixtures/flaskbb-navigation/source"
    case, snapshot = synthetic({
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*.py")
    })
    factory = selection(snapshot, mode="deep", focus=[{"path": "flaskbb/app.py", "symbol": "create_app", "responsibility": "Application factory consumers"}])
    factory_observation = observe(case, snapshot, factory)
    consumer = next(row for row in factory_observation["result"]["excerpts"] if row["source"]["path"] == "flaskbb/cli/main.py")
    assert "return create_app(config_file, instance_path)" in consumer["source"]["text"]
    assert [edge["path"] for edge in consumer["basis"]["import_chain"]] == ["flaskbb/cli/main.py", "flaskbb/__init__.py"]
    assert "def set_config" not in consumer["source"]["text"]
    setup = selection(snapshot, mode="deep", focus=[{"path": "flaskbb/app.py", "symbol": "configure_extensions", "responsibility": "Database and migration initialization"}])
    setup_observation = observe(case, snapshot, setup)
    excerpts = setup_observation["result"]["excerpts"]
    assert any("configure_extensions(app)" in row["source"]["text"] and row["basis"]["kind"] == "same_file_use" for row in excerpts)
    definitions = next(row for row in excerpts if row["source"]["path"] == "flaskbb/extensions.py")
    assert "db = SQLAlchemy(" in definitions["source"]["text"] and "alembic = Alembic()" in definitions["source"]["text"]
    for observation in (factory_observation, setup_observation):
        retained = rebind_source_reads(snapshot, [observation])
        context = build_context(case, snapshot, observations=retained, retain_reads_first=True, read_continuity=True, fill_remaining=True)
        delivered = excerpt_delivery(snapshot, observation["id"], retained, context["source_files"])
        assert delivered and all(row["complete"] for row in delivered)
        assert len(encoded(observation_projection(observation, observation["id"]))) <= 24_000


def test_invalid_anchors_and_legacy_policies_reject_new_contract():
    case, snapshot = synthetic({"models.py": "class Store:\n    def read(self):\n        return 7\n"})
    action = selection(snapshot)
    for invalid in (
        action | {"revision": "0" * 64},
        action | {"focus": [{"path": "../hidden.py", "responsibility": "Read", "start_line": 1, "end_line": 2}]},
        action | {"focus": [action["focus"][0] | {"symbol": "missing"}]},
        action | {"focus": [action["focus"][0], action["focus"][0]]},
        action | {"focus": [{"path": "models.py", "responsibility": "Read", "start_line": True, "end_line": 2}]},
    ):
        with pytest.raises(ValueError):
            validate_action(case, snapshot, invalid, V9)
    ambiguous_case, ambiguous = synthetic({"models.py": "def run():\n    pass\ndef run():\n    pass\n"})
    with pytest.raises(ValueError, match="ambiguous"):
        validate_action(ambiguous_case, ambiguous, selection(ambiguous, focus=[{"path": "models.py", "responsibility": "Read", "symbol": "run"}]), V9)
    for version in ("project-context-v7", "project-context-v8"):
        with pytest.raises(ValueError, match="fields"):
            validate_action(case, snapshot, action, POLICY | {"version": version})
        with pytest.raises(ValueError, match="excerpt Solver"):
            validate_action(case, snapshot, {"type": "read_public_contract", "revision": snapshot.revision, "start_line": 1, "end_line": 2}, POLICY | {"version": version})


def test_each_excerpt_rebinds_and_delivery_checks_exact_current_text():
    case, initial = synthetic({"models.py": "class Store:\n    def read(self):\n        return 7\n", "other.py": "first = 1\nsecond = 2\n"})
    action = selection(initial, focus=[selection(initial)["focus"][0], {"path": "other.py", "responsibility": "Other state", "start_line": 1, "end_line": 2}])
    observation = observe(case, initial, action)
    unchanged = SimpleNamespace(**(vars(initial) | {"revision": "b" * 64}))
    assert {item["continuity"]["status"] for item in rebind_source_reads(unchanged, [observation])} == {"unchanged_file"}
    current = SimpleNamespace(**(vars(initial) | {"revision": "c" * 64, "files": {
        "models.py": b"# inserted\n" + initial.files["models.py"], "other.py": b"first = 3\nsecond = 2\n"}}))
    retained = rebind_source_reads(current, [observation])
    assert [item["continuity"]["status"] for item in retained] == ["unique_excerpt_relocated", "pending_relocation"]
    block = navigate(case, current, {"type": "read_source", "revision": current.revision, "path": "models.py", "start_line": 3, "end_line": 4})
    delivered = excerpt_delivery(current, observation["id"], retained, [block])
    assert delivered[0]["current_range"] == [3, 4] and delivered[0]["complete"]
    assert delivered[1]["current_range"] is None and not delivered[1]["complete"]
    missing = excerpt_delivery(current, observation["id"], retained, [block | {"text": "forged\n"}])[0]
    assert missing["included_lines"] == 0 and missing["omitted_ranges"] == [[3, 4]]
    partial = excerpt_delivery(current, observation["id"], retained, [block | {"end_line": 3, "text": block["text"].splitlines(keepends=True)[0]}])[0]
    assert partial["included_lines"] == 1 and partial["omitted_ranges"] == [[4, 4]]
    full_read = navigate(case, current, {"type": "read_source", "revision": current.revision, "path": "other.py", "start_line": 1, "end_line": 2})
    reread = {"id": "e" * 64, "kind": "source", "action": {"type": "read_source"}, "result": full_read}
    retained = rebind_source_reads(current, [observation, reread])
    assert retained[1]["continuity"]["status"] == "reread_current_file"
    delivered = excerpt_delivery(current, observation["id"], retained, [block, full_read])[1]
    assert delivered["current_range"] is None and delivered["included_lines"] == 0 and not delivered["complete"]
    assert "no unique current location" in delivered["meaning"]


def test_actual_context_capacity_marks_selection_excerpts_omitted():
    case, snapshot = synthetic({"models.py": "# small\n", "large.html": "x" * 800 + "\n" + "y" * 800 + "\n"})
    action = selection(snapshot, focus=[{"path": "large.html", "responsibility": "Template", "start_line": 1, "end_line": 2}])
    observation = observe(case, snapshot, action)
    retained = rebind_source_reads(snapshot, [observation])
    context = build_context(case, snapshot, {"mode": "focused", "body_bytes": 1024, "inventory_bytes": 64000},
                            observations=retained, retain_reads_first=True, read_continuity=True, fill_remaining=True)
    delivery = excerpt_delivery(snapshot, observation["id"], retained, context["source_files"])[0]
    assert delivery["included_lines"] == 1 and delivery["omitted_ranges"] == [[2, 2]] and not delivery["complete"]


@pytest.mark.parametrize("use_visibility_policy", [False, True])
def test_changed_selection_labels_are_not_new_source_progress(exported, monkeypatch, use_visibility_policy):
    _, original = exported
    task = copy.deepcopy(original)
    task.update(status="ready", diagnostic_progress={}, observations=[])
    task["protocol"]["project_context_policy"] = V9
    if use_visibility_policy:
        task["protocol"]["investigation_policy"] = policy({"version": "investigation-budget-v1"})
    case = load_case(CASE)
    snapshot = load_candidate(case)
    for index in range(2):
        action = selection(snapshot, mode="direct" if index == 0 else "deep", reason=f"reason {index}", focus=[{
            "path": "flaskbb/forum/models.py", "responsibility": f"intent {index}", "start_line": 1, "end_line": 2}])
        observation = observe(case, snapshot, action) | {"id": str(index) * 64}
        task["observations"].append({"id": observation["id"]})
        task["latest_observation"] = observation["id"]
        monkeypatch.setattr(diagnostics, "public_observation", lambda *_: observation)
        diagnostics.remember_action(task, {"action": action}, case, before_revision=snapshot.revision,
                                    delivered_context={"source_files": [entry["source"] for entry in observation["result"]["excerpts"]]})
    assert task["diagnostic_progress"]["consecutive_revisits"] == 1


def assert_v9_prompt(request):
    body = json.loads(request.data)
    prompt = " ".join(item["content"] for item in body["messages"])
    for obsolete in ("focus_paths", "Direct prioritizes these files", "A selection does not execute an investigation", "select_investigation optionally accepts read:"):
        assert obsolete not in prompt
    assert "read_public_contract" in prompt and "responsibility-focused source excerpts" in prompt
    return json.loads(body["messages"][1]["content"])


def verify_requests(task):
    case = load_case(CASE)
    for attempt in task["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_bytes())
        frozen = read(receipt["diagnostic_context_reference"], Path(task["task_path"]).parent)
        base = load_candidate(case, frozen["current_candidate"])
        _verify_payload(receipt | {"candidate_reference": frozen["current_candidate"], "candidate_revision": base.revision},
                        Path(receipt["request_path"]).read_bytes())


def test_selection_recovery_delivers_exact_excerpts_to_next_request_and_report(tmp_path, monkeypatch):
    task = create(tmp_path, version="project-context-v9")

    def select(ctx):
        return {"type": "select_investigation", "revision": ctx["candidate"]["revision"], "mode": "direct",
            "reason": "Compare topic and user responsibilities.", "focus": [
                {"path": "flaskbb/forum/models.py", "responsibility": "Unread topic state", "symbol": "Topic.first_unread"},
                {"path": "flaskbb/user/models.py", "responsibility": "Tracking persistence", "symbol": "User.track_topic"}]}

    with monkeypatch.context() as patcher:
        interrupted_review(task, patcher, select)
    restored = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    again = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert len(again["observations"]) == len(again["attempts"]) == 1
    observation = public_observation(restored, restored["observations"][0])
    contexts = []

    def transport(request, **_):
        ctx = assert_v9_prompt(request)
        contexts.append(ctx)
        plan = ctx["context_plan"]["investigation"]
        assert plan["priority_paths"] == []
        assert len(plan["excerpt_delivery"]) == 2 and all(row["complete"] for row in plan["excerpt_delivery"])
        assert all("text" not in row["source"] for row in ctx["diagnostic_state"]["observations"][-1]["result"]["excerpts"])
        for entry in observation["result"]["excerpts"]:
            source = entry["source"]
            assert any(block["path"] == source["path"] and source["text"] in block["text"] for block in ctx["source_files"])
        return response(edit(ctx))

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_review" and len(contexts) == 1
    current = context_from_reference(freeze_context(result), load_case(CASE), load_candidate(load_case(CASE), result["current_candidate"]))
    assert {row["continuity"]["status"] for row in current["retained_source_reads"]} == {"unique_excerpt_relocated", "unchanged_file"}
    report = json.loads(Path(export_task_report(Path(task["task_path"]), tmp_path / "report")["json_path"]).read_bytes())
    assert len(report["investigation_consumption"]) == 1
    assert report["investigation_consumption"][0]["excerpt_delivery"] == contexts[0]["context_plan"]["investigation"]["excerpt_delivery"]
    assert report["final_evaluation"] == "not_run"
    verify_requests(result)


def test_contract_read_supports_constraint_revision_and_review_consumer(exported, tmp_path):
    spec, _ = exported
    task = create(tmp_path, spec, version="project-context-v9")
    contexts = []

    def transport(request, **_):
        ctx = assert_v9_prompt(request)
        contexts.append(ctx)
        revision = ctx["candidate"]["revision"]
        entry = next(row for row in ctx["project_context"]["topic_maintenance"]["topics"] if row["topic"]["kind"] == "constraint")
        assert all(row["path"] != "business-contract.md" for row in ctx["source_inventory"]["items"])
        assert all(row["path"] != "business-contract.md" for row in ctx["source_files"])
        contract = ctx["project_context"]["public_contract"]
        assert contract["line_count"] == len(ctx["business_contract"]["text"].splitlines())
        if len(contexts) <= 3:
            assert ctx["project_context"]["knowledge_review"]["phase"] == "review"
            assert "when to edit" not in ctx["project_context"]["instructions"]
            assert "no first-edit knowledge gate" not in ctx["project_context"]["instructions"]
        if len(contexts) == 1:
            return response({"type": "read_public_contract", "revision": revision, "start_line": 1, "end_line": 4})
        if len(contexts) == 2:
            result = ctx["diagnostic_state"]["observations"][-1]["result"]
            assert result["text"] == "".join(ctx["business_contract"]["text"].splitlines(keepends=True)[:4])
            page = copy.deepcopy(entry["topic"])
            page.update(explanation="The current public contract supplies these requirements; source behavior remains unverified.",
                        sources=[{key: result[key] for key in ("path", "start_line", "end_line")}])
            return response({"type": "revise_project_topic", "revision": revision, "origin": entry["origin"],
                             "previous_topic_sha256": entry["topic_sha256"], "page": page})
        if len(contexts) == 3:
            return response({"type": "complete_knowledge_review", "revision": revision, "topics": [{
                "origin": entry["origin"], "topic_sha256": entry["topic_sha256"], "disposition": "corrected",
                "reason": "The requirement interpretation now cites the current contract.", "sources": [{"path": "business-contract.md", "start_line": 1, "end_line": 4}]}],
                "remaining_scope": "Other historical interpretations remain unverified."})
        assert ctx["project_context"]["knowledge_review"]["phase"] == "solve"
        assert entry["topic"]["explanation"].startswith("The current public contract")
        assert entry["update_observation_id"]
        return response(edit(ctx))

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_review" and len(contexts) == 4
    assert result["knowledge_review_state"]["consumed_by"]["request_id"] == result["attempts"][3]["request_id"]
    verify_requests(result)
