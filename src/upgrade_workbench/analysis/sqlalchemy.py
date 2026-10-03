"""按版本家族路由 SQLAlchemy 分析，不让 2.0 规则污染 1.4 迁移。"""

from __future__ import annotations

import ast

from ..evidence import load_version_evidence
from .dependencies import build_dependencies
from .lifecycle import EVIDENCE, RULE, scan_lifecycles

ANALYZER_V14 = {
    "name": "sqlalchemy-1.3-to-1.4-version-and-runtime-evidence",
    "version": "0.1.0",
}
ANALYZER_V2 = {"name": "sqlalchemy-1.4-to-2-bounded-ast", "version": "0.2.0"}
RULE_EVIDENCE = {
    RULE: EVIDENCE,
    "sa_connection_error_recovery": "sqlalchemy-v2-autocommit",
    "sa_select_list": "sqlalchemy-v2-select",
    "sa_bound_metadata": "sqlalchemy-v2-connectionless",
    "sa_engine_execute": "sqlalchemy-v2-connectionless",
    "sa_execute_text": "sqlalchemy-v2-execute",
    "sa_session_autocommit": "sqlalchemy-v2-session-autocommit",
}


def scan_source(name: str, content: bytes) -> tuple[list, list]:
    findings, unknowns = [], []

    def record(node, rule, symbol, message, *, unknown=False):
        target = unknowns if unknown else findings
        item = {"file": name, "line": getattr(node, "lineno", 1), "symbol": symbol,
                "rule": rule, "status": "unknown" if unknown else "potential_impact",
                "message": message}
        if not unknown:
            item["evidence_key"] = RULE_EVIDENCE[rule]
        target.append(item)

    try:
        tree = ast.parse(content, filename=name)
    except (SyntaxError, ValueError, UnicodeError, RecursionError) as error:
        record(ast.Constant(None), "unparseable_source", "module", type(error).__name__, unknown=True)
        return findings, unknowns

    # 不做全局字符串搜索：仅接受模块级唯一绑定的导入，参数/局部重新绑定时放弃推断。
    imports = {}
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def scopes(node):
        function, cls = None, None
        while node in parents:
            node = parents[node]
            if function is None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                function = node
            if cls is None and isinstance(node, ast.ClassDef):
                cls = node
        return function, cls

    conflicting = set()
    for node in ast.walk(tree):
        if any(scopes(node)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sqlalchemy"):
            for alias in node.names:
                key = alias.asname or alias.name
                value = node.module + "." + alias.name
                if key in imports and imports[key] != value:
                    conflicting.add(key)
                imports[key] = value
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlalchemy":
                    imports[alias.asname or alias.name] = alias.name
                else:
                    conflicting.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            conflicting.update(alias.asname or alias.name for alias in node.names)
    if not imports:
        return findings, unknowns
    bound = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    bound |= {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
    bound |= {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for key in bound:
        imports.pop(key, None)
    for key in conflicting:
        imports.pop(key, None)
    if "*" in conflicting:
        imports.clear()

    def resolve(node):
        if isinstance(node, ast.Name):
            return imports.get(node.id)
        if isinstance(node, ast.Attribute):
            base = resolve(node.value)
            return base + "." + node.attr if base else None
        return None

    # 只跟踪直接 create_engine 赋值；任意 engine 命名的参数不自动视作 SQLAlchemy。
    engines = set()
    invalid = set()

    def receiver_key(node, expression):
        function, cls = scopes(node)
        if isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name) and expression.value.id == "self":
            return cls, ast.unparse(expression)
        return function, ast.unparse(expression)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, (ast.Name, ast.Attribute)):
                    continue
                receiver = receiver_key(node, target)
                if isinstance(node.value, ast.Call) and resolve(node.value.func) == "sqlalchemy.create_engine":
                    engines.add(receiver)
                else:
                    invalid.add(receiver)
    # 条件注入的 self.engine 仍可能是外部对象，必须标为潜在风险而非确定移除。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        symbol = resolve(node.func)
        if symbol in {"sqlalchemy.select", "sqlalchemy.sql.select", "sqlalchemy.sql.expression.select"}:
            if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
                record(node, "sa_select_list", ast.unparse(node.func), "Review positional select expressions; list constructor is removed.")
        elif symbol in {"sqlalchemy.MetaData", "sqlalchemy.schema.MetaData"}:
            if any(keyword.arg == "bind" for keyword in node.keywords):
                record(node, "sa_bound_metadata", ast.unparse(node.func), "Bound metadata is removed; pass connection explicitly.")
        elif symbol in {"sqlalchemy.orm.Session", "sqlalchemy.orm.sessionmaker"}:
            if any(keyword.arg == "autocommit" and isinstance(keyword.value, ast.Constant)
                   and keyword.value.value is True for keyword in node.keywords):
                record(node, "sa_session_autocommit", ast.unparse(node.func), "Session autocommit mode removed; preserve transaction ownership.")
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
            receiver = receiver_key(node, node.func.value)
            if receiver in engines:
                if receiver in invalid and isinstance(node.func.value, ast.Name):
                    record(node, "receiver_reassigned", "execute", "Local receiver also has non-engine assignments.", unknown=True)
                    continue
                record(node, "sa_engine_execute", ast.unparse(node.func),
                       "Potential Engine.execute removal; review injected receivers, explicit commit/rollback and result lifetime.")
                if receiver in invalid:
                    record(node, "receiver_alternative_assignment", ast.unparse(node.func),
                           "Receiver also has injected or alternative assignments; exact runtime type unresolved.", unknown=True)
            else:
                record(node, "execute_receiver_unresolved", "execute",
                       "Connection, Session and unrelated execute methods must not be treated as removed Engine.execute.", unknown=True)

    # 长连接采用 commit-as-you-go 时，一次数据库异常会让当前事务进入失败状态。
    # 这里只在同一模块明确创建 self 成员连接、通过它 execute/commit，且完全看不到
    # rollback 时报告一个有界风险；它不证明运行时一定失败，也不猜测异常类型。
    member_connects = []
    member_executes = []
    member_commits = []
    member_rollbacks = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "connect"
                and any(ast.unparse(target) == "self._conn" for target in targets)
            ):
                member_connects.append(node)
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if ast.unparse(node.func.value) != "self._conn":
            continue
        if node.func.attr == "execute":
            member_executes.append(node)
        elif node.func.attr == "commit":
            member_commits.append(node)
        elif node.func.attr == "rollback":
            member_rollbacks.append(node)
    if member_connects and member_executes and member_commits and not member_rollbacks:
        record(
            min(member_executes, key=lambda item: item.lineno),
            "sa_connection_error_recovery",
            "self._conn",
            "Long-lived member connection executes and commits, but no rollback call is visible; "
            "reuse after a failed transaction requires bounded runtime verification.",
        )
    lifecycle_findings, lifecycle_unknowns = scan_lifecycles(tree, resolve)
    findings.extend({"file": name, **item} for item in lifecycle_findings)
    unknowns.extend({"file": name, **item} for item in lifecycle_unknowns)
    return findings, unknowns


def analyze_case(case, *, candidate_reference=None):
    from ..candidates import load_candidate

    binding = load_version_evidence(case)["binding"]
    snapshot = load_candidate(case, candidate_reference)
    if binding["old_version"].startswith("1.3.") and binding["new_version"].startswith("1.4."):
        # 现有静态规则只针对 2.0 移除项和连接生命周期。1.3 -> 1.4 的
        # autoflush/cascade_backrefs 影响依赖对象状态和运行路径，不能伪造成源码命中。
        return {
            "schema_version": 1,
            "case_id": case.manifest.case_id,
            "case_fingerprint": case.fingerprint,
            "analyzer": ANALYZER_V14.copy(),
            "source_binding": {
                "revision": snapshot.revision,
                "files": {block["path"]: block["sha256"] for block in snapshot.blocks()},
            },
            "findings": [],
            "unknowns": [],
            "dependencies": build_dependencies(snapshot.blocks(), []),
            "scope_limitations": [
                "SQLAlchemy 2.0 removal rules are not applicable to this 1.3 -> 1.4 migration.",
                "Autoflush and cascade_backrefs behavior is evaluated from bound 1.4 documentation and isolated runtime checks; no sound source-only locator is claimed.",
            ],
        }

    findings, unknowns = [], []
    for block in snapshot.blocks():
        if block["path"].endswith(".py"):
            found, unknown = scan_source("source/" + block["path"], snapshot.files[block["path"]])
            findings.extend(found)
            unknowns.extend(unknown)
    return {"schema_version": 1, "case_id": case.manifest.case_id, "case_fingerprint": case.fingerprint,
            "analyzer": ANALYZER_V2.copy(), "source_binding": {"revision": snapshot.revision,
                "files": {block["path"]: block["sha256"] for block in snapshot.blocks()}},
            "findings": findings, "unknowns": unknowns,
            "dependencies": build_dependencies(snapshot.blocks(), findings),
            "scope_limitations": ["No complete type, control-flow or transaction inference.",
                                  "Legacy Query is supported and not automatically rewritten.",
                                  "Connection lifecycle checks recognize local syntax only; annotations are declarations, not runtime proof. No Session/async or general interprocedural ownership inference.",
                                  "Injected engine receivers are potential impacts, not proven runtime types."]}
