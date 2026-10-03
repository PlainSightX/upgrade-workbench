"""完整源码与请求工作集分离；导航只解析字节，不导入目标应用。"""

from __future__ import annotations

import ast
import hashlib
import json

from ..cases.manifest import relative_path

POLICY = {"mode": "focused", "body_bytes": 96_000, "inventory_bytes": 64_000}
NAVIGATION = {"list_sources", "outline_source", "search_source", "read_source"}


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def policy(value=None) -> dict:
    value = dict(POLICY if value is None else value)
    if set(value) != set(POLICY) or value["mode"] not in {"focused", "full"}:
        raise ValueError("Invalid source context policy")
    for key, maximum in (("body_bytes", 1_600_000), ("inventory_bytes", 64_000)):
        if type(value[key]) is not int or not 1024 <= value[key] <= maximum:
            raise ValueError("Invalid source context capacity")
    return value


def source_state(snapshot) -> dict:
    changed = sorted(n for n in snapshot.files if snapshot.files[n] != snapshot.original[n])
    return {"revision": snapshot.revision, "origin": snapshot.origin,
            "state": "original" if snapshot.origin == "original" else
                     "seeded" if snapshot.origin == "official_tool" else "modified",
            "has_source_changes": bool(changed), "changed_files": changed,
            "patch_sha256": snapshot.sha256}


def source_comparison(case, snapshot) -> dict:
    """返回原始源码到当前候选的可核验差异，不重新解释补丁语义。"""
    from ..candidates import load_candidate
    from .edits import diff_files

    original = load_candidate(case)
    if set(snapshot.original) != set(original.files) or snapshot.original != original.files:
        raise ValueError("Candidate original source does not match the registered case")
    if set(snapshot.files) != set(original.files):
        raise ValueError("Candidate source inventory does not match the registered case")

    if snapshot.reference is None:
        if snapshot.origin != "original" or snapshot.revision != original.revision:
            raise ValueError("Unregistered candidate revision cannot be compared")
    else:
        verified = load_candidate(case, snapshot.reference)
        if (
            verified.revision != snapshot.revision
            or verified.origin != snapshot.origin
            or verified.original != snapshot.original
            or verified.files != snapshot.files
            or verified.patch != snapshot.patch
        ):
            raise ValueError("Candidate snapshot differs from its immutable reference")

    names = sorted(name for name in snapshot.files if snapshot.files[name] != original.files[name])
    canonical = diff_files(original.files, {name: snapshot.files[name] for name in names})
    if canonical != snapshot.patch:
        raise ValueError("Candidate patch is not the canonical original-to-current diff")
    files = [
        {
            "path": name,
            "original_sha256": hashlib.sha256(original.files[name]).hexdigest(),
            "current_sha256": hashlib.sha256(snapshot.files[name]).hexdigest(),
        }
        for name in names
    ]
    return {
        "original_revision": original.revision,
        "current_revision": snapshot.revision,
        "current_origin": snapshot.origin,
        "patch_sha256": hashlib.sha256(canonical).hexdigest(),
        "changed_files": names,
        "file_identities": files,
        "canonical_unified_diff": canonical.decode("utf-8"),
    }


def inventory(case, snapshot) -> list[dict]:
    return [{"path": n, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b),
             "lines": len(b.decode("utf-8").splitlines(keepends=True)),
             "editable": n in case.manifest.allowed_changes}
            for n, b in sorted(snapshot.files.items())]


def page(items: list, cursor: str | None, identity: str, *, maximum=50, byte_limit=23_000) -> dict:
    offset = 0
    if cursor is not None:
        if not isinstance(cursor, str):
            raise ValueError("Invalid navigation cursor")
        parts = cursor.split(":")
        if len(parts) != 2 or parts[0] != identity or not parts[1].isdigit():
            raise ValueError("Stale navigation cursor")
        offset = int(parts[1])
        if not 0 <= offset < len(items):
            raise ValueError("Cursor outside result")
    selected = []
    for item in items[offset:offset + maximum]:
        if len(encoded(selected + [item])) > byte_limit:
            break
        selected.append(item)
    if not selected and offset < len(items):
        return {"error": {"code": "single_result_too_large"}, "total": len(items)}
    following = offset + len(selected)
    return {"items": selected, "total": len(items), "offset": offset,
            "next_cursor": f"{identity}:{following}" if following < len(items) else None}


def outline(data: bytes) -> list[dict]:
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (SyntaxError, ValueError):
        return [{"kind": "parse_error", "fallback": "read_source"}]
    result = []

    def walk(node, parent=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{parent}.{child.name}" if parent else child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                result.append({"kind": type(child).__name__, "name": name,
                               "start_line": start, "end_line": child.end_lineno})
                walk(child, name)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                result.append({"kind": "import", "name": ast.unparse(child),
                               "start_line": child.lineno, "end_line": child.end_lineno})
            else:
                walk(child, parent)

    walk(tree)
    return sorted(result, key=lambda r: (r["start_line"], r["name"]))


def navigate(case, snapshot, action: dict) -> dict:
    from ..candidates import load_candidate

    view = action.get("view", "current")
    selected = load_candidate(case) if view == "original" else snapshot
    if action["revision"] != selected.revision:
        raise ValueError("Stale source revision for selected view")
    kind = action["type"]
    common = {"view": view, "revision": selected.revision}
    inv = inventory(case, selected)
    if kind == "list_sources":
        prefix = action.get("prefix", "")
        items = [row for row in inv if row["path"].startswith(prefix)]
        return common | page(items, action.get("cursor"), digest([selected.revision, kind, prefix]))
    if kind in {"read_source", "outline_source"}:
        name = action["path"]
        relative_path(name)
        if name not in selected.files:
            raise ValueError("Path is outside registered public source")
        data = selected.files[name]
        common.update(path=name, file_sha256=hashlib.sha256(data).hexdigest())
        if kind == "outline_source":
            return common | page(outline(data), action.get("cursor"), digest([common, kind]))
        lines = data.decode("utf-8").splitlines(keepends=True)
        start, end = action["start_line"], action["end_line"]
        if start > len(lines):
            return common | {"error": {"code": "read_range_out_of_bounds",
                                      "available_lines": len(lines)}, "eof": True}
        actual_end = min(end, len(lines))
        result = common | {"requested_end_line": end, "start_line": start, "end_line": actual_end,
                           "eof": actual_end == len(lines), "text": "".join(lines[start - 1:actual_end])}
        if len(encoded(result)) > 24_000:
            return common | {"error": {"code": "tool_result_too_large", "maximum_bytes": 24_000}}
        return result
    if kind != "search_source":
        raise ValueError("Unknown navigation action")
    names = sorted(selected.files) if action.get("scope") == "repository" else action["paths"]
    if any(n not in selected.files for n in names):
        raise ValueError("Search path outside public source")
    matches = [{"path": name, "line": number, "text": line.rstrip("\r\n")}
               for name in sorted(names)
               for number, line in enumerate(selected.files[name].decode("utf-8").splitlines(keepends=True), 1)
               if action["query"] in line]
    key = digest([selected.revision, kind, action["query"], names])
    result = common | page(matches, action.get("cursor"), key, maximum=action.get("max_results", 50))
    result["search_semantics"] = "case-sensitive literal substring; no OR, regex or glob operators"
    if not matches and " OR " in action["query"]:
        result["query_feedback"] = "OR is literal text. Search one exact term per action. No query was expanded."
    return result


def _read_requests(snapshot, observations):
    return [
        (result["path"], result["start_line"], result["end_line"], "explicit_read")
        for item in observations
        for result in [item.get("result", {})]
        if result.get("revision") == snapshot.revision and result.get("view") == "current"
        and result.get("path") in snapshot.files and "start_line" in result and "end_line" in result
    ]


def rebind_source_reads(snapshot, observations):
    """延续阅读关注点；只重绑当前字节，不改变原观察的版本或证据含义。"""
    retained = []
    covered = {}
    expanded = []
    for item in observations:
        if item.get("action", {}).get("type") == "select_investigation" and "excerpts" in item.get("result", {}):
            expanded.extend(item | {"kind": "source", "action": {"type": "read_source"}, "result": row["source"]}
                            for row in item["result"]["excerpts"])
        else:
            expanded.append(item)
    for item in expanded:
        if item.get("action", {}).get("type") == "select_investigation" and "read" in item.get("result", {}):
            item = item | {"kind": "source", "action": {"type": "read_source"}, "result": item["result"]["read"]}
        if item.get("kind") != "source" or item.get("action", {}).get("type") != "read_source":
            continue
        result = item["result"]
        if result.get("view") != "current" or "text" not in result:
            continue
        name = result["path"]
        data = snapshot.files.get(name)
        origin = {key: result[key] for key in ("path", "revision", "view", "start_line", "end_line")}
        binding = {"source_revision": result["revision"], "source_start_line": result["start_line"],
                   "source_end_line": result["end_line"], "status": "pending_relocation"}
        bound = dict(origin)
        if data is not None:
            lines = data.decode("utf-8").splitlines(keepends=True)
            excerpt = result["text"].splitlines(keepends=True)
            file_hash = hashlib.sha256(data).hexdigest()
            start = None
            if file_hash == result["file_sha256"]:
                index = result["start_line"] - 1
                if lines[index:result["end_line"]] != excerpt:
                    raise ValueError("Retained source excerpt differs from its file identity")
                start = index
                binding["status"] = "current_read" if result["revision"] == snapshot.revision else "unchanged_file"
            elif excerpt:
                matches = [i for i in range(len(lines) - len(excerpt) + 1)
                           if lines[i:i + len(excerpt)] == excerpt]
                if len(matches) == 1:
                    start = matches[0]
                    binding["status"] = "unique_excerpt_relocated"
                else:
                    binding["reason"] = "excerpt_changed_or_removed" if not matches else "ambiguous_excerpt"
            if start is not None:
                end = start + len(excerpt)
                bound.update(revision=snapshot.revision, start_line=start + 1, end_line=end,
                             file_sha256=file_hash)
                covered.setdefault(name, set()).update(range(start + 1, end + 1))
            binding["current_file_lines"] = len(lines)
        else:
            binding["reason"] = "file_missing"
        retained.append({"observation_id": item["id"], "result": bound, "continuity": binding})
    for item in retained:
        binding = item["continuity"]
        name = item["result"]["path"]
        # 片段无法唯一定位时，只以当前文件的完整主动阅读消除定位未知。
        # 这不是业务复验；旧 probe/check 仍保持原 revision。
        count = binding.get("current_file_lines", 0)
        if (binding["status"] == "pending_relocation" and name in snapshot.files and count
                and covered.get(name, set()) >= set(range(1, count + 1))):
            binding["status"] = "reread_current_file"
    return retained


def excerpt_delivery(snapshot, observation_id, retained, blocks):
    """逐片段核对实际源码正文；清单或原始观察的存在不代表当前原文已送达。"""
    rows = []
    for item in retained:
        if item["observation_id"] != observation_id:
            continue
        source, continuity = item["result"], item["continuity"]
        name = source["path"]
        row = {"path": name, "source_range": [continuity["source_start_line"], continuity["source_end_line"]],
               "binding": continuity["status"], "current_range": None, "included_lines": 0,
               "complete": False, "omitted_ranges": []}
        if continuity["status"] == "reread_current_file":
            row["meaning"] = "Current file was fully reread; this historical excerpt has no unique current location."
        if continuity["status"] in {"current_read", "unchanged_file", "unique_excerpt_relocated"}:
            start, end = source["start_line"], source["end_line"]
            lines = snapshot.files[name].decode("utf-8").splitlines(keepends=True)
            covered = set()
            for block in blocks:
                first, last = block.get("start_line", 0), block.get("end_line", 0)
                if (block.get("path") == name and block.get("revision") == snapshot.revision
                        and first > 0 and block.get("text", "").splitlines(keepends=True) == lines[first - 1:last]):
                    covered.update(range(first, last + 1))
            missing = set(range(start, end + 1)) - covered
            row.update(current_range=[start, end], included_lines=end - start + 1 - len(missing),
                       complete=not missing, omitted_ranges=[[group[0], group[-1]] for group in _line_groups(missing)])
        rows.append(row)
    return rows


def _line_groups(numbers):
    groups = []
    for number in sorted(numbers):
        if not groups or number != groups[-1][-1] + 1:
            groups.append([])
        groups[-1].append(number)
    return groups


def _read_retention(snapshot, requests, ranges):
    wanted = {}
    for name, start, end, _reason in requests:
        lines = len(snapshot.files[name].decode("utf-8").splitlines(keepends=True))
        wanted.setdefault(name, set()).update(range(start, min(end, lines) + 1))
    missing = {name: numbers - ranges.get(name, set()) for name, numbers in wanted.items()}
    requested = sum(map(len, wanted.values()))
    omitted = sum(map(len, missing.values()))
    return {"requested_lines": requested, "included_lines": requested - omitted,
            "complete": omitted == 0,
            "omitted_ranges": [{"path": name, "start_line": group[0], "end_line": group[-1]}
                               for name, numbers in sorted(missing.items())
                               for group in _line_groups(numbers)]}


def build_context(case, snapshot, selection_policy=None, *, observations=(), findings=(), assistance="baseline", prioritize_reads=False, retain_reads_first=False, read_continuity=False, investigation_paths=(), fill_remaining=False) -> dict:
    """选择可见正文；分析和检索仍由调用者在完整快照上完成。"""
    selected_policy = policy(selection_policy)
    inv = inventory(case, snapshot)
    full_hash = digest(inv)
    listing = page(inv, None, digest([snapshot.revision, "list_sources", ""]),
                   maximum=len(inv), byte_limit=selected_policy["inventory_bytes"] - 512)
    requests = []
    localization = None
    if assistance == "failure_guided":
        from .localization import focus
        requests, localization = focus(snapshot, observations, findings)
    elif assistance != "baseline":
        raise ValueError("Unknown navigation assistance")
    # 调查计划只是装载优先级，不能伪装成已执行的源码阅读或静态定位证据。
    if any(name not in snapshot.files for name in investigation_paths):
        raise ValueError("Investigation priority is outside public source")
    requests = [(name, 1, None, "investigation_priority") for name in investigation_paths] + requests
    reads = _read_requests(snapshot, observations) if prioritize_reads or retain_reads_first else []
    if retain_reads_first:
        # 明确读取的原文先于自动定位；同一行仍只装入一次，容量不足保留最近读取。
        requests = list(reversed(reads)) + requests
    elif prioritize_reads:
        requests.extend(reads)
    changed = source_state(snapshot)["changed_files"]
    for name in sorted(set(changed) | set(case.manifest.allowed_changes)):
        requests.append((name, 1, None, "changed" if name in changed else "editable"))
    for item in observations:
        result = item.get("result", {})
        if result.get("revision") == snapshot.revision and result.get("view") == "current" and "text" in result:
            requests.append((result["path"], result["start_line"], result["end_line"], "recent_read"))
    for item in findings:
        name = item.get("path", item.get("file", "")).removeprefix("source/")
        if name in snapshot.files:
            line = item.get("line", 1)
            requests.append((name, max(1, line - 20), line + 60, "public_static_location"))
        for related in item.get("related_locations", []):
            name = related.get("path", related.get("file", "")).removeprefix("source/")
            if name in snapshot.files:
                line = related.get("line", 1)
                requests.append((name, max(1, line - 20), line + 60, "static_relation"))
    if fill_remaining:
        requests.extend((n, 1, None, "remaining_source") for n in sorted(snapshot.files))
    if selected_policy["mode"] == "full" and not fill_remaining:
        requests = [(n, 1, None, "full_mode") for n in sorted(snapshot.files)]
    used, blocks, ranges = 0, [], {}
    for name, start, end, reason in requests:
        lines = snapshot.files[name].decode("utf-8").splitlines(keepends=True)
        end = min(end or len(lines), len(lines))
        # 同一文件已发送的行不重复计入正文；大模块按完整行取有界前缀。
        covered = ranges.setdefault(name, set())
        numbers = [n for n in range(start, end + 1) if n not in covered]
        if not numbers:
            if not lines and not any(b["path"] == name for b in blocks):
                blocks.append({"path": name, "sha256": hashlib.sha256(snapshot.files[name]).hexdigest(),
                               "revision": snapshot.revision, "view": "current", "start_line": 0,
                               "end_line": 0, "text": "", "excerpt_sha256": hashlib.sha256(b"").hexdigest(),
                               "reason": reason})
            continue
        groups = []
        for number in numbers:
            if not groups or number != groups[-1][-1] + 1:
                groups.append([])
            groups[-1].append(number)
        for group in groups:
            included, content = [], []
            for number in group:
                size = len(lines[number - 1].encode("utf-8"))
                if used + size > selected_policy["body_bytes"]:
                    break
                included.append(number)
                content.append(lines[number - 1])
                used += size
            if included:
                text = "".join(content)
                covered.update(included)
                blocks.append({"path": name, "sha256": hashlib.sha256(snapshot.files[name]).hexdigest(),
                               "revision": snapshot.revision, "view": "current", "start_line": included[0],
                               "end_line": included[-1], "text": text,
                               "excerpt_sha256": hashlib.sha256(text.encode()).hexdigest(), "reason": reason})
    omissions = []
    for row in inv:
        remaining = row["lines"] - len(ranges.get(row["path"], set()))
        if remaining or not any(block["path"] == row["path"] for block in blocks):
            omissions.append({"path": row["path"], "omitted_lines": remaining})
    if selected_policy["mode"] == "full" and omissions:
        raise ValueError("Full source mode exceeds body capacity; select focused explicitly")
    if not blocks:
        raise ValueError("Source workset capacity cannot contain any complete line")
    retention = _read_retention(snapshot, reads, ranges) if retain_reads_first else None
    if read_continuity:
        if retention is None:
            raise ValueError("Source continuity requires retained-read accounting")
        pending = [{"observation_id": item["observation_id"], "path": item["result"]["path"],
                    **item["continuity"]} for item in observations
                   if item.get("continuity", {}).get("status") == "pending_relocation"]
        retention.update(pending_relocation=pending, complete=retention["complete"] and not pending,
                         meaning="Current source inclusion and unresolved reading locations; never behavioral evidence.")
        retention["status"] = ("pending_relocation" if pending else "capacity_omission" if not retention["complete"]
                               else "complete" if retention["requested_lines"] else "not_requested")
    return ({"localization": localization} if localization is not None else {}) | {"source_state": source_state(snapshot), "source_inventory": listing | {"sha256": full_hash},
            "source_files": blocks, "source_selection": {"policy": selected_policy, "kind": "selected",
            "body_bytes": used, "complete_source_bytes": sum(i["bytes"] for i in inv),
            "omitted_file_count": len(omissions), "omitted_lines": sum(r["omitted_lines"] for r in omissions),
            **({"read_retention": retention} if retain_reads_first else {}),
            "coverage": [{"path": b["path"], "start_line": b["start_line"], "end_line": b["end_line"]}
                         for b in blocks], "access": "list_sources / outline_source / search_source / read_source"}}
