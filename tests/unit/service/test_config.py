"""服务布局预检只验证新作业的本地边界，不创建任务或连接模型。"""

from contextlib import contextmanager
from pathlib import Path

import pytest

from upgrade_workbench.cases import load_case
from upgrade_workbench.cases.manifest import CaseValidationError
from upgrade_workbench.service import config
from upgrade_workbench.service.config import (
    ServiceLayoutPreflightError,
    Settings,
    preflight_layout,
)
from upgrade_workbench.service.engine import Engine

JOB_ID = "a" * 32
CASE = Path(__file__).resolve().parents[3] / "cases/bump-my-version-0.5.0-r2/manifest.json"


def test_settings_validate_parses_registered_version_evidence(tmp_path, monkeypatch):
    budget = tmp_path / "budget.sqlite"
    budget.write_bytes(b"")
    case_id = load_case(CASE).manifest.case_id
    settings = Settings(
        work_root=tmp_path / "work",
        budget=budget,
        database_url="postgresql://user:password@localhost/database",
        token="t" * 32,
        cases={case_id: CASE},
        profiles={"main": {}},
    )
    checked = []

    def reject(case):
        checked.append(case.manifest.case_id)
        raise CaseValidationError("invalid registered version evidence")

    monkeypatch.setattr(config, "load_version_evidence", reject)

    with pytest.raises(CaseValidationError, match="invalid registered version evidence"):
        settings.validate()
    assert checked == [case_id]
    assert not settings.work_root.exists()


def test_settings_validate_parses_registered_evaluation(tmp_path, monkeypatch):
    budget = tmp_path / "budget.sqlite"
    budget.write_bytes(b"")
    case_id = load_case(CASE).manifest.case_id
    settings = Settings(
        work_root=tmp_path / "work",
        budget=budget,
        database_url="postgresql://user:password@localhost/database",
        token="t" * 32,
        cases={case_id: CASE},
        profiles={"main": {}},
    )
    checked = []

    def reject(case):
        checked.append(case.manifest.case_id)
        raise CaseValidationError("invalid registered evaluation")

    monkeypatch.setattr(config, "load_evaluation", reject)

    with pytest.raises(CaseValidationError, match="invalid registered evaluation"):
        settings.validate()
    assert checked == [case_id]
    assert not settings.work_root.exists()


def test_preflight_covers_parallel_proposal_and_diagnostic_layouts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "_probe_git_layout", lambda parent, length: calls.append((parent, length)))
    result = preflight_layout(tmp_path / "work", JOB_ID, windows_limit=10_000)

    assert Path(result["job_root"]) == (tmp_path / "work" / "jobs" / JOB_ID).absolute()
    assert "proposal_git_cwd" in result["checked_paths"]
    assert "task_diagnostic_git_cwd" in result["checked_paths"]
    assert "task_comparison_git_cwd" in result["checked_paths"]
    assert "task_candidate_git_cwd" in result["checked_paths"]
    assert "task_seed_input_git_cwd" in result["checked_paths"]
    assert result["max_path_length"] < 10_000
    assert not (tmp_path / "work" / "jobs").exists()
    assert calls == [(tmp_path, result["max_path_length"])]
    assert result["git_materialization_verified"]


def test_preflight_rejects_path_budget_before_task_layout(tmp_path):
    with pytest.raises(ServiceLayoutPreflightError) as caught:
        preflight_layout(tmp_path / "work", JOB_ID, windows_limit=1, enforce_windows_limit=True)

    assert caught.value.code == "windows_path_too_long"
    assert str(caught.value) == "service_layout_preflight_failed"
    assert not (tmp_path / "work" / "jobs").exists()


def test_preflight_rejects_missing_git_before_task_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda _name: None)

    with pytest.raises(ServiceLayoutPreflightError) as caught:
        preflight_layout(tmp_path / "work", JOB_ID, windows_limit=10_000)

    assert caught.value.code == "git_unavailable"
    assert not (tmp_path / "work" / "jobs").exists()


def test_initialize_preflight_failure_does_not_create_or_bind_task(tmp_path, monkeypatch):
    class RejectingSettings:
        database_url = "postgresql://unused"
        budget = tmp_path / "budget.sqlite"

        def job_root(self, job_id):
            return tmp_path / "work" / "jobs" / job_id

        def preflight_layout(self, _job_id):
            raise ServiceLayoutPreflightError("windows_path_too_long")

    class FakeStore:
        bound = False

        def job(self, _job_id):
            case = load_case(CASE)
            return {"task_path": None, "request": {"manifest_path": str(CASE),
                    "case_fingerprint": case.fingerprint, "profile_settings": {}}}

        def bind_task(self, *_args):
            self.bound = True

    engine = Engine(RejectingSettings(), transport=lambda *_a, **_k: pytest.fail("transport must not run"))
    engine.store = FakeStore()
    monkeypatch.setattr("upgrade_workbench.service.engine.create_operation",
                        lambda *_a, **_k: pytest.fail("task must not be created"))

    with pytest.raises(ServiceLayoutPreflightError) as caught:
        engine.initialize({"job_id": JOB_ID})

    assert caught.value.code == "windows_path_too_long"
    assert engine.store.bound is False


def test_advance_uses_frozen_provider_transport_route(tmp_path, monkeypatch):
    events = []
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    class RouteSettings:
        database_url = "postgresql://unused"
        budget = tmp_path / "budget.sqlite"

    class FakeStore:
        def job(self, job_id):
            assert job_id == JOB_ID
            return {
                "request": {
                    "profile_settings": {
                        "provider_transport_route": "process_direct",
                    }
                }
            }

    @contextmanager
    def route(endpoint, selected):
        events.append(("enter", endpoint, selected))
        yield
        events.append(("exit", endpoint, selected))

    def advance(path, **options):
        events.append(("advance", path, options["api_key"]))

    engine = Engine(RouteSettings(), transport=lambda *_a, **_k: b"")
    engine.store = FakeStore()
    monkeypatch.setattr("upgrade_workbench.service.engine.provider_transport_route", route)
    monkeypatch.setattr("upgrade_workbench.service.engine.advance_task", advance)

    task_path = tmp_path / "task.json"
    task = {
        "task_path": str(task_path),
        "status": "ready",
        "seed_strategy": "none",
        "protocol": {
            "generation": {
                "endpoint": "https://api.deepseek.com/chat/completions",
            }
        },
    }
    engine._apply(task, {"action": "advance", "reviewed_tool": False}, JOB_ID)

    assert events == [
        ("enter", "https://api.deepseek.com/chat/completions", "process_direct"),
        ("advance", task_path, "offline-injected-transport"),
        ("exit", "https://api.deepseek.com/chat/completions", "process_direct"),
    ]


def test_preflight_rejects_git_found_but_unexecutable(tmp_path, monkeypatch):
    from upgrade_workbench.cases import PatchInfrastructureError

    calls = []
    def denied(*_args, **_kwargs):
        calls.append(True)
        raise PatchInfrastructureError("controlled Git launch failure")

    monkeypatch.setattr(config, "_git", denied)
    with pytest.raises(ServiceLayoutPreflightError) as caught:
        preflight_layout(tmp_path / "work", JOB_ID, windows_limit=10_000)
    assert caught.value.code == "git_unusable"
    assert calls == [True]
    assert not (tmp_path / "work" / "jobs").exists()


def test_git_preflight_really_initializes_checks_applies_and_cleans(tmp_path):
    # 长度位于本机Windows cwd边界内，Git实际执行；不加载案例或启动目标。
    before = set(tmp_path.iterdir())
    config._probe_git_layout(tmp_path, max(230, len(str(tmp_path)) + 60))
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("operation", ["launch", "apply"])
def test_git_preflight_classifies_real_process_boundary_failures(tmp_path, monkeypatch, operation):
    import subprocess

    from upgrade_workbench.cases import patches

    original = patches.subprocess.run
    calls = []

    def broken(args, **kwargs):
        calls.append(args)
        if operation == "launch":
            raise PermissionError("controlled process denial")
        if "apply" in args:
            return subprocess.CompletedProcess(args, 128, stdout=b"", stderr=b"controlled apply I/O failure")
        return original(args, **kwargs)

    monkeypatch.setattr(patches.subprocess, "run", broken)
    with pytest.raises(ServiceLayoutPreflightError) as caught:
        config._probe_git_layout(tmp_path, max(230, len(str(tmp_path)) + 60))
    assert caught.value.code == "git_unusable"
    assert calls
    assert list(tmp_path.iterdir()) == []
