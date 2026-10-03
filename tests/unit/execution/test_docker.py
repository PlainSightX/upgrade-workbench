"""验证执行边界；Docker 调用使用模拟结果，自有小测试验证 pytest 汇总。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from upgrade_workbench.execution import DockerExecutor, DockerUnavailable
from upgrade_workbench.execution.docker import (
    _RUNNER,
    _SUMMARY_PREFIX,
    _copy_snapshot,
    _parse_summary,
    _parse_summary_record,
    _redact,
    _validate_requirements,
)

BASE_ID = "sha256:" + "a" * 64
BASE_DIGEST = "python@sha256:" + "d" * 64
PG_DIGEST = "postgres@sha256:" + "e" * 64
ENV_ID = "sha256:" + "b" * 64
SNAPSHOT_ID = "sha256:" + "c" * 64


def completed(arguments, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(arguments, code, stdout, stderr)


def test_identical_lock_reuses_verified_environment_without_build(tmp_path, monkeypatch):
    fake = FakeDocker()
    executor = DockerExecutor(tmp_path / "execution")
    monkeypatch.setattr(executor, "_run", fake)
    lock = tmp_path / "requirements.lock"
    lock.write_text("pytest==9.0.2 --hash=sha256:" + "a" * 64 + "\n")
    original = executor.prepare_environment(lock)
    fake.commands.clear()
    reused = executor.prepare_environment(lock, base_image=BASE_DIGEST)
    assert reused["reused"] is True
    assert reused["image_id"] == original["image_id"]
    assert not any(command[0] == "build" for command in fake.commands)


def test_lock_change_does_not_reuse_prepared_environment(tmp_path, monkeypatch):
    fake = FakeDocker()
    executor = DockerExecutor(tmp_path / "execution")
    monkeypatch.setattr(executor, "_run", fake)
    lock = tmp_path / "requirements.lock"
    lock.write_text("pytest==9.0.2 --hash=sha256:" + "a" * 64 + "\n")
    executor.prepare_environment(lock)
    fake.commands.clear()
    lock.write_text("pytest==9.0.3 --hash=sha256:" + "b" * 64 + "\n")
    rebuilt = executor.prepare_environment(lock)
    assert not rebuilt.get("reused")
    assert any(command[0] == "build" for command in fake.commands)


class FakeDocker:
    def __init__(self):
        self.commands = []
        self.run_code = 0
        self.counts = {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0}
        self.nodeids = ["test_contract.py::test_result"]
        self.timeout = False
        self.timeout_machine_result = None
        self.machine_result = True
        self.windows_engine = False
        self.fail_command = None
        self.process_exception = None
        self.python_version = "3.12.10"
        self.labels = {}

    def __call__(self, arguments, timeout):
        self.commands.append(arguments)
        if arguments[0] == self.fail_command:
            return completed(arguments, 1, stderr="Controlled command failure")
        if arguments[0] == "run" and self.process_exception:
            raise self.process_exception
        if arguments[0] == "info":
            info = {
                "OSType": "windows" if self.windows_engine else "linux",
                "ServerVersion": "test",
            }
            return completed(arguments, stdout=json.dumps(info))
        if arguments[:2] == ["image", "inspect"]:
            reference = arguments[2]
            image_id = (
                ENV_ID
                if reference.startswith("upgrade-workbench-env:") or reference == ENV_ID
                else BASE_ID
            )
            if reference.startswith("upgrade-workbench-snapshot:"):
                image_id = SNAPSHOT_ID
            return completed(
                arguments,
                stdout=json.dumps(
                    [
                        {
                            "Id": image_id,
                            "Config": {"Labels": self.labels if image_id == ENV_ID else {}},
                            "Os": "linux",
                            "Architecture": "amd64",
                            "Variant": "",
                            "RepoDigests": [BASE_DIGEST],
                        }
                    ]
                ),
            )
        if arguments[0] == "build":
            import re
            text = (Path(arguments[-1]) / "Dockerfile").read_text(encoding="utf-8")
            self.labels = dict(pair.split("=", 1) for pair in re.search(r"^LABEL (.+)$", text, re.M)[1].split())
        if arguments[0] == "commit":
            return completed(arguments, stdout=SNAPSHOT_ID + "\n")
        if arguments[0] == "run":
            nonce = next(
                arg.split("=", 2)[2]
                for arg in arguments
                if arg.startswith("--env=UPGRADE_WORKBENCH_NONCE=")
            )
            record = {
                "nonce": nonce,
                "exit_code": self.run_code,
                "tests": self.counts,
                "nodeids": self.nodeids,
                "python_version": self.python_version,
            }
            if self.timeout:
                machine_line = _SUMMARY_PREFIX + json.dumps(record)
                output = {
                    "complete": machine_line,
                    "duplicate": machine_line + "\n" + machine_line,
                    "wrong_nonce": machine_line.replace(nonce, "wrong-nonce"),
                }.get(self.timeout_machine_result, "partial output")
                raise subprocess.TimeoutExpired(
                    arguments, timeout, output=output.encode(), stderr=b"partial error"
                )
            stdout = (
                _SUMMARY_PREFIX + json.dumps(record)
                if self.machine_result
                else "1 passed (uncontrolled text)"
            )
            return completed(arguments, self.run_code, stdout=stdout)
        return completed(arguments, stdout="built")


class PostgresDocker(FakeDocker):
    def __init__(self):
        super().__init__()
        self.db_start_failure = False
        self.db_cleanup_failure = False
        self.db_unsafe_mount = False

    def __call__(self, arguments, timeout):
        if arguments[:3] == ["image", "inspect", PG_DIGEST]:
            self.commands.append(arguments)
            return completed(arguments, stdout=json.dumps([{
                "Id": "sha256:" + "f" * 64, "Os": "linux",
                "Config": {"Volumes": {"/var/lib/postgresql/data": {}}},
            }]))
        if arguments[:2] == ["run", "--detach"]:
            self.commands.append(arguments)
            return completed(arguments, int(self.db_start_failure), stdout="database-id", stderr="" if not self.db_start_failure else "database start failed")
        if arguments[0] == "exec":
            self.commands.append(arguments)
            return completed(arguments, stdout="16.13\n")
        if arguments[0] == "inspect":
            self.commands.append(arguments)
            return completed(arguments, stdout=json.dumps([{
                "HostConfig": {"NetworkMode": "none", "PortBindings": {}, "Binds": None},
                "Mounts": [{"Type": "bind" if self.db_unsafe_mount else "tmpfs"}],
            }]))
        if arguments[:3] == ["rm", "--force", "--volumes"]:
            self.commands.append(arguments)
            return completed(arguments, int(self.db_cleanup_failure), stderr="busy" if self.db_cleanup_failure else "")
        return super().__call__(arguments, timeout)


def postgres_prepared(prepared, monkeypatch):
    executor, _, environment, source, checks, _ = prepared
    fake = PostgresDocker()
    monkeypatch.setattr(executor, "_run", fake)
    return executor, fake, environment, source, checks


def test_pg_target_only_shares_isolated_namespace_and_recreates_database(prepared, monkeypatch):
    executor, fake, environment, source, checks = postgres_prepared(prepared, monkeypatch)
    spec = {"schema_version": 1, "kind": "postgresql", "image": PG_DIGEST}
    first = executor.verify(environment["image_id"], source, checks, postgres=spec)
    second = executor.verify(environment["image_id"], source, checks, postgres=spec)
    assert first["status"] == second["status"] == "passed"
    assert first["database"]["container"] != second["database"]["container"]
    database_run = next(c for c in fake.commands if c[:2] == ["run", "--detach"])
    assert "--network=none" in database_run and "--read-only" in database_run
    assert not any(x.startswith(("--publish", "--volume=", "--mount=")) for x in database_run)
    assert "--network=container:" + first["database"]["container"] in first["command"]
    assert first["database"]["isolation_verified"]
    assert first["cleanup"]["database"]["ok"]


@pytest.mark.parametrize("failure", ["db_start_failure", "db_unsafe_mount"])
def test_pg_failure_never_falls_back_to_host_or_normal_network(prepared, monkeypatch, failure):
    executor, fake, environment, source, checks = postgres_prepared(prepared, monkeypatch)
    setattr(fake, failure, True)
    result = executor.verify(environment["image_id"], source, checks,
        postgres={"schema_version": 1, "kind": "postgresql", "image": PG_DIGEST})
    assert result["status"] == "error" and result["tests"]["passed"] == 0
    assert not any(c[0] == "run" and "--detach" not in c for c in fake.commands)
    assert result["cleanup"]["database"]["ok"]


def test_pg_cleanup_failure_prevents_acceptance(prepared, monkeypatch):
    executor, fake, environment, source, checks = postgres_prepared(prepared, monkeypatch)
    fake.db_cleanup_failure = True
    result = executor.verify(environment["image_id"], source, checks,
        postgres={"schema_version": 1, "kind": "postgresql", "image": PG_DIGEST})
    assert result["status"] == "error" and result["verification_status"] == "passed"
    assert result["cleanup"]["database"]["ok"] is False


def test_all_execution_resources_keep_operation_identity(prepared, monkeypatch):
    executor, fake, environment, source, checks = postgres_prepared(prepared, monkeypatch)
    result = executor.verify(environment["image_id"], source, checks,
        postgres={"schema_version": 1, "kind": "postgresql", "image": PG_DIGEST})
    assert result["status"] == "passed"
    ownership = result["ownership"]
    assert ownership == result["database"]["ownership"]
    for command in fake.commands:
        if command[0] in ("run", "create"):
            assert "--label=project=upgrade-workbench" in command
            assert f"--label=org.upgrade-workbench.operation={ownership['operation']}" in command
        elif command[0] == "commit":
            assert "project=upgrade-workbench" in command[2]
            assert f"org.upgrade-workbench.operation={ownership['operation']}" in command[2]


def test_target_timeout_also_cleans_database(prepared, monkeypatch):
    executor, fake, environment, source, checks = postgres_prepared(prepared, monkeypatch)
    fake.timeout = True
    result = executor.verify(environment["image_id"], source, checks,
        postgres={"schema_version": 1, "kind": "postgresql", "image": PG_DIGEST})
    assert result["status"] == "timeout" and result["cleanup"]["database"]["ok"]


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    fake = FakeDocker()
    executor = DockerExecutor(tmp_path / "execution")
    monkeypatch.setattr(executor, "_run", fake)
    lock = tmp_path / "requirements.lock"
    lock.write_text("pytest==9.0.2 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    environment = executor.prepare_environment(lock)
    source, checks = tmp_path / "source", tmp_path / "checks"
    source.mkdir()
    checks.mkdir()
    (source / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (checks / "test_contract.py").write_text(
        "def test_result():\n    assert True\n", encoding="utf-8"
    )
    return executor, fake, environment, source, checks, lock


def test_prepare_uses_hash_lock_and_records_exact_images(prepared):
    executor, fake, environment, source, checks, lock = prepared
    assert environment["image_id"] == ENV_ID
    assert environment["base_image_id"] == BASE_ID
    assert environment["lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert Path(environment["build_log"]).is_file()
    build = next(command for command in fake.commands if command[0] == "build")
    dockerfile = (Path(build[-1]) / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes --only-binary=:all:" in dockerfile
    assert "--isolated install" in dockerfile
    assert "--no-index --no-deps" not in dockerfile
    assert "--find-links" not in dockerfile
    assert dockerfile.startswith(f"FROM {BASE_DIGEST}\n")
    assert environment["base_image_digest"] == BASE_DIGEST
    assert environment["platform"] == "linux/amd64"
    assert environment["expected_python"] == "3.12"
    assert "assert sys.version_info[:2] == (3, 12)" in dockerfile
    assert dockerfile.index("assert sys.version_info") < dockerfile.index("pip --isolated install")
    assert dockerfile.index("RUN ") < dockerfile.index("LABEL ")
    assert "--network=default" in build
    assert sorted(item.name for item in Path(build[-1]).iterdir()) == [
        "Dockerfile",
        "requirements.lock",
    ]


def test_prepare_supports_case_python_and_hash_identified_local_wheels(tmp_path, monkeypatch):
    fake = FakeDocker()
    executor = DockerExecutor(tmp_path / "execution")
    monkeypatch.setattr(executor, "_run", fake)
    lock = tmp_path / "requirements.lock"
    lock.write_text("pytest==9.0.2 --hash=sha256:" + "a" * 64 + "\n")
    wheel = tmp_path / "legacy_package-1.0-py3-none-any.whl"
    wheel.write_bytes(b"reviewed wheel bytes")

    environment = executor.prepare_environment(
        lock, python_version="3.9", local_wheels=[wheel]
    )

    build = next(command for command in fake.commands if command[0] == "build")
    context = Path(build[-1])
    dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
    assert environment["expected_python"] == "3.9"
    assert environment["local_wheel_sha256"] == {
        wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()
    }
    assert "assert sys.version_info[:2] == (3, 9)" in dockerfile
    assert "--require-hashes --only-binary=:all:" in dockerfile
    local_install = (
        "python -m pip --isolated install --disable-pip-version-check "
        "--no-cache-dir --no-index --no-deps "
        f"/opt/upgrade-workbench/wheels/{wheel.name}"
    )
    assert local_install in dockerfile
    assert "--find-links" not in dockerfile
    assert dockerfile.index(local_install) < dockerfile.index("--require-hashes")
    assert (context / "wheels" / wheel.name).read_bytes() == wheel.read_bytes()


def test_python_or_local_wheel_change_prevents_environment_reuse(tmp_path, monkeypatch):
    fake = FakeDocker()
    executor = DockerExecutor(tmp_path / "execution")
    monkeypatch.setattr(executor, "_run", fake)
    lock = tmp_path / "requirements.lock"
    lock.write_text("pytest==9.0.2 --hash=sha256:" + "a" * 64 + "\n")
    wheel = tmp_path / "legacy_package-1.0-py3-none-any.whl"
    wheel.write_bytes(b"first")
    executor.prepare_environment(lock, python_version="3.9", local_wheels=[wheel])
    fake.commands.clear()
    wheel.write_bytes(b"second")

    rebuilt = executor.prepare_environment(
        lock, base_image=BASE_DIGEST, python_version="3.9", local_wheels=[wheel]
    )

    assert not rebuilt.get("reused")
    assert any(command[0] == "build" for command in fake.commands)


def test_verify_restricts_container_and_preserves_case_ids(prepared):
    executor, fake, environment, source, checks, lock = prepared
    result = executor.verify(ENV_ID, source, checks)
    assert result["status"] == "passed"
    assert result["nodeids"] == fake.nodeids
    assert result["snapshot_image_id"] == SNAPSHOT_ID
    assert result["python_version"] == "3.12.10"
    run = next(command for command in fake.commands if command[0] == "run")
    for option in (
        "--pull=never",
        "--network=none",
        "--user=65532:65532",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=128",
        "--memory=512m",
        "--memory-swap=512m",
        "--cpus=1",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "--env=PYTEST_ADDOPTS=",
        "--env=PYTHONPATH=/work/source/src:/work/source",
    ):
        assert option in run
    assert not any(item.startswith(("--volume", "--mount", "--privileged")) for item in run)
    assert run[-2] == SNAPSHOT_ID
    assert ["rm", "--force", result["container_name"]] in fake.commands
    create = next(command for command in fake.commands if command[0] == "create")
    assert ENV_ID in create
    assert "--network=none" in create
    assert [command[0] for command in result["materialization_commands"]] == [
        "create",
        "cp",
        "cp",
        "cp",
        "cp",
        "commit",
    ]
    assert result["snapshot_materialization"] == "stopped-container-copy-commit"
    assert not any(command[0] in ("start", "exec") for command in fake.commands)
    assert sum(command[0] == "build" for command in fake.commands) == 1
    assert result["cleanup"]["container"]["ok"]


def test_timeout_cleans_exact_container_and_snapshot(prepared):
    executor, fake, environment, source, checks, lock = prepared
    fake.timeout = True
    result = executor.verify(ENV_ID, source, checks, timeout_seconds=1)
    assert result["status"] == "timeout"
    assert result["process_exit_status"] == "timeout_without_complete_result"
    assert Path(result["stdout"]).read_text(encoding="utf-8") == "partial output"
    cleanup = [command for command in fake.commands if command[0] == "rm"]
    assert cleanup == [
        ["rm", "--force", result["container_name"]],
        ["rm", "--force", result["materialization_container_name"]],
    ]
    image_cleanup = [command for command in fake.commands if command[:2] == ["image", "rm"]]
    assert len(image_cleanup) == 1
    assert image_cleanup[0][-1].startswith("upgrade-workbench-snapshot:")
    assert all("prune" not in command for command in fake.commands)


def test_timeout_preserves_complete_failed_result_for_diagnosis(prepared):
    executor, fake, environment, source, checks, lock = prepared
    fake.timeout = True
    fake.timeout_machine_result = "complete"
    fake.run_code = 1
    fake.counts = {"collected": 2, "passed": 1, "failed": 1, "errors": 0, "skipped": 0}
    fake.nodeids = ["test_contract.py::test_ok", "test_contract.py::test_failed"]

    result = executor.verify(ENV_ID, source, checks, timeout_seconds=1)

    assert result["status"] == result["verification_status"] == "failed"
    assert result["exit_code"] == 1
    assert result["tests"] == fake.counts
    assert result["process_exit_status"] == "timeout_after_complete_result"
    assert all(item["ok"] for item in result["cleanup"].values())


def test_timeout_does_not_turn_complete_passing_result_into_a_pass(prepared):
    executor, fake, environment, source, checks, lock = prepared
    fake.timeout = True
    fake.timeout_machine_result = "complete"

    result = executor.verify(ENV_ID, source, checks, timeout_seconds=1)

    assert result["status"] == "error"
    assert result["verification_status"] == "passed"
    assert result["process_exit_status"] == "timeout_after_complete_result"
    assert "did not exit" in result["reason"]


@pytest.mark.parametrize("failure", [None, "timeout", "missing_result", "wrong_nonce", "duplicate", "wrong_python", "cleanup"])
def test_all_skipped_execution_marker_requires_complete_controlled_run(prepared, failure, monkeypatch):
    executor, fake, environment, source, checks, lock = prepared
    fake.counts.update(passed=0, skipped=1)
    if failure == "timeout":
        fake.timeout = True
        fake.timeout_machine_result = "complete"
    elif failure == "missing_result":
        fake.machine_result = False
    elif failure in {"wrong_nonce", "duplicate"}:
        def corrupt_summary(arguments, timeout):
            result = fake(arguments, timeout)
            if arguments[0] == "run":
                nonce = next(arg.split("=", 2)[2] for arg in arguments
                             if arg.startswith("--env=UPGRADE_WORKBENCH_NONCE="))
                result.stdout = (result.stdout.replace(nonce, "unbound-nonce") if failure == "wrong_nonce"
                                 else result.stdout + "\n" + result.stdout)
            return result
        monkeypatch.setattr(executor, "_run", corrupt_summary)
    elif failure == "wrong_python":
        fake.python_version = "3.11.10"
    elif failure == "cleanup":
        fake.fail_command = "rm"
    result = executor.verify(ENV_ID, source, checks)
    assert result["status"] == "error"
    if failure is None:
        assert result.get("execution_status") == "completed"
        assert result.get("test_outcome") == "all_skipped"
    else:
        assert result.get("execution_status") != "completed"


@pytest.mark.parametrize("machine_result", [None, "duplicate", "wrong_nonce"])
def test_timeout_rejects_partial_duplicate_or_unbound_results(prepared, machine_result):
    executor, fake, environment, source, checks, lock = prepared
    fake.timeout = True
    fake.timeout_machine_result = machine_result

    result = executor.verify(ENV_ID, source, checks, timeout_seconds=1)

    assert result["status"] == "timeout"
    assert result["process_exit_status"] == "timeout_without_complete_result"
    assert result["exit_code"] is None
    assert result["tests"]["collected"] == 0


@pytest.mark.parametrize("stage", ["create", "cp", "commit"])
def test_materialization_failure_never_runs_and_cleans_both_containers(prepared, stage):
    executor, fake, environment, source, checks, lock = prepared
    fake.fail_command = stage
    result = executor.verify(ENV_ID, source, checks)
    assert result["status"] == "error"
    assert not any(command[0] in ("run", "start", "exec") for command in fake.commands)
    assert ["rm", "--force", result["materialization_container_name"]] in fake.commands
    assert ["rm", "--force", result["container_name"]] in fake.commands
    assert "Controlled command failure" in Path(result["build_log"]).read_text(encoding="utf-8")


def test_cleanup_failure_cannot_report_an_unqualified_pass(prepared):
    executor, fake, environment, source, checks, lock = prepared
    fake.fail_command = "rm"
    result = executor.verify(ENV_ID, source, checks)
    assert result["status"] == "error"
    assert result["verification_status"] == "passed"
    assert not result["cleanup"]["container"]["ok"]
    assert not result["cleanup"]["materialization_container"]["ok"]
    assert result["cleanup"]["snapshot_image"]["ok"]


def test_unexpected_process_exception_still_preserves_report_and_cleanup(prepared):
    executor, fake, environment, source, checks, lock = prepared
    fake.process_exception = TypeError("Unexpected subprocess adapter failure")
    result = executor.verify(ENV_ID, source, checks)
    assert result["status"] == "error"
    assert "Unexpected subprocess adapter failure" in result["reason"]
    assert all(item["ok"] for item in result["cleanup"].values())
    saved = json.loads((Path(result["stdout"]).parent / "report.json").read_text(encoding="utf-8"))
    assert saved == result


def test_registry_digest_is_required_for_buildkit(tmp_path, monkeypatch):
    executor, fake = DockerExecutor(tmp_path / "execution"), FakeDocker()
    monkeypatch.setattr(executor, "_run", fake)
    monkeypatch.setattr(
        executor,
        "_inspect_image",
        lambda reference, timeout: {
            "Id": BASE_ID,
            "Config": {},
            "Os": "linux",
            "Architecture": "amd64",
            "RepoDigests": [],
        },
    )
    lock = tmp_path / "requirements.lock"
    lock.write_text("pytest==8.4.2 --hash=sha256:" + "a" * 64, encoding="utf-8")
    with pytest.raises(ValueError, match="RepoDigest"):
        executor.prepare_environment(lock)
    assert not any(command[0] == "build" for command in fake.commands)
    assert "RepoDigest" in next(
        (tmp_path / "execution" / "environments").glob("*/build.log")
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("counts", "code", "expected"),
    [
        ({"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}, 0, "error"),
        ({"collected": 0, "passed": 0, "failed": 0, "errors": 1, "skipped": 0}, 2, "failed"),
        ({"collected": 1, "passed": 0, "failed": 1, "errors": 0, "skipped": 0}, 1, "failed"),
        ({"collected": 1, "passed": 0, "failed": 0, "errors": 0, "skipped": 1}, 0, "error"),
    ],
)
def test_result_classification(prepared, counts, code, expected):
    executor, fake, environment, source, checks, lock = prepared
    fake.counts, fake.run_code = counts, code
    fake.nodeids = ["test_contract.py::test_result"] if counts["collected"] else []
    assert executor.verify(ENV_ID, source, checks)["status"] == expected


def test_uncontrolled_exit_zero_cannot_pass(prepared):
    executor, fake, environment, source, checks, lock = prepared
    fake.machine_result = False
    assert executor.verify(ENV_ID, source, checks)["status"] == "error"


def test_unexpected_runtime_cannot_pass(prepared):
    executor, fake, environment, source, checks, lock = prepared
    fake.python_version = "3.13.5"
    result = executor.verify(ENV_ID, source, checks)
    assert result["status"] == "error"
    assert result["python_version"] == "3.13.5"
    assert "Python 3.12 is required" in result["reason"]


def test_docker_missing_is_reported_without_starting_it(tmp_path, monkeypatch):
    executor = DockerExecutor(tmp_path)

    def missing(arguments, timeout):
        raise FileNotFoundError("Docker is not installed")

    monkeypatch.setattr(executor, "_run", missing)
    assert executor.probe()["ready"] is False
    with pytest.raises(DockerUnavailable):
        executor._require_ready()


def test_windows_engine_is_rejected(tmp_path, monkeypatch):
    executor, fake = DockerExecutor(tmp_path), FakeDocker()
    fake.windows_engine = True
    monkeypatch.setattr(executor, "_run", fake)
    assert executor.probe()["ready"] is False


def test_unregistered_image_is_rejected_before_docker(prepared):
    executor, fake, environment, source, checks, lock = prepared
    fake.commands.clear()
    with pytest.raises(ValueError, match="not prepared"):
        executor.verify(BASE_ID, source, checks)
    assert fake.commands == []


def test_changed_environment_id_is_rejected(prepared, monkeypatch):
    executor, fake, environment, source, checks, lock = prepared
    monkeypatch.setattr(executor, "_inspect_image", lambda reference, timeout: {"Id": BASE_ID})
    with pytest.raises(ValueError, match="no longer"):
        executor.verify(ENV_ID, source, checks)


@pytest.mark.parametrize(
    "line",
    [
        "pytest>=9",
        "pytest==9",
        "--index-url=https://example.invalid",
        "-e .",
        "pytest @ https://example.invalid/pytest.whl",
        "-r other.txt",
    ],
)
def test_non_locked_requirements_are_rejected(line):
    with pytest.raises(ValueError):
        _validate_requirements(line)


def test_uv_multiline_hash_export_is_accepted():
    _validate_requirements(
        "# This file was autogenerated by uv\npytest==9.0.2 \\\n"
        "    --hash=sha256:" + "a" * 64 + " \\\n"
        "    --hash=sha256:" + "b" * 64 + "\n# via project\n"
        "colorama==0.4.6 ; sys_platform == 'win32' --hash=sha256:" + "c" * 64 + "\n"
    )


def test_copy_rejects_symlink_and_path_escape(tmp_path):
    source, outside, output = tmp_path / "source", tmp_path / "outside", tmp_path / "output"
    source.mkdir()
    outside.write_text("outside", encoding="utf-8")
    try:
        (source / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    with pytest.raises(ValueError, match="Links"):
        _copy_snapshot(source, output)


def test_copy_rejects_recursive_output_root(tmp_path):
    with pytest.raises(ValueError, match="contain"):
        _copy_snapshot(tmp_path, tmp_path / "nested")


def test_secret_log_redaction(monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_KEY", "unique-secret-value")
    result = _redact("unique-secret-value https://user:password@host.invalid token=hidden-value")
    assert "unique-secret-value" not in result
    assert "user:password" not in result
    assert "hidden-value" not in result


def test_process_uses_argument_array_and_no_shell(tmp_path, monkeypatch):
    captured = {}

    def run(arguments, **kwargs):
        captured.update(arguments=arguments, kwargs=kwargs)
        return completed(arguments)

    monkeypatch.setattr(subprocess, "run", run)
    DockerExecutor(tmp_path)._run(["info"], 1)
    assert captured["arguments"] == ["docker", "info"]
    assert captured["kwargs"]["shell"] is False
    if os.name == "nt":
        assert captured["kwargs"]["creationflags"] == subprocess.CREATE_NO_WINDOW


@pytest.mark.parametrize(
    ("test_source", "expected"),
    [
        (
            "def test_one():\n    assert True\n",
            {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        ),
        (
            "def test_one():\n    assert False\n",
            {"collected": 1, "passed": 0, "failed": 1, "errors": 0, "skipped": 0},
        ),
        (
            "import absent_workbench_example_module\n",
            {"collected": 0, "passed": 0, "failed": 0, "errors": 1, "skipped": 0},
        ),
        ("VALUE = 1\n", {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}),
        (
            "import warnings\ndef test_one():\n    warnings.warn('')\n",
            {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        ),
        (
            "import pytest\n@pytest.fixture\ndef broken():\n    raise ValueError('setup')\ndef test_one(broken):\n    pass\n",
            {"collected": 1, "passed": 0, "failed": 0, "errors": 1, "skipped": 0},
        ),
    ],
)
def test_controlled_runner_measures_real_pytest_results(tmp_path, test_source, expected):
    # 仅运行本测试显式定义的自有小样例，未加载任何第三方目标源码。
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "test_owned_example.py").write_text(test_source, encoding="utf-8")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text(
        _RUNNER.replace("/work/checks", checks.as_posix()).replace(
            "/work/pytest.ini", config.as_posix()
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="",
        PYTEST_PLUGINS="",
        UPGRADE_WORKBENCH_NONCE="test-nonce",
    )
    result = subprocess.run(
        [sys.executable, str(runner)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env=environment,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    summary = _parse_summary(result.stdout, "test-nonce", result.returncode)
    assert summary is not None, result.stdout + result.stderr
    assert summary[0] == expected
    assert len(summary[1]) == expected["collected"]


def test_summary_rejects_inconsistent_or_duplicate_records():
    record = {
        "nonce": "correct",
        "exit_code": 0,
        "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        "nodeids": ["test_a.py::test_a"],
        "python_version": "3.12.10",
    }
    line = _SUMMARY_PREFIX + json.dumps(record)
    assert _parse_summary(line, "wrong", 0) is None
    assert _parse_summary(line + "\n" + line, "correct", 0) is None
    record["nodeids"] = []
    assert _parse_summary(_SUMMARY_PREFIX + json.dumps(record), "correct", 0) is None


def test_summary_rejects_unbound_or_unsafe_failure_details():
    record = {
        "nonce": "correct",
        "exit_code": 1,
        "tests": {"collected": 1, "passed": 0, "failed": 1, "errors": 0, "skipped": 0},
        "nodeids": ["test_a.py::test_a"],
        "python_version": "3.12.10",
        "failures": [{
            "nodeid": "other.py::test_other", "when": "call", "outcome": "failed",
            "message": "AssertionError", "location": None,
        }],
        "warnings": [], "failures_truncated": False, "warnings_truncated": False,
    }
    line = _SUMMARY_PREFIX + json.dumps(record)
    assert _parse_summary_record(line, "correct", 1) is None
    record["failures"][0]["nodeid"] = "test_a.py::test_a"
    record["failures"][0]["location"] = {"path": "C:/secret.py", "line": 1}
    assert _parse_summary_record(_SUMMARY_PREFIX + json.dumps(record), "correct", 1) is None


@pytest.mark.parametrize("change", [
    {"filename": "C:/secret.py"},
    {"filename": "../secret.py"},
    {"line": "1"},
    {"when": "session"},
    {"nodeid": "other.py::test_other"},
])
def test_summary_rejects_unbounded_or_unbound_warning_details(change):
    warning = {"category": "UserWarning", "message": "bounded warning", "filename": "test_a.py",
               "line": 1, "when": "runtest", "nodeid": "test_a.py::test_a"} | change
    record = {
        "nonce": "correct", "exit_code": 0,
        "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        "nodeids": ["test_a.py::test_a"], "python_version": "3.12.10",
        "failures": [], "warnings": [warning],
        "failures_truncated": False, "warnings_truncated": False,
    }
    assert _parse_summary_record(_SUMMARY_PREFIX + json.dumps(record), "correct", 0) is None


def test_summary_accepts_collection_warning_without_collected_nodeid():
    record = {
        "nonce": "correct", "exit_code": 1,
        "tests": {"collected": 0, "passed": 0, "failed": 0, "errors": 1, "skipped": 0},
        "nodeids": [], "python_version": "3.12.10", "failures": [],
        "warnings": [{"category": "PytestConfigWarning", "message": "config warning",
                      "filename": "pytest.ini", "line": 1, "when": "config", "nodeid": ""}],
        "failures_truncated": False, "warnings_truncated": False,
    }
    assert _parse_summary_record(_SUMMARY_PREFIX + json.dumps(record), "correct", 1) is not None


def test_controlled_runner_keeps_failure_assertion_and_warnings_separate(tmp_path):
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "test_owned_example.py").write_text(
        "import warnings\n"
        "def test_first():\n"
        "    warnings.warn('noise warning')\n"
        "    assert 1 == 2\n"
        "def test_second():\n"
        "    raise RuntimeError('nested failure')\n",
        encoding="utf-8",
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text(
        _RUNNER.replace("/work/checks", checks.as_posix()).replace(
            "/work/pytest.ini", config.as_posix()
        ), encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS="",
                       PYTEST_PLUGINS="", UPGRADE_WORKBENCH_NONCE="failure-nonce")
    result = subprocess.run([sys.executable, str(runner)], capture_output=True, text=True,
                            encoding="utf-8", timeout=30, env=environment, shell=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    parsed = _parse_summary_record(result.stdout, "failure-nonce", result.returncode)
    assert parsed is not None, result.stdout + result.stderr
    assert {item["nodeid"] for item in parsed["failures"]} == {
        "test_owned_example.py::test_first", "test_owned_example.py::test_second"
    }
    first = next(item for item in parsed["failures"] if item["nodeid"].endswith("test_first"))
    assert "assert 1 == 2" in first["message"]
    assert "AssertionError" in first["message"]
    assert parsed["warnings"] and parsed["warnings"][0]["category"] == "UserWarning"
