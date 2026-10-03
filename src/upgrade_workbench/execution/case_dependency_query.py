"""把案例锁定环境接到固定依赖查询；查询收据不形成业务验收证据。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

from ..runtime import assert_no_links
from .cache import RECIPE, atomic_json, content_key
from .docker import DockerExecutor, _validate_requirements, _validate_timeout

MAX_OUTPUT_BYTES = 24_000
_PREFIX = "UPGRADE_WORKBENCH_DEPENDENCY:"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9,_.-]+\])?==([A-Za-z0-9][A-Za-z0-9.!+_-]*)")
_FIELDS = {
    "list_files": {"prefix"},
    "read_file": {"path", "start_line", "end_line"},
    "search_text": {"query", "paths", "max_results"},
    "inspect_symbol": {"module", "qualname"},
}


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8")


def _canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def _relative(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("Dependency path must be bounded relative text")
    path = PurePosixPath(value)
    if (any(char in value for char in "\\:\x00") or path.is_absolute()
            or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts)):
        raise ValueError("Dependency path must be canonical and relative")


def _validate_action(action):
    if not isinstance(action, dict) or not isinstance(action.get("operation"), str):
        raise ValueError("One typed dependency query is required")
    fields = _FIELDS.get(action["operation"])
    if fields is None or set(action) != fields | {"type", "environment", "operation", "distribution"}:
        raise ValueError("Dependency query fields do not match the fixed operation")
    if action["type"] != "query_dependency" or action["environment"] not in ("old", "new"):
        raise ValueError("Dependency query requires an old or new environment")
    if not isinstance(action["distribution"], str) or not _NAME.fullmatch(action["distribution"]):
        raise ValueError("Invalid dependency distribution")
    if action["operation"] == "list_files":
        prefix = action["prefix"]
        if not isinstance(prefix, str) or len(prefix) > 256:
            raise ValueError("Dependency prefix must be bounded text")
        if prefix:
            _relative(prefix.removesuffix("/"))
    elif action["operation"] == "read_file":
        _relative(action["path"])
        start, end = action["start_line"], action["end_line"]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end or end - start >= 200:
            raise ValueError("Dependency read range must contain 1..200 lines")
    elif action["operation"] == "search_text":
        if (not isinstance(action["query"], str) or not 1 <= len(action["query"]) <= 200
                or "\x00" in action["query"]):
            raise ValueError("Dependency search query must be bounded text")
        if not isinstance(action["paths"], list) or not 1 <= len(action["paths"]) <= 64:
            raise ValueError("Dependency search requires 1..64 registered paths")
        for path in action["paths"]:
            _relative(path)
        if type(action["max_results"]) is not int or not 1 <= action["max_results"] <= 50:
            raise ValueError("Dependency search result count must be 1..50")
    else:
        if any(not isinstance(action[key], str) or len(action[key]) > 512 or not _SYMBOL.fullmatch(action[key]) for key in ("module", "qualname")):
            raise ValueError("Dependency module and qualname must be dotted identifiers")


def _locked_versions(lock_bytes, distribution):
    text = lock_bytes.decode("utf-8-sig")
    _validate_requirements(text)
    versions = set()
    for line in text.splitlines():
        match = _PIN.match(line.strip())
        if match and _canonical_name(match.group(1)) == _canonical_name(distribution):
            versions.add(match.group(2))
    if not versions:
        raise ValueError("Distribution is not registered in the selected dependency lock")
    return sorted(versions)


def _artifact(path):
    assert_no_links(path)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _validate_preparation(value, lock_hash, environment, wheel_hashes):
    if (value.get("lock_sha256") != lock_hash or value.get("local_wheel_sha256") != wheel_hashes
            or value.get("recipe") != RECIPE or content_key(value) != value.get("cache_key")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value.get("image_id", ""))):
        raise ValueError("Dependency preparation does not match its frozen inputs")
    if environment is not None and (
        value.get("base_image_digest") != environment.base_image
        or value.get("expected_python") != environment.python_version
    ):
        raise ValueError("Dependency preparation conflicts with the case environment")


def _read_output(execution, config, roots):
    if (execution.get("status") != "passed" or execution.get("exit_code") != 0
            or execution.get("tests") != {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0}
            or not execution.get("cleanup") or not all(row.get("ok") is True for row in execution["cleanup"].values())):
        raise ValueError("Dependency query execution or cleanup did not complete")
    path = Path(execution["stdout"])
    assert_no_links(path)
    if not any(path.resolve().is_relative_to(root) for root in roots) or path.stat().st_size > 2_000_000:
        raise ValueError("Dependency output is outside its execution root or exceeds the log limit")
    lines = [line[len(_PREFIX):] for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(_PREFIX)]
    if len(lines) != 1 or len(lines[0].encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("Dependency query requires one bounded result record")
    record = json.loads(lines[0])
    if set(record) != {"nonce", "query_id", "public"} or record["nonce"] != config["nonce"] or record["query_id"] != config["query_id"]:
        raise ValueError("Dependency output belongs to another query")
    public = record["public"]
    if (not {"status", "operation", "distribution", "installed_version", "python_version", "result", "limitations"} <= set(public)
            or public.get("status") not in {"observed", "unavailable"}
            or public.get("operation") != config["action"]["operation"]
            or public.get("distribution") != config["action"]["distribution"]
            or public.get("python_version") != execution.get("python_version")
            or not isinstance(public.get("limitations"), list)):
        raise ValueError("Dependency output identity is invalid")
    if public["status"] == "observed" and public.get("installed_version") not in config["expected_versions"]:
        raise ValueError("Observed dependency version does not match the selected lock")
    return public


def run_case_dependency_query(
    case, action, directory, executor=None, prepare_timeout=600, query_timeout=60,
):
    """查询单个锁定环境；完成收据先落盘，调用者随后登记事实与任务游标。"""
    from ..cases import load_case
    from ..cases.manifest import read_verified_file

    _validate_action(action)
    _validate_timeout(prepare_timeout)
    _validate_timeout(query_timeout)
    current = load_case(case.manifest_path)
    if current.fingerprint != case.fingerprint:
        raise ValueError("Case changed before dependency query")
    directory = Path(directory).absolute()
    assert_no_links(directory)
    if directory.resolve().is_relative_to(current.root.resolve()):
        raise ValueError("Dependency query output overlaps immutable case")
    lock_name = getattr(current.manifest, action["environment"] + "_lock")
    lock_bytes = read_verified_file(current.root, lock_name, current.manifest.file_hashes[lock_name])
    versions = _locked_versions(lock_bytes, action["distribution"])
    directory.mkdir(parents=True, exist_ok=False)
    report_path = directory / "report.json"
    script_bytes = Path(__file__).with_name("_dependency_query_runner.py").read_bytes()
    query_identity = {
        "case_fingerprint": case.fingerprint, "action": action,
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "runner_sha256": hashlib.sha256(script_bytes).hexdigest(),
    }
    query_id = hashlib.sha256(_json_bytes(query_identity)).hexdigest()
    report = {
        "schema_version": 1, "kind": "dependency_query", **query_identity,
        "query_id": query_id, "report_path": str(report_path), "status": "running",
        "phase": "materialize", "preparation": None, "execution": None, "cleanup": None,
        "public": None, "artifacts": [],
    }
    started = time.monotonic()
    atomic_json(report_path, report)
    try:
        lock_path = directory / "requirements.txt"
        lock_path.write_bytes(lock_bytes)
        inputs = [lock_path]
        wheels = []
        if current.environment:
            for name in current.environment.local_wheels:
                wheel = directory / "assets" / name
                wheel.parent.mkdir(parents=True, exist_ok=True)
                wheel.write_bytes(read_verified_file(current.root, name, current.manifest.file_hashes[name]))
                wheels.append(wheel)
                inputs.append(wheel)
        wheel_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in wheels}
        frozen = [_artifact(path) for path in inputs]
        runner = executor if executor is not None else DockerExecutor(directory / "executor")
        roots = [directory.resolve()]
        if getattr(runner, "work_root", None) is not None:
            roots.append(Path(runner.work_root).resolve())
        report["phase"] = "preparing"
        atomic_json(report_path, report)
        options = ({"base_image": current.environment.base_image, "python_version": current.environment.python_version}
                   if current.environment else {})
        preparation = runner.prepare_environment(lock_path, timeout_seconds=prepare_timeout, local_wheels=wheels, **options)
        report["preparation"] = preparation
        preparation_path = directory / "preparation.json"
        atomic_json(preparation_path, preparation)
        report["artifacts"].append(_artifact(preparation_path))
        _validate_preparation(preparation, report["lock_sha256"], current.environment, wheel_hashes)
        if preparation.get("build_log"):
            build_log = Path(preparation["build_log"])
            assert_no_links(build_log)
            if not any(build_log.resolve().is_relative_to(root) for root in roots):
                raise ValueError("Dependency preparation log is outside its execution root")
            report["artifacts"].append(_artifact(build_log))
        checks, source = directory / "checks", directory / "source"
        checks.mkdir()
        source.mkdir()
        script_path = checks / "test_dependency_query.py"
        script_path.write_bytes(script_bytes)
        config = {"action": action, "expected_versions": versions, "query_id": query_id, "nonce": uuid4().hex}
        parameter_path = checks / "query.json"
        atomic_json(parameter_path, config)
        frozen.extend(_artifact(path) for path in (script_path, parameter_path))
        report["artifacts"].extend(frozen)
        report["phase"] = "querying"
        atomic_json(report_path, report)
        execution = runner.verify(preparation["image_id"], source, checks, timeout_seconds=query_timeout, diagnostic=True)
        report["execution"] = execution
        report["cleanup"] = execution.get("cleanup")
        execution_path = directory / "execution.json"
        atomic_json(execution_path, execution)
        report["artifacts"].append(_artifact(execution_path))
        for name in ("stdout", "stderr", "build_log"):
            if execution.get(name):
                path = Path(execution[name])
                assert_no_links(path)
                if not any(path.resolve().is_relative_to(root) for root in roots):
                    raise ValueError("Dependency receipt path is outside its execution root")
                report["artifacts"].append(_artifact(path))
        if execution.get("image_id") != preparation["image_id"]:
            raise ValueError("Dependency query ran in a different environment")
        public = _read_output(execution, config, roots)
        if not public["python_version"].startswith(preparation["expected_python"] + "."):
            raise ValueError("Dependency query used a different Python version")
        public["environment"] = {
            "role": action["environment"], "lock_sha256": report["lock_sha256"],
            "image_id": preparation["image_id"], "cache_key": preparation["cache_key"],
            "base_image_id": preparation["base_image_id"],
            "python_version": public["python_version"],
            "preparation_sha256": _artifact(preparation_path)["sha256"],
        }
        public["scope_limit"] = "Installed dependency facts only; no candidate source or business acceptance was evaluated"
        if len(_json_bytes(public)) > MAX_OUTPUT_BYTES:
            public.update(status="unavailable", code="result_too_large", result=None)
            public["limitations"] = ["Dependency fact exceeds 24000 bytes; request a narrower query"]
        for frozen_file in frozen:
            if _artifact(Path(frozen_file["path"])) != frozen_file:
                raise ValueError("Dependency query inputs changed during execution")
        if load_case(case.manifest_path).fingerprint != case.fingerprint:
            raise ValueError("Case changed during dependency query")
        report.update(status=public["status"], public=public, phase="completed")
    except Exception as error:
        report.update(status="execution_incomplete", failure={"type": type(error).__name__, "message": str(error)[:1000]})
    finally:
        report["duration_seconds"] = round(time.monotonic() - started, 6)
        atomic_json(report_path, report)
    return report
