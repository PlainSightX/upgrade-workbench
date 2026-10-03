"""诊断日志的只读分页；游标绑定已登记输出，不提供任意文件读取。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .cases.manifest import assert_no_links
from .generation.source_context import encoded

_CURSOR = re.compile(r"log-v1:([a-z_]+):(stdout|stderr):([0-9a-f]{64}):(0|[1-9][0-9]{0,9})\Z")


def output_cursor(stage, stream, sha256, offset=0):
    return f"log-v1:{stage}:{stream}:{sha256}:{offset}"


def public_text(text):
    from .diagnostics import _clean

    # 完整清理后再分页，防止绝对路径横跨页边界而绕过清理。
    return _clean("\n".join(line for line in text.splitlines()
                            if "UPGRADE_WORKBENCH_RESULT" not in line))


def without_warnings(text):
    """仅分离 pytest 明确标识的警告节，保留其他业务行及后续总结。"""
    lines = text.splitlines()
    result = []
    in_warnings = False
    for line in lines:
        if re.match(r"^=+\s*warnings summary\s*=+\s*$", line):
            in_warnings = True
            continue
        if in_warnings and re.match(r"^=+\s*\S.*=+\s*$", line):
            in_warnings = False
        if not in_warnings:
            result.append(line)
    return "\n".join(result)


def output_page(task, reference, cursor, *, current_revision):
    from .diagnostics import _root, public_observation, read

    if reference not in task["observations"] or reference["id"] in task.get("hidden_observation_ids", []):
        raise ValueError("Observation output is not visible in this context")
    match = _CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
    if match is None:
        raise ValueError("Use an exact registered output cursor")
    stage, stream, sha256, offset = match.groups()
    offset = int(offset)
    public = public_observation(task, reference)
    value = read(reference, _root(task))
    execution_ref = value.get("execution_reference")
    if not execution_ref:
        raise ValueError("This observation has no registered execution output")
    receipt = read(execution_ref, _root(task))
    available = public["result"].get("stages", {}).get(stage, {}).get("output_streams", [])
    expected = {"stream": stream, "sha256": sha256,
                "cursor": output_cursor(stage, stream, sha256)}
    if expected not in available:
        raise ValueError("Output cursor is not registered on this observation")
    matches = [item for item in receipt.get("dependencies", [])
               if (item.get("stage"), item.get("stream"), item.get("sha256")) == (stage, stream, sha256)]
    if len(matches) != 1:
        raise ValueError("Execution output is not uniquely registered")
    path = Path(matches[0]["path"])
    assert_no_links(path)
    if not path.resolve().is_relative_to(_root(task).resolve()):
        raise ValueError("Execution output is outside this task")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("Execution output changed")
    text = public_text(raw.decode("utf-8", errors="replace"))
    if offset > len(text):
        raise ValueError("Output cursor exceeds the sanitized output")
    end = min(len(text), offset + 8000)
    while True:
        result = {"observation_id": reference["id"], "revision": public["revision"],
                  "stale": public["revision"] != current_revision,
                  "stage": stage, "stream": stream, "sha256": sha256,
                  "offset": offset, "next_cursor": output_cursor(stage, stream, sha256, end)
                  if end < len(text) else None,
                  "eof": end == len(text), "text": text[offset:end],
                  "offset_unit": "sanitized_unicode_characters",
                  "scope": "Existing diagnostic output; not a new execution or acceptance result"}
        if len(encoded(result)) <= 20_000:
            return result
        end = offset + (end - offset) // 2
