"""复制到受限容器的固定查询脚本；不接受模型代码，也不读取应用源码。"""

from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import importlib.metadata
import inspect
import json
import re
import stat
import sys
from pathlib import Path, PurePosixPath

PREFIX = "UPGRADE_WORKBENCH_DEPENDENCY:"
MAX_OUTPUT_BYTES = 24_000
MAX_FILE_BYTES = 2_000_000


def _encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8")


def _canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def _safe_name(value):
    path = PurePosixPath(value)
    return bool(value) and not any(char in value for char in "\\:\x00") and (
        not path.is_absolute() and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _owned_files(distribution):
    """RECORD 中的脚本相对路径可能越过 site-packages；这些条目不成为查询入口。"""
    root = Path(distribution.locate_file("")).resolve(strict=True)
    result = {}
    for entry in distribution.files or ():
        name = str(entry)
        if not _safe_name(name):
            continue
        candidate = Path(distribution.locate_file(entry))
        if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents):
            continue
        if not candidate.exists():
            continue
        if not candidate.resolve(strict=True).is_relative_to(root):
            continue
        if not stat.S_ISREG(candidate.lstat().st_mode):
            continue
        result[name] = candidate
    return result


def _sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            value.update(chunk)
    return value.hexdigest()


def _unavailable(result, code, message):
    result.update(status="unavailable", code=code, result=None)
    result["limitations"].append(message)
    return result


def _text(path):
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Dependency file exceeds the source byte limit")
    return path.read_text(encoding="utf-8")


def _inspect(action, files, result):
    module_name = action["module"]
    relative = module_name.replace(".", "/")
    module_paths = {relative + ".py", relative + "/__init__.py"}
    module_paths.update(relative + suffix for suffix in importlib.machinery.EXTENSION_SUFFIXES)
    owned_modules = {files[name].resolve() for name in module_paths if name in files}
    if not owned_modules:
        return _unavailable(result, "module_not_owned", "Requested module is not owned by this distribution")
    module = importlib.import_module(module_name)
    actual_file = getattr(module, "__file__", None)
    if not actual_file or Path(actual_file).resolve() not in owned_modules:
        return _unavailable(result, "module_origin_mismatch", "Imported module differs from registered distribution files")
    value = module
    try:
        for part in action["qualname"].split("."):
            value = inspect.getattr_static(value, part)
            if isinstance(value, (staticmethod, classmethod)):
                value = value.__func__
    except AttributeError:
        return _unavailable(result, "symbol_not_found", "Requested symbol is absent from the installed module")

    signature = None
    try:
        signature = str(inspect.signature(value))
    except (TypeError, ValueError):
        result["limitations"].append("Runtime signature is unavailable for this object")
    source_file = None
    try:
        source_file = inspect.getsourcefile(value)
    except TypeError:
        pass
    origins = {path.resolve(): name for name, path in files.items()}
    source = None
    if source_file:
        source_path = Path(source_file).resolve()
        if source_path not in origins:
            return _unavailable(result, "symbol_origin_mismatch", "Symbol source belongs to another distribution")
        source = {"path": origins[source_path], "sha256": _sha256(source_path)}
    else:
        result["limitations"].append("Python source is unavailable; module ownership is verified")
    mro = None
    if inspect.isclass(value):
        mro = [base.__module__ + "." + base.__qualname__ for base in inspect.getmro(value)]
    result["result"] = {
        "module": module_name, "qualname": action["qualname"], "signature": signature,
        "object_type": type(value).__module__ + "." + type(value).__qualname__,
        "mro": mro, "source": source,
        "module_source": {"path": origins[Path(actual_file).resolve()], "sha256": _sha256(Path(actual_file))},
        "attributes": sorted(name for name in vars(value) if not name.startswith("_"))[:100]
        if hasattr(value, "__dict__") else [],
    }
    return result


def query(config):
    """返回环境事实；脚本成功不代表任何应用行为合同成立。"""
    action = config["action"]
    result = {
        "status": "observed", "operation": action["operation"],
        "distribution": action["distribution"], "installed_version": None,
        "python_version": sys.version.split()[0], "result": None, "limitations": [],
    }
    try:
        distribution = importlib.metadata.distribution(action["distribution"])
    except importlib.metadata.PackageNotFoundError:
        return _unavailable(result, "distribution_not_installed", "Selected distribution is not installed")
    if _canonical_name(distribution.metadata["Name"] or "") != _canonical_name(action["distribution"]):
        return _unavailable(result, "distribution_identity_mismatch", "Installed distribution name does not match the query")
    version = distribution.version
    if not isinstance(version, str) or not version or len(version) > 128:
        return _unavailable(result, "installed_version_unavailable", "Installed version metadata is missing or malformed")
    result["installed_version"] = version
    if version not in config["expected_versions"]:
        return _unavailable(result, "installed_version_mismatch", "Installed version differs from the selected hash-locked requirements")
    try:
        files = _owned_files(distribution)
        if not files:
            return _unavailable(result, "file_manifest_unavailable", "No regular distribution-owned files are available")
        operation = action["operation"]
        if operation == "inspect_symbol":
            return _inspect(action, files, result)
        if operation == "read_file":
            path = files.get(action["path"])
            if path is None:
                return _unavailable(result, "file_not_owned", "Requested path is not a registered distribution file")
            lines = _text(path).splitlines(keepends=True)
            if action["start_line"] > len(lines):
                return _unavailable(result, "read_range_out_of_bounds", "Requested start line is past the end of the file")
            end = min(action["end_line"], len(lines))
            result["result"] = {
                "path": action["path"], "sha256": _sha256(path),
                "start_line": action["start_line"], "end_line": end, "eof": end == len(lines),
                "text": "".join(lines[action["start_line"] - 1:end]),
            }
            return result
        if operation == "list_files":
            names = [name for name in sorted(files) if name.startswith(action["prefix"])]
            rows = []
            result["result"] = {"files": rows, "total": len(names), "truncated": False}
            for name in names:
                rows.append({"path": name, "sha256": _sha256(files[name])})
                if len(_encoded(result)) > MAX_OUTPUT_BYTES - 2_000:
                    rows.pop()
                    result["result"].update(truncated=True, next_path=name)
                    result["limitations"].append("File listing is bounded; narrow the prefix to continue")
                    break
            return result
        matches = []
        result["result"] = {"matches": matches, "truncated": False, "search_semantics": "case-sensitive literal substring"}
        for name in action["paths"]:
            if name not in files:
                return _unavailable(result, "file_not_owned", "A search path is not a registered distribution file")
            path = files[name]
            lines = _text(path).splitlines()
            file_hash = _sha256(path)
            for number, line in enumerate(lines, 1):
                if action["query"] not in line:
                    continue
                if len(matches) >= action["max_results"]:
                    result["result"]["truncated"] = True
                    return result
                matches.append({"path": name, "sha256": file_hash, "line": number, "text": line})
                if len(_encoded(result)) > MAX_OUTPUT_BYTES - 2_000:
                    matches.pop()
                    result["result"]["truncated"] = True
                    result["limitations"].append("Search output is bounded; narrow paths or query to continue")
                    return result
        return result
    except Exception as error:
        return _unavailable(result, "dependency_query_unavailable", "Dependency query could not complete: " + type(error).__name__)


def output_record(config, result):
    record = {"nonce": config["nonce"], "query_id": config["query_id"], "public": result}
    if len(_encoded(record)) > MAX_OUTPUT_BYTES:
        _unavailable(result, "result_too_large", "Query output exceeds 24000 bytes; request a narrower observation")
        record["public"] = result
    return record


def test_dependency_query():
    config = json.loads(Path(__file__).with_name("query.json").read_text(encoding="utf-8"))
    record = output_record(config, query(config))
    # 被导入库可能输出未换行日志；协议记录始终另起一行。
    print("\n" + PREFIX + _encoded(record).decode("utf-8"), flush=True)
