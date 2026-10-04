"""新配置准备不借用本机旧身份；冲突及无效输入在写入前被拒绝。"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.service import bootstrap
from upgrade_workbench.service.bootstrap import prepare_config
from upgrade_workbench.service.config import ServiceLayoutPreflightError, Settings

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "cases/csvkit-csvsql-sqlalchemy-2-a3/manifest.json"


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    budget = tmp_path / "budget.sqlite3"
    BudgetLedger(budget, {
        "mode": "user_managed", "limit_usd": None, "model": "test-model",
        "input_per_million": "1", "output_per_million": "1",
        "pricing_source": "https://example.test/prices", "pricing_checked_at": "2026-10-02",
    })
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "protocol_revision": 4, "max_calls": 3, "seed_strategy": "none",
        "generation": {"model": "test-model", "endpoint": "https://example.test/completions",
                       "thinking_mode": "disabled", "max_output_tokens": 100,
                       "timeout_seconds": 10},
    }), encoding="utf-8")
    # 路径实测由独立布局测试和wheel真实准备覆盖；这里单独隔离身份写入语义。
    monkeypatch.setattr(Settings, "preflight_layout", lambda *a: {"ok": True})
    return dict(config=tmp_path / "config.json", work_root=tmp_path / "work",
                budget=budget, case=CASE, profile=profile,
                database_url="postgresql://test:private@127.0.0.1:65432/new_database")


def test_prepare_then_reuse_preserves_exact_config_and_budget(inputs):
    before = inputs["budget"].read_bytes()
    created = prepare_config(**inputs)
    config = inputs["config"].read_bytes()
    reused = prepare_config(**inputs)
    assert created["status"] == "config_prepared"
    assert reused["status"] == "existing_config_reused"
    assert inputs["config"].read_bytes() == config
    assert inputs["budget"].read_bytes() == before
    assert "private" not in json.dumps(created)
    assert len(Settings.load(inputs["config"]).token) >= 32


@pytest.mark.parametrize("field,value", [
    ("database_url", "postgresql://test:other@127.0.0.1/new_database"),
    ("work_root", Path("other-work")),
])
def test_conflict_has_no_identity_or_work_root_writes(inputs, field, value):
    prepare_config(**inputs)
    before = inputs["config"].read_bytes()
    changed = inputs | {field: value}
    with pytest.raises(ValueError, match="existing_service_config_identity_conflict"):
        prepare_config(**changed)
    assert inputs["config"].read_bytes() == before
    if field == "work_root":
        assert not value.exists()


def test_missing_budget_does_not_create_config_or_work_root(inputs):
    inputs["budget"].unlink()
    with pytest.raises(ValueError, match="reviewed_budget_required"):
        prepare_config(**inputs)
    assert not inputs["config"].exists()
    assert not inputs["work_root"].exists()


def test_invalid_profile_does_not_create_identity(inputs):
    inputs["profile"].write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid_service_profile"):
        prepare_config(**inputs)
    assert not inputs["config"].exists()


@pytest.mark.parametrize("change", ["incomplete_generation", "call_limit", "wrong_seed", "execution_scope"])
def test_unusable_task_profile_is_rejected_without_writes(inputs, change):
    before = inputs["budget"].read_bytes()
    profile = json.loads(inputs["profile"].read_text(encoding="utf-8"))
    if change == "incomplete_generation":
        profile["generation"] = {"model": "test-model"}
    elif change == "call_limit":
        profile["max_calls"] = 31
    elif change == "wrong_seed":
        inputs["case"] = ROOT / "cases/csvkit-csvsql-sqlalchemy-2-a3/manifest.json"
        profile.pop("seed_strategy")
    else:
        profile["execution"] = {"check_group": "feedback"}
    inputs["profile"].write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid_service_profile"):
        prepare_config(**inputs)
    assert not inputs["config"].exists()
    assert not inputs["work_root"].exists()
    assert inputs["budget"].read_bytes() == before


def test_profile_cannot_override_service_owned_task_identity(inputs):
    profile = json.loads(inputs["profile"].read_text(encoding="utf-8"))
    profile["budget_path"] = "some-other-budget.sqlite3"
    inputs["profile"].write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid_service_profile"):
        prepare_config(**inputs)
    assert not inputs["config"].exists()


def test_bad_windows_layout_precedes_config_write(inputs, monkeypatch):
    def fail(*args):
        raise ServiceLayoutPreflightError("windows_path_too_long")
    monkeypatch.setattr(Settings, "preflight_layout", fail)
    with pytest.raises(ServiceLayoutPreflightError):
        prepare_config(**inputs)
    assert not inputs["config"].exists()
    assert not inputs["work_root"].exists()


def test_invalid_database_credentials_precede_identity(inputs):
    with pytest.raises(ValueError, match="invalid_service_credentials"):
        prepare_config(**(inputs | {"database_url": "invalid"}))
    assert not inputs["config"].exists()
    assert not inputs["work_root"].exists()


@pytest.mark.parametrize("field", ["config", "work_root", "budget"])
def test_prepare_cannot_write_into_frozen_case(inputs, field, monkeypatch):
    import upgrade_workbench.cases as cases

    loaded = cases.load_case(CASE)
    from types import SimpleNamespace
    # 使用可控输入区，不更改真实冻结案例；每次只改变一个写入路径。
    fake_root = inputs["budget"].parent if field == "budget" else inputs["config"].parent / "frozen"
    if field != "budget":
        inputs[field] = fake_root / "output"
    monkeypatch.setattr(cases, "load_case", lambda _: SimpleNamespace(
        root=fake_root, manifest=loaded.manifest,
    ))
    with pytest.raises(ValueError, match="service_output_must_be_outside_frozen_case"):
        prepare_config(**inputs)
    assert not inputs["config"].exists()


def test_normalized_drive_root_is_rejected_before_creating_files(inputs):
    anchor = Path(inputs["work_root"].anchor)
    disguised = anchor / "uncreated-folder" / ".."
    with pytest.raises(ValueError, match="service_work_root_must_not_be_drive_root"):
        prepare_config(**(inputs | {"work_root": disguised}))
    assert not inputs["config"].exists()


def test_interrupted_publication_can_retry_without_partial_identity(inputs, monkeypatch):
    def interrupted_dump(data, stream, **kwargs):
        stream.write('{"token":')
        raise OSError("injected_config_write_failure")

    with monkeypatch.context() as patch:
        patch.setattr(bootstrap.json, "dump", interrupted_dump)
        with pytest.raises(OSError, match="injected_config_write_failure"):
            prepare_config(**inputs)
    assert not inputs["config"].exists()
    assert not list(inputs["config"].parent.glob(".uw-config-*.partial"))
    assert prepare_config(**inputs)["status"] == "config_prepared"
    assert len(Settings.load(inputs["config"]).token) >= 32


@pytest.mark.parametrize("operation", ["fsync", "link"])
def test_publication_io_failure_leaves_no_formal_identity(inputs, monkeypatch, operation):
    def fail(*args):
        raise OSError("injected_" + operation + "_failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, operation, fail)
        with pytest.raises(OSError, match="injected_" + operation + "_failure"):
            prepare_config(**inputs)
    assert not inputs["config"].exists()
    assert not list(inputs["config"].parent.glob(".uw-config-*.partial"))
    assert prepare_config(**inputs)["status"] == "config_prepared"


@pytest.mark.parametrize("conflict", [False, True])
def test_concurrent_publication_preserves_one_complete_winner(inputs, monkeypatch, conflict):
    # 两个调用都完整写好后再竞争发布，测试真实文件系统的无覆盖语义。
    rendezvous = Barrier(2)
    link = os.link

    def concurrent_link(source, target):
        rendezvous.wait(timeout=10)
        return link(source, target)

    monkeypatch.setattr(os, "link", concurrent_link)
    other = dict(inputs)
    if conflict:
        other["database_url"] = "postgresql://test:other@127.0.0.1:65432/new_database"

    def run(arguments):
        try:
            return prepare_config(**arguments)["status"]
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(run, [inputs, other]))
    assert outcomes.count("config_prepared") == 1
    expected = "existing_service_config_identity_conflict" if conflict else "existing_config_reused"
    assert outcomes.count(expected) == 1
    winner = Settings.load(inputs["config"])
    assert len(winner.token) >= 32
    assert winner.database_url in {inputs["database_url"], other["database_url"]}
    assert not list(inputs["config"].parent.glob(".uw-config-*.partial"))


def test_invalid_existing_config_is_preserved_without_replacement(inputs):
    broken = b'{"token":'
    inputs["config"].write_bytes(broken)
    with pytest.raises(ValueError, match="invalid_existing_service_config"):
        prepare_config(**inputs)
    assert inputs["config"].read_bytes() == broken
    assert not list(inputs["config"].parent.glob(".uw-config-*.partial"))


@pytest.mark.parametrize("cases", [None, [], "invalid"])
def test_invalid_existing_config_structure_preserves_identity(inputs, cases):
    prepare_config(**inputs)
    malformed = json.loads(inputs["config"].read_bytes())
    malformed["cases"] = cases
    before = json.dumps(malformed).encode("utf-8")
    inputs["config"].write_bytes(before)
    with pytest.raises(ValueError, match="invalid_existing_service_config"):
        prepare_config(**inputs)
    assert inputs["config"].read_bytes() == before


def test_process_exit_during_write_does_not_publish_identity(inputs):
    # 子进程直接退出，不会执行finally；残留临时文件不作为正式配置消费。
    program = """
import json, os, sys
from pathlib import Path
from upgrade_workbench.service.bootstrap import prepare_config
from upgrade_workbench.service.config import Settings
arguments = json.loads(sys.argv[1])
for name in ("config", "work_root", "budget", "case", "profile"):
    arguments[name] = Path(arguments[name])
Settings.preflight_layout = lambda *args: {"ok": True}
def interrupted_dump(data, stream, **kwargs):
    stream.write('{"token":')
    stream.flush()
    os._exit(37)
json.dump = interrupted_dump
prepare_config(**arguments)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", program, json.dumps(inputs, default=str)],
        cwd=ROOT, capture_output=True, timeout=30,
    )
    assert result.returncode == 37
    assert not inputs["config"].exists()
    leftovers = list(inputs["config"].parent.glob(".uw-config-*.partial"))
    assert len(leftovers) == 1
    assert prepare_config(**inputs)["status"] == "config_prepared"
    assert len(Settings.load(inputs["config"]).token) >= 32
    for temporary in leftovers:
        temporary.unlink()
