"""普通 P6 入口实际消费知识并恢复；假模型不计入迁移成果。"""

import json
from pathlib import Path

import pytest

from upgrade_workbench import cli, tasks
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.candidates import apply_increment, load_candidate
from upgrade_workbench.cases import CaseValidationError, load_case
from upgrade_workbench.diagnostics import context_from_reference, freeze_context
from upgrade_workbench.generation.project_context import project_view
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.service.recovery import recover_task

ROOT = Path(__file__).resolve().parents[2]
CASE = ROOT / "cases/openapi-python-client-0.10.0/manifest.delivery-v2.json"
OWNER = "c" * 32


def _pages():
    paths = [
        ("entry", "module", ["openapi_python_client/__init__.py"]),
        ("flow", "flow", ["openapi_python_client/__init__.py", "openapi_python_client/parser/__init__.py"]),
        ("contract", "constraint", ["business-contract.md"]),
    ]
    return [{"id": name, "kind": kind, "title": name, "explanation": "Offline test only.",
             "unknowns": ["Runtime is unobserved."],
             "sources": [{"path": path, "start_line": 1, "end_line": 2} for path in sources]}
            for name, kind, sources in paths]


def _response(action):
    return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": [{
        "finish_reason": "stop", "message": {"content": json.dumps({"summary": "Offline", "action": action})}}]}).encode()


@pytest.fixture
def operation(tmp_path, capsys):
    ledger = tmp_path / "budget.sqlite"
    BudgetLedger(ledger, {"mode": "user_managed", "limit_usd": None, "model": "owned-model",
        "input_per_million": "1", "output_per_million": "1", "pricing_source": "https://example.test/pricing",
        "pricing_checked_at": "2026-09-25"})
    config = {
        "protocol_revision": 6, "max_calls": 8, "execution": {},
        "investigator_policy": "disabled", "contract_audit_policy": "disabled",
        "project_context_policy": {"version": "project-context-v1", "context_tokens": 1_000_000,
                                   "framing_reserve_tokens": 4096},
        "generation": {"model": "owned-model", "endpoint": "https://example.test/chat/completions",
                       "thinking_mode": "disabled", "max_output_tokens": 2048, "timeout_seconds": 10,
                       "max_source_bytes": 400000, "max_request_bytes": 600000},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert cli.main(["--work-root", str(tmp_path / "work"), "task-create", str(CASE),
                     "--config", str(config_path), "--budget", str(ledger), "--seed", "none"]) == 0
    summary = json.loads(capsys.readouterr().out)
    task = tasks.inspect_task(Path(summary["task_path"]))
    assert task["protocol"]["protocol_revision"] == 6
    assert task["protocol"]["finish_policy"] == "probe-lineage-v1"
    assert "repository_preparation" not in task["protocol"]
    return task


def test_p6_cli_knowledge_navigation_and_candidate_invalidation(operation):
    contexts = []

    def transport(request, **_kwargs):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        contexts.append(context)
        assert context["diagnostic_state"]["finish_requirements"]["format_limits"] == {
            "explanation_max_characters": 2000, "evidence_refs_max_items": 8,
        }
        revision = context["candidate"]["revision"]
        if len(contexts) == 1:
            assert context["project_context"]["preparation_required"]
            return _response({"type": "record_project_knowledge", "revision": revision, "pages": _pages()})
        assert not context["project_context"]["preparation_required"]
        assert "Before the FIRST edit" not in context["project_context"]["instructions"]
        assert "preparation is complete" in context["project_context"]["instructions"]
        if len(contexts) == 2:
            return _response({"type": "read_source", "revision": revision,
                "path": "openapi_python_client/templates/model.py.jinja", "start_line": 1, "end_line": 20})
        assert context["diagnostic_state"]["retained_source_reads"]
        return _response({"type": "run_public_checks", "revision": revision})

    task = tasks.advance_task(Path(operation["task_path"]), api_key="offline-fixture-credential-dc6", transport=transport)
    assert task["status"] == "pending_diagnostic_review", task.get("stop_reason")
    assert len(contexts) == 3
    for attempt in task["attempts"]:
        receipt = json.loads(Path(attempt["receipt"]).read_text(encoding="utf-8"))
        _verify_payload(receipt, Path(receipt["request_path"]).read_bytes())
    case = load_case(CASE)
    snapshot = load_candidate(case)
    changed = apply_increment(case, snapshot, {"type": "submit_candidate", "base_revision": snapshot.revision,
        "edits": [{"path": "openapi_python_client/__init__.py", "old": "import shutil", "new": "# Offline revision\nimport shutil"}]},
        Path(task["work_root"]) / "changed")
    task["current_candidate"] = changed.reference
    runtime = context_from_reference(freeze_context(task), case, changed)
    view = project_view(changed, runtime)
    assert view["knowledge_stale"] and not view["preparation_required"]
    assert not runtime["retained_source_reads"]


def test_p6_receipt_recovery_does_not_resend_or_charge_twice(operation, monkeypatch):
    class Crash(BaseException):
        pass

    # 服务恢复沿用现有 owner 合同；模拟落收据后、落状态前的真实边界。
    path = Path(operation["task_path"])
    task = tasks.inspect_task(path)
    task["service_owner"] = OWNER
    path.write_text(json.dumps(task), encoding="utf-8")
    original = tasks.complete_request
    calls = []

    def crash(prepared, **kwargs):
        original(prepared, **kwargs)
        raise Crash()

    def transport(request, **_kwargs):
        calls.append(1)
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        return _response({"type": "record_project_knowledge", "revision": context["candidate"]["revision"], "pages": _pages()})

    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        tasks.advance_task(path, api_key="offline-fixture-credential-dc6", transport=transport, execution_owner=OWNER)
    recovered = recover_task(path, Path(operation["work_root"]), OWNER)
    assert recovered["status"] == "ready"
    assert recovered["project_knowledge_reference"]
    assert len(calls) == recovered["budget"]["calls"] == len(recovered["observations"]) == 1
    assert recover_task(path, Path(operation["work_root"]), OWNER) == recovered


def test_operation_rejects_mismatched_evaluation_before_task_creation(operation, tmp_path):
    root = tmp_path / "rejected"
    with pytest.raises(CaseValidationError, match="Evaluation identity"):
        tasks.create_operation(CASE.with_name("manifest.delivery.json"), root,
            budget_path=Path(operation["budget_path"]), seed_strategy="none", protocol_revision=6,
            generation=operation["protocol"]["generation"])
    assert not root.exists()


def test_business_url_survives_public_log_redaction_without_exposing_host_paths():
    from upgrade_workbench.diagnostics import _clean

    raw = ('url="http://example.test/pets/42" docs=https://errors.pydantic.dev/2.11/u/removed-kwargs '
           'host="C:/Users/private/key.txt" other="D:\\private\\trace.log" '
           'file="file:///C:/Users/private/key.txt" source=/work/source/client.py tmp=/tmp/probe/test.py '
           '路径C:\\Users\\private\\key.txt pathD:\\private\\trace.log')
    clean = _clean(raw)
    assert 'url="http://example.test/pets/42"' in clean
    assert 'docs=https://errors.pydantic.dev/2.11/u/removed-kwargs' in clean
    assert clean.count('<host-path>') == 5
    assert 'private' not in clean
    assert 'source=source/client.py' in clean
    assert 'tmp=<execution-path>' in clean
