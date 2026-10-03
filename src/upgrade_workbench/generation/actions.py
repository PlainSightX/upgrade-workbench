"""Agent 只读动作只接触登记的公开源码，不提供文件系统或 Shell 入口。"""

from __future__ import annotations

import json

from ..cases import LoadedCase, load_case
from ..cases.manifest import read_verified_file, relative_path

MAX_TOOL_BYTES = 24_000
MAX_TOOL_RESULTS = 8
SCOPE_ESCAPE_FIELDS = frozenset({"command", "cwd", "env", "environment", "shell", "working_directory"})


class AgentActionError(ValueError):
    """模型动作不在公开源码工具合同内。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "action_outside_public_source_contract",
    ) -> None:
        super().__init__(message)
        self.code = code


def action_field_error_code(fields) -> str:
    """普通拼写错误与试图扩大执行边界的字段使用不同拒绝码。"""
    return (
        "action_outside_public_source_contract"
        if set(fields) & SCOPE_ESCAPE_FIELDS
        else "action_invalid_fields"
    )


def _source_names(case: LoadedCase) -> set[str]:
    from .request import solver_source_names

    return {name[7:] for name in solver_source_names(case)}


def _path(name: object, available: set[str]) -> str:
    if not isinstance(name, str):
        raise AgentActionError("Source path must be a string")
    try:
        relative_path(name)
    except ValueError as error:
        raise AgentActionError("Source path must be canonical") from error
    if name not in available:
        raise AgentActionError("Source path is outside the registered public source")
    return name


def validate_action(case: LoadedCase, value: object, *, candidate=None) -> dict:
    """一次响应只选一个动作；旧协议对原始编辑，新协议对当前revision增量编辑。"""
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise AgentActionError("One typed action is required", code="action_invalid_fields")
    kind = value["type"]
    keys = {
        "read_source": {"type", "path", "start_line", "end_line"},
        "search_source": {"type", "query", "paths", "max_results"},
        "submit_candidate": {"type", "edits"},
        "finish": {"type"},
    }
    if candidate is not None:
        keys["submit_candidate"] = {"type", "base_revision", "edits"}
    optional = {"view"} if candidate is not None and kind in {"read_source", "search_source"} else set()
    if kind not in keys:
        raise AgentActionError("Unsupported action type")
    missing = sorted(keys[kind] - set(value))
    extra = sorted(str(key)[:80] for key in set(value) - keys[kind] - optional)
    if missing or extra:
        raise AgentActionError(
            f"Invalid {kind} fields; action not executed. missing={missing}; extra={extra[:5]}; "
            f"required={sorted(keys[kind])}; optional={sorted(optional)}.",
            code=action_field_error_code(extra),
        )
    if optional and value.get("view", "current") not in {"original", "current"}:
        raise AgentActionError(
            "Source view must be original or current",
            code="action_invalid_fields",
        )
    available = _source_names(case)
    if kind == "read_source":
        _path(value["path"], available)
        start, end = value["start_line"], value["end_line"]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise AgentActionError(
                "Read range must contain between 1 and 200 source lines",
                code="action_invalid_fields",
            )
        if end - start + 1 > 200:
            # 返回模型自己提交的数值及可操作边界，不泄露文件或检查内容。
            raise AgentActionError(
                f"Rejected read_source range {start}..{end}: {end - start + 1} inclusive lines, "
                f"maximum 200. For start_line={start}, end_line must be <= {start + 199}. "
                "No source was read for this rejected action; submit a corrected range.",
                code="action_invalid_fields",
            )
    elif kind == "search_source":
        query, paths, maximum = value["query"], value["paths"], value["max_results"]
        if not isinstance(query, str) or not 1 <= len(query) <= 200 or "\x00" in query:
            raise AgentActionError("Search requires a bounded literal query", code="action_invalid_fields")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 128:
            raise AgentActionError(
                "Search requires explicit registered source paths",
                code="action_invalid_fields",
            )
        checked = [_path(name, available) for name in paths]
        if len(set(checked)) != len(checked):
            raise AgentActionError("Search paths must not repeat", code="action_invalid_fields")
        if type(maximum) is not int or not 1 <= maximum <= 50:
            raise AgentActionError(
                "Search max_results must be between 1 and 50",
                code="action_invalid_fields",
            )
    elif kind == "submit_candidate":
        # 复用同一转换器，保证提交和旧 exact_edits 具有相同的路径/范围约束。
        from .edits import edits_to_patch

        if candidate is None:
            edits_to_patch(case, value["edits"])
        else:
            from .edits import edited_sources

            if value["base_revision"] != candidate.revision:
                raise AgentActionError(
                    "Stale base_revision; read the current candidate and resubmit against its exact revision.",
                    code="stale_base_revision",
                )
            edited_sources(case, value["edits"], candidate.files)
    return value


def execute_source_action(case: LoadedCase, action: dict, *, candidate=None) -> dict:
    """从核验的公开原始/候选字节读取；不读取检查、锁或任意宿主路径。"""
    current = load_case(case.manifest_path)
    if current.fingerprint != case.fingerprint:
        raise AgentActionError("Case changed before read-only action")
    action = validate_action(current, action, candidate=candidate)
    kind = action["type"]
    if kind not in {"read_source", "search_source"}:
        raise AgentActionError("Only read_source and search_source are executable tools")

    def source(name: str) -> list[str]:
        if candidate is not None:
            files = candidate.original if action.get("view") == "original" else candidate.files
            return files[name].decode("utf-8").splitlines(keepends=True)
        registered = "source/" + name
        return read_verified_file(
            current.root, registered, current.manifest.file_hashes[registered]
        ).decode("utf-8").splitlines(keepends=True)

    if kind == "read_source":
        lines = source(action["path"])
        if action["end_line"] > len(lines):
            result = {"error": {"code": "read_range_out_of_bounds", "available_lines": len(lines)}}
        else:
            result = {
                "path": action["path"], "start_line": action["start_line"],
                "end_line": action["end_line"],
                "text": "".join(lines[action["start_line"] - 1:action["end_line"]]),
            }
    else:
        matches = []
        truncated = False
        for name in action["paths"]:
            for number, line in enumerate(source(name), start=1):
                if action["query"] not in line:
                    continue
                if len(matches) == action["max_results"]:
                    truncated = True
                    break
                matches.append({"path": name, "line": number, "text": line.rstrip("\r\n")})
            if truncated:
                break
        result = {"matches": matches, "truncated": truncated}
    if candidate is not None:
        result.update(view=action.get("view", "current"), revision=candidate.revision)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_TOOL_BYTES:
        # 可重新提出更小的读取请求；不能截取一部分后假装是完整工具结果。
        result = {"error": {"code": "tool_result_too_large", "maximum_bytes": MAX_TOOL_BYTES}}
    if load_case(current.manifest_path).fingerprint != current.fingerprint:
        raise AgentActionError("Case changed during read-only action")
    return result
