"""从已登记快照构造保守的符号依赖；不导入目标、不把静态关联当运行时影响。"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from pathlib import PurePosixPath

MAX_EDGES = 4000
MAX_ASSOCIATIONS = 4000
MAX_DEPTH = 8


def _module(path: str) -> tuple[str, bool] | None:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts.pop(0)
    package = bool(parts and parts[-1] == "__init__")
    if package:
        parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts), package


def _names(node: ast.AST) -> set[str]:
    result = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            result.add(child.id)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            result.update(alias.asname or alias.name.split(".")[0] for alias in child.names)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            result.add(child.name)
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, (ast.Store, ast.Del)):
            owner = child.value
            while isinstance(owner, ast.Attribute):
                owner = owner.value
            if isinstance(owner, ast.Name):
                result.add(owner.id)
    return result


def _qualified(node: ast.expr, env: dict) -> str | None:
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = _qualified(node.value, env)
        return f"{owner}.{node.attr}" if owner else None
    return None


def build_dependencies(blocks: list[dict], findings: list[dict]) -> dict:
    """导入/再导出/直接继承可追溯，条件绑定和循环不靠猜测补全。"""
    modules, unknowns = {}, []
    conflicts = set()
    for block in blocks:
        if not block["path"].endswith(".py"):
            continue
        identity = _module(block["path"])
        if identity is None:
            unknowns.append({"path": block["path"], "line": 1, "reason": "unresolved_module_path"})
            continue
        name, package = identity
        if name in modules:
            conflicts.add(name)
        try:
            tree = ast.parse(block["text"])
        except (SyntaxError, ValueError, RecursionError):
            unknowns.append({"path": block["path"], "line": 1, "reason": "unparseable_module"})
            continue
        modules[name] = {"path": block["path"], "package": package, "tree": tree,
                         "exports": {}, "classes": [], "imports": []}
    for name in sorted(conflicts):
        unknowns.append({"path": modules[name]["path"], "line": 1, "reason": "ambiguous_module_path"})
        del modules[name]

    for module, info in modules.items():
        env = info["exports"]
        for node in info["tree"].body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                base = node.module or "" if isinstance(node, ast.ImportFrom) else ""
                if isinstance(node, ast.ImportFrom) and node.level:
                    package_parts = module.split(".") if info["package"] else module.split(".")[:-1]
                    if node.level > len(package_parts):
                        base = ""
                    else:
                        base = ".".join(package_parts[:len(package_parts) - node.level + 1]
                                        + ([base] if base else []))
                for alias in node.names:
                    if alias.name == "*":
                        env.update(dict.fromkeys(env))
                        unknowns.append({"path": info["path"], "line": node.lineno,
                                         "reason": "wildcard_import"})
                        continue
                    if isinstance(node, ast.Import):
                        binding = alias.asname or alias.name.split(".")[0]
                        target = alias.name if alias.asname else alias.name.split(".")[0]
                    else:
                        binding = alias.asname or alias.name
                        target = f"{base}.{alias.name}" if base else None
                    env[binding] = target
                    info["imports"].append((node, binding, target))
                continue
            if isinstance(node, ast.ClassDef):
                target = f"{module}.{node.name}"
                info["classes"].append((node, target, env.copy()))
                env[node.name] = target
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                env[node.name] = None
            else:
                names = _names(node)
                if "*" in names:
                    env.update(dict.fromkeys(env))
                env.update(dict.fromkeys(names))
                if isinstance(node, (ast.If, ast.Try, ast.TryStar, ast.For, ast.While,
                                     ast.With, ast.Match, ast.AsyncWith, ast.AsyncFor)):
                    unknowns.append({"path": info["path"], "line": node.lineno,
                                     "reason": "conditional_bindings_not_resolved"})
                if any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                       and child.func.id in {"exec", "eval"} for child in ast.walk(node)):
                    env.update(dict.fromkeys(env))
                    unknowns.append({"path": info["path"], "line": node.lineno,
                                     "reason": "dynamic_namespace"})

    classes, duplicate_classes = {}, set()
    for info in modules.values():
        for node, target, _ in info["classes"]:
            # 模块末尾被重绑定的类不作为可导出的稳定符号。
            if info["exports"].get(node.name) == target:
                if target in classes:
                    duplicate_classes.add(target)
                classes[target] = {"path": info["path"], "line": node.lineno, "symbol": node.name}
    for target in sorted(duplicate_classes):
        location = classes.pop(target)
        unknowns.append({"path": location["path"], "line": location["line"],
                         "reason": "duplicate_class_binding"})

    def resolve(name: str | None, visited: tuple[str, ...] = ()) -> str | None:
        if not name or name in visited or len(visited) >= MAX_DEPTH:
            return None
        if name in classes:
            return name
        owner, _, symbol = name.rpartition(".")
        if owner not in modules:
            return None
        return resolve(modules[owner]["exports"].get(symbol), (*visited, name))

    edges = []
    for module, info in modules.items():
        for node, binding, raw in info["imports"]:
            target = resolve(raw)
            # 被后续覆盖的别名不用于传播；外部模块不冒充当前快照内的依赖。
            if target and info["exports"].get(binding) == raw:
                edges.append({"path": info["path"], "line": node.lineno, "symbol": binding,
                              "relation": "imports_symbol", "target": target, "dependent": None})
            elif raw and any(raw.startswith(name + ".") for name in modules):
                unknowns.append({"path": info["path"], "line": node.lineno,
                                 "reason": "unresolved_or_shadowed_local_import"})
        for node, dependent, env in info["classes"]:
            if dependent not in classes:
                continue
            for base in node.bases:
                raw = _qualified(base, env)
                target = resolve(raw)
                if target:
                    edges.append({"path": info["path"], "line": base.lineno, "symbol": node.name,
                                  "relation": "inherits_symbol", "target": target,
                                  "dependent": dependent})
                elif raw and any(raw.startswith(name + ".") for name in modules):
                    unknowns.append({"path": info["path"], "line": base.lineno,
                                     "reason": "unresolved_local_base"})
    if len(edges) > MAX_EDGES:
        raise ValueError("Static dependency edge capacity exceeded; no silent truncation")
    edges.sort(key=lambda item: (item["path"], item["line"], item["relation"], item["target"]))
    incoming = defaultdict(list)
    for edge in edges:
        incoming[edge["target"]].append(edge)
    path_modules = {info["path"]: module for module, info in modules.items()}
    associations = []
    for finding in findings:
        path = finding["file"].removeprefix("source/")
        owner = path_modules.get(path)
        symbol = finding["symbol"].split(".")[0]
        target = f"{owner}.{symbol}"
        if target not in classes:
            continue
        root = {"path": path, "line": finding["line"], "rule": finding["rule"]}
        queue = deque([(target, [])])
        visited = {target}
        seen_edges = set()
        while queue:
            name, chain = queue.popleft()
            for edge in incoming[name]:
                edge_key = (edge["path"], edge["line"], edge["relation"], edge["target"])
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                if len(chain) >= MAX_DEPTH:
                    unknowns.append({"path": edge["path"], "line": edge["line"],
                                     "reason": "propagation_depth_limit"})
                    continue
                trail = [*chain, edge]
                associations.append({"root": root, "location": edge, "chain": trail,
                                     "status": "potential_static_dependency"})
                if len(associations) > MAX_ASSOCIATIONS:
                    raise ValueError("Static association capacity exceeded; no silent truncation")
                if edge["dependent"] and edge["dependent"] not in visited:
                    visited.add(edge["dependent"])
                    queue.append((edge["dependent"], trail))
    return {"schema_version": 1, "method": "bounded_static_import_inheritance_v1",
            "source_hashes": {block["path"]: block["sha256"] for block in blocks},
            "edges": edges, "associations": associations, "unknowns": unknowns,
            "limitations": ["Imports do not prove runtime use or compatibility changes.",
                            "Conditional definitions, function-local scopes and runtime calls are unresolved.",
                            "Re-exports and inheritance are bounded to eight links; no complete type/call graph."]}
