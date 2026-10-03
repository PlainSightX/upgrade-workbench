"""对已核验依赖源码执行固定只读查询；本模块不导入目标应用。"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..runtime import assert_no_links

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?[A-Za-z0-9.+-]*\Z")
_QUALNAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z")
_MAX_SOURCE_BYTES = 2_000_000
_MAX_TEXT_BYTES = 24_000


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Dependency path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("Dependency path is not canonical")
    return value


def _regular_file(path: Path) -> None:
    assert_no_links(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError(f"Registered dependency file is unavailable: {path.name}") from error
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Registered dependency path is not a regular file: {path.name}")


@dataclass(frozen=True)
class DependencyEnvironmentIdentity:
    """依赖事实的环境身份；不包含凭据、命令或宿主执行参数。"""

    role: str
    lock_sha256: str
    image_id: str
    base_image_id: str
    python_version: str

    def __post_init__(self) -> None:
        if self.role not in {"old", "new"}:
            raise ValueError("Dependency environment role must be old or new")
        if not _SHA256.fullmatch(self.lock_sha256):
            raise ValueError("Dependency environment lock_sha256 is invalid")
        if not _IMAGE_ID.fullmatch(self.image_id) or not _IMAGE_ID.fullmatch(self.base_image_id):
            raise ValueError("Dependency environment image identity is invalid")
        if not _PYTHON_VERSION.fullmatch(self.python_version):
            raise ValueError("Dependency environment Python version is invalid")

    def as_dict(self) -> dict[str, str]:
        return {
            "role": self.role,
            "lock_sha256": self.lock_sha256,
            "image_id": self.image_id,
            "base_image_id": self.base_image_id,
            "python_version": self.python_version,
        }


@dataclass(frozen=True)
class VerifiedDependencyRoot:
    """只允许查询绑定时登记并通过哈希校验的依赖文件。"""

    root: Path
    environment: DependencyEnvironmentIdentity
    distribution: str
    installed_version: str
    files: tuple[tuple[str, str], ...]
    binding_sha256: str


def bind_dependency_root(
    root: Path,
    *,
    environment: DependencyEnvironmentIdentity,
    distribution: str,
    installed_version: str,
    file_hashes: dict[str, str],
) -> VerifiedDependencyRoot:
    """绑定调用方已经物化的依赖根，并再次核验允许读取的文件字节。"""
    root = Path(root).absolute()
    assert_no_links(root)
    if not root.is_dir():
        raise ValueError("Verified dependency root must be an existing directory")
    if not _NAME.fullmatch(distribution):
        raise ValueError("Dependency distribution name is invalid")
    if not isinstance(installed_version, str) or not installed_version or len(installed_version) > 128:
        raise ValueError("Dependency installed version is invalid")
    if any(ord(character) < 32 for character in installed_version):
        raise ValueError("Dependency installed version contains control characters")
    if not isinstance(file_hashes, dict) or not file_hashes:
        raise ValueError("Dependency root requires a non-empty verified file manifest")

    registered: list[tuple[str, str]] = []
    folded: set[str] = set()
    for name, expected in sorted(file_hashes.items()):
        _relative_path(name)
        if name.casefold() in folded:
            raise ValueError("Dependency file manifest contains case-aliased paths")
        folded.add(name.casefold())
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise ValueError("Dependency file manifest contains an invalid SHA-256")
        path = root.joinpath(*PurePosixPath(name).parts)
        _regular_file(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Dependency file SHA-256 mismatch: {name}")
        registered.append((name, expected))

    identity = {
        "environment": environment.as_dict(),
        "distribution": distribution,
        "installed_version": installed_version,
        "files": registered,
    }
    return VerifiedDependencyRoot(
        root=root,
        environment=environment,
        distribution=distribution,
        installed_version=installed_version,
        files=tuple(registered),
        binding_sha256=_digest(identity),
    )


def _validate_binding(binding: VerifiedDependencyRoot) -> dict[str, str]:
    if not isinstance(binding, VerifiedDependencyRoot):
        raise TypeError("Dependency query requires a VerifiedDependencyRoot")
    expected = _digest({
        "environment": binding.environment.as_dict(),
        "distribution": binding.distribution,
        "installed_version": binding.installed_version,
        "files": list(binding.files),
    })
    if expected != binding.binding_sha256:
        raise ValueError("Dependency root binding identity changed")
    return dict(binding.files)


def _read_registered(
    binding: VerifiedDependencyRoot, files: dict[str, str], name: str,
) -> bytes | None:
    _relative_path(name)
    if name not in files:
        return None
    path = binding.root.joinpath(*PurePosixPath(name).parts)
    _regular_file(path)
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != files[name]:
        raise ValueError(f"Dependency file changed after verification: {name}")
    return contents


def _base_result(binding: VerifiedDependencyRoot, operation: str) -> dict:
    return {
        "operation": operation,
        "environment": binding.environment.as_dict(),
        "distribution": binding.distribution,
        "installed_version": binding.installed_version,
        "binding_sha256": binding.binding_sha256,
    }


def _action(
    action: dict, operation: str, required: set[str], optional: set[str] = frozenset(),
) -> None:
    if not isinstance(action, dict) or action.get("operation") != operation:
        raise ValueError("Dependency query operation is invalid")
    keys = set(action)
    minimum = required | {"operation"}
    if not minimum <= keys <= minimum | optional:
        raise ValueError(f"Invalid fields for dependency query operation {operation}")


def _decoded_source(contents: bytes) -> tuple[str | None, str | None]:
    if len(contents) > _MAX_SOURCE_BYTES:
        return None, "Registered dependency source exceeds the static inspection limit."
    try:
        return contents.decode("utf-8"), None
    except UnicodeError:
        return None, "Registered dependency file is not UTF-8 source text."


def _symbol_nodes(tree: ast.AST, parent: str = "") -> list[tuple[str, ast.AST]]:
    result: list[tuple[str, ast.AST]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{parent}.{node.name}" if parent else node.name
            result.append((name, node))
            result.extend(_symbol_nodes(node, name))
    return result


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix}({ast.unparse(node.args)}){returns}"


def _assignment_names(node: ast.AST) -> list[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        targets: list[ast.AST] = []
        if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                names.add(target.attr)
    return sorted(names)


def _inspect_symbol(contents: bytes, path: str, qualname: str) -> tuple[dict | None, list[str]]:
    text, limitation = _decoded_source(contents)
    if limitation is not None:
        return None, [limitation]
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError) as error:
        return None, [f"Static source parsing failed: {type(error).__name__}."]
    found = dict(_symbol_nodes(tree)).get(qualname)
    if found is None:
        return None, ["Symbol was not found in the registered dependency source file."]

    limitations = ["Static inspection does not import dependencies or establish runtime behavior."]
    result = {
        "kind": type(found).__name__,
        "qualname": qualname,
        "path": path,
        "start_line": found.lineno,
        "end_line": found.end_lineno,
        "signature": None,
        "declared_bases": [],
        "mro": None,
        "attributes": [],
    }
    if isinstance(found, (ast.FunctionDef, ast.AsyncFunctionDef)):
        result["signature"] = _function_signature(found)
    elif isinstance(found, ast.ClassDef):
        result["declared_bases"] = [ast.unparse(base) for base in found.bases]
        result["attributes"] = sorted({
            child.name
            for child in found.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        } | set(_assignment_names(found)))
        initializer = next((
            child
            for child in found.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name == "__init__"
        ), None)
        if initializer is not None:
            result["signature"] = _function_signature(initializer)
        limitations.append(
            "Runtime MRO is unavailable without importing the dependency; "
            "declared bases are reported instead."
        )
    return result, limitations


def execute_dependency_query(binding: VerifiedDependencyRoot, action: dict) -> dict:
    """执行固定查询；未知信息返回 limitation，安全合同错误直接拒绝。"""
    files = _validate_binding(binding)
    operation = action.get("operation") if isinstance(action, dict) else None
    if operation == "list_files":
        _action(action, operation, set(), {"prefix"})
        prefix = action.get("prefix", "")
        if not isinstance(prefix, str):
            raise ValueError("Dependency file prefix must be text")
        if prefix:
            _relative_path(prefix.removesuffix("/"))
        items = [
            {"path": name, "sha256": digest}
            for name, digest in sorted(files.items())
            if name.startswith(prefix)
        ]
        return _base_result(binding, operation) | {
            "result": {"files": items}, "limitations": [],
        }

    if operation == "read_file":
        _action(action, operation, {"path", "start_line", "end_line"})
        start, end = action["start_line"], action["end_line"]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= 100_000:
            raise ValueError("Dependency read range is invalid")
        contents = _read_registered(binding, files, action["path"])
        if contents is None:
            return _base_result(binding, operation) | {
                "result": None,
                "limitations": [
                    "Requested file is not present in the verified dependency manifest."
                ],
            }
        text, limitation = _decoded_source(contents)
        if limitation is not None:
            return _base_result(binding, operation) | {
                "result": None, "limitations": [limitation],
            }
        lines = text.splitlines(keepends=True)
        selected = "".join(lines[start - 1:min(end, len(lines))])
        if len(selected.encode("utf-8")) > _MAX_TEXT_BYTES:
            return _base_result(binding, operation) | {
                "result": None,
                "limitations": [
                    "Requested dependency source range exceeds the result byte limit."
                ],
            }
        return _base_result(binding, operation) | {
            "result": {
                "path": action["path"],
                "sha256": files[action["path"]],
                "start_line": start,
                "end_line": min(end, len(lines)),
                "text": selected,
            },
            "limitations": [],
        }

    if operation == "search_text":
        _action(action, operation, {"query"}, {"paths", "max_results"})
        query = action["query"]
        maximum = action.get("max_results", 50)
        if not isinstance(query, str) or not query or len(query) > 500:
            raise ValueError("Dependency search query is invalid")
        if type(maximum) is not int or not 1 <= maximum <= 100:
            raise ValueError("Dependency search result limit is invalid")
        names = action.get("paths", sorted(files))
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError("Dependency search paths must be a list of registered paths")
        matches: list[dict] = []
        limitations: list[str] = []
        for name in names:
            contents = _read_registered(binding, files, name)
            if contents is None:
                limitations.append(f"Registered dependency file is unavailable for search: {name}")
                continue
            text, limitation = _decoded_source(contents)
            if limitation is not None:
                limitations.append(f"{name}: {limitation}")
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    matches.append({"path": name, "line": number, "text": line})
                    if len(matches) >= maximum:
                        break
            if len(matches) >= maximum:
                break
        return _base_result(binding, operation) | {
            "result": {
                "matches": matches,
                "search_semantics": "case-sensitive literal substring",
            },
            "limitations": limitations,
        }

    if operation == "inspect_symbol":
        _action(action, operation, {"path", "qualname"})
        qualname = action["qualname"]
        if not isinstance(qualname, str) or not _QUALNAME.fullmatch(qualname):
            raise ValueError("Dependency symbol qualname is invalid")
        contents = _read_registered(binding, files, action["path"])
        if contents is None:
            return _base_result(binding, operation) | {
                "result": None,
                "limitations": [
                    "Requested symbol source is not present in the verified dependency manifest."
                ],
            }
        result, limitations = _inspect_symbol(contents, action["path"], qualname)
        return _base_result(binding, operation) | {
            "result": result, "limitations": limitations,
        }

    raise ValueError("Unknown dependency query operation")
