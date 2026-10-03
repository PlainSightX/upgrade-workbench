"""新命令只负责可检查输入输出；本文件不会访问真实密钥、网络或 Docker。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from upgrade_workbench import cli
from upgrade_workbench.generation import provider


@pytest.fixture(autouse=True)
def prevent_external_work(monkeypatch):
    sender = Mock(side_effect=AssertionError("Unexpected provider request"))
    process = Mock(side_effect=AssertionError("Unexpected subprocess execution"))
    monkeypatch.setattr(provider, "_send_once", sender)
    monkeypatch.setattr(subprocess, "run", process)
    monkeypatch.setattr(cli, "DockerExecutor", Mock(side_effect=AssertionError("Unexpected Docker")))
    yield
    sender.assert_not_called()
    process.assert_not_called()
    cli.DockerExecutor.assert_not_called()


def test_analyze_resolves_directory_and_emits_report(tmp_path: Path, monkeypatch, capsys) -> None:
    case = tmp_path / "case"
    case.mkdir()
    work = tmp_path / "work"
    analyze = Mock(return_value={"status": "analysis_completed", "version_evidence": None})
    monkeypatch.setattr(cli, "create_analysis", analyze)
    assert cli.main(["--work-root", str(work), "analyze", str(case)]) == 0
    analyze.assert_called_once_with(case / "manifest.json", work)
    assert json.loads(capsys.readouterr().out)["version_evidence"] is None


def test_propose_passes_explicit_model_budget_without_completion(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    manifest = tmp_path / "manifest.json"
    work = tmp_path / "work"
    prepare = Mock(return_value={"status": "request_prepared", "calls": 0})
    complete = Mock(side_effect=AssertionError("propose must not complete"))
    monkeypatch.setattr(cli, "prepare_case_proposal", prepare)
    monkeypatch.setattr(cli, "complete_request", complete)
    assert cli.main([
        "--work-root", str(work), "propose", str(manifest), "--model", "exact-model",
        "--endpoint", "https://provider.example/v1/chat/completions", "--max-output-tokens", "777",
        "--timeout", "29", "--thinking-mode", "disabled", "--public-source-ack",
    ]) == 0
    prepare.assert_called_once_with(
        manifest, work, model="exact-model", endpoint="https://provider.example/v1/chat/completions",
        max_output_tokens=777, timeout_seconds=29, public_source_ack=True, thinking_mode="disabled",
    )
    assert json.loads(capsys.readouterr().out) == {"status": "request_prepared", "calls": 0}
    complete.assert_not_called()


def test_completion_reads_only_named_environment_key_and_never_prints_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    receipt = tmp_path / "proposal.json"
    prepared = {"status": "request_prepared"}
    receipt.write_text(json.dumps(prepared), encoding="utf-8")
    getter = Mock(return_value="unit-test-only-secret")
    monkeypatch.setattr(cli, "os", SimpleNamespace(environ=SimpleNamespace(get=getter)))
    complete = Mock(return_value={"status": "pending_review", "calls": 1})
    monkeypatch.setattr(cli, "complete_request", complete)
    assert cli.main(["complete-proposal", str(receipt), "--api-key-env", "TEST_PROVIDER_KEY"]) == 0
    getter.assert_called_once_with("TEST_PROVIDER_KEY", "")
    complete.assert_called_once_with(prepared, api_key="unit-test-only-secret")
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "pending_review", "calls": 1}
    assert "unit-test-only-secret" not in output.out + output.err


@pytest.mark.parametrize("environment_name", ["BAD-NAME", "X=Y", "9KEY"])
def test_bad_env_name_is_rejected_before_reading_key(
    tmp_path: Path, monkeypatch, capsys, environment_name: str
) -> None:
    getter = Mock(side_effect=AssertionError("Invalid environment variable must not be read"))
    monkeypatch.setattr(cli, "os", SimpleNamespace(environ=SimpleNamespace(get=getter)))
    assert cli.main([
        "complete-proposal", str(tmp_path / "missing.json"), "--api-key-env", environment_name,
    ]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_request"
    getter.assert_not_called()


def test_missing_env_value_fails_without_fallback_or_network(tmp_path, monkeypatch, capsys) -> None:
    receipt = tmp_path / "proposal.json"
    receipt.write_text("{}", encoding="utf-8")
    getter = Mock(return_value="")
    monkeypatch.setattr(cli, "os", SimpleNamespace(environ=SimpleNamespace(get=getter)))
    assert cli.main(["complete-proposal", str(receipt), "--api-key-env", "ABSENT_KEY"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_request"
    getter.assert_called_once_with("ABSENT_KEY", "")


@pytest.mark.parametrize("contents", ["{bad json", "[]", "null", "{}", "partial_receipt"])
def test_bad_receipts_are_controlled_failures(tmp_path, monkeypatch, capsys, contents) -> None:
    receipt = tmp_path / "proposal.json"
    if contents == "partial_receipt":
        contents = json.dumps({"report_path": str(receipt), "status": "request_prepared"})
    receipt.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(
        cli, "os", SimpleNamespace(environ={"TEST_KEY": "unit-test-only-secret"})
    )
    assert cli.main(["complete-proposal", str(receipt), "--api-key-env", "TEST_KEY"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "invalid_request"
    assert "unit-test-only-secret" not in output.out + output.err
