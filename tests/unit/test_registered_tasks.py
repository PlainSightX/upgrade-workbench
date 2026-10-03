"""批次调度在付费前核对登记身份，不能换账本或跳过新补丁审阅。"""

import importlib.util
import json
from pathlib import Path

import pytest

from upgrade_workbench.batches import IDENTITY

SPEC = importlib.util.spec_from_file_location(
    "registered_driver", Path(__file__).parents[2] / "tools/dev/run_registered_tasks.py",
)
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def _task(tmp_path):
    path = tmp_path / "task.json"
    task = {key: key for key in IDENTITY}
    task.update(task_path=str(path), status="pending_review", attempts=[],
                candidate={"sha256": "candidate", "patch_path": "patch"})
    path.write_text(json.dumps(task), encoding="utf-8")
    return task, {key: task[key] for key in IDENTITY}


def test_modified_ledger_is_rejected_before_dispatch(tmp_path, monkeypatch):
    task, registered = _task(tmp_path)
    task["budget_path"] = "another-ledger.sqlite"
    Path(task["task_path"]).write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(driver, "advance_task", lambda *a, **kw: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="identity changed"):
        driver.advance_registered(registered, {})


def test_other_case_review_does_not_approve_same_patch(tmp_path, monkeypatch):
    task, registered = _task(tmp_path)
    monkeypatch.setattr(driver, "advance_task", lambda *a, **kw: pytest.fail("must not dispatch"))
    result = driver.advance_registered(registered, {"other-case:candidate": {"decision": "approved"}})
    assert result["status"] == "pending_review"


def test_changed_registration_is_rejected(tmp_path):
    path = tmp_path / "batch.json"
    protocol = {"max_calls": 10}
    registration = {"protocol": protocol, "protocol_sha256": driver.digest(protocol), "tasks": []}
    value = {"batch_path": str(path), "registration": registration,
             "registration_sha256": driver.digest(registration)}
    value["registration"]["protocol"]["max_calls"] = 20
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="protocol changed"):
        driver.registered_batch(path)


def test_legacy_runner_rejects_modern_task_before_dispatch(tmp_path, monkeypatch):
    task, registered = _task(tmp_path)
    task["schema_version"] = 5
    Path(task["task_path"]).write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(driver, "advance_task", lambda *a, **kw: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="schema 1/2 only"):
        driver.advance_registered(registered, {})


def test_legacy_runner_waits_at_diagnostic_review(tmp_path, monkeypatch):
    task, registered = _task(tmp_path)
    task["status"] = "pending_diagnostic_review"
    Path(task["task_path"]).write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(driver, "advance_task", lambda *a, **kw: pytest.fail("must not dispatch"))
    assert driver.advance_registered(registered, {})["status"] == "pending_diagnostic_review"


def test_legacy_runner_rejects_modern_registration_before_creating_tasks(tmp_path, monkeypatch):
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps({"protocol_revision": 4}), encoding="utf-8")
    monkeypatch.setattr(driver, "create_task", lambda *a, **kw: pytest.fail("must not create"))
    monkeypatch.setattr("sys.argv", ["runner", "register", "--protocol", str(protocol),
                                   "--batch", str(tmp_path / "batch.json"), "--budget", "budget",
                                   "--phase", "development", "--cases", "case", "--arms", "full",
                                   "--repeats", "1"])
    with pytest.raises(ValueError, match="protocol revision 2/3 only"):
        driver.main()
    assert not (tmp_path / "batch.json").exists()
