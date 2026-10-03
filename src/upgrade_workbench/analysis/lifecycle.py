"""连接所有权的有界语法分析；不执行目标，也不声称完整类型/控制流证明。"""

from __future__ import annotations

import ast

RULE = "sa_connection_lifetime"
EVIDENCE = "sqlalchemy-v2-connectionless"
ENGINE_BASES = {"direct_factory", "declared_engine", "member_factory", "member_annotation"}
OWNERSHIPS = {"local", "callee_unknown"}
CLEANUPS = {"not_established", "normal_path_only", "transaction_scope_only"}
SCOPE = "local_syntax_not_all_paths_or_proven_version_regression"
ENGINE_TYPES = {"sqlalchemy.Engine", "sqlalchemy.engine.Engine", "sqlalchemy.engine.base.Engine"}
FACTORIES = {"sqlalchemy.create_engine", "sqlalchemy.engine.create_engine", "sqlalchemy.engine.create.create_engine"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
SCOPES = (*FUNCTIONS, ast.ClassDef)


def local_nodes(root):
    """遍历本作用域；嵌套定义只暴露名称，不混入其函数体。"""
    yield root
    for child in ast.iter_child_nodes(root):
        if isinstance(child, SCOPES):
            yield child
        else:
            yield from local_nodes(child)


def label(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = label(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


class _Index:
    def __init__(self, tree, resolve):
        self.tree, self.resolve = tree, resolve
        self.parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        self.scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, FUNCTIONS))]
        self.nodes = {scope: list(local_nodes(scope)) for scope in self.scopes}
        self.type_imports = self.guarded_type_imports()

    def guarded_type_imports(self):
        """TYPE_CHECKING中的导入只能解释标注，绝不证明运行时工厂身份。"""
        flags = set()
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "typing":
                flags.update(a.asname or a.name for a in node.names if a.name == "TYPE_CHECKING")
            elif isinstance(node, ast.Import):
                flags.update((a.asname or a.name) + ".TYPE_CHECKING" for a in node.names if a.name == "typing")
        result = {}
        for guard in self.tree.body:
            if not isinstance(guard, ast.If) or label(guard.test) not in flags:
                continue
            flag = label(guard.test).split(".")[0]
            if any(isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id == flag
                   for n in ast.walk(self.tree)):
                continue
            allowed = {n for child in (*guard.body, *guard.orelse) for n in ast.walk(child)}
            for node in guard.body:
                aliases = {}
                if isinstance(node, ast.Import):
                    aliases = {a.asname or a.name.split(".")[0]: a.name if a.asname else a.name.split(".")[0]
                               for a in node.names if a.name == "sqlalchemy" or a.name.startswith("sqlalchemy.")}
                elif isinstance(node, ast.ImportFrom) and node.module and (node.module == "sqlalchemy" or node.module.startswith("sqlalchemy.")):
                    aliases = {a.asname or a.name: node.module + "." + a.name for a in node.names if a.name != "*"}
                for name, qualified in aliases.items():
                    writes = [n for scope in self.scopes for n in self.writes(name, scope)]
                    shadowed = any(isinstance(n, ast.arg) and n.arg == name for n in ast.walk(self.tree))
                    if not shadowed and all(n in allowed for n in writes):
                        if name not in result:
                            result[name] = qualified
                        elif result[name] != qualified:
                            result[name] = None
        return result

    def annotation(self, node):
        resolved = self.resolve(node)
        if resolved:
            return resolved
        name = label(node)
        if not name:
            return None
        root, _, rest = name.partition(".")
        base = self.type_imports.get(root)
        return base + ("." + rest if rest else "") if base else None

    def owner(self, node):
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, FUNCTIONS):
                return node
        return self.tree

    def containing_class(self, node):
        while node in self.parents:
            node = self.parents[node]
            if isinstance(node, ast.ClassDef):
                return node
        return None

    def writes(self, name, scope):
        result = []
        for node in self.nodes[scope]:
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)) and label(node) == name:
                result.append(node)
            elif isinstance(node, SCOPES) and node is not scope and getattr(node, "name", None) == name:
                result.append(node)
            elif isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == name:
                result.append(node)
            elif isinstance(node, ast.ExceptHandler) and node.name == name:
                result.append(node)
        return result

    def engine(self, expression, scope, seen=frozenset()):
        """仅沿唯一赋值/标注及同类self成员追踪；歧义即停止。"""
        if isinstance(expression, ast.Call) and self.resolve(expression.func) in FACTORIES:
            return "direct_factory", expression.lineno
        name = label(expression)
        key = (name, scope)
        if not name or key in seen or len(seen) >= 32:
            return None
        seen = seen | {key}
        if isinstance(expression, ast.Attribute):
            cls = self.containing_class(scope)
            if not cls or not name.startswith("self.") or name.count(".") != 1 or self.writes("self", scope):
                return None
            methods = [s for s in self.scopes if self.containing_class(s) is cls]
            writes = [(s, n) for s in methods for n in self.writes(name, s)]
            if len(writes) != 1:
                return None
            writer, target = writes[0]
            assignment = self.parents[target]
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                return None
            origin = self.engine(assignment.value, writer, seen)
            if origin:
                return ("member_annotation" if "annotation" in origin[0] or origin[0] == "declared_engine" else "member_factory", target.lineno)
            return None
        writes = self.writes(name, scope)
        parameters = [n for n in self.nodes[scope] if isinstance(n, ast.arg) and n.arg == name]
        if parameters:
            annotation = parameters[0].annotation
            if writes or annotation is None:
                return None
            if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
                try:
                    annotation = ast.parse(annotation.value, mode="eval").body
                except (SyntaxError, ValueError):
                    return None
            return ("declared_engine", parameters[0].lineno) if self.annotation(annotation) in ENGINE_TYPES else None
        if not writes and scope is not self.tree:
            # 闭包和global/nonlocal写入不作跨作用域推断。
            if isinstance(self.parents.get(scope), (ast.Module, ast.ClassDef)):
                return self.engine(expression, self.tree, seen)
            return None
        if len(writes) != 1:
            return None
        target = writes[0]
        assignment = self.parents.get(target)
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or assignment.value is None:
            return None
        if getattr(expression, "lineno", 0) < assignment.lineno:
            return None
        return self.engine(assignment.value, scope, seen)

    def is_closing(self, call):
        if not isinstance(call, ast.Call) or len(call.args) != 1 or call.keywords:
            return False
        symbol = label(call.func)
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "contextlib":
                names = [a.asname or a.name for a in node.names if a.name == "closing"]
            elif isinstance(node, ast.Import):
                names = [(a.asname or a.name) + ".closing" for a in node.names if a.name == "contextlib"]
            else:
                continue
            if symbol in names:
                base = symbol.split(".")[0]
                # 原始import自己算一次绑定，其余重绑定/参数都使身份不明。
                bindings = sum(len(self.writes(base, s)) for s in self.scopes)
                shadowed = any(isinstance(n, ast.arg) and n.arg == base for n in ast.walk(self.tree))
                return bindings == 1 and not shadowed
        return False

    def direct_context(self, call):
        parent = self.parents.get(call)
        if self.is_closing(parent):
            parent = self.parents.get(parent)
        return isinstance(parent, ast.withitem) and parent.context_expr in (call, self.parents.get(call))

    def aliases(self, call, scope):
        assignment = self.parents.get(call)
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
            return set()
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        names = {t.id for t in targets if isinstance(t, ast.Name) and len(self.writes(t.id, scope)) == 1}
        # 仅同一次赋值的名称共享所有权；不把后续可能未执行的别名当成清理保证。
        return names

    def close_statement(self, statement, names):
        call = statement.value if isinstance(statement, ast.Expr) else None
        return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name) and call.func.value.id in names
                and call.func.attr == "close" and not call.args and not call.keywords)

    def protected(self, call, scope, names):
        if self.direct_context(call):
            return True
        if not names:
            return False
        assignment = self.parents.get(call)
        # finally必须直接先close；前置调用、条件清理和重新赋值均不作保证。
        parent = assignment
        while parent in self.parents:
            child, parent = parent, self.parents[parent]
            if parent is scope:
                break
            if isinstance(parent, ast.Try) and child is assignment and child in parent.body and parent.finalbody:
                if self.close_statement(parent.finalbody[0], names):
                    return True
        container = self.parents.get(assignment)
        for field in ("body", "orelse", "finalbody"):
            body = getattr(container, field, [])
            if assignment not in body:
                continue
            following = body[body.index(assignment) + 1:]
            # 别名赋值可能有异常和重绑定，首版只认立即开始的保护范围。
            if not following:
                continue
            first = following[0]
            if isinstance(first, ast.Try) and first.finalbody:
                if self.close_statement(first.finalbody[0], names):
                    return True
            if isinstance(first, ast.With) and first.items:
                expression = first.items[0].context_expr
                if self.is_closing(expression):
                    expression = expression.args[0]
                if isinstance(expression, ast.Name) and expression.id in names:
                    return True
            if self.close_statement(first, names):
                return True
        return False


def scan_lifecycles(tree, resolve):
    index = _Index(tree, resolve)
    findings, unknowns = [], []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute) or call.func.attr != "connect":
            continue
        scope = index.owner(call)
        origin = index.engine(call.func.value, scope)
        item = {"line": call.lineno, "symbol": label(call.func) or "connect", "rule": RULE}
        if origin is None:
            unknowns.append(item | {"rule": "connection_receiver_unresolved", "status": "unknown",
                "message": "connect receiver is not uniquely established as a SQLAlchemy Engine; no leak inferred."})
            continue
        names = index.aliases(call, scope)
        parent = index.parents.get(call)
        if index.protected(call, scope, names):
            continue
        returned = isinstance(parent, (ast.Return, ast.Yield, ast.YieldFrom)) or any(
            isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom)) and isinstance(n.value, ast.Name)
            and n.value.id in names for n in index.nodes[scope])
        stored = isinstance(parent, (ast.Assign, ast.AnnAssign)) and not names
        if returned or stored:
            unknowns.append(item | {"rule": "connection_ownership_escaped", "status": "unknown",
                "message": "Connection returned, yielded, stored or rebound; caller/long-lived ownership needs separate review."})
            continue
        cleanup = "not_established"
        if any(index.close_statement(n, names) for n in index.nodes[scope]):
            cleanup = "normal_path_only"
        elif any(isinstance(n, ast.With) and any(isinstance(w.context_expr, ast.Call)
                 and isinstance(w.context_expr.func, ast.Attribute) and w.context_expr.func.attr == "begin"
                 and label(w.context_expr.func.value) in names for w in n.items) for n in index.nodes[scope]):
            cleanup = "transaction_scope_only"
        findings.append(item | {"status": "potential_impact", "evidence_key": EVIDENCE,
            "lifecycle": {"engine_basis": origin[0], "engine_line": origin[1],
                          "ownership": "callee_unknown" if isinstance(parent, (ast.Call, ast.keyword)) else "local",
                          "cleanup": cleanup, "scope": SCOPE},
            "message": "No recognized exception-safe connection close at acquisition. Transaction exit is not connection close; callee ownership and runtime behavior remain unproven."})
    return findings, unknowns
