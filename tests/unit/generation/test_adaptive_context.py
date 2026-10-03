"""可控假响应验证实际任务消费与恢复，不把回归测试写成模型效果评测。"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.candidates import apply_increment, load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import (
    context_from_reference,
    freeze_context,
    public_observation,
    read,
    store,
)
from upgrade_workbench.generation.project_context import (
    ADAPTIVE_VERSION,
    assemble,
    validate_action,
)
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.generation.source_context import POLICY as SOURCE_POLICY
from upgrade_workbench.reporting import export_task_report
from upgrade_workbench.service.recovery import recover_task

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "cases/flaskbb-sqlalchemy-1.4.21-r2/manifest-v4.json"
CONFIG = {"version": ADAPTIVE_VERSION, "context_tokens": 1_000_000, "framing_reserve_tokens": 4096}
OWNER = "c" * 32


def response(action):
    return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": [{
        "finish_reason": "stop", "message": {"content": json.dumps({"summary": "Offline mechanism check", "action": action})}}]}).encode()


def selection(revision, mode="deep", reason="Inspect incoming and outgoing uses before editing."):
    return {"type": "select_investigation", "revision": revision, "mode": mode,
            "reason": reason, "focus_paths": ["flaskbb/user/models.py"]}


def create(tmp_path, config=CONFIG, protocol=6):
    ledger = tmp_path / "budget.sqlite"
    BudgetLedger(ledger, {"mode": "user_managed", "limit_usd": None, "model": "owned-model",
        "input_per_million": "1", "output_per_million": "1", "pricing_source": "https://example.test/pricing",
        "pricing_checked_at": "2026-09-27"})
    return tasks.create_operation(CASE, tmp_path / "work", budget_path=ledger, seed_strategy="none",
        protocol_revision=protocol, max_calls=10, service_owner=OWNER, project_context_policy=config,
        generation={"model": "owned-model", "endpoint": "https://example.test/chat/completions",
                    "thinking_mode": "disabled", "max_output_tokens": 2048, "timeout_seconds": 10,
                    "max_source_bytes": 1600000, "max_request_bytes": 850000})


def test_ordinary_choice_read_switch_edit_and_export(tmp_path):
    task = create(tmp_path)
    contexts = []

    def transport(request, **_kwargs):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        rev = ctx["candidate"]["revision"]
        assert not ctx["project_context"]["preparation_required"]
        actions = [selection(rev),
                   {"type": "read_source", "revision": rev, "path": "flaskbb/user/models.py", "start_line": 1, "end_line": 60},
                   selection(rev, "direct", "The remaining change is local; keep this implementation first."),
                   {"type": "query_project_relations", "revision": rev, "path": "flaskbb/user/models.py", "cursor": None},
                   {"type": "submit_candidate", "base_revision": rev, "edits": [
                       {"path": "flaskbb/forum/models.py", "old": "import logging", "new": "import logging  # offline change"}]}]
        return response(actions[len(contexts) - 1])

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert len(contexts) == 5 and result["status"] == "pending_review", result.get("stop_reason")
    assert result.get("project_knowledge_reference") is None
    assert contexts[0]["project_context"]["investigation_selection"] is None
    deep = contexts[1]["context_plan"]["investigation"]["priority_paths"]
    assert deep[0] == "flaskbb/user/models.py" and len(deep) > 1
    assert contexts[1]["source_files"][0]["reason"] == "investigation_priority"
    assert contexts[1]["source_selection"]["read_retention"]["requested_lines"] == 0
    direct = contexts[3]["context_plan"]["investigation"]["priority_paths"]
    assert direct == ["flaskbb/user/models.py"]
    assert contexts[3]["source_selection"]["read_retention"]["included_lines"] == 60
    for attempt in result["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_text(encoding="utf-8"))
        frozen = read(receipt["diagnostic_context_reference"], Path(task["task_path"]).parent)
        base = load_candidate(load_case(CASE), frozen["current_candidate"])
        _verify_payload(receipt | {"candidate_reference": frozen["current_candidate"], "candidate_revision": base.revision},
                        Path(receipt["request_path"]).read_bytes())
    exported = export_task_report(Path(result["task_path"]), tmp_path / "export")
    report = json.loads(Path(exported["json_path"]).read_text(encoding="utf-8"))
    assert [row["mode"] for row in report["investigation_selections"]] == ["deep", "direct"]
    assert [row["mode"] for row in report["investigation_consumption"]] == ["deep", "deep", "direct", "direct"]
    assert all(row["request"]["sha256"] for row in report["investigation_consumption"])
    assert all(row["delivery_state"] == "response_received" for row in report["investigation_consumption"])
    assert report["result"] == "not_evaluated"
    # 修改后偏好仍保留，阅读按当前源码绑定；选择本身不升级为当前行为证据。
    case = load_case(CASE)
    base = load_candidate(case)
    changed = apply_increment(case, base, {"type": "submit_candidate", "base_revision": base.revision,
        "edits": [{"path": "flaskbb/forum/models.py", "old": "import logging", "new": "import logging  # next revision"}]}, tmp_path / "changed")
    result["current_candidate"] = changed.reference
    runtime = context_from_reference(freeze_context(result), case, changed)
    assert runtime["investigation_selection"]["revision"] == base.revision
    assert runtime["investigation_selection"]["current_revision"] == changed.revision
    assert runtime["retained_source_reads"][0]["continuity"]["status"] == "unchanged_file"


def test_selection_recovery_once_and_forgery_rejected(tmp_path, monkeypatch):
    task = create(tmp_path)
    original = tasks.complete_request
    calls = []

    class Crash(BaseException):
        pass

    def crash(prepared, **kwargs):
        original(prepared, **kwargs)
        raise Crash()

    def transport(request, **_kwargs):
        calls.append(1)
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        return response(selection(ctx["candidate"]["revision"]))

    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    result = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert result["status"] == "ready"
    assert len(result["observations"]) == result["budget"]["calls"] == len(calls) == 1
    assert recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER) == result
    case = load_case(CASE)
    runtime = context_from_reference(freeze_context(result), case, load_candidate(case))
    assert runtime["investigation_selection"]["mode"] == "deep"
    # 反复读取模式理由也不能把已有停滞计数清零，兼容旧/新两种预算策略。
    from upgrade_workbench.diagnostics import remember_action
    from upgrade_workbench.investigation_policy import policy
    for budget in (None, policy({"version": "investigation-budget-v1"})):
        current = copy.deepcopy(result)
        if budget:
            current["protocol"]["investigation_policy"] = budget
        current["diagnostic_progress"] = {"revision": load_candidate(case).revision,
            "consecutive_revisits": 2, "seen_observations": [], "seen_output_pages": []}
        remember_action(current, {"action": {"type": "get_observation", "observation_id": result["observations"][0]["id"], "cursor": None}},
                        case, before_revision=load_candidate(case).revision)
        assert current["diagnostic_progress"]["consecutive_revisits"] == 2
    ref = result["observations"][0]
    value = read(ref, Path(task["task_path"]).parent)
    value["action"]["mode"] = value["result"]["mode"] = "direct"
    forged = store(Path(task["task_path"]).parent / "observations", value)
    with pytest.raises(ValueError, match="identity"):
        public_observation(result, forged)


@pytest.mark.parametrize("version", [None, "project-context-v1", "project-context-v2", "project-context-v3"])
def test_choice_requires_new_policy(version):
    case = load_case(CASE)
    snapshot = load_candidate(case)
    with pytest.raises(ValueError):
        validate_action(case, snapshot, selection(snapshot.revision), CONFIG | {"version": version} if version else None)


@pytest.mark.parametrize("bad", ["role", "revision", "path", "mode", "empty", "duplicates"])
def test_selection_boundaries(bad):
    case = load_case(CASE)
    snapshot = load_candidate(case)
    action = selection(snapshot.revision)
    if bad == "revision":
        action["revision"] = "wrong"
    if bad == "path":
        action["focus_paths"] = ["checks/acceptance/test_hidden.py"]
    if bad == "mode":
        action["mode"] = "shell"
    if bad == "empty":
        action["focus_paths"] = []
    if bad == "duplicates":
        action["focus_paths"] *= 2
    with pytest.raises(ValueError):
        validate_action(case, snapshot, action, CONFIG, role="investigator" if bad == "role" else "solver")


def test_old_first_edit_gate_and_new_protocol_boundary(tmp_path):
    with pytest.raises(ValueError, match="requires P6"):
        create(tmp_path / "p4", protocol=4)
    task = create(tmp_path / "old", CONFIG | {"version": "project-context-v3"})
    contexts = []

    def transport(request, **_kwargs):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        if len(contexts) == 1:
            return response({"type": "submit_candidate", "base_revision": ctx["candidate"]["revision"],
                "edits": [{"path": "flaskbb/forum/models.py", "old": "import logging", "new": "import logging  # rejected"}]})
        assert "knowledge" in json.dumps(ctx["task_context"]).lower()
        return response({"type": "run_public_checks", "revision": ctx["candidate"]["revision"]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert len(contexts) == 2 and result["current_candidate"] is None
    assert result["status"] == "pending_diagnostic_review"


def test_deep_selection_changes_actual_source_at_equal_capacity():
    # 非可编辑邻居的原文须实际送达；只改 context_plan 的模式文字不够。
    files = {"focus.py": b"from neighbor import run\nrun()\n",
             "neighbor.py": b"def run():\n    return 1\n",
             "aaa_editable.py": b"value = 1\n" * 8000,
             "zz_other.txt": b"non-code public input\n"}
    snapshot = SimpleNamespace(files=files, original=files, revision="a" * 64, origin="original", sha256=None)
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=["aaa_editable.py"]))
    opts = dict(model="owned-model", system="test", output_tokens=2048, thinking_mode="disabled", request_limit=30000)
    base = {"project_context_policy": CONFIG, "observations": [], "investigation_selection": {
        "mode": "direct", "focus_paths": ["focus.py"], "reason": "Local inspection", "revision": snapshot.revision}}
    direct = assemble(case, snapshot, {"potential_impacts": []}, base, SOURCE_POLICY, **opts)
    deep_runtime = copy.deepcopy(base)
    deep_runtime["investigation_selection"]["mode"] = "deep"
    deep = assemble(case, snapshot, {"potential_impacts": []}, deep_runtime, SOURCE_POLICY, **opts)
    assert direct["context_plan"]["input_byte_ceiling"] == deep["context_plan"]["input_byte_ceiling"]
    assert not any(row["path"] == "neighbor.py" for row in direct["source_files"])
    assert any(row["path"] == "neighbor.py" and row["text"] == files["neighbor.py"].decode() for row in deep["source_files"])
    assert deep["source_selection"]["read_retention"]["requested_lines"] == 0
    wide = assemble(case, snapshot, {"potential_impacts": []}, base, SOURCE_POLICY, **(opts | {"request_limit": 600000}))
    assert any(row["path"] == "zz_other.txt" for row in wide["source_files"])
    full = assemble(case, snapshot, {"potential_impacts": []}, deep_runtime,
                    SOURCE_POLICY | {"mode": "full"}, **(opts | {"request_limit": 600000}))
    assert full["source_selection"]["omitted_file_count"] == 0
    assert [row["path"] for row in full["source_files"]][:2] == ["focus.py", "neighbor.py"]


def test_unsent_or_redacted_requests_are_not_exported_as_consumed(tmp_path):
    from upgrade_workbench.reporting import _investigation_consumption

    attempts = []
    for number, receipt in enumerate(({"calls": 0}, {"calls": 1, "request_storage": "metadata_only"})):
        path = tmp_path / f"receipt-{number}.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        attempts.append({"receipt": str(path)})
    assert _investigation_consumption({"attempts": attempts}, tmp_path) == []
