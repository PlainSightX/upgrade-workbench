"""连续修改中的源码关注点与知识适用性；不把离线响应当作模型修复成果。"""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.candidates import apply_increment, load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import context_from_reference, freeze_context
from upgrade_workbench.generation.project_context import (
    CONTINUITY_VERSION,
    bind_pages,
    knowledge_applicability,
    knowledge_result,
)
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.generation.source_context import POLICY, build_context, rebind_source_reads
from upgrade_workbench.planning import prepare_case_proposal

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "cases/copier-6.2.0-r3/manifest.json"
CONFIG = {"version": CONTINUITY_VERSION, "context_tokens": 1_000_000, "framing_reserve_tokens": 4096}


def snapshot(files, revision):
    return SimpleNamespace(files=files, original=files, revision=revision, origin="original", sha256=None)


def observation(files, name, start=1, end=None, revision="old", identity="read"):
    lines = files[name].decode().splitlines(keepends=True)
    end = len(lines) if end is None else end
    return {"id": identity, "kind": "source", "action": {"type": "read_source"}, "result": {
        "view": "current", "revision": revision, "path": name, "start_line": start, "end_line": end,
        "file_sha256": hashlib.sha256(files[name]).hexdigest(), "text": "".join(lines[start - 1:end])}}


def context(files, rows, budget=96000):
    snap = snapshot(files, "new")
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=list(files)))
    return build_context(case, snap, POLICY | {"body_bytes": budget},
                         observations=rebind_source_reads(snap, rows),
                         retain_reads_first=True, read_continuity=True)


def test_unmodified_file_and_uniquely_moved_excerpt_are_current_source():
    old = {"stable.py": b"value = 1\n", "moved.py": b"first = 0\nkeep = 3\nlast = 2\n"}
    current = old | {"moved.py": b"# added\n" + old["moved.py"]}
    rows = [observation(old, "stable.py"), observation(old, "moved.py", 2, 2, identity="moved")]
    untouched = copy.deepcopy(rows)
    rebound = rebind_source_reads(snapshot(current, "new"), rows)
    assert [r["continuity"]["status"] for r in rebound] == ["unchanged_file", "unique_excerpt_relocated"]
    assert rebound[1]["result"]["start_line"] == 3
    assert all(r["result"]["revision"] == "new" for r in rebound)
    assert rows == untouched
    ctx = context(current, rows)
    assert ctx["source_selection"]["read_retention"]["complete"]
    assert all(b["revision"] == "new" for b in ctx["source_files"])


@pytest.mark.parametrize("replacement,reason", [
    (b"keep = 4\n", "excerpt_changed_or_removed"),
    (b"keep = 3\nkeep = 3\n", "ambiguous_excerpt"),
])
def test_changed_or_ambiguous_read_cannot_disappear_as_zero_over_zero(replacement, reason):
    old = {"code.py": b"keep = 3\n"}
    rows = [observation(old, "code.py")]
    result = context({"code.py": replacement}, rows)["source_selection"]["read_retention"]
    assert result["requested_lines"] == 0
    assert result["complete"] is False and result["status"] == "pending_relocation"
    assert result["pending_relocation"][0]["reason"] == reason


def test_full_current_reread_resolves_location_only_and_partial_does_not():
    old = {"code.py": b"a = 1\nb = 2\n"}
    current = {"code.py": b"a = 3\nb = 4\n"}
    historical = observation(old, "code.py")
    first = observation(current, "code.py", 1, 1, revision="new", identity="first")
    second = observation(current, "code.py", 2, 2, revision="new", identity="second")
    probe = {"id": "probe", "kind": "probe", "action": {"type": "run_probe"},
             "result": {"revision": "old", "status": "passed"}}
    assert context(current, [historical, first])["source_selection"]["read_retention"]["pending_relocation"]
    rows = [historical, first, second, probe]
    rebound = rebind_source_reads(snapshot(current, "new"), rows)
    assert rebound[0]["continuity"]["status"] == "reread_current_file"
    assert len(rebound) == 3 and probe["result"]["revision"] == "old"
    assert context(current, rows)["source_selection"]["read_retention"]["complete"]


def test_rebound_reads_still_report_capacity_omission():
    old = {"code.py": b"value = 1\n" * 400}
    result = context(old, [observation(old, "code.py")], budget=1024)["source_selection"]["read_retention"]
    assert result["requested_lines"] == 400
    assert result["status"] == "capacity_omission" and not result["complete"]


def test_original_view_is_not_promoted_and_file_identity_cannot_hide_forged_text():
    files = {"code.py": b"value = 1\n"}
    row = observation(files, "code.py")
    row["result"]["view"] = "original"
    assert rebind_source_reads(snapshot(files, "new"), [row]) == []
    row["result"].update(view="current", text="forged\n")
    with pytest.raises(ValueError, match="excerpt"):
        rebind_source_reads(snapshot(files, "new"), [row])


def pages():
    def page(identity, kind, names):
        return {"id": identity, "kind": kind, "title": identity, "explanation": "Offline interpretation.",
                "unknowns": ["Behavior not verified."],
                "sources": [{"path": name, "start_line": 1, "end_line": 2} for name in names]}
    return [page("worker", "module", ["copier/main.py"]),
            page("flow", "flow", ["copier/main.py", "copier/user_data.py"]),
            page("contract", "constraint", ["business-contract.md"])]


def test_unchanged_citations_are_not_automatically_valid_interpretations(tmp_path):
    case = load_case(CASE)
    base = load_candidate(case)
    knowledge = bind_pages(case, base, pages())
    changed = apply_increment(case, base, {"type": "submit_candidate", "base_revision": base.revision,
        "edits": [{"path": "copier/main.py", "old": "import platform", "new": "import platform  # change"}]},
        tmp_path / "candidate")
    status = knowledge_applicability(case, changed, knowledge)
    assert [r["source_status"] for r in status["topics"]] == ["changed", "changed", "unchanged"]
    assert all(r["interpretation_status"] == "needs_review" for r in status["topics"])
    action = {"type": "read_project_topic", "topic_id": "contract", "revision": changed.revision}
    result = knowledge_result(case, changed, action, knowledge, context_policy=CONFIG)
    assert result["stale"] and result["applicability"]["source_status"] == "unchanged"
    assert "applicability" not in knowledge_result(case, changed, action, knowledge)
    with pytest.raises(ValueError, match="same frozen case"):
        knowledge_applicability(case, changed, knowledge | {"case_fingerprint": "wrong"})


def test_v3_real_request_reconstruction_across_two_candidate_changes(tmp_path):
    ledger = tmp_path / "budget.sqlite"
    BudgetLedger(ledger, {"mode": "user_managed", "limit_usd": None, "model": "owned-model",
        "input_per_million": "1", "output_per_million": "1", "pricing_source": "https://example.test/pricing",
        "pricing_checked_at": "2026-09-26"})
    task = tasks.create_operation(CASE, tmp_path / "work", budget_path=ledger, seed_strategy="none",
        protocol_revision=4, max_calls=8, project_context_policy=CONFIG,
        generation={"model": "owned-model", "endpoint": "https://example.test/chat/completions",
                    "thinking_mode": "disabled", "max_output_tokens": 2048, "timeout_seconds": 10,
                    "max_source_bytes": 400000, "max_request_bytes": 600000})
    calls = []

    def transport(request, **_kwargs):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        calls.append(ctx)
        rev = ctx["candidate"]["revision"]
        actions = [
            {"type": "record_project_knowledge", "revision": rev, "pages": pages()},
            {"type": "read_source", "revision": rev, "path": "copier/user_data.py", "start_line": 1, "end_line": 60},
            {"type": "read_source", "revision": rev, "path": "copier/main.py", "start_line": 1, "end_line": 60},
            {"type": "read_project_topic", "revision": rev, "topic_id": "flow"},
            {"type": "run_public_checks", "revision": rev}]
        return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps({"summary": "Offline", "action": actions[len(calls) - 1]})}}]}).encode()

    task = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert task["status"] == "pending_diagnostic_review" and len(calls) == 5
    case = load_case(CASE)
    current = load_candidate(case)
    for number in (1, 2):
        old = "import platform" if number == 1 else "import platform  # round 1"
        current = apply_increment(case, current, {"type": "submit_candidate", "base_revision": current.revision,
            "edits": [{"path": "copier/main.py", "old": old, "new": f"import platform  # round {number}"}]},
            tmp_path / f"candidate-{number}")
        task["current_candidate"] = current.reference
        reference = freeze_context(task)
        runtime = context_from_reference(reference, case, current)
        assert any(r["continuity"]["status"] == "unchanged_file" for r in runtime["retained_source_reads"])
        assert all(r["stale"] for r in runtime["observations"])
        prepared = prepare_case_proposal(CASE, Path(task["work_root"]), public_source_ack=True,
            candidate_reference=current.reference, output_format="diagnostic_actions",
            diagnostic_context_reference=reference, source_policy=task["protocol"]["source_policy"],
            **task["protocol"]["generation"])
        body = Path(prepared["request_path"]).read_bytes()
        _verify_payload(prepared, body)
        payload = json.loads(body)
        ctx = json.loads(payload["messages"][1]["content"])
        retention = ctx["source_selection"]["read_retention"]
        assert retention["requested_lines"] == 60 and retention["included_lines"] == 60
        assert retention["pending_relocation"] and not retention["complete"]
        assert ctx["project_context"]["knowledge_applicability"]["topics"][0]["source_status"] == "changed"
        ctx["project_context"]["knowledge_applicability"]["topics"][0]["source_status"] = "unchanged"
        payload["messages"][1]["content"] = json.dumps(ctx)
        with pytest.raises(ValueError):
            _verify_payload(prepared, json.dumps(payload).encode())
