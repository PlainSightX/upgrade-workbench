"""验证命令行协议与失败边界；所有案例执行和 Docker 操作都使用替身。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from upgrade_workbench import cli
from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.cases import CaseValidationError


@pytest.fixture(autouse=True)
def prevent_external_work(monkeypatch):
    for name in ("load_case", "run_comparison", "DockerExecutor"):
        monkeypatch.setattr(cli, name, Mock(side_effect=AssertionError(f"Unexpected {name} call")))
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("Unexpected subprocess execution"))
    )


@pytest.mark.parametrize(
    "task_status,result_status,evaluation,expected_code",
    [
        ("submitted", "candidate_verified", "completed", 0),
        ("submitted", "candidate_not_accepted", "completed", 2),
        ("no_change_claimed", "no_source_change_needed_for_registered_checks", "completed", 0),
        ("no_change_claimed", "no_change_claim_not_accepted", "completed", 2),
        ("submitted", "execution_incomplete", "completed", 2),
        ("submitted", "candidate_verified", "running", 2),
        ("submitted", None, "not_run", 2),
        ("submitted", "unknown_future_status", "completed", 2),
    ],
)
def test_finalize_acceptance_maps_independent_result(
    monkeypatch, capsys, task_status, result_status, evaluation, expected_code,
):
    state = {"task_id": "offline", "task_path": "task.json", "case_id": "offline",
             "status": task_status, "attempts": [], "final_evaluation": evaluation,
             "final_result": {"status": result_status} if result_status else None}
    finalize = Mock(return_value=state)
    monkeypatch.setattr(cli, "finalize_task", finalize)

    assert cli.main(["task-finalize", "task.json"]) == expected_code
    assert json.loads(capsys.readouterr().out)["final_result"] == state["final_result"]
    finalize.assert_called_once_with(Path("task.json"))


@pytest.mark.parametrize("action", ["task-status", "task-export"])
@pytest.mark.parametrize("task_status", ["submitted", "no_candidate", "outcome_unknown"])
def test_read_and_export_are_operations_even_when_candidate_failed(
    tmp_path, monkeypatch, capsys, action, task_status,
):
    state = {"task_id": "offline", "task_path": "task.json", "case_id": "offline",
             "status": task_status, "attempts": [], "final_evaluation": "completed",
             "final_result": {"status": "candidate_not_accepted"}}
    monkeypatch.setattr(cli, "inspect_task", Mock(return_value=state))
    exported = {"result": "candidate_not_accepted", "json_path": str(tmp_path / "report.json")}
    monkeypatch.setattr(cli, "export_task_report", Mock(return_value=exported))
    argv = [action, "task.json"]
    if action == "task-export":
        argv += ["--output", str(tmp_path)]

    assert cli.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == exported if action == "task-export" else report["final_result"] == state["final_result"]


@pytest.mark.parametrize("input_kind", ["directory", "manifest_file"])
def test_inspect_resolves_manifest_without_executing_target(
    tmp_path, monkeypatch, capsys, input_kind
):
    case_dir = tmp_path / "case with spaces"
    case_dir.mkdir()
    manifest_path = case_dir / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    source = {"project": "synthetic", "version": "1.0"}
    loaded = SimpleNamespace(
        fingerprint="a" * 64,
        manifest=SimpleNamespace(
            case_id="synthetic-inspect",
            title="只读核验案例",
            source=SimpleNamespace(model_dump=lambda: source),
            allowed_changes=["source/example.py"],
            file_hashes={"source/example.py": "b" * 64, "checks/test_example.py": "c" * 64},
        ),
    )
    load = Mock(return_value=loaded)
    monkeypatch.setattr(cli, "load_case", load)
    supplied = case_dir if input_kind == "directory" else manifest_path

    code = cli.main(["inspect", str(supplied)])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert "只读核验案例" in captured.out
    assert json.loads(captured.out) == {
        "case_id": "synthetic-inspect",
        "title": "只读核验案例",
        "fingerprint": "a" * 64,
        "source": source,
        "allowed_changes": ["source/example.py"],
        "verified_files": 2,
        "target_code_executed": False,
    }
    load.assert_called_once_with(manifest_path)
    cli.run_comparison.assert_not_called()
    cli.DockerExecutor.assert_not_called()
    subprocess.run.assert_not_called()


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("calibration_passed", 0),
        ("candidate_verified", 0),
        ("comparison_only", 0),
        ("no_regression_observed", 0),
        ("baseline_invalid", 2),
        ("execution_incomplete", 2),
        ("test_set_changed", 2),
        ("candidate_not_accepted", 2),
        ("blocked_environment", 2),
        ("rejected_input", 2),
        ("execution_error", 2),
        ("interrupted", 2),
        ("running", 2),
        ("unknown_future_status", 2),
    ],
)
def test_verify_preserves_json_report_and_maps_status_to_exit_code(
    tmp_path, monkeypatch, capsys, status, expected_code
):
    manifest_path = tmp_path / "manifest.json"
    report = {"status": status, "reason": "保留原始判定", "stages": {"old_original": {}}}
    compare = Mock(return_value=report)
    monkeypatch.setattr(cli, "run_comparison", compare)

    code = cli.main(["verify", str(manifest_path)])

    captured = capsys.readouterr()
    assert code == expected_code
    assert captured.err == ""
    assert json.loads(captured.out) == report
    compare.assert_called_once_with(
        manifest_path,
        Path(".local"),
        candidate_patch=None,
        candidate_origin=None,
        prepare_timeout=600,
        test_timeout=60,
    )
    cli.load_case.assert_not_called()
    cli.DockerExecutor.assert_not_called()


@pytest.mark.parametrize("origin", ["calibration_reference", "user_candidate", "agent_candidate", "official_tool"])
def test_verify_forwards_candidate_origin_work_root_and_timeouts(
    tmp_path, monkeypatch, capsys, origin
):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    work_root = tmp_path / "isolated work"
    candidate = tmp_path / "candidate patch.diff"
    compare = Mock(return_value={"status": "candidate_verified"})
    monkeypatch.setattr(cli, "run_comparison", compare)

    code = cli.main(
        [
            "--work-root",
            str(work_root),
            "verify",
            str(case_dir),
            "--candidate",
            str(candidate),
            "--candidate-origin",
            origin,
            "--prepare-timeout",
            "123",
            "--test-timeout",
            "17",
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "candidate_verified"}
    compare.assert_called_once_with(
        case_dir / "manifest.json",
        work_root,
        candidate_patch=candidate,
        candidate_origin=origin,
        prepare_timeout=123,
        test_timeout=17,
    )


def test_verify_forwards_an_explicit_base_image(tmp_path, monkeypatch, capsys):
    manifest_path = tmp_path / "manifest.json"
    base_image = "python:3.12-slim@sha256:" + "a" * 64
    compare = Mock(return_value={"status": "comparison_only"})
    monkeypatch.setattr(cli, "run_comparison", compare)

    code = cli.main(["verify", str(manifest_path), "--base-image", base_image])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "comparison_only"}
    compare.assert_called_once_with(
        manifest_path,
        Path(".local"),
        candidate_patch=None,
        candidate_origin=None,
        prepare_timeout=600,
        test_timeout=60,
        base_image=base_image,
    )


@pytest.mark.parametrize("action", ["inspect", "verify"])
@pytest.mark.parametrize("error_type", [CaseValidationError, OSError, ValueError])
def test_request_errors_are_reported_as_json(tmp_path, monkeypatch, capsys, action, error_type):
    failing_call = Mock(side_effect=error_type("invalid synthetic input"))
    monkeypatch.setattr(cli, "load_case" if action == "inspect" else "run_comparison", failing_call)

    code = cli.main([action, str(tmp_path / "missing.json")])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "status": "invalid_request",
        "reason": "invalid synthetic input",
    }
    failing_call.assert_called_once()


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["inspect"],
        ["verify"],
        ["unknown-action"],
        ["verify", "manifest.json", "--candidate-origin", "unknown-origin"],
        ["verify", "manifest.json", "--prepare-timeout", "not-an-integer"],
        ["verify", "manifest.json", "--test-timeout", "1.5"],
        ["verify", "manifest.json", "--unknown-option"],
    ],
)
def test_malformed_cli_syntax_exits_before_loading_or_executing(capsys, arguments):
    with pytest.raises(SystemExit) as caught:
        cli.main(arguments)

    captured = capsys.readouterr()
    assert caught.value.code == 2
    assert captured.out == ""
    assert "usage:" in captured.err
    assert "error:" in captured.err
    cli.load_case.assert_not_called()
    cli.run_comparison.assert_not_called()
    cli.DockerExecutor.assert_not_called()
    subprocess.run.assert_not_called()


@pytest.mark.parametrize(
    ("docker_ready", "git_available", "expected_code"),
    [(True, True, 0), (False, True, 2), (True, False, 2), (False, False, 2)],
)
def test_doctor_reports_environment_readiness_without_starting_execution(
    tmp_path, monkeypatch, capsys, docker_ready, git_available, expected_code
):
    work_root = tmp_path / "diagnostics"
    docker_report = {
        "ready": docker_ready,
        "reason": "" if docker_ready else "Docker engine unavailable",
    }
    probe = Mock(return_value=docker_report)
    executor = Mock(return_value=SimpleNamespace(probe=probe))
    find_git = Mock(return_value="git" if git_available else None)
    monkeypatch.setattr(cli, "DockerExecutor", executor)
    monkeypatch.setattr(cli.shutil, "which", find_git)

    code = cli.main(["--work-root", str(work_root), "doctor"])

    captured = capsys.readouterr()
    assert code == expected_code
    assert captured.err == ""
    assert json.loads(captured.out) == {"docker": docker_report, "git_available": git_available}
    executor.assert_called_once_with(work_root / "executor")
    probe.assert_called_once_with()
    find_git.assert_called_once_with("git")
    cli.load_case.assert_not_called()
    cli.run_comparison.assert_not_called()
    subprocess.run.assert_not_called()


def test_doctor_filesystem_error_is_reported_as_invalid_request(monkeypatch, capsys):
    executor = Mock(side_effect=OSError("Cannot create executor directory"))
    monkeypatch.setattr(cli, "DockerExecutor", executor)

    code = cli.main(["doctor"])

    assert code == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "invalid_request",
        "reason": "Cannot create executor directory",
    }
    executor.assert_called_once_with(Path(".local") / "executor")


@pytest.fixture
def budget_ledger(tmp_path):
    return BudgetLedger(tmp_path / "budget.sqlite", {
        "limit_usd": "200", "checkpoint_usd": "100", "calibration_usd": "20",
        "model": "fixed-model", "input_per_million": "0.30", "output_per_million": "1.20",
        "pricing_source": "https://example.test/prices", "pricing_checked_at": "2026-09-16",
        "quotas": {"calibration": 50, "development": 300, "holdout": 150},
        "holdout_reserve_usd": "25",
    })


def test_budget_amend_cli_requires_current_review_and_preserves_every_other_limit(budget_ledger, capsys):
    previous = budget_ledger.spec
    old_hash = budget_ledger.snapshot()["configuration_sha256"]
    arguments = [
        "budget-amend-holdout", str(budget_ledger.path), "--reserve-usd", "35",
        "--expected-sha256", old_hash, "--reason", "为32k输出保留足够留出集费用",
    ]
    assert cli.main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "holdout_reserve_amended"
    assert report["amendment"]["old_configuration_sha256"] == old_hash
    assert report["amendment"]["new_configuration_sha256"] == report["budget"]["configuration_sha256"]
    expected = dict(previous, holdout_reserve_usd="35")
    assert BudgetLedger(budget_ledger.path).spec == expected
    assert report["budget"]["calls"] == 0
    assert report["budget"]["amendments"] == 1
    assert cli.main(arguments) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_request"
    assert BudgetLedger(budget_ledger.path).snapshot()["amendments"] == 1


@pytest.mark.parametrize("amount", ["25", "20", "200", "NaN", "not-money"])
def test_budget_amend_cli_invalid_reserve_is_structured_and_nonmutating(budget_ledger, capsys, amount):
    before = budget_ledger.snapshot()
    assert cli.main([
        "budget-amend-holdout", str(budget_ledger.path), "--reserve-usd", amount,
        "--expected-sha256", before["configuration_sha256"], "--reason", "explicit review",
    ]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_request"
    assert BudgetLedger(budget_ledger.path).snapshot() == before


def test_budget_amend_cli_cannot_change_the_global_budget(budget_ledger, capsys):
    before = budget_ledger.snapshot()
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "budget-amend-holdout", str(budget_ledger.path), "--reserve-usd", "35",
            "--expected-sha256", before["configuration_sha256"], "--reason", "explicit review",
            "--limit-usd", "300",
        ])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert BudgetLedger(budget_ledger.path).snapshot() == before
