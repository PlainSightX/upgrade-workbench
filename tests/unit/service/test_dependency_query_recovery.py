"""验证查询报告先于任务保存的恢复边界；无模型、容器或数据库服务调用。"""

import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import public_fact
from upgrade_workbench.tasks import advance_task, create_operation, inspect_task

ROOT = Path(__file__).resolve().parents[3]


def _provider_response(content):
    return json.dumps({
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
    }).encode()


def _query_report(case, action, directory, status):
    directory.mkdir(parents=True)
    lock_sha = hashlib.sha256(case.new_lock.read_bytes()).hexdigest()
    public = {
        "status": status, "distribution": action["distribution"], "installed_version": "2.6.4",
        "operation": action["operation"], "result": {"operation_confirmed": action["operation"]},
        "environment": {
            "role": "new", "lock_sha256": lock_sha, "image_id": "sha256:" + "a" * 64,
            "base_image_id": "sha256:" + "b" * 64, "python_version": "3.12.0",
            "cache_key": "c" * 64, "preparation_sha256": "d" * 64,
        },
        "limitations": ["Injected report; no dependency or target code executed."],
    }
    if status == "unavailable":
        public.update(installed_version=None, result=None, code="distribution_not_installed")
    report = {
        "schema_version": 1, "kind": "dependency_query", "case_fingerprint": case.fingerprint,
        "action": action, "lock_sha256": lock_sha,
        "query_id": hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest(),
        "report_path": str(directory / "report.json"), "status": status, "public": public,
    }
    Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
    return report

def _service_query_recovery(tmp_path, monkeypatch, *, report_status="observed"):
    from types import SimpleNamespace

    from upgrade_workbench import execution
    from upgrade_workbench.service.engine import Engine
    from upgrade_workbench.service.recovery import owned_task, version

    owner = "a" * 32
    manifest = ROOT / "cases/fastapi-jwt-auth-pydantic-2-r2/manifest.json"
    ledger = tmp_path / "budget.sqlite"
    BudgetLedger(ledger, {
        "mode": "user_managed", "limit_usd": None, "model": "owned-model",
        "input_per_million": "1", "output_per_million": "1",
        "pricing_source": "https://example.test/pricing", "pricing_checked_at": "2026-09-21",
    })
    task = create_operation(
        manifest, tmp_path / "work", budget_path=ledger, service_owner=owner,
        seed_strategy="none", protocol_revision=6, max_calls=3,
        investigator_policy="disabled", contract_audit_policy="disabled",
        generation={
            "model": "owned-model", "endpoint": "https://example.test/chat/completions",
            "thinking_mode": "disabled", "max_output_tokens": 1024, "timeout_seconds": 10,
        },
    )
    path = Path(task["task_path"])
    action = {
        "type": "query_dependency", "environment": "new", "operation": "inspect_symbol",
        "distribution": "pydantic", "module": "pydantic", "qualname": "BaseModel",
    }
    task = advance_task(
        path, api_key="unused", execution_owner=owner,
        transport=lambda *_args, **_kwargs: _provider_response(
            {"summary": "Inspect dependency state.", "action": action}
        ),
    )
    directory = path.parent / "dependency_query_runs" / task["pending_dependency_query"]["request"]["id"]
    report = _query_report(load_case(manifest), action, directory, report_status)
    command = {
        "id": "b" * 32, "status": "running", "before_version": version(path),
        "request": {"action": "advance"},
    }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Recovery must never execute or enqueue work")

    def finish(command_id, status, result):
        assert command_id == command["id"]
        command.update(status=status, result=result)

    engine = object.__new__(Engine)
    engine.store = SimpleNamespace(command=lambda *_args: command, finish=finish, start=forbidden)
    engine.settings = SimpleNamespace(job_root=lambda _job: Path(task["work_root"]))
    engine.task = lambda _job: owned_task(path, Path(task["work_root"]), owner)
    engine._apply = forbidden
    engine.hook = lambda *_args: None
    monkeypatch.setattr(execution, "run_case_dependency_query", forbidden)
    return task, report, command, engine, {"job_id": owner, "command_id": command["id"]}


@pytest.mark.parametrize("status", ["observed", "unavailable"])
def test_service_recovers_query_report_before_task_save_without_replay(tmp_path, monkeypatch, status):
    task, report, command, engine, state = _service_query_recovery(
        tmp_path, monkeypatch, report_status=status,
    )
    path = Path(task["task_path"])
    ledger_before = Path(task["budget_path"]).read_bytes()
    report_before = Path(report["report_path"]).read_bytes()
    engine.execute(state)

    recovered = inspect_task(path)
    assert command["status"] == "completed" and command["result"]["recovered"] is True
    assert recovered["status"] == "ready" and recovered["pending_dependency_query"] is None
    assert len(recovered["dependency_facts"]) == 1
    assert public_fact(recovered, recovered["dependency_facts"][0])["status"] == status
    assert recovered["attempts"] == task["attempts"] and recovered["budget"] == task["budget"]
    assert recovered["current_candidate"] == task["current_candidate"]
    assert Path(task["budget_path"]).read_bytes() == ledger_before
    assert Path(report["report_path"]).read_bytes() == report_before
    saved = path.read_bytes()
    engine.execute(state)
    assert path.read_bytes() == saved


@pytest.mark.parametrize("damage", [
    "missing_directory", "missing_report", "running", "partial", "execution_incomplete",
    "incomplete", "corrupt", "fingerprint", "action", "lock", "path", "environment",
])
def test_service_query_recovery_blocks_missing_or_invalid_report_without_mutation(
    tmp_path, monkeypatch, damage,
):
    task, report, command, engine, state = _service_query_recovery(tmp_path, monkeypatch)
    path = Path(task["task_path"])
    report_path = Path(report["report_path"])
    original_report = report_path.read_bytes()
    before = path.read_bytes()
    ledger_before = Path(task["budget_path"]).read_bytes()
    if damage in {"missing_directory", "missing_report"}:
        report_path.unlink()
        if damage == "missing_directory":
            report_path.parent.rmdir()
    elif damage == "corrupt":
        report_path.write_text("{not-json", encoding="utf-8")
    else:
        if damage in {"running", "partial", "execution_incomplete"}:
            report.update(status=damage, public=None)
        elif damage == "incomplete":
            report.pop("public")
        elif damage == "fingerprint":
            report["case_fingerprint"] = "0" * 64
        elif damage == "action":
            report["action"] = dict(report["action"], qualname="Changed")
        elif damage == "lock":
            report["lock_sha256"] = "0" * 64
        elif damage == "path":
            report["report_path"] = str(report_path.with_name("other.json"))
        elif damage == "environment":
            report["public"]["environment"]["lock_sha256"] = "0" * 64
        report_path.write_text(json.dumps(report), encoding="utf-8")
    engine.execute(state)

    assert command["status"] == "blocked"
    assert command["result"]["reason"] == "artifact_or_execution_requires_reconciliation"
    assert path.read_bytes() == before
    assert Path(task["budget_path"]).read_bytes() == ledger_before
    assert not (path.parent / "dependency_facts").exists()
    # 修正磁盘证据也不能重新打开历史blocked命令。
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(original_report)
    engine.execute(state)
    assert command["status"] == "blocked" and path.read_bytes() == before


@pytest.mark.parametrize("status", ["completed", "blocked", "rejected"])
def test_query_receipt_never_reopens_terminal_service_command(tmp_path, monkeypatch, status):
    task, _report, command, engine, state = _service_query_recovery(tmp_path, monkeypatch)
    command["status"] = status
    before = Path(task["task_path"]).read_bytes()
    engine.execute(state)
    assert command["status"] == status and "result" not in command
    assert Path(task["task_path"]).read_bytes() == before


@pytest.mark.parametrize("change", ["non_query_task", "non_advance_command"])
def test_unchanged_running_non_query_command_stays_blocked(tmp_path, monkeypatch, change):
    from upgrade_workbench.service.recovery import version

    task, _report, command, engine, state = _service_query_recovery(tmp_path, monkeypatch)
    path = Path(task["task_path"])
    if change == "non_query_task":
        task.update(status="ready", pending_dependency_query=None)
        path.write_text(json.dumps(task), encoding="utf-8")
        command["before_version"] = version(path)
    else:
        command["request"]["action"] = "review"
    before = path.read_bytes()
    engine.execute(state)
    assert command["status"] == "blocked" and path.read_bytes() == before
