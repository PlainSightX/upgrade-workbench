"""知识层接入测试；假响应只验证机制，不计为真实迁移成果。"""

import copy
import json
from pathlib import Path

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.candidates import apply_increment, load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import context_from_reference, freeze_context, public_knowledge
from upgrade_workbench.generation.project_context import (
    ON_DEMAND_VERSION,
    bind_pages,
    knowledge_result,
    repository_catalog,
    repository_map,
    validate_action,
)
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.service.recovery import recover_task

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "cases/copier-6.2.0-r3/manifest.json"
POLICY = {"version": "project-context-v1", "context_tokens": 1_000_000, "framing_reserve_tokens": 4096}
OWNER = "c" * 32


def pages():
    def page(id, kind, names):
        return {"id": id, "kind": kind, "title": id, "explanation": "Offline fixture interpretation.",
                "unknowns": ["Behavior has not been executed."],
                "sources": [{"path": p, "start_line": 1, "end_line": 2} for p in names]}
    return [page("worker", "module", ["copier/main.py"]),
            page("answers-flow", "flow", ["copier/main.py", "copier/user_data.py"]),
            page("contract", "constraint", ["business-contract.md"])]


def response(action):
    return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": [{
        "finish_reason": "stop", "message": {"content": json.dumps({"summary": "Offline test", "action": action})}}]}).encode()


@pytest.fixture(params=[POLICY, POLICY | {"version": ON_DEMAND_VERSION}], ids=["v1", "v2"])
def task(tmp_path, request):
    ledger = tmp_path / "budget.sqlite"
    BudgetLedger(ledger, {"mode": "user_managed", "limit_usd": None, "model": "owned-model",
        "input_per_million": "1", "output_per_million": "1", "pricing_source": "https://example.test/pricing",
        "pricing_checked_at": "2026-09-25"})
    return tasks.create_operation(CASE, tmp_path / "work", budget_path=ledger, seed_strategy="none",
        protocol_revision=4, max_calls=8, service_owner=OWNER, project_context_policy=request.param,
        generation={"model": "owned-model", "endpoint": "https://example.test/chat/completions",
                    "thinking_mode": "disabled", "max_output_tokens": 2048, "timeout_seconds": 10,
                    "max_source_bytes": 400000, "max_request_bytes": 600000})


def test_automatic_preparation_real_request_tools_and_retention(task):
    contexts = []

    def transport(request, **_kwargs):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        rev = ctx["candidate"]["revision"]
        n = len(contexts)
        if n == 1:
            assert ctx["project_context"]["preparation_required"] is True
            assert ctx["context_plan"]["effective_source_body_budget"] > 96000
            assert ctx["source_selection"]["omitted_file_count"] == 0
            return response({"type": "record_project_knowledge", "revision": rev, "pages": pages(),
                "issues": [{"hypothesis": "Check behavior", "evidence_refs": ["business_contract"],
                            "unknown": "Runtime", "next_observation": "Run checks"}]})
        assert ctx["diagnostic_state"]["project_knowledge"]["pages"][1]["id"] == "answers-flow"
        if n == 2:
            return response({"type": "read_project_topic", "revision": rev, "topic_id": "answers-flow"})
        if n == 3:
            return response({"type": "query_project_relations", "revision": rev, "path": "copier/main.py", "cursor": None})
        if n == 4:
            return response({"type": "read_source", "revision": rev, "path": "copier/user_data.py", "start_line": 1, "end_line": 60})
        assert ctx["diagnostic_state"]["retained_source_reads"]
        assert ctx["diagnostic_state"]["runtime_findings"] == []
        return response({"type": "run_public_checks", "revision": rev})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review", result.get("stop_reason")
    assert len(contexts) == 5
    for attempt in result["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_text(encoding="utf-8"))
        _verify_payload(receipt, Path(receipt["request_path"]).read_bytes())
    case = load_case(CASE)
    snapshot = load_candidate(case)
    changed = apply_increment(case, snapshot, {"type": "submit_candidate", "base_revision": snapshot.revision,
        "edits": [{"path": "copier/main.py", "old": "import platform", "new": "import platform  # test revision"}]},
        Path(task["work_root"]) / "test-revision")
    result["current_candidate"] = changed.reference
    runtime = context_from_reference(freeze_context(result), case, changed)
    assert runtime["project_knowledge"]["revision"] == snapshot.revision
    assert runtime["retained_source_reads"] == []
    topic = knowledge_result(case, changed, {"type": "read_project_topic", "revision": changed.revision, "topic_id": "answers-flow"}, runtime["project_knowledge"])
    assert topic["stale"] is True
    assert repository_map(changed)["revision"] == changed.revision
    # 篡改解释正文，即使重写外层hash，也不能绕过模型回执与源码重建。
    ref = result["project_knowledge_reference"]
    value = json.loads(Path(ref["path"]).read_text(encoding="utf-8"))
    value["knowledge"]["pages"][0]["explanation"] = "forged"
    from upgrade_workbench.diagnostics import store
    forged = store(Path(task["task_path"]).parent / "project_knowledge", value)
    with pytest.raises(ValueError, match="identity"):
        public_knowledge(result, forged)


def test_refs_disabled_role_and_stale_revision():
    case = load_case(CASE)
    snapshot = load_candidate(case)
    action = {"type": "record_project_knowledge", "revision": snapshot.revision, "pages": pages()}
    validate_action(case, snapshot, action, POLICY)
    for enabled, role in ((None, "solver"), (POLICY, "investigator")):
        with pytest.raises(ValueError):
            validate_action(case, snapshot, action, enabled, role=role)
    bad = copy.deepcopy(action)
    bad["pages"][0]["sources"][0]["path"] = "checks/acceptance/test_behavior_preservation.py"
    with pytest.raises(ValueError, match="outside"):
        bind_pages(case, snapshot, bad["pages"])
    bad = copy.deepcopy(action)
    bad["pages"][0]["sources"][0]["end_line"] = 999999
    with pytest.raises(ValueError, match="range"):
        bind_pages(case, snapshot, bad["pages"])
    with pytest.raises(ValueError, match="Stale"):
        validate_action(case, snapshot, action | {"revision": "wrong"}, POLICY)


def test_generation_recovery_is_once_and_charged(task, monkeypatch):
    class Crash(BaseException):
        pass
    original = tasks.complete_request
    calls = []

    def crash(prepared, **kwargs):
        original(prepared, **kwargs)
        raise Crash()

    def transport(request, **_kwargs):
        calls.append(1)
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        return response({"type": "record_project_knowledge", "revision": context["candidate"]["revision"], "pages": pages()})

    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    result = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert result["status"] == "ready", result
    assert result["project_knowledge_reference"]
    assert len(result["observations"]) == 1 and result["budget"]["calls"] == 1 and len(calls) == 1
    again = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert again == result


def test_large_page_rejected_before_it_can_poison_history():
    case = load_case(CASE)
    snapshot = load_candidate(case)
    value = pages()
    value[0]["unknowns"] = ["待核实" * 330] * 8
    with pytest.raises(ValueError, match="readable tool capacity"):
        bind_pages(case, snapshot, value)


def test_p6_opt_in_and_investigator_gate():
    from upgrade_workbench.generation.protocol_v6 import validate
    case = load_case(CASE)
    snapshot = load_candidate(case)
    action = {"type": "record_project_knowledge", "revision": snapshot.revision, "pages": pages(),
        "issues": [{"hypothesis": "Review", "evidence_refs": ["business_contract"],
                    "unknown": "Runtime", "next_observation": "Check"}]}
    assert validate(case, action, snapshot, project_context_policy=POLICY) == action
    with pytest.raises(ValueError):
        validate(case, action, snapshot)
    with pytest.raises(ValueError, match="Only Solver"):
        validate(case, action, snapshot, role="investigator", project_context_policy=POLICY)


def test_request_planning_budget_and_forged_map(task):
    from upgrade_workbench.generation.project_context import assemble
    from upgrade_workbench.generation.request import _instructions
    from upgrade_workbench.planning import prepare_case_proposal
    case = load_case(CASE)
    snapshot = load_candidate(case)
    runtime = context_from_reference(freeze_context(task), case, snapshot)
    prepared = prepare_case_proposal(CASE, Path(task["work_root"]), public_source_ack=True,
        output_format="diagnostic_actions", diagnostic_context_reference=freeze_context(task),
        source_policy=task["protocol"]["source_policy"], **task["protocol"]["generation"])
    body = Path(prepared["request_path"]).read_bytes()
    _verify_payload(prepared, body)
    payload = json.loads(body)
    context = json.loads(payload["messages"][1]["content"])
    with pytest.raises(ValueError, match="capacity"):
        assemble(case, snapshot, context, runtime, task["protocol"]["source_policy"],
            model="owned-model", system=_instructions("diagnostic_actions"), output_tokens=2048,
            thinking_mode="disabled", request_limit=8192)
    context["project_context"]["map"]["modules"][0]["path"] = "forged.py"
    payload["messages"][1]["content"] = json.dumps(context)
    with pytest.raises(ValueError, match="selection|context"):
        _verify_payload(prepared, json.dumps(payload).encode())


def test_capacity_crosses_selection_plateau_with_exact_serialized_bytes(monkeypatch):
    from types import SimpleNamespace

    from upgrade_workbench.generation import project_context as pc
    from upgrade_workbench.generation import source_context as sc

    source = ('value = "\\\\雪"\n' * 4000).encode()
    snapshot = SimpleNamespace(files={"model.py": source}, original={"model.py": source},
                               revision="a" * 64, origin="original", sha256=None)
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=["model.py"]))
    context = {"potential_impacts": [], "fixed": "k" * 20000}
    runtime = {"project_context_policy": POLICY, "observations": []}
    opts = dict(model="owned-model", system="test", output_tokens=2048, thinking_mode="disabled")
    wide = pc.assemble(case, snapshot, context, runtime, sc.POLICY, request_limit=600000, **opts)
    limit = len(pc.payload_for(wide, **opts)) - 500
    original = sc.build_context
    calls = []

    def spy(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append((args[2]["body_bytes"], result["source_selection"]["body_bytes"]))
        return result

    monkeypatch.setattr(sc, "build_context", spy)
    result = pc.assemble(case, snapshot, context, runtime, sc.POLICY, request_limit=limit, **opts)
    assert len(calls) < 12  # 按超额字节收敛，不能逐字重扫项目源码。
    assert result["fixed"] == context["fixed"] and result["potential_impacts"] == []
    assert len(pc.payload_for(result, **opts)) <= limit
    assert result["source_selection"]["body_bytes"] > 0.99 * len(source)
    for block in result["source_files"]:
        assert block["text"] == "".join(source.decode().splitlines(keepends=True)[block["start_line"] - 1:block["end_line"]])
    with pytest.raises(pc.ContextCapacityError, match="source_projection_exceeds_capacity"):
        pc.assemble(case, snapshot, context, runtime, sc.POLICY | {"mode": "full"}, request_limit=limit, **opts)


def test_capacity_failure_is_durable_host_result_before_reservation(task, monkeypatch):
    from upgrade_workbench.generation.project_context import ContextCapacityError

    error = ContextCapacityError("required_context_exceeds_capacity", ceiling=8192, fixed_bytes=9000)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(tasks, "prepare_case_proposal", fail)
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", execution_owner=OWNER,
                               transport=lambda *_a, **_k: pytest.fail("Provider must not be called"))
    assert result["status"] == "execution_incomplete"
    assert result["context_capacity_failure"] == error.details
    assert BudgetLedger(Path(task["budget_path"])).snapshot()["calls"] == 0 and result["attempts"] == []
    assert tasks.inspect_task(Path(task["task_path"]))["context_capacity_failure"] == error.details


def test_small_complete_source_fits_without_arbitrary_metadata_reserve():
    from types import SimpleNamespace

    from upgrade_workbench.generation import project_context as pc
    from upgrade_workbench.generation import source_context as sc

    source = b"value = 1\n"
    snapshot = SimpleNamespace(files={"model.py": source}, original={"model.py": source},
                               revision="a" * 64, origin="original", sha256=None)
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=["model.py"]))
    context = {"potential_impacts": [], "fixed": "k" * 20000}
    runtime = {"project_context_policy": POLICY, "observations": []}
    opts = dict(model="owned-model", system="test", output_tokens=2048, thinking_mode="disabled")
    wide = pc.assemble(case, snapshot, context, runtime, sc.POLICY, request_limit=600000, **opts)
    limit = len(pc.payload_for(wide, **opts))
    assert limit - wide["context_plan"]["fixed_context_bytes"] < 5120
    result = pc.assemble(case, snapshot, context, runtime, sc.POLICY, request_limit=limit, **opts)
    assert result["source_selection"]["body_bytes"] == len(source)
    with pytest.raises(pc.ContextCapacityError, match="required_context_exceeds_capacity"):
        pc.assemble(case, snapshot, context, runtime, sc.POLICY, request_limit=8192, **opts)


def test_catalog_keeps_cross_file_discovery_and_exact_paged_details():
    from types import SimpleNamespace

    from upgrade_workbench.generation.source_context import navigate, outline

    files = {f"part_{i}.py": b"VALUE = 1\n" for i in range(60)}
    files["entry.py"] = ("".join(f"import part_{i}\n" for i in range(60))
                         + "".join(f"def f_{i}():\n    return part_{i}.VALUE\n" for i in range(60))).encode()
    files["broken.py"] = b"def unfinished("
    files["template.txt"] = b"{{ value }}\n"
    snapshot = SimpleNamespace(files=files, revision="a" * 64)
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=["entry.py"]))
    catalog = repository_catalog(snapshot)
    assert {row["path"] for row in catalog["modules"]} == set(files)
    entry = next(row for row in catalog["modules"] if row["path"] == "entry.py")
    assert set(entry["imports"]) == set(files) - {"entry.py", "broken.py", "template.txt"}
    assert entry["symbol_count"] == 60 and "symbols" not in entry
    assert next(row for row in catalog["modules"] if row["path"] == "broken.py")["parse_error"]
    action = {"type": "query_project_relations", "revision": snapshot.revision,
              "path": "entry.py", "cursor": None}
    relations = []
    while True:
        result = knowledge_result(case, snapshot, action)
        relations.extend(result["items"])
        if result["next_cursor"] is None:
            break
        action["cursor"] = result["next_cursor"]
    assert relations == repository_map(snapshot)["relations"]
    assert all(files[edge["from"]].decode().splitlines()[edge["line"] - 1] == edge["statement"] for edge in relations)
    with pytest.raises(ValueError, match="Stale"):
        knowledge_result(case, SimpleNamespace(files=files, revision="b" * 64), action)
    action = {"type": "outline_source", "revision": snapshot.revision, "path": "entry.py"}
    symbols = []
    while True:
        result = navigate(case, snapshot, action)
        symbols.extend(result["items"])
        if result["next_cursor"] is None:
            break
        action["cursor"] = result["next_cursor"]
    assert symbols == outline(files["entry.py"])
    assert navigate(case, snapshot, {"type": "read_source", "revision": snapshot.revision,
        "path": "entry.py", "start_line": 61, "end_line": 62})["text"] == "def f_0():\n    return part_0.VALUE\n"
