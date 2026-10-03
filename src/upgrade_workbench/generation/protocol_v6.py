"""Protocol 6: bounded dependency facts, candidate selection, and investigator roles."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from . import protocol_v5
from .actions import AgentActionError

FORMAT = "protocol_v6_actions"
INVESTIGATOR_FORMAT = "investigator_actions"
DIAGNOSTIC_ACTIONS = protocol_v5.DIAGNOSTIC_ACTIONS | {"query_dependency", "get_fact"}
DIAGNOSTIC_POLICY = dict(protocol_v5.DIAGNOSTIC_POLICY)
DEPENDENCY_QUERY_POLICY = {"max_queries": 8, "max_result_bytes": 24_000}
DEFAULT_ROLE_BUDGET = {"investigator_max_calls": 3, "solver_reserved_calls": 2}
DECISION_OBJECTIVE = {
    "schema_version": 1,
    "mode": "bounded_local_repair",
    "goal": "advance_current_candidate_against_registered_public_contract",
    "decision_boundary": "best_evidence_backed_candidate_or_unresolved",
    "acceptance_scope": "public_progress_only",
}

COMPARISON_BINDING_FIELDS = frozenset({
    "schema_version",
    "decision_point_id",
    "case_fingerprint",
    "historical_primary_input_sha256",
    "candidate_revision",
    "candidate_sha256",
    "decision_objective_sha256",
    "common_protocol_sha256",
    "pair_input_sha256",
})

_DIGEST = re.compile(r"[0-9a-f]{64}")
_DISTRIBUTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_QUALNAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_DEPENDENCY_FIELDS = {
    "list_files": {"prefix"},
    "search_text": {"query", "paths", "max_results"},
    "read_file": {"path", "start_line", "end_line"},
    "inspect_symbol": {"module", "qualname"},
}


def validate_decision_objective(value: object) -> dict | None:
    """目标只描述局部决策，不允许夹带根因、源码位置或人工修法。"""
    if value is None:
        return None
    if (
        type(value) is not dict
        or set(value) != set(DECISION_OBJECTIVE)
        or any(
            type(value[key]) is not type(expected) or value[key] != expected
            for key, expected in DECISION_OBJECTIVE.items()
        )
    ):
        raise ValueError("Protocol 6 decision objective must use the frozen bounded contract")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 512:
        raise ValueError("Protocol 6 decision objective exceeds its capacity")
    return json.loads(encoded)


def decision_objective_sha256(value: object) -> str:
    frozen = validate_decision_objective(value)
    if frozen is None:
        raise ValueError("A decision objective is required")
    encoded = json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_comparison_binding(value: object, decision_objective: object) -> dict:
    """重算完整配对身份；公开请求只暴露决策点和最终 pair 哈希。"""
    objective = validate_decision_objective(decision_objective)
    if objective is None:
        raise ValueError("A decision objective is required for comparison binding")
    if type(value) is not dict or set(value) != COMPARISON_BINDING_FIELDS:
        raise ValueError("Protocol 6 comparison binding has an invalid schema")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["decision_point_id"], str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value["decision_point_id"])
        or any(
            not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key])
            for key in COMPARISON_BINDING_FIELDS - {"schema_version", "decision_point_id"}
        )
        or value["decision_objective_sha256"] != decision_objective_sha256(objective)
    ):
        raise ValueError("Protocol 6 comparison binding identity is invalid")
    core = {
        key: value[key]
        for key in COMPARISON_BINDING_FIELDS
        if key != "pair_input_sha256"
    }
    expected_pair = hashlib.sha256(
        json.dumps(core, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if value["pair_input_sha256"] != expected_pair:
        raise ValueError("Protocol 6 comparison pair hash does not match its inputs")
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _reject(code: str, message: str) -> None:
    """Retain stable codes while remaining compatible with the pre-v6 exception shape."""
    try:
        raise AgentActionError(message, code=code)
    except TypeError:
        error = AgentActionError(message)
        error.code = code
        raise error


def _canonical_relative_path(
    value: object,
    *,
    max_length: int,
    allow_empty: bool = False,
    allow_trailing_slash: bool = False,
) -> bool:
    """按执行桥的 POSIX 相对路径语义过滤模型提供的依赖路径。"""
    if not isinstance(value, str) or len(value) > max_length:
        return False
    if not value:
        return allow_empty
    candidate = value.removesuffix("/") if allow_trailing_slash else value
    if not candidate or any(character in candidate for character in "\\:\x00"):
        return False
    path = PurePosixPath(candidate)
    return (
        not path.is_absolute()
        and path.as_posix() == candidate
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _action_fields(value: dict, required: set[str], *, optional=frozenset(), hint="") -> None:
    """列出模型可纠正的键差异；不回显额外字段的值。"""
    missing = sorted(required - set(value))
    extra = sorted(str(key)[:80] for key in set(value) - required - optional)
    if missing or extra:
        _reject(
            "action_invalid_fields",
            f"Invalid {value['type']} fields; action not executed. missing={missing}; "
            f"extra={extra[:5]}; allowed={sorted(required | optional)}; "
            f"optional={sorted(optional)}. {hint}",
        )


def _dependency_action(value: dict) -> dict:
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in _DEPENDENCY_FIELDS:
        problem = (
            f"query_dependency operation must be a string; actual_type={type(operation).__name__}"
            if not isinstance(operation, str) else "Unknown query_dependency operation"
        )
        _reject(
            "action_invalid_fields",
            f"{problem}; allowed={sorted(_DEPENDENCY_FIELDS)}. Put operation arguments at action level, "
            "not inside operation or arguments; action not executed.",
        )
    required = {"type", "environment", "operation", "distribution"} | _DEPENDENCY_FIELDS[operation]
    _action_fields(
        value, required, optional={"issues"},
        hint="Keep every operation argument at action level, not in a nested object.",
    )
    if not isinstance(value["environment"], str) or value["environment"] not in {"old", "new"}:
        _reject(
            "action_invalid_fields",
            "Dependency environment must be old or new; action not executed",
        )
    if not isinstance(value["distribution"], str) or not _DISTRIBUTION.fullmatch(
        value["distribution"]
    ):
        _reject(
            "action_invalid_fields",
            "Dependency distribution is invalid; action not executed",
        )
    if operation == "list_files":
        prefix = value["prefix"]
        if not _canonical_relative_path(
            prefix,
            max_length=256,
            allow_empty=True,
            allow_trailing_slash=True,
        ):
            _reject(
                "action_invalid_fields",
                "Dependency file prefix is invalid; action not executed",
            )
    elif operation == "search_text":
        query = value["query"]
        paths = value["paths"]
        maximum = value["max_results"]
        if not isinstance(query, str) or not query or len(query) > 200 or "\x00" in query:
            _reject(
                "action_invalid_fields",
                "Dependency text query is invalid; action not executed",
            )
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= 64
            or any(
                not _canonical_relative_path(path, max_length=512)
                for path in paths
            )
        ):
            _reject(
                "action_invalid_fields",
                "Dependency search paths are invalid; action not executed",
            )
        if type(maximum) is not int or not 1 <= maximum <= 50:
            _reject(
                "action_invalid_fields",
                "Dependency search max_results must be 1..50; action not executed",
            )
    elif operation == "read_file":
        path = value["path"]
        start, end = value["start_line"], value["end_line"]
        if not _canonical_relative_path(path, max_length=512):
            _reject(
                "action_invalid_fields",
                "Dependency file path is invalid; action not executed",
            )
        if (
            type(start) is not int
            or type(end) is not int
            or not 1 <= start <= end
            or end - start + 1 > 200
        ):
            _reject(
                "action_invalid_fields",
                "Dependency read range must contain 1..200 inclusive lines",
            )
    else:
        if not isinstance(value["module"], str) or not _MODULE.fullmatch(value["module"]):
            _reject(
                "action_invalid_fields",
                "Dependency module is invalid; action not executed",
            )
        if not isinstance(value["qualname"], str) or not _QUALNAME.fullmatch(
            value["qualname"]
        ):
            _reject(
                "action_invalid_fields",
                "Dependency qualname is invalid; action not executed",
            )
    return value


def _handoff(value: dict) -> dict:
    """Use the role registry as the single Investigator output authority."""
    from .roles import RoleContractError, role_spec

    if not isinstance(value, dict) or value.get("type") != "handoff":
        _reject(
            "action_invalid_fields",
            "Investigator handoff must be one typed handoff action; action not executed",
        )
    report = {key: item for key, item in value.items() if key != "type"}
    try:
        role_spec("investigator").validate_output(report)
    except RoleContractError as error:
        _reject("action_invalid_fields", f"{error}; action not executed")
    return value


def validate_workflow_profile(value: str) -> str:
    if not isinstance(value, str) or value not in {"workbench", "simple_tools"}:
        raise ValueError("Unknown Protocol 6 workflow profile")
    return value


def validate(case, value, snapshot, *, role: str = "solver", workflow_profile: str = "workbench", project_context_policy=None, finish_limits=None, knowledge_review_binding=None):
    """Keep a single candidate writer while allowing independent read-only investigation."""
    if role not in {"solver", "investigator"}:
        raise ValueError("Unknown protocol 6 role")
    validate_workflow_profile(workflow_profile)
    if workflow_profile == "simple_tools" and role != "solver":
        raise ValueError("The simple tools workflow has only a Solver")
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        _reject("action_invalid_fields", "One typed action is required; action not executed")
    from .knowledge_review import require_action

    try:
        require_action(knowledge_review_binding, value, snapshot, role=role, context_policy=project_context_policy)
    except ValueError as error:
        _reject("action_invalid_fields", str(error))
    action = value
    if "issues" in value and value["type"] not in {"finish", "handoff"}:
        try:
            protocol_v5._issues(value["issues"], allow_facts=True)
        except (ValueError, TypeError) as error:
            _reject("action_invalid_fields", f"{error}; action not executed")
        action = {key: item for key, item in value.items() if key != "issues"}
    from .project_context import ACTIONS
    from .project_context import validate_action as validate_knowledge

    if action["type"] in ACTIONS:
        validate_knowledge(case, snapshot, action, project_context_policy, role=role)
    else:
        _validate_action(case, action, snapshot, role=role, workflow_profile=workflow_profile, finish_limits=finish_limits)
    # 元数据保留在收据中，由运行时核验引用后再与执行参数分离。
    return value


def _validate_action(case, value, snapshot, *, role: str, workflow_profile: str, finish_limits=None):
    kind = value["type"]
    if workflow_profile == "simple_tools" and kind == "finish":
        from . import protocol_v4

        try:
            _action_fields(
                value, {"type", "reason", "explanation", "evidence_refs"},
                hint="Simple tools finish requires exactly its declared fields; finish does not accept "
                     "issues; keep bounded caveats in explanation.",
            )
            if value["reason"] not in {"candidate_ready", "no_change_claimed", "unresolved"}:
                raise ValueError("Invalid finish reason")
            limits = finish_limits or {"explanation_max_characters": 2000, "evidence_refs_max_items": 8}
            protocol_v4._text(value["explanation"], limits["explanation_max_characters"], field="finish.explanation")
            protocol_v4.evidence_refs(value["evidence_refs"], allow_facts=True, maximum=limits["evidence_refs_max_items"])
        except (ValueError, TypeError) as error:
            _reject("action_invalid_fields", str(error))
        return value
    if kind == "query_dependency":
        return _dependency_action(value)
    if kind == "get_fact":
        _action_fields(value, {"type", "fact_id"}, optional={"issues"})
        if (
            not isinstance(value["fact_id"], str)
            or not _DIGEST.fullmatch(value["fact_id"])
        ):
            _reject(
                "action_invalid_fields",
                "get_fact requires one exact fact_id; action not executed",
            )
        return value
    if role == "investigator":
        if kind == "handoff":
            return _handoff(value)
        if kind in {"submit_candidate", "restore_candidate", "finish"}:
            _reject(
                "role_write_forbidden",
                "The Investigator is read-only and cannot submit, restore, accept, reject, or "
                "finish a candidate",
            )
    return protocol_v5.validate(case, value, snapshot, allow_facts=True, finish_limits=finish_limits)


def _dependency_examples() -> str:
    arguments = {
        "list_files": {"prefix": "example_dependency"},
        "search_text": {"query": "Thing", "paths": ["example_dependency/api.py"], "max_results": 5},
        "read_file": {"path": "example_dependency/api.py", "start_line": 1, "end_line": 200},
        "inspect_symbol": {"module": "example_dependency.api", "qualname": "Thing"},
    }
    examples = []
    for operation, fields in _DEPENDENCY_FIELDS.items():
        action = {
            "type": "query_dependency", "environment": "new", "operation": operation,
            "distribution": "example-dependency",
            **{field: arguments[operation][field] for field in sorted(fields)},
        }
        examples.append(json.dumps({"summary": "Inspect the locked dependency.", "action": action}))
    return "\n" + "\n".join(examples) + "\n"


def instructions(*, role: str = "solver", workflow_profile: str = "workbench") -> str:
    from .roles import INVESTIGATION_NEXT_ACTIONS

    validate_workflow_profile(workflow_profile)
    if workflow_profile == "simple_tools" and role != "solver":
        raise ValueError("The simple tools workflow has only a Solver")
    base = protocol_v5.instructions()
    base = base.replace(
        "Any action may include issues:", "Every action except finish and handoff may include issues:",
    ).replace(
        "move issues from the top level into action.",
        "move issues into a non-terminal action; for finish or handoff, remove issues and use their declared fields.",
    )
    if workflow_profile == "simple_tools":
        # 共用工具和真实执行边界，仅取消逐条覆盖自报及自动候选推荐。
        base = base.split("When restore_candidates", 1)[0] + (
            "candidate_options lists reviewed historical candidates in history order with public observations. "
            "You may manually restore one using {type:restore_candidate,revision,reason,evidence_refs}; "
            "cite its exposed public_observation_ref. Restoration does not establish acceptance. "
            "Finish with exactly {type:finish,reason,explanation,evidence_refs}. reason is candidate_ready, "
            "no_change_claimed or unresolved. Do not include contract_coverage. The business contract and "
            "contract_requirements are the full task requirements; no per-requirement self-report is required. "
            "candidate_ready requires actual reviewed changes and current completed passing public checks. "
            "Known measured counterexamples must be repaired and checked again. Cite and explain current "
            "probe failures and observation-only limitations. no_change_claimed requires unchanged source "
            "and current passing public checks. You may finish unresolved when you cannot deliver. "
            "Finish submits a candidate for separate independent final acceptance; it never grants approval. "
            "On FINAL_CALL_FINISH_ONLY, return finish rather than spending the final call on another action. "
        )
    dependency = (
        " Protocol 6 adds read-only dependency facts. operation is one string: "
        + ", ".join(_DEPENDENCY_FIELDS)
        + ". All its parameters are fields of action; never nest them inside operation or arguments. "
        "environment is old or new. Dependency paths are canonical relative POSIX paths. "
        "read_file uses 1-based inclusive ranges of at most 200 lines. search_text uses a literal query "
        "of at most 200 characters, 1..64 paths, and max_results from 1..50. "
        "Complete query examples (replace example names using the registered dependency):"
        + _dependency_examples()
        + "Every non-terminal action may include issues:[{hypothesis,evidence_refs,unknown,next_observation}]. "
        "At most 4 issues, 8 references per issue and 1000 characters per text field; issues must be a list, "
        "never null. finish and handoff do not accept issues; put caveats in their declared fields. "
        "Use {type:get_fact,fact_id,issues?} with the exact registered fact ID. "
        "These queries inspect only the registered locked "
        "dependency environment; they do not establish application call-site applicability or business "
        "behavior. Use get_fact with one exact fact_id to reread an immutable result. Cite facts as "
        "fact:<id> in evidence_refs, including issue references; only existing task facts are valid. "
        "Facts can guide investigation but never establish behavioral observation support. "
        "Do not relabel them as observations. If diagnostic_state.historical_inputs is "
        "present, it contains an immutable copy of material visible at an earlier decision cutoff. Use "
        "that material to choose the next investigation or repair, but do not treat embedded prior "
        "observations, counters, task IDs, or budgets as current execution evidence. Active task_context "
        "and the current diagnostic_state own the present task. Do not cite an embedded historical "
        "observation or fact ID unless the same ID is separately exposed by the current task."
    )
    if role == "solver":
        return base + dependency
    return (
        base.split("Submit incremental edits", 1)[0]
        + dependency
        + " You are an independent read-only Investigator. You may navigate source, query locked "
        "dependencies, request reviewed public observations, and reread facts. You cannot edit, restore, "
        "approve, reject or finish any candidate. When the bounded investigation is sufficient, return "
        "handoff with type plus exactly observed_facts, scope_limits, remaining_hypotheses, conflicts "
        "and next_discriminating_action. Each observed fact contains statement, scope and evidence_refs; "
        "each remaining hypothesis contains hypothesis, evidence_refs and next_check; each conflict contains "
        "claim, conflict and evidence_refs. next_discriminating_action is null or contains kind, description "
        "and evidence_refs. Its kind must be one of: "
        + ", ".join(INVESTIGATION_NEXT_ACTIONS)
        + ". These are recommendation categories, not execution tool names. To recommend "
        "run_probe, propose_probe or run_public_checks, use request_observation and describe the "
        "desired observation; the handoff itself executes nothing. Use at most 12 observed_facts, "
        "8 scope_limits, 8 remaining_hypotheses and 8 conflicts; each text field is at most 700 "
        "characters, with at most 8 references per evidence_refs list. Return summary and action "
        "in ONE JSON object, not two separate objects. task_context.edit_feedback discloses the "
        "remaining Investigator calls, including this call; it is separate from total task calls. "
        "On the last Investigator call, return handoff using available evidence and explicitly "
        "list unknowns rather than requesting another tool. A handoff need not establish a repair "
        "or business acceptance. Keep claims scoped to cited public evidence; a handoff is advisory, "
        "not fact. "
    )
