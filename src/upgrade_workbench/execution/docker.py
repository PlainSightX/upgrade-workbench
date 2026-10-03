"""隔离验证已复核源码快照；不承诺抵御恶意源码。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..runtime import configured_cache
from .cache import RECIPE, EnvironmentCache, atomic_json, content_key

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BASE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}\Z")
_REPO_DIGEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:\-]*@sha256:[0-9a-f]{64}\Z")
_PINNED_REQUIREMENT = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9,_.-]+\])?"
    r"==[A-Za-z0-9][A-Za-z0-9.!+_-]*(?:\s*;\s*[^\r\n]+)?\Z"
)
_HASH = re.compile(r"\s+--hash=sha256:[0-9a-fA-F]{64}(?=\s|$)")
_PROXIES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_SUMMARY_PREFIX = "UPGRADE_WORKBENCH_RESULT:"


# nonce 只避免普通日志碰撞，并非对恶意被测代码的安全认证。
_RUNNER = r'''"""执行器持有的 pytest 汇总入口。"""
import json
import os
import sys

import pytest


class Summary:
    def __init__(self):
        self.nodeids = []
        self.outcomes = {}
        self.collection_errors = 0
        self.failures = []
        self.warnings = []
        self.failures_truncated = False
        self.warnings_truncated = False

    @staticmethod
    def _bounded(value, limit):
        value = str(value or "")
        if len(value) <= limit:
            return value
        marker = "\n... <truncated by runner> ...\n"
        width = max(1, limit - len(marker))
        left = width // 2
        return value[:left] + marker + value[-(width - left):]

    @staticmethod
    def _safe_path(value):
        value = str(value or "")
        for prefix in ("/work/source/", "/work/checks/"):
            if value.startswith(prefix):
                return value[len(prefix):]
        if value.startswith(("/", "\\")) or ":" in value[:3]:
            return "<execution-path>"
        return value

    def _location(self, report):
        location = getattr(report, "location", None)
        if not isinstance(location, tuple) or len(location) < 2:
            return None
        path, lineno = location[0], location[1]
        return {"path": self._safe_path(path), "line": int(lineno) + 1 if isinstance(lineno, int) else None}

    def _failure(self, report, when=None):
        longrepr = getattr(report, "longreprtext", None)
        if not longrepr:
            longrepr = str(getattr(report, "longrepr", ""))
        phase = when or str(getattr(report, "when", "call") or "call")
        return {
            "nodeid": str(getattr(report, "nodeid", "") or ""),
            "when": phase,
            "outcome": "failed" if phase == "call" else "error",
            "message": self._bounded(longrepr, 8000),
            "location": self._location(report),
        }

    def pytest_collection_finish(self, session):
        self.nodeids = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors += 1
            if len(self.failures) < 32:
                self.failures.append(self._failure(report, when="collect"))
            else:
                self.failures_truncated = True

    def pytest_runtest_logreport(self, report):
        old = self.outcomes.get(report.nodeid)
        if report.failed:
            self.outcomes[report.nodeid] = "failed" if report.when == "call" else "errors"
            if len(self.failures) < 32:
                self.failures.append(self._failure(report))
            else:
                self.failures_truncated = True
        elif report.skipped and old not in ("failed", "errors"):
            self.outcomes[report.nodeid] = "skipped"
        elif report.when == "call" and report.passed and old not in ("failed", "errors", "skipped"):
            self.outcomes[report.nodeid] = "passed"

    def pytest_warning_recorded(self, warning_message, when, nodeid):
        if len(self.warnings) >= 64:
            self.warnings_truncated = True
            return
        message = warning_message.message
        self.warnings.append({
            "category": getattr(message, "__class__", type(message)).__name__,
            "message": self._bounded(message, 1200),
            "filename": self._bounded(self._safe_path(getattr(warning_message, "filename", "")), 400),
            "line": getattr(warning_message, "lineno", None),
            "when": str(when),
            "nodeid": str(nodeid or ""),
        })


summary = Summary()
exit_code = int(pytest.main([
    "-c", "/work/pytest.ini", "--rootdir=/work/checks", "--confcutdir=/work/checks",
    "--import-mode=importlib", "-p", "no:cacheprovider", "-q", "/work/checks",
] + (["--capture=no"] if os.environ.get("UPGRADE_WORKBENCH_DIAGNOSTIC") == "1" else []), plugins=[summary]))
counts = {name: list(summary.outcomes.values()).count(name) for name in ("passed", "failed", "errors", "skipped")}
counts["errors"] += summary.collection_errors
counts["collected"] = len(summary.nodeids)
record = {
    "nonce": os.environ["UPGRADE_WORKBENCH_NONCE"],
    "exit_code": exit_code,
    "tests": counts,
    "nodeids": summary.nodeids,
    "python_version": sys.version.split()[0],
    "failures": summary.failures,
    "warnings": summary.warnings,
    "failures_truncated": summary.failures_truncated,
    "warnings_truncated": summary.warnings_truncated,
}
print("UPGRADE_WORKBENCH_RESULT:" + json.dumps(record, sort_keys=True), flush=True)
raise SystemExit(exit_code)
'''


class DockerUnavailable(RuntimeError):
    """Docker CLI 或 Linux 引擎当前不可用。"""


class DockerExecutor:
    """承担复核快照复制、日志、资源限制和本次容器清理。"""

    def __init__(self, work_root: Path, docker_binary: str = "docker", *, cache_root: Path | None = None) -> None:
        self.work_root = Path(work_root).absolute()
        _reject_links(self.work_root)
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.work_root = self.work_root.resolve(strict=True)
        self.docker_binary = docker_binary
        self.cache_root = cache_root if cache_root is not None else configured_cache(self.work_root)

    def _run(self, arguments: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        """固定参数数组且不启用 shell；Windows 子进程不创建可见窗口。"""
        environment = os.environ.copy()
        if arguments[0] == "build":
            # docker build 默认使用当前 Docker context 的本地驱动，避免继承远程 builder 覆盖。
            environment.pop("BUILDX_BUILDER", None)
            environment.pop("BUILDKIT_HOST", None)
            environment["DOCKER_BUILDKIT"] = "1"
        return subprocess.run(
            [self.docker_binary, *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )

    def probe(self) -> dict[str, Any]:
        try:
            result = self._run(["info", "--format", "{{json .}}"], 10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ready": False, "reason": _redact(str(exc))}
        if result.returncode:
            return {"ready": False, "reason": _redact(result.stderr or "Docker engine unavailable")}
        try:
            info = json.loads(result.stdout)
            if not isinstance(info, dict):
                raise ValueError("Docker info is not an object")
        except (ValueError, TypeError) as exc:
            return {"ready": False, "reason": _redact(str(exc))}
        linux = info.get("OSType") == "linux"
        return {
            "ready": linux,
            "reason": "" if linux else "A Linux Docker engine is required",
            "server_version": info.get("ServerVersion"),
            "os_type": info.get("OSType"),
            "architecture": info.get("Architecture"),
        }

    def _require_ready(self) -> None:
        status = self.probe()
        if not status["ready"]:
            raise DockerUnavailable(status.get("reason") or "Docker is unavailable")

    def _inspect_image(self, reference: str, timeout: float) -> dict[str, Any]:
        result = self._run(["image", "inspect", reference], timeout)
        _require_success(result, "Cannot inspect image")
        images = json.loads(result.stdout)
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
            raise ValueError("Invalid Docker image inspection")
        image = images[0]
        if not _IMAGE_ID.fullmatch(image.get("Id", "")):
            raise ValueError("Docker did not return an immutable image ID")
        config = image.get("Config") or {}
        if config.get("OnBuild") or config.get("Volumes"):
            raise ValueError(
                "Images with ONBUILD instructions or declared volumes are not supported"
            )
        return image

    def prepare_environment(
        self,
        lock_path: Path,
        base_image: str = "python:3.12-slim",
        timeout_seconds: int = 600,
        python_version: str = "3.12",
        local_wheels: list[Path] | tuple[Path, ...] = (),
    ) -> dict[str, Any]:
        """联网安装带 hash 的完整锁文件；成功后登记不可变环境镜像 ID。"""
        _validate_timeout(timeout_seconds)
        if not _BASE_REFERENCE.fullmatch(base_image):
            raise ValueError("Invalid base image reference")
        if not re.fullmatch(r"[0-9]+\.[0-9]+", python_version):
            raise ValueError("python_version must use major.minor form")
        lock = Path(lock_path).absolute()
        _reject_links(lock)
        if not lock.is_file():
            raise ValueError("A regular locked requirements file is required")
        lock_bytes = lock.read_bytes()
        _validate_requirements(lock_bytes.decode("utf-8-sig"))
        wheel_inputs: list[tuple[Path, str, bytes]] = []
        wheel_names: set[str] = set()
        for supplied in local_wheels:
            wheel = Path(supplied).absolute()
            _reject_links(wheel)
            if not wheel.is_file() or wheel.suffix != ".whl":
                raise ValueError("local_wheels must contain regular .whl files")
            key = wheel.name.casefold()
            if key in wheel_names:
                raise ValueError("local_wheels must have unique filenames")
            wheel_names.add(key)
            contents = wheel.read_bytes()
            wheel_inputs.append((wheel, hashlib.sha256(contents).hexdigest(), contents))
        # 缓存键忽略映射顺序，安装命令也必须稳定；新配方不借用旧的非规范顺序镜像。
        wheel_inputs.sort(key=lambda item: item[0].name)
        wheel_hashes = {path.name: digest for path, digest, _ in wheel_inputs}
        self._require_ready()
        started = time.monotonic()
        deadline = started + timeout_seconds
        operation = uuid4().hex
        directory = self._directory("environments", operation)
        context = directory / "context"
        context.mkdir()
        tag = f"upgrade-workbench-env:{operation}"
        log = directory / "build.log"
        records: list[str] = []
        registered = False
        built_here = False
        cache_lock = None
        try:
            inspect = self._run(["image", "inspect", base_image], _remaining(deadline))
            if inspect.returncode:
                pull = self._run(["pull", base_image], _remaining(deadline))
                records.extend((pull.stdout, pull.stderr))
                _require_success(pull, "Base image pull failed")
            base = self._inspect_image(base_image, _remaining(deadline))
            candidates = ([base_image] if _REPO_DIGEST.fullmatch(base_image) else []) + (
                base.get("RepoDigests") or []
            )
            base_digest = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, str) and _REPO_DIGEST.fullmatch(item)
                ),
                None,
            )
            if base_digest is None:
                raise ValueError(
                    "Base image requires a registry RepoDigest; mutable tags and local image IDs cannot pin BuildKit input"
                )
            if self._inspect_image(base_digest, _remaining(deadline))["Id"] != base["Id"]:
                raise ValueError("Base registry digest does not identify the inspected base image")
            platform = _image_platform(base)
            lock_hash = hashlib.sha256(lock_bytes).hexdigest()
            identity = {"base_image_id": base["Id"], "base_image_digest": base_digest,
                        "platform": platform, "expected_python": python_version,
                        "lock_sha256": lock_hash, "local_wheel_sha256": wheel_hashes,
                        "recipe": RECIPE}
            key = content_key(identity)
            tag = f"upgrade-workbench-env:{key}"
            cache = EnvironmentCache(self.cache_root) if self.cache_root is not None else None
            if cache is not None:
                cache_lock = cache.lock(key)
                cache_lock.acquire(timeout=_remaining(deadline))
                previous = cache.read(key)
                if previous is not None:
                    try:
                        cached = self._inspect_image(previous["image_id"], _remaining(deadline))
                    except RuntimeError:
                        cached = None  # 缺失镜像重建；损坏身份或索引则拒绝，不信任同名标签。
                    if cached is not None:
                        self._check_cached_image(cached, previous, key, platform)
                        report = previous | {"reused": True, "cache_hit": True,
                            "requested_base_image": base_image, "build_log": str(log),
                            "duration_seconds": round(time.monotonic() - started, 6),
                            "dependency_download_allowed": False}
                        atomic_json(self._directory("prepared-images") / f"{cached['Id'].split(':')[1]}.json", report)
                        registered = True
                        records.append("Borrowed verified dependency environment " + cached["Id"])
                        return report
            registry_root = self.work_root / "prepared-images"
            _reject_links(registry_root)
            if registry_root.is_dir():
                for previous_path in sorted(registry_root.glob("*.json")):
                    _reject_links(previous_path)
                    try:
                        previous = json.loads(previous_path.read_text(encoding="utf-8"))
                    except (ValueError, OSError):
                        continue
                    if not isinstance(previous, dict) or (
                        previous.get("cache_key") != key
                        or previous.get("recipe") != RECIPE
                        or previous.get("lock_sha256") != lock_hash
                        or previous.get("base_image_id") != base["Id"]
                        or previous.get("base_image_digest") != base_digest
                        or previous.get("platform") != platform
                        or previous.get("expected_python") != python_version
                        or previous.get("local_wheel_sha256", {}) != wheel_hashes
                        or not _IMAGE_ID.fullmatch(previous.get("image_id", ""))
                        or previous_path.stem != previous["image_id"].split(":")[1]
                        or previous.get("image_tag") != tag
                    ):
                        continue
                    try:
                        cached = self._inspect_image(previous["image_id"], _remaining(deadline))
                    except (RuntimeError, ValueError):
                        continue
                    if cached["Id"] == previous["image_id"] and _image_platform(cached) == platform:
                        self._check_cached_image(cached, previous, key, platform)
                        if cache is not None:
                            cache.publish(key, previous)
                        # 只复用固定锁及基础镜像相同的依赖环境；每次仍重新执行应用检查。
                        registered = True
                        records.append("Reused verified immutable environment " + cached["Id"])
                        return {
                            **previous, "reused": True, "requested_base_image": base_image,
                            "reuse_record": str(previous_path), "build_log": str(log),
                            "duration_seconds": round(time.monotonic() - started, 6),
                            "dependency_download_allowed": False,
                        }
            (context / "requirements.lock").write_bytes(lock_bytes)
            if wheel_inputs:
                wheel_dir = context / "wheels"
                wheel_dir.mkdir()
                for path, _, contents in wheel_inputs:
                    (wheel_dir / path.name).write_bytes(contents)
            local_wheel_install = ""
            if wheel_inputs:
                exact_wheel_paths = " ".join(
                    shlex.quote(f"/opt/upgrade-workbench/wheels/{path.name}")
                    for path, _, _ in wheel_inputs
                )
                # 先安装已核验的确切字节，避免同版本的索引 wheel 抢占本地构建。
                local_wheel_install = (
                    "&& python -m pip --isolated install --disable-pip-version-check "
                    "--no-cache-dir --no-index --no-deps "
                    f"{exact_wheel_paths} "
                )
            expected = tuple(int(part) for part in python_version.split("."))
            (context / "Dockerfile").write_text(
                f"FROM {base_digest}\n"
                "USER 0:0\n"
                "COPY requirements.lock /opt/upgrade-workbench/requirements.lock\n"
                + ("COPY wheels /opt/upgrade-workbench/wheels\n" if wheel_inputs else "")
                + f'RUN python -c "import sys; assert sys.version_info[:2] == {expected!r}, '
                f"'Python {python_version} is required; got ' + sys.version; print('Python runtime:', sys.version)\" "
                + local_wheel_install
                + "&& python -m pip --isolated install --disable-pip-version-check --no-cache-dir "
                "--require-hashes --only-binary=:all: -r /opt/upgrade-workbench/requirements.lock\n"
                f"LABEL project=upgrade-workbench org.upgrade-workbench.kind=environment org.upgrade-workbench.content={key} org.upgrade-workbench.recipe={RECIPE}\n"
                "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1\n"
                "HEALTHCHECK NONE\nENTRYPOINT []\nWORKDIR /work\nUSER 65532:65532\n",
                encoding="utf-8",
            )
            built_here = True
            result = self._run(
                [
                    "build",
                    "--pull=false",
                    "--load",
                    "--platform",
                    platform,
                    "--force-rm",
                    "--network=default",
                    *_proxy_build_arguments(),
                    "--tag",
                    tag,
                    str(context),
                ],
                _remaining(deadline),
            )
            records.extend((result.stdout, result.stderr))
            _require_success(result, "Environment image build failed")
            image = self._inspect_image(tag, _remaining(deadline))
            report = {
                "cache_key": key, "recipe": RECIPE, "cache_hit": False,
                "image_id": image["Id"],
                "image_tag": tag,
                "base_image": base_image,
                "base_image_id": base["Id"],
                "base_image_digest": base_digest,
                "platform": platform,
                "builder": "docker-default-driver",
                "expected_python": python_version,
                "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
                "local_wheel_sha256": wheel_hashes,
                "build_log": str(log),
                "duration_seconds": round(time.monotonic() - started, 6),
                "dependency_download_allowed": True,
            }
            self._check_cached_image(image, report, key, platform)
            registry = self._directory("prepared-images") / f"{image['Id'].split(':')[1]}.json"
            temporary = registry.with_suffix(f".{operation}.tmp")
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(registry)
            if cache is not None:
                cache.publish(key, report)
            registered = True
            return report
        except subprocess.TimeoutExpired as exc:
            records.extend(
                (_text(exc.stdout), _text(exc.stderr), "Environment preparation timed out")
            )
            raise TimeoutError(f"Environment preparation timed out; log: {log}") from exc
        except Exception as exc:
            records.append(_redact(str(exc)))
            raise
        finally:
            log.write_text(_redact("\n".join(records)), encoding="utf-8")
            if not registered and built_here:
                self._cleanup(["image", "rm", "--force", tag])
            if cache_lock is not None:
                cache_lock.release()

    @staticmethod
    def _check_cached_image(image, record, key, platform):
        labels = (image.get("Config") or {}).get("Labels") or {}
        if (image["Id"] != record["image_id"] or _image_platform(image) != platform
                or labels.get("project") != "upgrade-workbench"
                or labels.get("org.upgrade-workbench.content") != key
                or labels.get("org.upgrade-workbench.recipe") != RECIPE
                or record.get("image_tag") != f"upgrade-workbench-env:{key}"):
            raise ValueError("environment_image_identity_conflict")

    def verify(
        self,
        image_id: str,
        source_dir: Path,
        checks_dir: Path,
        timeout_seconds: int = 60,
        postgres: dict | None = None,
        diagnostic: bool = False,
    ) -> dict[str, Any]:
        """离线构建复核快照并运行测试，返回日志位置及结构化判定。"""
        _validate_timeout(timeout_seconds)
        if type(diagnostic) is not bool:
            raise ValueError("Diagnostic mode must be boolean")
        if not _IMAGE_ID.fullmatch(image_id):
            raise ValueError("verify requires a prepared immutable sha256 image ID")
        record_path = self.work_root / "prepared-images" / f"{image_id.split(':')[1]}.json"
        _reject_links(record_path)
        if not record_path.is_file():
            raise ValueError("Image was not prepared by this executor work root")
        environment = json.loads(record_path.read_text(encoding="utf-8"))
        expected_python = environment.get("expected_python")
        if not isinstance(expected_python, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+", expected_python
        ):
            raise ValueError("Prepared environment has an invalid expected Python version")
        tag = environment.get("image_tag", "")
        if environment.get("image_id") != image_id or not re.fullmatch(
            r"upgrade-workbench-env:(?:[0-9a-f]{32}|[0-9a-f]{64})", tag
        ):
            raise ValueError("Invalid prepared environment record")
        self._require_ready()
        if self._inspect_image(image_id, 10)["Id"] != image_id:
            raise ValueError("Prepared environment no longer matches its registered image ID")
        operation = uuid4().hex
        directory = self._directory("runs", operation)
        context = directory / "context"
        context.mkdir()
        _copy_snapshot(Path(source_dir), context / "source")
        _copy_snapshot(Path(checks_dir), context / "checks")
        (context / "runner.py").write_text(_RUNNER, encoding="utf-8")
        (context / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        for entry in context.iterdir():
            entry.chmod(0o755 if entry.is_dir() else 0o644)
        container = f"upgrade-workbench-verify-{operation}"
        materialization_container = f"upgrade-workbench-materialize-{operation}"
        snapshot_tag = f"upgrade-workbench-snapshot:{operation}"
        nonce = uuid4().hex
        stdout_path, stderr_path = directory / "stdout.log", directory / "stderr.log"
        started = time.monotonic()
        deadline = started + timeout_seconds
        safety = {
            "reviewed_snapshots_only": True,
            "network": "none",
            "user": "65532:65532",
            "read_only": True,
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "pids_limit": 128,
            "memory": "512m",
            "memory_swap": "512m",
            "cpus": "1",
            "tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"},
            "host_mounts": [],
            "host_keys_or_docker_socket": False,
            "timeout_seconds": timeout_seconds,
            "pytest_plugin_autoload": False,
            "pythonpath": "/work/source/src:/work/source",
            "diagnostic_output": diagnostic,
        }
        report: dict[str, Any] = {
            "status": "error",
            "exit_code": None,
            "tests": {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "nodeids": [],
            "failure_details": [],
            "warning_details": [],
            "failure_details_truncated": False,
            "warning_details_truncated": False,
            "image_id": image_id,
            "expected_python": expected_python,
            "python_version": None,
            "snapshot_image_id": None,
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "build_log": str(directory / "build.log"),
            "safety": safety,
            "container_name": container,
            "snapshot_materialization": "stopped-container-copy-commit",
            "materialization_container_name": materialization_container,
            "materialization_commands": [],
        }
        database = None
        if postgres is not None:
            from .postgres import EphemeralPostgres

            database = EphemeralPostgres(self, operation, postgres)
            report["database"] = database.report
        # 在目标进程启动前留下确定的资源身份；硬退出后只能清理本次对象。
        report["snapshot_image_tag"] = snapshot_tag
        report["ownership"] = {"project": "upgrade-workbench", "operation": operation}
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        stdout, stderr, build_log = "", "", ""
        try:
            if database is not None:
                database.start(deadline)
                safety["network"] = "isolated_postgres_loopback_no_external_network"
            # create 不执行 ENTRYPOINT/CMD；cp 与 commit 均由 daemon 操作停止状态文件系统。
            materialize = [
                [
                    "create",
                    "--pull=never",
                    "--name",
                    materialization_container,
                    "--label",
                    "org.upgrade-workbench.kind=materialization",
                    "--label=project=upgrade-workbench",
                    f"--label=org.upgrade-workbench.operation={operation}",
                    "--network=none",
                    "--user=65532:65532",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    "--entrypoint=python",
                    *[f"--env={key}=" for key in _PROXIES],
                    image_id,
                    "-c",
                    "pass",
                ],
                *[
                    ["cp", str(context / name), f"{materialization_container}:/work/{name}"]
                    for name in ("source", "checks", "runner.py", "pytest.ini")
                ],
                [
                    "commit",
                    "--change",
                    f"LABEL project=upgrade-workbench org.upgrade-workbench.kind=snapshot org.upgrade-workbench.operation={operation}",
                    materialization_container,
                    snapshot_tag,
                ],
            ]
            for arguments in materialize:
                report["materialization_commands"].append(arguments)
                copied = self._run(arguments, _remaining(deadline))
                build_log += copied.stdout + "\n" + copied.stderr + "\n"
                _require_success(copied, f"Snapshot materialization failed during {arguments[0]}")
            snapshot_id = self._inspect_image(snapshot_tag, _remaining(deadline))["Id"]
            if copied.stdout.strip() != snapshot_id:
                raise ValueError(
                    "Committed snapshot tag does not match the returned immutable image ID"
                )
            report["snapshot_image_id"] = snapshot_id
            command = [
                "run",
                "--pull=never",
                "--name",
                container,
                "--label",
                "org.upgrade-workbench.kind=verification",
                "--label=project=upgrade-workbench",
                f"--label=org.upgrade-workbench.operation={operation}",
                "--network=" + ("container:" + database.name if database is not None else "none"),
                "--user=65532:65532",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pids-limit=128",
                "--memory=512m",
                "--memory-swap=512m",
                "--cpus=1",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                "--ipc=private",
                "--log-driver=none",
                "--no-healthcheck",
                "--workdir=/work",
                "--entrypoint=python",
                "--env=HOME=/tmp",
                "--env=PYTHONDONTWRITEBYTECODE=1",
                "--env=PYTHONUNBUFFERED=1",
                "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "--env=PYTEST_ADDOPTS=",
                "--env=PYTEST_PLUGINS=",
                "--env=PYTHONPATH=/work/source/src:/work/source",
                f"--env=UPGRADE_WORKBENCH_NONCE={nonce}",
                f"--env=UPGRADE_WORKBENCH_DIAGNOSTIC={int(diagnostic)}",
                *(["--env=UPGRADE_WORKBENCH_DATABASE_URL=postgresql+psycopg2://workbench@127.0.0.1:5432/workbench",
                   "--env=UPGRADE_WORKBENCH_PG_DSN=dbname=workbench user=workbench host=127.0.0.1 port=5432 connect_timeout=3"]
                  if database is not None else []),
                *[f"--env={key}=" for key in _PROXIES],
                snapshot_id,
                "/work/runner.py",
            ]
            report["command"] = command
            result = self._run(command, _remaining(deadline))
            stdout, stderr = result.stdout, result.stderr
            summary_record = _parse_summary_record(stdout, nonce, result.returncode)
            if summary_record is None:
                report["exit_code"] = result.returncode
                report["reason"] = "Missing or invalid machine-readable pytest result"
            else:
                _apply_summary_record(report, summary_record, expected_python, result.returncode)
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _text(exc.stdout), _text(exc.stderr)
            timed_out_summary = _parse_timeout_summary_record(stdout, nonce)
            if timed_out_summary is None:
                report.update(
                    status="timeout",
                    reason="Verification exceeded its time limit",
                    process_exit_status="timeout_without_complete_result",
                )
            else:
                exit_code, summary_record = timed_out_summary
                _apply_summary_record(report, summary_record, expected_python, exit_code)
                report.update(
                    verification_status=report["status"],
                    process_exit_status="timeout_after_complete_result",
                    process_exit_reason=(
                        "The controlled pytest runner emitted a complete result, but the "
                        "container process did not exit before the deadline"
                    ),
                )
                if "execution_status" in report:
                    report["execution_status"] = "incomplete"
                # 失败观测仍可用于诊断；通过结果不能掩盖目标进程未正常退出。
                if report["status"] == "passed":
                    report.update(
                        status="error",
                        reason="Verification process did not exit after a complete passing result",
                    )
        except TimeoutError as exc:
            report.update(status="timeout", reason=str(exc))
        except Exception as exc:
            stderr = _redact(str(exc))
            report["reason"] = stderr
        finally:
            # CLI 超时后容器仍可能运行；只清理本次随机名字。
            report["cleanup"] = {
                "container": self._cleanup(["rm", "--force", container]),
                "materialization_container": self._cleanup(
                    ["rm", "--force", materialization_container]
                ),
                "snapshot_image": self._cleanup(["image", "rm", "--force", snapshot_tag]),
            }
            if database is not None:
                report["cleanup"]["database"] = database.cleanup()
            if not all(item["ok"] for item in report["cleanup"].values()):
                report["verification_status"] = report["status"]
                report.update(
                    status="error", reason="Cleanup did not complete; inspect cleanup details"
                )
                if "execution_status" in report:
                    report["execution_status"] = "incomplete"
            stdout_path.write_text(_redact(stdout), encoding="utf-8")
            stderr_path.write_text(_redact(stderr), encoding="utf-8")
            (directory / "build.log").write_text(_redact(build_log), encoding="utf-8")
            report["duration_seconds"] = round(time.monotonic() - started, 6)
            (directory / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return report

    def _directory(self, *parts: str) -> Path:
        directory = self.work_root.joinpath(*parts)
        _reject_links(directory)
        if not directory.resolve().is_relative_to(self.work_root):
            raise ValueError("Work directory escapes the executor work root")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _cleanup(self, command: list[str]) -> dict[str, Any]:
        try:
            result = self._run(command, 10)
            absent = "No such container" in result.stderr or "No such image" in result.stderr
            return {"ok": result.returncode == 0 or absent, "detail": _redact(result.stderr)}
        except Exception as exc:
            return {"ok": False, "detail": _redact(str(exc))}


def _validate_timeout(timeout: int) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("timeout_seconds must be a positive integer")


def _image_platform(image: dict[str, Any]) -> str:
    os_type, architecture, variant = (
        image.get("Os"),
        image.get("Architecture"),
        image.get("Variant"),
    )
    if (
        os_type != "linux"
        or not isinstance(architecture, str)
        or not re.fullmatch(r"[a-z0-9_]+", architecture)
    ):
        raise ValueError("Base image must identify a Linux architecture")
    if variant and (not isinstance(variant, str) or not re.fullmatch(r"[a-z0-9_]+", variant)):
        raise ValueError("Invalid base image architecture variant")
    return f"linux/{architecture}" + (f"/{variant}" if variant else "")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Operation exceeded its time limit")
    return remaining


def _proxy_build_arguments() -> list[str]:
    return [item for name in _PROXIES for item in ("--build-arg", f"{name}=")]


def _require_success(result: subprocess.CompletedProcess[str], message: str) -> None:
    if result.returncode:
        raise RuntimeError(f"{message}: {_redact(result.stderr)}")


def _text(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def _redact(text: str) -> str:
    value = _text(text)
    for key, secret in os.environ.items():
        if len(secret) >= 8 and re.search(r"token|secret|password|api.?key", key, re.IGNORECASE):
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(https?://)[^\s/@]+@", r"\1[REDACTED]@", value, flags=re.IGNORECASE)
    value = re.sub(
        r"((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s&\"']+",
        r"\1[REDACTED]",
        value,
        flags=re.IGNORECASE,
    )
    return value[:2_000_000]


def _reject_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (
            hasattr(candidate, "is_junction") and candidate.is_junction()
        ):
            raise ValueError(f"Links and junctions are forbidden in snapshots: {candidate}")


def _copy_snapshot(source: Path, target: Path) -> None:
    origin = source.absolute()
    _reject_links(origin)
    if not origin.is_dir():
        raise ValueError("A snapshot must be a regular directory")
    origin = origin.resolve(strict=True)
    if target.resolve().is_relative_to(origin):
        raise ValueError("Snapshot source must not contain the executor output directory")
    target.mkdir()
    for directory, directories, files in os.walk(origin, followlinks=False):
        parent = Path(directory)
        for name in sorted(directories + files):
            entry = parent / name
            _reject_links(entry)
            if not entry.resolve(strict=True).is_relative_to(origin):
                raise ValueError(f"Snapshot path escapes its root: {entry}")
            mode = entry.stat(follow_symlinks=False).st_mode
            destination = target / entry.relative_to(origin)
            if stat.S_ISDIR(mode):
                destination.mkdir(exist_ok=True)
                destination.chmod(0o755)
            elif stat.S_ISREG(mode):
                shutil.copyfile(entry, destination, follow_symlinks=False)
                # 源主机文件模式不决定 Linux 非 root 进程能否读取快照。
                destination.chmod(0o644)
            else:
                raise ValueError(f"Only regular files and directories are allowed: {entry}")


def _validate_requirements(text: str) -> None:
    logical: list[str] = []
    pending = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        pending += " " + (line[:-1].rstrip() if continued else line)
        if not continued:
            logical.append(pending.strip())
            pending = ""
    if pending or not logical:
        raise ValueError("Requirements must be a complete, nonempty hash-locked export")
    names = set()
    for requirement in logical:
        hashes = _HASH.findall(requirement)
        pinned = _HASH.sub("", requirement).strip()
        match = _PINNED_REQUIREMENT.fullmatch(pinned)
        if not hashes or not match or "--" in pinned or " @ " in pinned:
            raise ValueError(
                "Each requirement must use an exact version and SHA256 hashes; options and URLs are forbidden"
            )
        names.add(match.group(1).lower().replace("_", "-"))
    if "pytest" not in names:
        raise ValueError("The prepared requirements must include pinned, hash-locked pytest")


def _parse_summary_record(stdout: str, nonce: str, exit_code: int) -> dict[str, Any] | None:
    records = [
        line[len(_SUMMARY_PREFIX) :]
        for line in stdout.splitlines()
        if line.startswith(_SUMMARY_PREFIX)
    ]
    if len(records) != 1:
        return None
    try:
        record = json.loads(records[0])
        if (
            not isinstance(record, dict)
            or record.get("nonce") != nonce
            or record.get("exit_code") != exit_code
        ):
            return None
        tests, nodeids = record["tests"], record["nodeids"]
        python_version = record.get("python_version")
        if not isinstance(python_version, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+-]*", python_version
        ):
            return None
        names = ("collected", "passed", "failed", "errors", "skipped")
        if not isinstance(tests, dict) or set(tests) != set(names):
            return None
        if any(type(tests[name]) is not int or tests[name] < 0 for name in names):
            return None
        if not isinstance(nodeids, list) or any(
            not isinstance(item, str) or not item for item in nodeids
        ):
            return None
        if len(nodeids) != tests["collected"] or len(set(nodeids)) != len(nodeids):
            return None
        # collection errors 可发生在尚未形成测试项的模块，不计入此上限。
        if tests["passed"] + tests["failed"] + tests["skipped"] > tests["collected"]:
            return None
        if exit_code == 0 and sum(tests[name] for name in names[1:]) != tests["collected"]:
            return None
        failures = record.get("failures", [])
        warnings = record.get("warnings", [])
        failure_limit = 32
        warning_limit = 64
        if (
            not isinstance(failures, list)
            or len(failures) > failure_limit
            or not isinstance(warnings, list)
            or len(warnings) > warning_limit
            or any(not isinstance(item, dict) for item in failures + warnings)
            or type(record.get("failures_truncated", False)) is not bool
            or type(record.get("warnings_truncated", False)) is not bool
        ):
            return None
        for item in failures:
            if (
                set(item) - {"nodeid", "when", "outcome", "message", "location"}
                or not isinstance(item.get("nodeid"), str)
                or len(item["nodeid"]) > 512
                or not isinstance(item.get("when"), str)
                or item.get("when") not in {"collect", "setup", "call", "teardown"}
                or item.get("outcome") not in {"failed", "error"}
                or not isinstance(item.get("message"), str)
                or not item.get("message")
                or len(item["message"]) > 8000
                or (item["when"] != "collect" and item["nodeid"] not in nodeids)
                or (item.get("location") is not None and (
                    not isinstance(item["location"], dict)
                    or set(item["location"]) != {"path", "line"}
                    or not isinstance(item["location"]["path"], str)
                    or not item["location"]["path"]
                    or len(item["location"]["path"]) > 512
                    or item["location"]["path"].startswith(("/", "\\"))
                    or ":" in item["location"]["path"]
                    or ".." in item["location"]["path"].replace("\\", "/").split("/")
                    or (item["location"]["line"] is not None and (
                        type(item["location"]["line"]) is not int or item["location"]["line"] < 1
                    ))
                ))
            ):
                return None
        for item in warnings:
            if (
                set(item) - {"category", "message", "filename", "line", "when", "nodeid"}
                or not isinstance(item.get("category"), str)
                or not item["category"]
                or len(item["category"]) > 128
                or not isinstance(item.get("message"), str)
                or len(item["message"]) > 1200
                or not isinstance(item.get("filename"), str)
                or len(item["filename"]) > 512
                or item["filename"].startswith(("/", "\\"))
                or ":" in item["filename"]
                or ".." in item["filename"].replace("\\", "/").split("/")
                or (item.get("line") is not None and (
                    type(item["line"]) is not int or item["line"] < 1
                ))
                or item.get("when") not in {"config", "collect", "runtest"}
                or not isinstance(item.get("nodeid"), str)
                or len(item["nodeid"]) > 512
                or (item["when"] == "runtest" and item["nodeid"] and item["nodeid"] not in nodeids)
            ):
                return None
        return {
            "tests": {name: tests[name] for name in names},
            "nodeids": sorted(nodeids),
            "python_version": python_version,
            "failures": failures,
            "warnings": warnings,
            "failures_truncated": record.get("failures_truncated", False),
            "warnings_truncated": record.get("warnings_truncated", False),
        }
    except (ValueError, KeyError, TypeError):
        return None


def _parse_timeout_summary_record(
    stdout: str, nonce: str
) -> tuple[int, dict[str, Any]] | None:
    """仅接受超时输出中唯一、绑定当前执行且内部完整的 pytest 结果。"""
    records = [
        line[len(_SUMMARY_PREFIX) :]
        for line in stdout.splitlines()
        if line.startswith(_SUMMARY_PREFIX)
    ]
    if len(records) != 1:
        return None
    try:
        raw = json.loads(records[0])
        exit_code = raw.get("exit_code")
        if type(exit_code) is not int or exit_code < 0 or exit_code > 5:
            return None
    except (AttributeError, json.JSONDecodeError, TypeError):
        return None
    record = _parse_summary_record(stdout, nonce, exit_code)
    if record is None:
        return None
    return exit_code, record


def _apply_summary_record(
    report: dict[str, Any],
    summary_record: dict[str, Any],
    expected_python: str,
    exit_code: int,
) -> None:
    """把已经校验的 runner 结果投影为验证结论，不处理进程退出语义。"""
    counts = summary_record["tests"]
    python_version = summary_record["python_version"]
    report.update(
        exit_code=exit_code,
        tests=counts,
        nodeids=summary_record["nodeids"],
        python_version=python_version,
        failure_details=summary_record["failures"],
        warning_details=summary_record["warnings"],
        failure_details_truncated=summary_record["failures_truncated"],
        warning_details_truncated=summary_record["warnings_truncated"],
    )
    if not python_version.startswith(expected_python + "."):
        report["reason"] = f"Python {expected_python} is required; got {python_version}"
    elif counts["errors"] and exit_code in (0, 1, 2):
        report.update(status="failed", reason="Test collection, setup, or teardown failed")
    elif counts["collected"] == 0:
        report["reason"] = "No tests were collected"
    elif exit_code in (2, 3, 4, 5):
        report["reason"] = "Pytest or container execution infrastructure error"
    elif counts["failed"] or exit_code:
        report["status"] = "failed"
    elif counts["passed"] == 0:
        report["reason"] = "No tests passed (all selected tests were skipped)"
        # 完整退出但没有执行测试；保留不通过判定，诊断层另行判断观察是否可用。
        report.update(execution_status="completed", test_outcome="all_skipped")
    else:
        report["status"] = "passed"


def _parse_summary(
    stdout: str, nonce: str, exit_code: int
) -> tuple[dict[str, int], list[str], str] | None:
    """保留旧调用者的三元组接口；结构化失败详情由内部解析器提供。"""
    record = _parse_summary_record(stdout, nonce, exit_code)
    if record is None:
        return None
    return record["tests"], record["nodeids"], record["python_version"]
