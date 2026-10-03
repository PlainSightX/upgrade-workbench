"""失败位置驱动的有限定义/引用工作集；不执行源码，不声称完整类型或调用图。"""

from __future__ import annotations

import ast
import difflib
import hashlib
import re

from .source_context import outline


def _source_path(value):
    """把公开执行产物中的容器源码前缀还原为案例登记路径。"""
    if not isinstance(value, str):
        return ""
    path = value.replace("\\", "/")
    for prefix in ("/work/source/", "source/", "./source/"):
        if path.startswith(prefix):
            return path.removeprefix(prefix)
    return path.removeprefix("./")


def locations(value, files):
    """只接受公开产物中可落到登记文件的行号，不能从任意字符串猜根因。"""
    result = []
    if isinstance(value, dict):
        name = _source_path(value.get("path", value.get("file", "")))
        line = value.get("line", value.get("lineno"))
        if name in files and isinstance(line, int) and not isinstance(line, bool) and line > 0:
            result.append((name, line))
        for key, child in value.items():
            if key in {"message", "stdout", "stderr", "failure_excerpt"} and isinstance(child, str):
                for path, number in re.findall(r'(?:/work/source/)?([\w./-]+\.py)[:(](\d+)', child):
                    path = _source_path(path)
                    if path in files:
                        result.append((path, int(number)))
            elif isinstance(child, (dict, list)):
                result.extend(locations(child, files))
    elif isinstance(value, list):
        for child in value:
            result.extend(locations(child, files))
    return list(dict.fromkeys(result))


def focus(snapshot, observations, findings):
    requests, seeds, evidence = [], [], []
    for observation in observations:
        if observation.get("revision", observation.get("result", {}).get("revision")) != snapshot.revision:
            continue
        for path, line in locations(observation.get("result", {}), snapshot.files):
            seeds.append((path, line, "current_public_observation"))
    for path, line in locations(list(findings), snapshot.files):
        seeds.append((path, line, "static_location"))
    for path, current in snapshot.files.items():
        old = snapshot.original[path]
        if old == current:
            continue
        before, after = old.decode().splitlines(), current.decode().splitlines()
        for op, _, _, start, end in difflib.SequenceMatcher(a=before, b=after).get_opcodes():
            if op != "equal":
                seeds.append((path, max(1, start + 1), "edited_location"))

    definitions, trees, unknown = {}, {}, []
    for path, data in snapshot.files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(data.decode())
        except (SyntaxError, ValueError):
            unknown.append({"path": path, "reason": "ast_unavailable"})
            continue
        trees[path] = tree
        definitions[path] = [r for r in outline(data) if r["kind"] in {"ClassDef", "FunctionDef", "AsyncFunctionDef"}]
    symbols = set()
    focused = set()
    for path, line, reason in seeds[:24]:
        rows = [r for r in definitions.get(path, []) if r["start_line"] <= line <= r["end_line"]]
        row = min(rows, key=lambda r: r["end_line"] - r["start_line"]) if rows else None
        start, end = (row["start_line"], row["end_line"]) if row else (max(1, line - 12), line + 36)
        end = min(end, start + 159)
        requests.append((path, start, end, reason))
        focused.add(path)
        if row:
            symbols.add(row["name"].split(".")[-1])
        tree = trees.get(path)
        if tree:
            for node in ast.walk(tree):
                if start <= getattr(node, "lineno", 0) <= end:
                    if isinstance(node, ast.Name):
                        symbols.add(node.id)
                    elif isinstance(node, ast.Attribute):
                        symbols.add(node.attr)
        evidence.append({"path": path, "line": line, "reason": reason,
                         "symbol": row["name"] if row else None, "revision": snapshot.revision})
    symbols -= {"self", "cls", "get", "set", "append", "str", "int", "len", "id"}
    relations = []
    for path, tree in trees.items():
        for row in definitions[path]:
            if row["name"].split(".")[-1] in symbols:
                relations.append((path, row["start_line"], min(row["end_line"], row["start_line"] + 79), "name_matched_definition"))
        # 同名引用只是待核实关系。属性接收者与动态分派并未被类型证明。
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
                if name in symbols:
                    relations.append((path, max(1, node.lineno - 5), node.lineno + 10, "possible_call_relation"))
    related = sorted(set(relations), key=lambda r: (r[0] not in focused, r[0], r[1], r[2], r[3]))[:18]
    requests.extend(related)
    # 原/新版本并看，删除 hook 或跳过保存链时仍能看见原有行为，而不是仅剩补丁后的定义。
    changed = [p for p in snapshot.files if snapshot.files[p] != snapshot.original[p]]
    behavior = []
    for path in changed:
        edited = {row["name"] for row in definitions.get(path, [])
                  if any(p == path and reason == "edited_location" and row["start_line"] <= line <= row["end_line"]
                         for p, line, reason in seeds)}
        for view, data in (("original", snapshot.original[path]), ("current", snapshot.files[path])):
            spans = [row for row in outline(data) if row["name"] in edited]
            for line, text in enumerate(data.decode().splitlines(), 1):
                if re.search(r'\b(save|commit|rollback|flush|serialize|default|dispatch|hook)\b|save_before|save_after', text):
                    behavior.append({"path": path, "line": line, "view": view,
                                     "text": text[:240], "candidate_revision": snapshot.revision,
                                     "file_sha256": hashlib.sha256(data).hexdigest(),
                                     "within_edited_symbol": any(row["start_line"] <= line <= row["end_line"] for row in spans)})
    behavior.sort(key=lambda row: (not row["within_edited_symbol"], row["path"], row["line"], row["view"]))
    return requests, {"seeds": evidence[:24], "possible_relations": [
        {"path": p, "start_line": s, "end_line": e, "reason": r} for p, s, e, r in related],
        "related_behavior": behavior[:24], "unknown": unknown[:8],
        "scope": "bounded AST name/reference hints; not a resolved type graph; dynamic imports, reexports and dispatch may be missed",
        "truncated": len(relations) > 18 or len(behavior) > 24 or len(unknown) > 8}
