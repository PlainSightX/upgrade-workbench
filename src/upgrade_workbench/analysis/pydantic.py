"""对核验过的源码做有限的 Pydantic 1 -> 2 静态定位，不导入目标代码。"""

from __future__ import annotations

import ast
from typing import Any

from upgrade_workbench.cases.manifest import LoadedCase

ANALYZER = {"name": "pydantic-v1-to-v2-bounded-ast", "version": "0.3.0"}
RULE_EVIDENCE = {
    "nullable_without_default": "pydantic-v2-required-nullable-fields",
    "any_without_default": "pydantic-v2-required-nullable-fields",
    "class_config": "pydantic-v2-config",
    "legacy_validator": "pydantic-v2-validators",
    "basesettings_moved": "pydantic-v2-basesettings",
    "import_moved_or_removed": "pydantic-v2-import-moves",
    "pydantic_dataclass": "pydantic-v2-dataclasses",
    "custom_type_hook": "pydantic-v2-custom-types",
}
IMPORT_MOVES = {
    "pydantic.BaseSettings": "pydantic_settings.BaseSettings",
    "pydantic.generics.GenericModel": "pydantic.BaseModel",
    "pydantic.error_wrappers.ValidationError": "pydantic.ValidationError",
    "pydantic.parse.Protocol": "removed; review caller behavior before replacement",
    "pydantic.color.Color": "pydantic_extra_types.color.Color",
    "pydantic.types.PaymentCardNumber": "pydantic_extra_types.payment.PaymentCardNumber",
    "pydantic.types.PaymentCardBrand": "pydantic_extra_types.payment.PaymentCardBrand",
}
LIMITATIONS = [
    "Only Pydantic 1 to 2 rules listed in this report are checked; no complete migration claim.",
    "Only direct module-level BaseModel/BaseSettings subclasses are field-analyzed.",
    "Pydantic dataclass decorators are source-order resolved; their fields are not BaseModel fields.",
    "Custom type hooks are syntactic protocol occurrences, including conditional definitions; "
    "their runtime reachability and use by Pydantic are not established.",
    "Cross-file inheritance, function-local classes, dynamic namespaces and runtime mutation "
    "are unresolved; imports and ordinary bindings are tracked in source order.",
    "Annotations do not establish business intent; potential impacts require frozen behavior tests.",
    "An empty finding list does not prove compatibility; no target source is imported or executed.",
]
Env = dict[str, str | None]


def _resolve(node: ast.expr, env: Env) -> str | None:
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = _resolve(node.value, env)
        return f"{owner}.{node.attr}" if owner else None
    return None


def _bound_names(node: ast.AST) -> set[str]:
    """控制流中的绑定不选分支，统一使相关名称失去确定性。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)
        elif isinstance(child, ast.Import):
            names.update(alias.asname or alias.name.split(".")[0] for alias in child.names)
        elif isinstance(child, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in child.names)
        elif isinstance(child, ast.ExceptHandler) and child.name:
            names.add(child.name)
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, (ast.Store, ast.Del)):
            owner = child.value
            while isinstance(owner, ast.Attribute):
                owner = owner.value
            if isinstance(owner, ast.Name):
                names.add(owner.id)
    return names


def _annotation(node: ast.expr, env: Env) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _annotation(ast.parse(node.value, mode="eval").body, env)
        except (SyntaxError, ValueError, RecursionError):
            return None
    name = _resolve(node, env)
    if name in {"typing.Any", "typing_extensions.Any"}:
        return "any"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for member in (node.left, node.right):
            if isinstance(member, ast.Constant) and member.value is None:
                return "nullable"
            if _annotation(member, env) == "nullable":
                return "nullable"
    if isinstance(node, ast.Subscript):
        wrapper = _resolve(node.value, env)
        members = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        if wrapper in {"typing.Optional", "typing_extensions.Optional"}:
            return "nullable"
        if wrapper in {"typing.Union", "typing_extensions.Union"}:
            if any(isinstance(member, ast.Constant) and member.value is None for member in members):
                return "nullable"
        if wrapper in {"typing.Annotated", "typing_extensions.Annotated"} and members:
            return _annotation(members[0], env)
    return None


def _field_default(value: ast.expr | None, env: Env) -> str:
    if value is None:
        return "absent"
    if isinstance(value, ast.Constant) and value.value is Ellipsis:
        return "explicit_required"
    if isinstance(value, ast.Call) and _resolve(value.func, env) == "pydantic.Field":
        if any(keyword.arg is None for keyword in value.keywords):
            return "unresolved"
        if any(keyword.arg == "default_factory" for keyword in value.keywords):
            return "factory"
        defaults = [keyword.value for keyword in value.keywords if keyword.arg == "default"]
        if value.args:
            defaults.insert(0, value.args[0])
        if not defaults:
            return "absent"
        if len(defaults) != 1 or isinstance(defaults[0], ast.Starred):
            return "unresolved"
        default = defaults[0]
        if isinstance(default, ast.Constant) and default.value is Ellipsis:
            return "explicit_required"
        return "provided"
    if isinstance(value, ast.Call) and _resolve(value.func, env) is None:
        return "unresolved"
    return "provided"


def _annotated_default(annotation: ast.expr, env: Env) -> str:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return _annotated_default(ast.parse(annotation.value, mode="eval").body, env)
        except (SyntaxError, ValueError, RecursionError):
            return "unresolved"
    if isinstance(annotation, ast.Subscript) and _resolve(annotation.value, env) in {
        "typing.Annotated", "typing_extensions.Annotated"
    } and isinstance(annotation.slice, ast.Tuple):
        metadata = annotation.slice.elts[1:]
        fields = [
            member for member in metadata
            if isinstance(member, ast.Call) and _resolve(member.func, env) == "pydantic.Field"
        ]
        if len(fields) > 1 or len(fields) != len(metadata):
            return "unresolved"
        if fields:
            return _field_default(fields[0], env)
    return "absent"


class _FileAnalyzer:
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.findings: list[dict[str, Any]] = []
        self.unknowns: list[dict[str, Any]] = []

    def record(
        self, node: ast.AST, symbol: str, rule: str, reason: str, evidence_key: str | None = None
    ) -> None:
        if evidence_key is not None and RULE_EVIDENCE.get(rule) != evidence_key:
            raise ValueError(f"Unregistered rule/evidence mapping: {rule}")
        item = {
            "file": self.filename,
            "line": getattr(node, "lineno", 1),
            "column": getattr(node, "col_offset", 0),
            "symbol": symbol,
            "rule": rule,
            "status": "potential_impact" if evidence_key else "unknown",
            "evidence_key": evidence_key,
            "reason": reason,
        }
        (self.findings if evidence_key else self.unknowns).append(item)

    def imports(self, node: ast.Import | ast.ImportFrom, env: Env) -> None:
        for alias in node.names:
            if alias.name == "*":
                env.update(dict.fromkeys(env))
                self.record(node, "*", "wildcard_import", "Wildcard bindings are not resolved.")
                continue
            if isinstance(node, ast.Import):
                qualified = alias.name
                binding = alias.asname or qualified.split(".")[0]
                env[binding] = qualified if alias.asname else qualified.split(".")[0]
            else:
                qualified = f"{node.module}.{alias.name}" if node.module else alias.name
                binding = alias.asname or alias.name
                env[binding] = None if node.level else qualified
            if not getattr(node, "level", 0) and qualified in IMPORT_MOVES:
                settings = qualified == "pydantic.BaseSettings"
                self.record(
                    node, qualified, "basesettings_moved" if settings else "import_moved_or_removed",
                    f"Version 2 location/status: {IMPORT_MOVES[qualified]}.",
                    "pydantic-v2-basesettings" if settings else "pydantic-v2-import-moves",
                )

    def model(self, node: ast.ClassDef, outer: Env, *, model_fields: bool = True) -> None:
        env = outer.copy()
        for statement in node.body:
            if model_fields and isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                symbol = f"{node.name}.{statement.target.id}"
                kind = _annotation(statement.annotation, env)
                default = _field_default(statement.value, env)
                if default == "absent":
                    default = _annotated_default(statement.annotation, env)
                if kind and default == "absent":
                    self.record(
                        statement, symbol, "any_without_default" if kind == "any"
                        else "nullable_without_default",
                        "Version 1 may supply implicit None; version 2 requires this field. "
                        "Decide omission behavior from the business contract, not nullability alone.",
                        "pydantic-v2-required-nullable-fields",
                    )
                elif kind and default == "unresolved":
                    self.record(statement, symbol, "dynamic_field_default", "Field default is dynamic.")
                elif not kind and any(
                    isinstance(part, ast.Name) and env.get(part.id) is None
                    for part in ast.walk(statement.annotation)
                    if isinstance(part, ast.Name) and part.id in env
                ):
                    self.record(
                        statement, symbol, "unresolved_annotation",
                        "An annotation name was shadowed or conditionally bound.",
                    )
            elif model_fields and isinstance(statement, ast.ClassDef) and statement.name == "Config":
                self.record(
                    statement, f"{node.name}.Config", "class_config",
                    "Class Config is deprecated in version 2; review key changes before model_config.",
                    "pydantic-v2-config",
                )
            elif isinstance(statement, ast.ClassDef):
                self.record(
                    statement, f"{node.name}.{statement.name}", "nested_class",
                    "Nested class scope is not resolved by this analyzer.",
                )
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in statement.decorator_list:
                    function = decorator.func if isinstance(decorator, ast.Call) else decorator
                    name = _resolve(function, env)
                    if name in {"pydantic.validator", "pydantic.root_validator"}:
                        self.record(
                            decorator, f"{node.name}.{statement.name}", "legacy_validator",
                            f"{name} is deprecated; review signatures, ordering and exception behavior.",
                            "pydantic-v2-validators",
                        )
                    elif name is None:
                        self.record(
                            decorator, f"{node.name}.{statement.name}", "unresolved_decorator",
                            "Decorator binding cannot be established; no migration rule inferred.",
                        )
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                self.imports(statement, env)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                env[statement.name] = None
            else:
                env.update(dict.fromkeys(_bound_names(statement)))

    def scan(self, tree: ast.Module) -> None:
        # 钩子名字提示协议迁移，但不证明条件分支执行或此类真的被模型使用。
        for cls in (part for part in ast.walk(tree) if isinstance(part, ast.ClassDef)):
            for method in cls.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name in {
                    "__get_validators__", "__modify_schema__",
                }:
                    self.record(
                        method, f"{cls.name}.{method.name}", "custom_type_hook",
                        "Pydantic custom-type protocol spelling found; inspect signature/schema "
                        "migration if this type is used. Runtime reachability is unknown.",
                        "pydantic-v2-custom-types",
                    )
        env: Env = {}
        for statement in tree.body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                self.imports(statement, env)
                continue
            if isinstance(statement, ast.ClassDef):
                dataclass_decorators = [
                    decorator for decorator in statement.decorator_list
                    if _resolve(decorator.func if isinstance(decorator, ast.Call) else decorator, env)
                    == "pydantic.dataclasses.dataclass"
                ]
                for decorator in dataclass_decorators:
                    self.record(
                        decorator, statement.name, "pydantic_dataclass",
                        "Review validation timing, __post_init__, extra handling and config; "
                        "do not apply BaseModel implicit-default rules to dataclass fields.",
                        "pydantic-v2-dataclasses",
                    )
                bases = [_resolve(base, env) for base in statement.bases]
                direct = any(base in {"pydantic.BaseModel", "pydantic.BaseSettings"} for base in bases)
                if direct:
                    for base_node, base_name in zip(statement.bases, bases, strict=True):
                        if base_name == "pydantic.BaseSettings":
                            self.record(
                                base_node, statement.name, "basesettings_moved",
                                "BaseSettings moved to the pydantic-settings package in version 2.",
                                "pydantic-v2-basesettings",
                            )
                    self.model(statement, env)
                    if len(bases) > 1 or statement.keywords or statement.decorator_list:
                        self.record(
                            statement, statement.name, "modified_model_construction",
                            "Mixins, metaclasses or decorators may alter the model; effects are unresolved.",
                        )
                elif dataclass_decorators:
                    self.model(statement, env, model_fields=False)
                elif statement.bases and not all(
                    isinstance(base, ast.Name) and base.id == "object" and "object" not in env
                    for base in statement.bases
                ):
                    self.record(
                        statement, statement.name, "unresolved_inheritance",
                        "Not a verified direct Pydantic base: "
                        + ", ".join(ast.unparse(base) for base in statement.bases),
                    )
                env[statement.name] = None
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(statement):
                    if isinstance(child, ast.ClassDef):
                        self.record(
                            child, f"{statement.name}.{child.name}", "function_local_class",
                            "Deferred globals and function-local bindings are not resolved.",
                        )
                env[statement.name] = None
                continue
            if isinstance(statement, (ast.If, ast.Try, ast.TryStar, ast.For, ast.AsyncFor,
                                      ast.While, ast.With, ast.AsyncWith, ast.Match)):
                self.record(
                    statement, "<module>", "conditional_or_contextual_bindings",
                    "Branch/context execution is not modeled; affected names are invalidated.",
                )
                if "*" in _bound_names(statement):
                    env.update(dict.fromkeys(env))
            if any(
                isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                and child.func.id in {"exec", "eval"}
                for child in ast.walk(statement)
            ):
                env.update(dict.fromkeys(env))
                self.record(statement, "<module>", "dynamic_namespace", "Dynamic execution is unresolved.")
            env.update(dict.fromkeys(_bound_names(statement)))


def analyze_case(case: LoadedCase, *, candidate_reference: dict | None = None) -> dict[str, Any]:
    """重新核验每个源码文件后解析 AST；损坏输入失败退出，语法问题保留为 unknown。"""
    from upgrade_workbench.candidates import load_candidate

    from .dependencies import build_dependencies

    snapshot = load_candidate(case, candidate_reference)
    findings: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []
    for block in snapshot.blocks():
        name = "source/" + block["path"]
        if not name.endswith(".py"):
            continue
        content = snapshot.files[block["path"]]
        analyzer = _FileAnalyzer(name)
        try:
            tree = ast.parse(content, filename=name)
        except (SyntaxError, ValueError, UnicodeError, RecursionError) as error:
            location = ast.Constant(value=None)
            location.lineno = getattr(error, "lineno", None) or 1
            analyzer.record(location, "<module>", "unparseable_source", str(error))
        else:
            analyzer.scan(tree)
        findings.extend(analyzer.findings)
        unknowns.extend(analyzer.unknowns)
    return {
        "schema_version": 1,
        "case_id": case.manifest.case_id,
        "case_fingerprint": case.fingerprint,
        "analyzer": ANALYZER.copy(),
        "source_binding": {
            "revision": snapshot.revision,
            "files": {block["path"]: block["sha256"] for block in snapshot.blocks()},
        },
        "findings": findings,
        "unknowns": unknowns,
        "dependencies": build_dependencies(snapshot.blocks(), findings),
        "scope_limitations": LIMITATIONS.copy(),
    }
