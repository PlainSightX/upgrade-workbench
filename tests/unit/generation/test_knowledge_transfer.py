"""跨任务导入走普通请求与恢复；假响应不计作真实模型效果。"""

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
from upgrade_workbench.cli import main
from upgrade_workbench.diagnostics import context_from_reference, freeze_context
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.generation.source_context import encoded
from upgrade_workbench.knowledge_transfer import export_bundle, load_bundle, public_import
from upgrade_workbench.reporting import export_task_report
from upgrade_workbench.service.recovery import recover_task

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "cases/flaskbb-sqlalchemy-1.4.21-r2/manifest-v4.json"
POLICY = {"version": "project-context-v4", "context_tokens": 1_000_000, "framing_reserve_tokens": 4096}
OWNER = "c" * 32


def response(action):
    return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": [{
        "finish_reason": "stop", "message": {"content": json.dumps({"summary": "Offline mechanism test", "action": action})}}]}).encode()


def create(root, imported=None, version="project-context-v4", max_calls=6, **options):
    budget = root / "budget.sqlite"
    BudgetLedger(budget, {"mode": "user_managed", "limit_usd": None, "model": "owned-model",
        "input_per_million": "1", "output_per_million": "1", "pricing_source": "https://example.test/pricing",
        "pricing_checked_at": "2026-09-27"})
    return tasks.create_operation(CASE, root / "work", budget_path=budget, seed_strategy="none",
        protocol_revision=6, max_calls=max_calls, service_owner=OWNER,
        project_context_policy=POLICY | {"version": version}, knowledge_import=imported,
        generation={"model": "owned-model", "endpoint": "https://example.test/chat/completions",
                    "thinking_mode": "disabled", "max_output_tokens": 2048, "timeout_seconds": 10,
                    "max_source_bytes": 1600000, "max_request_bytes": 850000}, **options)


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    root = tmp_path_factory.mktemp("knowledge-donor")
    task = create(root)
    calls = []

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        rev = ctx["candidate"]["revision"]
        calls.append(rev)
        if len(calls) > 1:
            return response({"type": "run_public_checks", "revision": rev})
        def page(identity, kind, names):
            return {"id": identity, "kind": kind, "title": identity,
                "explanation": "Historical source explanation; no behavior claim.", "unknowns": ["Runtime behavior unknown."],
                "sources": [{"path": name, "start_line": 1, "end_line": 2} for name in names]}
        return response({"type": "record_project_knowledge", "revision": rev, "pages": [
            page("worker", "module", ["flaskbb/forum/models.py"]),
            page("answers", "flow", ["flaskbb/forum/models.py", "flaskbb/user/models.py"]),
            page("contract", "constraint", ["business-contract.md"])]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_diagnostic_review"
    spec = export_bundle(Path(task["task_path"]), root / "knowledge.json")
    spec = {key: spec[key] for key in ("path", "sha256")}
    return spec, result


def test_cli_export_and_ordinary_import_consumption_survive_external_file_removal(exported, tmp_path, capsys):
    spec, source = exported
    bundle_file = tmp_path / "portable.json"
    assert main(["task-knowledge-export", source["task_path"], "--output", str(bundle_file)]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["sha256"] == spec["sha256"]
    task = create(tmp_path, {"path": str(bundle_file), "sha256": cli["sha256"]})
    bundle_file.rename(tmp_path / "retired-export.json")
    contexts = []

    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(ctx)
        imported = ctx["project_context"]["imported_knowledge"]
        assert imported["origin"]["task_id"] == source["task_id"] != task["task_id"]
        assert imported["changed_declared_inputs"] == imported["changed_source_paths"] == []
        assert all(p["applicability"]["status"] == "reference_only" for p in imported["topics"])
        assert ctx["diagnostic_state"]["project_knowledge"] is None
        assert not ctx["project_context"]["preparation_required"]
        rev = ctx["candidate"]["revision"]
        if len(contexts) == 1:
            return response({"type": "read_source", "revision": rev, "path": "flaskbb/forum/models.py", "start_line": 1, "end_line": 12})
        return response({"type": "submit_candidate", "base_revision": rev, "edits": [
            {"path": "flaskbb/forum/models.py", "old": "import logging", "new": "import logging  # local test"}]})

    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    assert result["status"] == "pending_review", result.get("stop_reason")
    report_path = export_task_report(Path(task["task_path"]), tmp_path / "report")["json_path"]
    report = json.loads(Path(report_path).read_bytes())
    assert len(report["knowledge_import_consumption"]) == 2
    assert report["knowledge_import"]["changed_source_paths"] == ["flaskbb/forum/models.py"]
    assert all(p["applicability"]["status"] == "needs_review" for p in report["knowledge_import"]["topics"])
    assert report["final_evaluation"] == "not_run"
    assert not result.get("project_knowledge_reference")
    # 原请求在导入文件移动后仍重建；候选提交回执必须使用执行前的候选身份。
    first = json.loads(Path(result["attempts"][0]["receipt"]).read_bytes())
    _verify_payload(first, Path(first["request_path"]).read_bytes())


def test_input_changes_and_ambiguous_citation_are_not_validated_knowledge(exported, tmp_path):
    spec, _ = exported
    task = create(tmp_path, spec)
    case = load_case(CASE)
    original = load_candidate(case)
    lines = original.files["flaskbb/forum/models.py"].decode().splitlines(keepends=True)
    excerpt = "".join(lines[:2])
    changed = replace(original, files=original.files | {"flaskbb/forum/models.py": (excerpt + excerpt).encode()}, revision="d"*64)
    hashes = case.manifest.file_hashes | {case.manifest.new_lock: "f"*64}
    new_case = replace(case, manifest=case.manifest.model_copy(update={"file_hashes": hashes}))
    current = public_import(task, new_case, changed)
    from upgrade_workbench.generation.project_context import import_provenance
    assert current["changed_declared_inputs"] == [case.manifest.new_lock]
    assert current["topics"][0]["applicability"]["sources"][0]["status"] == "ambiguous_excerpt"
    assert current["topics"][0]["applicability"]["sources"][0]["current_location"] is None
    visible = import_provenance(current)["topics"][0]
    assert visible["applicability"]["sources"][0]["status"] == "ambiguous_excerpt"
    assert visible["applicability"]["sources"][0]["current_location"] is None
    assert "explanation" not in visible
    shifted = replace(original, files=original.files | {"flaskbb/forum/models.py": b"# inserted\n" + original.files["flaskbb/forum/models.py"]}, revision="e"*64)
    moved = import_provenance(public_import(task, case, shifted))["topics"][0]["applicability"]["sources"][0]
    assert moved["status"] == "unique_excerpt_relocated" and moved["current_location"]["start_line"] == 2
    # 未引用的公开源码变化同样要求解释复核，避免仅引用哈希导致伪命中。
    other = next(name for name in original.files if name not in {"flaskbb/forum/models.py", "flaskbb/user/models.py"})
    changed = replace(original, files=original.files | {other: original.files[other] + b"\n"})
    assert all(p["applicability"]["status"] == "needs_review" for p in public_import(task, case, changed)["topics"])
    narrowed = replace(case, manifest=case.manifest.model_copy(update={"allowed_changes": case.manifest.allowed_changes[:1]}))
    assert "case.allowed_changes" in public_import(task, narrowed, original)["changed_declared_inputs"]


def test_recovery_applies_response_once_with_frozen_import(exported, tmp_path, monkeypatch):
    spec, _ = exported
    task = create(tmp_path, spec)
    complete = tasks.complete_request
    calls = []
    class Crash(BaseException):
        pass
    def crash(prepared, **kwargs):
        complete(prepared, **kwargs)
        raise Crash()
    def transport(request, **_):
        ctx = json.loads(json.loads(request.data)["messages"][1]["content"])
        calls.append(ctx["project_context"]["imported_knowledge"]["bundle_sha256"])
        return response({"type": "read_source", "revision": ctx["candidate"]["revision"], "path": "flaskbb/forum/models.py", "start_line": 1, "end_line": 10})
    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)
    restored = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert restored["status"] == "ready" and len(restored["attempts"]) == len(calls) == 1
    again = recover_task(Path(task["task_path"]), Path(task["work_root"]), OWNER)
    assert len(again["observations"]) == len(restored["observations"]) == 1
    case = load_case(CASE)
    frozen = freeze_context(restored)
    runtime = context_from_reference(frozen, case, load_candidate(case))
    assert runtime["imported_knowledge"]["bundle_sha256"] == spec["sha256"]
    Path(restored["knowledge_import_reference"]["path"]).write_bytes(b"{}")
    with pytest.raises(ValueError, match="changed"):
        tasks.inspect_task(Path(task["task_path"]))


def test_reject_wrong_repository_hash_extra_content_and_old_policy(exported, tmp_path):
    spec, _ = exported
    with pytest.raises(ValueError, match="digest"):
        load_bundle(spec | {"sha256": "0"*64}, load_case(CASE))
    different = ROOT / "cases/copier-6.2.0-r3/manifest.json"
    with pytest.raises(ValueError, match="same repository"):
        load_bundle(spec, load_case(different))
    raw = json.loads(Path(spec["path"]).read_bytes())
    raw["hidden_acceptance"] = "must not transfer"
    data = encoded(raw)
    bad = tmp_path / "bad.json"
    bad.write_bytes(data)
    with pytest.raises(ValueError, match="Invalid knowledge bundle"):
        load_bundle({"path": str(bad), "sha256": hashlib.sha256(data).hexdigest()}, load_case(CASE))
    with pytest.raises(ValueError, match="adaptive"):
        create(tmp_path / "old-policy", spec, version="project-context-v3")
    assert not (tmp_path / "old-policy/work/tasks").exists()


def test_bundle_reference_cannot_change_after_protocol_freeze(exported, tmp_path):
    spec, _ = exported
    task = create(tmp_path, spec)
    altered = copy.deepcopy(task)
    altered["knowledge_import_reference"]["id"] = "a"*64
    case = load_case(CASE)
    with pytest.raises(ValueError, match="binding"):
        public_import(altered, case, load_candidate(case))
