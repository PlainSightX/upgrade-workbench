"""用自有冻结案例与假执行器验证三路比较归因，不执行目标源码。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_evaluation import InspectionExecutor
from test_evaluation import evaluated_case as evaluated_case

from upgrade_workbench.cases import load_case
from upgrade_workbench.execution import DockerUnavailable
from upgrade_workbench.workflow import run_comparison

NODEIDS = ["test_owned.py::test_first", "test_owned.py::test_second"]
ORIGINAL = b"VALUE = 1\n"
PATCH = "--- a/owned.py\n+++ b/owned.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"


def measured(
    *,
    status: str = "passed",
    passed: int = 2,
    failed: int = 0,
    errors: int = 0,
    skipped: int = 0,
    nodeids: list[str] | None = None,
    exit_code: int | None = None,
) -> dict:
    identities = list(NODEIDS if nodeids is None else nodeids)
    return {
        "status": status,
        "exit_code": (0 if status == "passed" else 1) if exit_code is None else exit_code,
        "tests": {
            "collected": len(identities),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
        },
        "nodeids": identities,
        "reason": "Owned executor fixture result",
    }


class FakeExecutor:
    """仅返回预置测量值，同时记录工作流实际传入的冻结路径。"""

    def __init__(self, results: list[dict], *, prepare_error=None, on_verify=None):
        self.results = copy.deepcopy(results)
        self.prepared = []
        self.verified = []
        self.prepare_error = prepare_error
        self.on_verify = on_verify

    def prepare_environment(self, lock_path: Path, *, timeout_seconds: int) -> dict:
        self.prepared.append((lock_path, timeout_seconds, lock_path.read_bytes()))
        if self.prepare_error is not None:
            raise self.prepare_error
        return {
            "image_id": "sha256:" + str(len(self.prepared)) * 64,
            "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        }

    def verify(
        self, image_id: str, source_dir: Path, checks_dir: Path, *, timeout_seconds: int
    ) -> dict:
        self.verified.append(
            {
                "image_id": image_id,
                "source_dir": source_dir,
                "checks_dir": checks_dir,
                "timeout_seconds": timeout_seconds,
                "source_bytes": (source_dir / "owned.py").read_bytes(),
            }
        )
        if self.on_verify is not None:
            self.on_verify(self, source_dir, checks_dir)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def owned_case(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "owned-case"
    files = {
        "source/owned.py": ORIGINAL,
        "source/unchanged.py": b"UNCHANGED = True\n",
        "checks/test_owned.py": b"def test_first():\n    assert True\n\ndef test_second():\n    assert True\n",
        "requirements/old.txt": b"pytest==8.4.2 --hash=sha256:" + b"a" * 64 + b"\n",
        "requirements/new.txt": b"pytest==8.4.2 --hash=sha256:" + b"b" * 64 + b"\n",
        "SOURCE.json": b'{"kind":"synthetic_calibration","owned":true}\n',
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "schema_version": 1,
        "case_id": "owned-workflow",
        "title": "Owned workflow fixture",
        "source": {
            "kind": "synthetic_calibration",
            "repository": "owned-fixture",
            "revision": "v1",
            "license": "MIT",
            "description": "Does not execute any third-party project",
        },
        "review": {"status": "reviewed", "note": "Fixture only"},
        "snapshot_dir": "source",
        "checks_dir": "checks",
        "old_lock": "requirements/old.txt",
        "new_lock": "requirements/new.txt",
        "allowed_changes": ["owned.py"],
        "file_hashes": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "expected_new_original": "failed",
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    patch_path = tmp_path / "owned.patch"
    patch_path.write_bytes(PATCH.encode())
    return manifest_path, patch_path


def stored_report(report: dict) -> dict:
    path = Path(report["report_path"])
    assert path.is_file()
    assert path.with_suffix(".md").is_file()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == report
    assert "current_action" not in saved
    assert saved["duration_seconds"] >= 0
    assert not path.with_name("report.tmp").exists()
    return saved


def run_owned(owned_case, tmp_path, runner, *, origin=None, **kwargs):
    manifest, patch = owned_case
    return run_comparison(
        manifest,
        tmp_path / "runs",
        executor=runner,
        candidate_patch=patch if origin is not None else None,
        candidate_origin=origin,
        **kwargs,
    )


def test_explicit_base_image_is_forwarded_to_both_environment_preparations(owned_case, tmp_path):
    base_image = "python:3.12-slim@sha256:" + "a" * 64
    selected_images = []

    class ExplicitImageExecutor(FakeExecutor):
        def prepare_environment(self, lock_path: Path, *, timeout_seconds: int, base_image: str):
            selected_images.append(base_image)
            return super().prepare_environment(lock_path, timeout_seconds=timeout_seconds)

    runner = ExplicitImageExecutor([measured(), measured(status="failed", passed=1, failed=1)])
    report = run_owned(owned_case, tmp_path, runner, base_image=base_image)

    assert report["status"] == "comparison_only"
    assert selected_images == [base_image, base_image]
    assert report["requested_base_image"] == base_image
    stored_report(report)


def test_diagnostic_output_only_forwards_public_group(evaluated_case, tmp_path):
    class DiagnosticExecutor(InspectionExecutor):
        def verify(self, image_id, source_dir, checks_dir, **kwargs):
            assert kwargs["diagnostic"] is True
            return super().verify(image_id, source_dir, checks_dir, **kwargs)

    executor = DiagnosticExecutor()
    report = run_comparison(evaluated_case, tmp_path / "public", check_group="feedback",
                            diagnostic=True, executor=executor)
    assert report["status"] == "comparison_only" and report["diagnostic_output"] is True
    assert all(set(files) == {"test_public.py"} for files in executor.inputs)
    for group in ("all", "acceptance"):
        with pytest.raises(ValueError, match="only available for public feedback"):
            run_comparison(evaluated_case, tmp_path / group, check_group=group,
                           diagnostic=True, executor=executor)
        assert not (tmp_path / group).exists()


def test_owned_success_output_is_captured_unless_diagnostic_enabled(tmp_path):
    import os
    import subprocess
    import sys

    from upgrade_workbench.execution.docker import _RUNNER, _parse_summary_record

    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "test_owned_output.py").write_text(
        "def test_measurement():\n    print('OWNED_SUCCESS_MEASUREMENT: expected scope')\n", encoding="utf-8")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text(_RUNNER.replace("/work/checks", checks.as_posix()).replace(
        "/work/pytest.ini", config.as_posix()), encoding="utf-8")
    environment = os.environ.copy()
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="", PYTEST_PLUGINS="",
                       UPGRADE_WORKBENCH_NONCE="owned-output")
    for enabled in (False, True):
        environment["UPGRADE_WORKBENCH_DIAGNOSTIC"] = str(int(enabled))
        result = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True,
            encoding="utf-8", timeout=30, env=environment, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        measured = _parse_summary_record(result.stdout, "owned-output", result.returncode)
        assert measured is not None and measured["tests"]["passed"] == 1
        assert ("OWNED_SUCCESS_MEASUREMENT" in result.stdout) is enabled


def test_case_environment_is_frozen_and_forwarded(owned_case, tmp_path):
    manifest_path, _ = owned_case
    root = manifest_path.parent
    wheel_name = "wheels/legacy_package-1.0-py3-none-any.whl"
    wheel = root / wheel_name
    wheel.parent.mkdir()
    wheel.write_bytes(b"reviewed wheel")
    contract = {
        "schema_version": 1,
        "base_image": "python@sha256:" + "d" * 64,
        "python_version": "3.9",
        "local_wheels": [wheel_name],
    }
    environment_path = root / "environment.json"
    environment_path.write_text(json.dumps(contract), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_hashes"].update({
        "environment.json": hashlib.sha256(environment_path.read_bytes()).hexdigest(),
        wheel_name: hashlib.sha256(wheel.read_bytes()).hexdigest(),
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    calls = []

    class EnvironmentExecutor(FakeExecutor):
        def prepare_environment(self, lock_path, **options):
            calls.append(options)
            receipt = super().prepare_environment(
                lock_path, timeout_seconds=options["timeout_seconds"]
            )
            return {
                **receipt,
                "base_image_id": "sha256:" + "a" * 64,
                "base_image_digest": contract["base_image"],
            }

    report = run_owned(
        owned_case,
        tmp_path,
        EnvironmentExecutor([measured(), measured(status="failed", passed=1, failed=1)]),
    )

    assert report["status"] == "comparison_only"
    assert report["case_environment"] == contract
    assert [call["python_version"] for call in calls] == ["3.9", "3.9"]
    assert [call["base_image"] for call in calls] == [contract["base_image"]] * 2
    for call in calls:
        assert len(call["local_wheels"]) == 1
        assert call["local_wheels"][0].read_bytes() == b"reviewed wheel"
        assert "inputs" in call["local_wheels"][0].parts
    stored_report(report)


def test_new_environment_uses_the_old_receipts_digest_instead_of_a_mutable_tag(
    owned_case, tmp_path
):
    requested = "python:3.12-slim"
    digest = "python@sha256:" + "a" * 64
    base_id = "sha256:" + "b" * 64
    selected_images = []

    class ResolvedImageExecutor(FakeExecutor):
        def prepare_environment(self, lock_path: Path, *, timeout_seconds: int, base_image: str):
            selected_images.append(base_image)
            receipt = super().prepare_environment(lock_path, timeout_seconds=timeout_seconds)
            return {**receipt, "base_image_digest": digest, "base_image_id": base_id}

    runner = ResolvedImageExecutor(
        [
            measured(),
            measured(status="failed", passed=1, failed=1),
            measured(),
        ]
    )
    report = run_owned(owned_case, tmp_path, runner, origin="user_candidate", base_image=requested)

    assert report["status"] == "candidate_verified"
    assert selected_images == [requested, digest]
    assert report["requested_base_image"] == requested
    assert report["environments"]["old"]["base_image_id"] == base_id
    assert report["environments"]["new"]["base_image_id"] == base_id
    assert len(runner.verified) == 3
    assert runner.verified[1]["image_id"] == runner.verified[2]["image_id"]
    stored_report(report)


def test_base_image_identity_mismatch_stops_before_new_environment_tests(owned_case, tmp_path):
    digest = "python@sha256:" + "a" * 64
    selected_images = []

    class MismatchedImageExecutor(FakeExecutor):
        def prepare_environment(self, lock_path: Path, *, timeout_seconds: int, base_image: str):
            selected_images.append(base_image)
            receipt = super().prepare_environment(lock_path, timeout_seconds=timeout_seconds)
            actual_id = "sha256:" + ("b" if len(self.prepared) == 1 else "c") * 64
            return {**receipt, "base_image_digest": digest, "base_image_id": actual_id}

    runner = MismatchedImageExecutor([measured()])
    report = run_owned(
        owned_case, tmp_path, runner, origin="agent_candidate", base_image="python:3.12-slim"
    )

    assert report["status"] == "execution_error"
    assert "different base images" in report["reason"]
    assert selected_images == ["python:3.12-slim", digest]
    assert len(runner.prepared) == 2
    assert len(runner.verified) == 1
    assert report["stages"]["old_original"]["status"] == "passed"
    assert report["stages"]["new_original"]["status"] == "not_run"
    assert report["stages"]["new_candidate"]["status"] == "not_run"
    assert report["last_action"] == "prepare_new_environment"
    stored_report(report)


def test_comparison_only_uses_same_original_and_frozen_checks(owned_case, tmp_path):
    runner = FakeExecutor([measured(), measured(status="failed", passed=1, failed=1)])
    report = run_owned(owned_case, tmp_path, runner, prepare_timeout=11, test_timeout=7)
    assert report["status"] == "comparison_only"
    assert report["expected_behavior_observed"] is True
    assert report["stages"]["new_candidate"]["status"] == "not_supplied"
    assert len(runner.prepared) == len(runner.verified) == 2
    assert runner.verified[0]["source_dir"] == runner.verified[1]["source_dir"]
    assert runner.verified[0]["checks_dir"] == runner.verified[1]["checks_dir"]
    assert runner.verified[0]["checks_dir"] != owned_case[0].parent / "checks"
    assert all(call["source_bytes"] == ORIGINAL for call in runner.verified)
    assert all(call[1] == 11 for call in runner.prepared)
    assert all(call["timeout_seconds"] == 7 for call in runner.verified)
    stored_report(report)


@pytest.mark.parametrize(
    "old",
    [
        measured(status="failed", passed=1, failed=1),
        measured(passed=1, skipped=1),
        measured(status="error", passed=0, skipped=2, exit_code=0),
        measured(passed=0, nodeids=[]),
        measured(nodeids=[NODEIDS[0], NODEIDS[0]]),
        measured(passed=1),
        measured(status="failed", passed=0, errors=1, nodeids=[], exit_code=2),
    ],
)
def test_old_baseline_requires_every_collected_test_to_pass(owned_case, tmp_path, old):
    runner = FakeExecutor([old])
    report = run_owned(owned_case, tmp_path, runner, origin="user_candidate")
    assert report["status"] == "baseline_invalid"
    assert len(runner.prepared) == len(runner.verified) == 1
    assert report["stages"]["new_original"]["status"] == "not_run"
    assert report["stages"]["new_candidate"]["status"] == "not_run"
    stored_report(report)


@pytest.mark.parametrize("status", ["error", "timeout", "unknown"])
def test_old_execution_error_is_not_a_measured_baseline_failure(owned_case, tmp_path, status):
    runner = FakeExecutor([measured(status=status, passed=0, nodeids=[], exit_code=125)])
    report = run_owned(owned_case, tmp_path, runner)
    assert report["status"] == "execution_incomplete"
    assert len(runner.prepared) == len(runner.verified) == 1
    stored_report(report)


@pytest.mark.parametrize("stage", ["new_original", "new_candidate"])
def test_changed_test_identity_prevents_candidate_acceptance(owned_case, tmp_path, stage):
    direct = measured(status="failed", passed=1, failed=1)
    candidate = measured()
    replacement = [NODEIDS[0], "test_other.py::test_other"]
    (direct if stage == "new_original" else candidate)["nodeids"] = replacement
    runner = FakeExecutor([measured(), direct, candidate])
    report = run_owned(owned_case, tmp_path, runner, origin="agent_candidate")
    assert report["status"] == "test_set_changed"
    stored_report(report)


def test_new_import_failure_can_be_compared_against_full_candidate_test_set(owned_case, tmp_path):
    runner = FakeExecutor(
        [
            measured(),
            measured(status="failed", passed=0, errors=1, nodeids=[], exit_code=2),
            measured(),
        ]
    )
    report = run_owned(owned_case, tmp_path, runner, origin="user_candidate")
    assert report["status"] == "candidate_verified"
    assert report["stages"]["new_original"]["tests"]["errors"] == 1
    assert report["stages"]["new_candidate"]["nodeids"] == NODEIDS
    stored_report(report)


def test_new_test_identity_is_not_excused_by_an_unrelated_setup_error(owned_case, tmp_path):
    runner = FakeExecutor(
        [
            measured(),
            measured(
                status="failed",
                passed=1,
                errors=1,
                nodeids=[NODEIDS[0], "test_other.py::test_other"],
            ),
            measured(),
        ]
    )
    report = run_owned(owned_case, tmp_path, runner, origin="agent_candidate")
    assert report["status"] == "test_set_changed"
    stored_report(report)


@pytest.mark.parametrize(
    ("candidate_result", "expected"),
    [
        (measured(passed=1, skipped=1), "candidate_not_accepted"),
        (measured(status="error", passed=0, skipped=2, exit_code=0), "candidate_not_accepted"),
        (measured(status="failed", passed=1, failed=1), "candidate_not_accepted"),
        (measured(status="error", passed=0, nodeids=[]), "execution_incomplete"),
        (measured(status="timeout", passed=0, nodeids=[]), "execution_incomplete"),
    ],
)
def test_candidate_must_execute_and_pass_all_checks(
    owned_case, tmp_path, candidate_result, expected
):
    runner = FakeExecutor(
        [measured(), measured(status="failed", passed=1, failed=1), candidate_result]
    )
    report = run_owned(owned_case, tmp_path, runner, origin="user_candidate")
    assert report["status"] == expected
    stored_report(report)


@pytest.mark.parametrize("has_candidate", [False, True])
@pytest.mark.parametrize(
    "direct",
    [
        measured(passed=1, skipped=1),
        measured(status="error", passed=0, skipped=2, exit_code=0),
    ],
)
def test_direct_upgrade_with_skips_is_not_a_complete_comparison(
    owned_case, tmp_path, has_candidate, direct
):
    results = [measured(), direct]
    if has_candidate:
        results.append(measured())
    runner = FakeExecutor(results)
    report = run_owned(
        owned_case, tmp_path, runner, origin="agent_candidate" if has_candidate else None
    )
    assert report["status"] == "comparison_incomplete"
    assert report["expected_behavior_observed"] is False
    stored_report(report)


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("calibration_reference", "calibration_passed"),
        ("user_candidate", "candidate_verified"),
        ("agent_candidate", "candidate_verified"),
        ("official_tool", "candidate_verified"),
    ],
)
def test_candidate_origin_and_content_are_attributed_without_model_claims(
    owned_case, tmp_path, origin, expected
):
    initial = load_case(owned_case[0])
    runner = FakeExecutor([measured(), measured(status="failed", passed=1, failed=1), measured()])
    report = run_owned(owned_case, tmp_path, runner, origin=origin)
    assert report["status"] == expected
    assert report["candidate_origin"] == report["candidate"]["origin"] == origin
    digest = hashlib.sha256(PATCH.encode()).hexdigest()
    assert (
        report["candidate"]["candidate_sha256"] == report["candidate"]["supplied_sha256"] == digest
    )
    assert report["candidate"]["changed_files"] == ["owned.py"]
    assert report["automatic_patch_generation"] is False
    assert report["model_usage"] == {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    assert report["case_fingerprint"] == initial.fingerprint == load_case(owned_case[0]).fingerprint
    assert (initial.source_dir / "owned.py").read_bytes() == ORIGINAL
    assert runner.verified[-1]["source_bytes"] == b"VALUE = 2\n"
    assert runner.verified[-1]["image_id"] == runner.verified[1]["image_id"]
    assert runner.verified[-1]["source_dir"] != runner.verified[1]["source_dir"]
    stored_report(report)


def test_all_pass_direct_upgrade_does_not_credit_candidate_as_a_fix(owned_case, tmp_path):
    runner = FakeExecutor([measured(), measured(), measured()])
    report = run_owned(owned_case, tmp_path, runner, origin="agent_candidate")
    assert report["status"] == "no_regression_observed"
    assert report["expected_behavior_observed"] is False
    stored_report(report)


def test_docker_blocker_has_a_durable_report_and_unrun_stages(owned_case, tmp_path):
    runner = FakeExecutor([], prepare_error=DockerUnavailable("Linux Docker engine unavailable"))
    report = run_owned(owned_case, tmp_path, runner, origin="calibration_reference")
    assert report["status"] == "blocked_environment"
    assert report["reason"] == "Linux Docker engine unavailable"
    assert report["last_action"] == "prepare_old_environment"
    assert all(stage["status"] == "not_run" for stage in report["stages"].values())
    assert runner.verified == []
    assert report["candidate_origin"] == "calibration_reference"
    stored_report(report)


@pytest.mark.parametrize("error", [RuntimeError("build failed"), TimeoutError("prepare timeout")])
def test_preparation_error_is_not_a_baseline_failure(owned_case, tmp_path, error):
    report = run_owned(owned_case, tmp_path, FakeExecutor([], prepare_error=error))
    assert report["status"] == "execution_error"
    assert report["stages"]["old_original"]["status"] == "not_run"
    stored_report(report)


@pytest.mark.parametrize(
    "name",
    [
        "source/owned.py",
        "checks/test_owned.py",
        "requirements/new.txt",
        "SOURCE.json",
        "manifest.json",
    ],
)
def test_case_input_drift_rejects_an_otherwise_passing_candidate(owned_case, tmp_path, name):
    def change_input(runner, source_dir, checks_dir):
        if len(runner.verified) == 3:
            target = owned_case[0].parent / name
            if name == "manifest.json":
                value = json.loads(target.read_text(encoding="utf-8"))
                value["title"] = "Identity changed during comparison"
                target.write_text(json.dumps(value), encoding="utf-8")
            else:
                target.write_bytes(b"Changed during comparison\n")

    runner = FakeExecutor(
        [measured(), measured(status="failed", passed=1, failed=1), measured()],
        on_verify=change_input,
    )
    report = run_owned(owned_case, tmp_path, runner, origin="agent_candidate")
    assert report["status"] == "rejected_input"
    stored_report(report)


def test_frozen_check_drift_cannot_be_attributed_to_the_original_manifest(owned_case, tmp_path):
    def change_frozen_check(runner, source_dir, checks_dir):
        if len(runner.verified) == 1:
            (checks_dir / "test_owned.py").write_bytes(b"def test_first():\n    assert True\n")

    runner = FakeExecutor(
        [measured(), measured(status="failed", passed=1, failed=1), measured()],
        on_verify=change_frozen_check,
    )
    report = run_owned(owned_case, tmp_path, runner, origin="agent_candidate")
    assert report["status"] == "rejected_input"
    assert len(runner.verified) == 1
    stored_report(report)


@pytest.mark.parametrize(
    "artifact", ["original", "old_lock", "new_lock", "provenance", "candidate_patch", "added_check"]
)
def test_frozen_artifact_drift_stops_before_the_next_comparison_stage(
    owned_case, tmp_path, artifact
):
    def change_frozen_input(runner, source_dir, checks_dir):
        if len(runner.verified) != 1:
            return
        targets = {
            "original": source_dir / "owned.py",
            "old_lock": checks_dir.parent / "requirements/old.txt",
            "new_lock": checks_dir.parent / "requirements/new.txt",
            "provenance": checks_dir.parent / "SOURCE.json",
            "candidate_patch": checks_dir.parent / "candidate.patch",
            "added_check": checks_dir / "test_unregistered.py",
        }
        targets[artifact].write_bytes(b"changed frozen input\n")

    runner = FakeExecutor(
        [measured(), measured(status="failed", passed=1, failed=1), measured()],
        on_verify=change_frozen_input,
    )
    report = run_owned(owned_case, tmp_path, runner, origin="agent_candidate")
    assert report["status"] == "rejected_input"
    assert len(runner.verified) == 1
    assert (owned_case[0].parent / "source/owned.py").read_bytes() == ORIGINAL
    stored_report(report)


def test_candidate_snapshot_drift_cannot_retain_its_patch_attribution(owned_case, tmp_path):
    def change_candidate(runner, source_dir, checks_dir):
        if len(runner.verified) == 3:
            (source_dir / "owned.py").write_bytes(b"VALUE = 99\n")

    runner = FakeExecutor(
        [measured(), measured(status="failed", passed=1, failed=1), measured()],
        on_verify=change_candidate,
    )
    report = run_owned(owned_case, tmp_path, runner, origin="agent_candidate")
    assert report["status"] == "rejected_input"
    stored_report(report)


def test_external_patch_changes_do_not_replace_the_frozen_candidate(owned_case, tmp_path):
    def change_supplied_path(runner, source_dir, checks_dir):
        if len(runner.verified) == 1:
            owned_case[1].write_bytes(b"different supplied file after freezing\n")

    runner = FakeExecutor(
        [measured(), measured(status="failed", passed=1, failed=1), measured()],
        on_verify=change_supplied_path,
    )
    report = run_owned(owned_case, tmp_path, runner, origin="user_candidate")
    assert report["status"] == "candidate_verified"
    assert report["candidate"]["candidate_sha256"] == hashlib.sha256(PATCH.encode()).hexdigest()
    assert runner.verified[-1]["source_bytes"] == b"VALUE = 2\n"
    assert (
        Path(report["report_path"]).parent / "inputs/candidate.patch"
    ).read_bytes() == PATCH.encode()
    stored_report(report)


@pytest.mark.parametrize(
    "patch", ["invalid unified diff\n", PATCH.replace("owned.py", "../checks/test_owned.py")]
)
def test_malformed_or_out_of_scope_patch_is_rejected_before_environment_work(
    owned_case, tmp_path, patch
):
    owned_case[1].write_bytes(patch.encode())
    runner = FakeExecutor([])
    report = run_owned(owned_case, tmp_path, runner, origin="user_candidate")
    assert report["status"] == "rejected_input"
    assert runner.prepared == runner.verified == []
    assert (owned_case[0].parent / "source/owned.py").read_bytes() == ORIGINAL
    assert not (Path(report["report_path"]).parent / "candidate").exists()
    stored_report(report)


def test_interrupted_verification_has_durable_incomplete_stage(owned_case, tmp_path):
    runner = FakeExecutor([KeyboardInterrupt()])
    report = run_owned(owned_case, tmp_path, runner)
    assert report["status"] == "interrupted"
    assert report["stages"]["old_original"]["status"] == "incomplete"
    assert report["last_action"] == "old_original"
    stored_report(report)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"candidate_origin": "agent_candidate"},
        {"candidate_patch": Path("missing.patch")},
        {"candidate_patch": Path("missing.patch"), "candidate_origin": "invented_origin"},
        {"prepare_timeout": 0},
        {"test_timeout": -1},
        {"test_timeout": True},
    ],
)
def test_invalid_request_is_rejected_before_creating_a_run(owned_case, tmp_path, kwargs):
    root = tmp_path / "runs"
    with pytest.raises(ValueError):
        run_comparison(owned_case[0], root, executor=FakeExecutor([]), **kwargs)
    assert not root.exists()
