"""v9 有界语法连接：不导入目标代码，不把词法绑定称为运行时调用图。"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import PurePosixPath

from .source_context import outline


def _module(path):
    parts = list(PurePosixPath(path).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _bindings(body):
    """只把直接语句当明确绑定；条件写入仍登记，从而阻止透明穿透。"""
    result = defaultdict(list)

    def visit(node, direct=True):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result[node.name].append((node, direct))
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                result[alias.asname or alias.name.split(".")[0]].append((node, direct))
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            result[node.id].append((node, False))
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    result[target.id].append((node, direct and node.value is not None))
                else:
                    visit(target, False)
            if node.value is not None:
                visit(node.value, False)
            return
        if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            result[node.name].append((node, False))
        if isinstance(node, ast.MatchMapping) and node.rest:
            result[node.rest].append((node, False))
        for child in ast.iter_child_nodes(node):
            visit(child, False)

    for node in body:
        visit(node)
    return result


class ConnectionIndex:
    """单次调查共享解析结果；旧 outline/map 的公开格式保持不变。"""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.modules = {_module(name): name for name in snapshot.files if name.endswith(".py")}
        self.files = {}
        self.neighbor_cache = {}
        self.scope_bindings = {}
        for path in self.modules.values():
            text = snapshot.files[path].decode("utf-8")
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                self.files[path] = {"tree": None, "symbols": [], "bindings": {}, "uses": [], "lines": text.splitlines()}
                continue
            symbols = [row for row in outline(snapshot.files[path]) if row["kind"] in {"ClassDef", "FunctionDef", "AsyncFunctionDef"}]
            bindings = _bindings(tree.body)
            for name, entries in bindings.items():
                for node, direct in entries:
                    if direct and isinstance(node, (ast.Assign, ast.AnnAssign)):
                        symbols.append({"kind": "assignment", "name": name, "start_line": node.lineno, "end_line": node.end_lineno})
            self.files[path] = {"tree": tree, "symbols": symbols, "bindings": bindings,
                "uses": [node for node in ast.walk(tree) if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)],
                "scopes": [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))],
                "lines": text.splitlines()}

    def symbols(self, path):
        return self.files.get(path, {}).get("symbols", [])

    def _import_target(self, path, node, name):
        if not isinstance(node, ast.ImportFrom):
            return None
        alias = next((row for row in node.names if (row.asname or row.name) == name and row.name != "*"), None)
        if alias is None:
            return None
        module = _module(path)
        package = module.split(".") if path.endswith("/__init__.py") else module.split(".")[:-1]
        if node.level > len(package) + 1:
            return None
        prefix = package[:len(package) - node.level + 1] if node.level else []
        target = self.modules.get(".".join(prefix + ([node.module] if node.module else [])))
        return (target, alias.name) if target else None

    def resolve(self, path, name, *, seen=(), depth=0):
        identity = (path, name)
        if identity in seen:
            return None, "reexport_cycle"
        bindings = self.files[path]["bindings"]
        if "*" in bindings:
            return None, "wildcard_binding_unknown"
        entries = bindings.get(name, [])
        if len(entries) != 1 or not entries[0][1]:
            return None, "ambiguous_or_conditional_binding"
        node = entries[0][0]
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([node.lineno] + [item.lineno for item in getattr(node, "decorator_list", [])])
            return {"path": path, "name": name, "start_line": first, "end_line": node.end_lineno, "chain": []}, None
        target = self._import_target(path, node, name)
        if target is None:
            return None, "external_or_unsupported_import"
        if depth >= 2:
            return None, "reexport_depth_limit"
        resolved, issue = self.resolve(*target, seen=(*seen, identity), depth=depth + 1)
        if resolved:
            edge = {"path": path, "line": node.lineno, "local_name": name, "target_path": target[0], "target_name": target[1]}
            return resolved | {"chain": [edge, *resolved["chain"]]}, None
        return None, issue

    def _use_binding(self, path, use):
        info = self.files[path]
        scopes = [node for node in info["scopes"] if node.lineno <= use.lineno <= node.end_lineno]
        function_scope = False
        for scope in sorted(scopes, key=lambda node: node.end_lineno - node.lineno):
            if isinstance(scope, ast.ClassDef):
                if function_scope:
                    continue
            else:
                function_scope = True
                args = scope.args
                if use.id in {arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, *([args.vararg] if args.vararg else []), *([args.kwarg] if args.kwarg else [])]}:
                    return None, "shadowed_local_binding"
            if scope not in self.scope_bindings:
                self.scope_bindings[scope] = _bindings(scope.body) if not isinstance(scope, ast.Lambda) else {}
            bindings = self.scope_bindings[scope]
            if use.id in bindings:
                rows = bindings[use.id]
                if len(rows) == 1 and rows[0][1] and rows[0][0].lineno < use.lineno:
                    target = self._import_target(path, rows[0][0], use.id)
                    if target:
                        resolved, issue = self.resolve(*target, depth=1)
                        if resolved:
                            return resolved | {"chain": [{"path": path, "line": rows[0][0].lineno,
                                "local_name": use.id, "target_path": target[0], "target_name": target[1]}, *resolved["chain"]]}, None
                        return None, issue
                return None, "shadowed_local_binding"
        return self.resolve(path, use.id)

    def _range(self, path, line):
        scopes = [row for row in self.symbols(path) if row["start_line"] <= line <= row["end_line"]]
        if scopes:
            scope = min(scopes, key=lambda row: row["end_line"] - row["start_line"])
            return scope["start_line"], scope["end_line"]
        return max(1, line - 8), min(line + 8, len(self.files[path]["lines"]))

    def connections(self, anchors, omit):
        candidates = []
        for anchor, symbol, start, end in anchors:
            if symbol is None or anchor["path"] not in self.files:
                omit("no_static_symbol_for_anchor", path=anchor["path"])
                continue
            path, root = anchor["path"], symbol.split(".")[0]
            expected = (path, root)
            # 候选发现先覆盖全部本地绑定；容量只限制送达，不能把路径前缀当相关度。
            for name, info in self.files.items():
                if info["tree"] is None:
                    continue
                for use in info["uses"]:
                    if name == path and start <= use.lineno <= end:
                        continue
                    if isinstance(use, ast.Name):
                        resolved, issue = self._use_binding(name, use)
                        matches = resolved and (resolved["path"], resolved["name"]) == expected and "." not in symbol
                        if not matches:
                            if issue in {"reexport_depth_limit", "reexport_cycle"} or use.id == root and issue in {"ambiguous_or_conditional_binding", "shadowed_local_binding", "wildcard_binding_unknown"}:
                                omit(issue, path=name, line=use.lineno, symbol=use.id)
                            continue
                        basis = {"kind": "same_file_use" if name == path else "import_bound_use",
                                 "anchor_path": path, "anchor_symbol": symbol, "match_line": use.lineno,
                                 "import_chain": resolved["chain"]}
                        rank = 0
                    else:
                        if "." not in symbol or use.attr != symbol.rsplit(".", 1)[-1]:
                            continue
                        # 方法接收者无法由此确定；只在直接静态邻居中保留词法候选。
                        neighbors = self._neighbors(path)
                        if name not in neighbors and name != path:
                            continue
                        basis = {"kind": "static_neighbor_lexical_use", "anchor_path": path,
                                 "anchor_symbol": symbol, "match_line": use.lineno}
                        rank = 2
                    first, last = self._range(name, use.lineno)
                    candidates.append({"path": name, "start_line": first, "end_line": last,
                        "responsibility": anchor["responsibility"], "basis": basis, "rank": rank})
            for use in self.files[path]["uses"]:
                if not isinstance(use, ast.Name) or not start <= use.lineno <= end:
                    continue
                resolved, _ = self._use_binding(path, use)
                if not resolved or not resolved["chain"] or resolved["path"] == path:
                    continue
                candidates.append({"path": resolved["path"], "start_line": resolved["start_line"], "end_line": resolved["end_line"],
                    "responsibility": anchor["responsibility"], "rank": 1, "basis": {
                        "kind": "used_import_binding_definition", "anchor_path": path, "anchor_symbol": symbol,
                        "use_line": use.lineno, "binding_name": use.id, "import_chain": resolved["chain"]}})
            # 解析失败的直接邻居只能提供明确标注的词法窗口，不取消其他已找到的连接。
            for name in sorted(self._neighbors(path)):
                if self.files[name]["tree"] is not None:
                    continue
                omit("neighbor_parse_error", path=name, fallback="bounded_lexical_match_range")
                for number, line in enumerate(self.files[name]["lines"], 1):
                    if symbol.rsplit(".", 1)[-1] in line:
                        first, last = self._range(name, number)
                        candidates.append({"path": name, "start_line": first, "end_line": last,
                            "responsibility": anchor["responsibility"], "rank": 3,
                            "basis": {"kind": "unparsed_neighbor_lexical_use", "anchor_path": path,
                                      "anchor_symbol": symbol, "match_line": number}})
        return self._coalesce(candidates)

    def _neighbors(self, path):
        if path in self.neighbor_cache:
            return self.neighbor_cache[path]
        result = set()
        for name, info in self.files.items():
            tree = info["tree"]
            if tree is None:
                continue
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Import):
                    targets = [self.modules.get(alias.name) for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    targets = [target[0] for alias in node.names
                               if (target := self._import_target(name, node, alias.asname or alias.name))]
                for target in targets:
                    if target and path in {name, target}:
                        result.add(target if name == path else name)
        self.neighbor_cache[path] = result
        return result

    @staticmethod
    def _coalesce(candidates):
        groups = []
        seen = set()
        for row in sorted(candidates, key=lambda row: (row["rank"], row["path"], row["start_line"], row["end_line"])):
            key = (row["path"], row["start_line"], row["end_line"], row["basis"]["kind"])
            if key in seen:
                continue
            seen.add(key)
            # 相邻被用定义合并后仍保留每个绑定的证据，不让十个单行扩展耗尽八段容量。
            previous = groups[-1] if groups else None
            if (previous and row["basis"]["kind"] == "used_import_binding_definition"
                    and previous["basis"]["kind"] in {"used_import_binding_definition", "used_import_binding_definitions"}
                    and previous["path"] == row["path"] and row["start_line"] - previous["end_line"] <= 12
                    and row["end_line"] - previous["start_line"] < 200):
                previous["end_line"] = max(previous["end_line"], row["end_line"])
                members = previous["basis"].get("bindings", [previous["basis"]])
                previous["basis"] = {"kind": "used_import_binding_definitions", "bindings": [*members, row["basis"]]}
            else:
                groups.append({key: value for key, value in row.items() if key != "rank"})
        return groups
