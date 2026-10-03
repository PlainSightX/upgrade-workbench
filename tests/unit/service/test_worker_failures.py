"""数据库故障只改变调度失败边界，不授权重放有副作用的命令。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from psycopg import OperationalError

from upgrade_workbench.service import cli
from upgrade_workbench.service.config import ServiceLayoutPreflightError
from upgrade_workbench.service.engine import Engine, WorkerDatabaseError


def test_polling_failure_has_safe_phase_and_no_job_mutation():
    engine = object.__new__(Engine)
    engine.store = SimpleNamespace(runnable=Mock(side_effect=OperationalError("private DSN")),
                                   block=Mock())
    engine.step = Mock()

    with pytest.raises(RuntimeError, match="worker_database_unavailable") as caught:
        engine.run_once()

    assert caught.value.phase == "poll"
    assert caught.value.job_id is None
    engine.step.assert_not_called()
    engine.store.block.assert_not_called()
    assert "private DSN" not in str(caught.value)


def test_execution_failure_stops_without_retry_or_database_block():
    engine = object.__new__(Engine)
    engine.store = SimpleNamespace(runnable=Mock(return_value=["job", "later"]), block=Mock())
    engine.step = Mock(side_effect=OperationalError("private DSN"))

    with pytest.raises(RuntimeError, match="worker_database_unavailable") as caught:
        engine.run_once()

    assert caught.value.phase == "job"
    assert caught.value.job_id == "job"
    assert caught.value.retryable is False
    engine.step.assert_called_once_with("job")
    engine.store.block.assert_not_called()


@pytest.mark.parametrize("initial", [ValueError("artifact"), ServiceLayoutPreflightError("layout")])
def test_execution_failure_when_block_record_also_loses_database(initial):
    engine = object.__new__(Engine)
    engine.store = SimpleNamespace(runnable=Mock(return_value=["job"]),
                                   block=Mock(side_effect=OperationalError("private DSN")))
    engine.step = Mock(side_effect=initial)
    with pytest.raises(WorkerDatabaseError) as caught:
        engine.run_once()
    assert caught.value.phase == "job" and caught.value.retryable is False
    engine.step.assert_called_once()
    engine.store.block.assert_called_once()


def test_polling_retries_are_bounded_and_permanent_failure_stops(monkeypatch, capsys):
    error = WorkerDatabaseError(OperationalError("private DSN"), phase="poll")
    engine = SimpleNamespace(run_once=Mock(side_effect=error))
    sleep = Mock()
    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert cli._run_worker(engine, once=False) == 2
    assert engine.run_once.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
    assert '"will_retry": false' in capsys.readouterr().out

    class AuthenticationFailure(OperationalError):
        sqlstate = "28P01"

    engine.run_once = Mock(side_effect=WorkerDatabaseError(AuthenticationFailure("private DSN"), phase="poll"))
    sleep.reset_mock()
    assert cli._run_worker(engine, once=False) == 2
    engine.run_once.assert_called_once()
    sleep.assert_not_called()


def test_job_failure_cli_never_retries(monkeypatch):
    error = WorkerDatabaseError(OperationalError("private DSN"), phase="job", job_id="job")
    engine = SimpleNamespace(run_once=Mock(side_effect=error))
    monkeypatch.setattr(cli.time, "sleep", Mock(side_effect=AssertionError("no command retry")))
    assert cli._run_worker(engine, once=False) == 2
    engine.run_once.assert_called_once()


def test_execution_finish_failure_uses_running_recovery_without_apply(tmp_path, monkeypatch):
    from upgrade_workbench.service import engine as module
    from upgrade_workbench.service.recovery import version

    path = tmp_path / "task.json"
    path.write_text('{"status":"ready"}', encoding="utf-8")
    command = {"status": "queued", "request": {"action": "advance", "expected_version": version(path)}}
    engine = object.__new__(Engine)
    engine.settings = SimpleNamespace(job_root=lambda _job: tmp_path)
    engine.task = lambda _job: {"task_path": str(path), "status": "ready"}
    engine.hook = lambda *_args: None
    engine._result = Mock(side_effect=lambda _job, **kw: {"recovered": kw.get("recovered", False)})
    engine._apply = Mock(side_effect=lambda *_args: path.write_text('{"status":"pending_review"}', encoding="utf-8"))

    def start(_command, before):
        command.update(status="running", before_version=before)

    def finish(_command, status, result):
        if not result.get("recovered"):
            raise OperationalError("database stopped after side effect")
        command.update(status=status, result=result)

    engine.store = SimpleNamespace(command=lambda *_args: command, start=start, finish=finish)
    recover = Mock()
    monkeypatch.setattr(module, "recover_task", recover)
    monkeypatch.setattr(module, "clear_stale_core_lock", Mock())
    state = {"job_id": "job", "command_id": "command"}
    with pytest.raises(OperationalError):
        engine.execute(state)
    assert command["status"] == "running"
    engine.execute(state)
    assert command["status"] == "completed" and command["result"]["recovered"] is True
    engine._apply.assert_called_once()
    recover.assert_called_once_with(path, tmp_path, "job", adopt_dependency_report=False)


def test_once_polling_failure_returns_clear_error(monkeypatch, capsys):
    engine = object.__new__(Engine)
    engine.store = SimpleNamespace(runnable=Mock(side_effect=OperationalError("private DSN")))
    monkeypatch.setattr(cli.Settings, "load", Mock(return_value=SimpleNamespace(validate=lambda: None)))
    monkeypatch.setattr(cli, "Engine", Mock(return_value=engine))
    monkeypatch.setattr(cli.time, "sleep", Mock(side_effect=AssertionError("--once must not wait")))

    assert cli.main(["--config", "unused.json", "worker", "--once"]) == 2
    assert '"phase": "poll"' in capsys.readouterr().out
