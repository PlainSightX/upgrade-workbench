"""模型角色的最小权限与输出合同；角色身份不授予任务状态权限。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


class RoleContractError(ValueError):
    """角色输出或权限请求违反固定合同。"""


Validator = Callable[[object], dict]

# 交接中的建议类别不是立即执行的工具名；提示和校验共用同一组取值。
INVESTIGATION_NEXT_ACTIONS = (
    "read_source", "search_source", "query_dependency", "request_observation", "stop"
)


@dataclass(frozen=True)
class RoleSpec:
    role: str
    output_format: str
    validator: Validator | None
    candidate_write_allowed: bool
    terminal_write_allowed: bool
    observation_request_allowed: bool
    recoverable_statuses: frozenset[str]

    def validate_output(self, value: object, *, protocol_validator: Validator | None = None) -> dict:
        validator = protocol_validator if self.role in {"solver", "repository_reader"} else self.validator
        if validator is None:
            raise RoleContractError(f"{self.role} requires its protocol validator")
        return validator(value)

    def require_candidate_write(self) -> None:
        if not self.candidate_write_allowed:
            raise RoleContractError(f"Role {self.role} cannot write or select a candidate")


def recoverable_attempt_contract(task: dict, attempt: dict) -> tuple[RoleSpec, str]:
    """恢复前绑定任务 schema、角色和请求格式；新 schema 不接受隐式身份。"""
    schema_version = task.get("schema_version")
    legacy_formats = {
        3: {"solver": "diagnostic_actions"},
        4: {"solver": "contract_actions", "contract_auditor": "contract_audit"},
    }
    preparation = task.get("protocol", {}).get("repository_preparation")
    if preparation is not None:
        if schema_version != 3:
            raise RoleContractError("Repository preparation requires P4")
        role, expected_format = preparation_contract(preparation, attempt.get("preparation_phase"))
        if attempt.get("role") != role or not re.fullmatch(r"[0-9a-f]{64}", attempt.get("request_sha256", "")):
            raise RoleContractError("Preparation requires explicit role and request identity")
        spec = role_spec(role)
        output_format = attempt.get("output_format")
    elif schema_version == 5:
        role = attempt.get("role")
        output_format = attempt.get("output_format")
        if not isinstance(role, str) or not isinstance(output_format, str):
            raise RoleContractError(
                "Schema 5 recovery requires an explicit attempt role and output_format"
            )
        spec = role_spec(role)
        expected_format = spec.output_format
    elif schema_version in legacy_formats:
        role = attempt.get("role", "solver")
        if role not in legacy_formats[schema_version]:
            raise RoleContractError("Attempt role is not available in this task schema")
        spec = role_spec(role)
        expected_format = legacy_formats[schema_version][role]
        output_format = attempt.get("output_format", expected_format)
    else:
        raise RoleContractError("Role-bound recovery requires task schema 3, 4, or 5")
    if output_format != expected_format:
        raise RoleContractError(
            f"Role {role} requires output_format {expected_format!r} at this recovery boundary"
        )
    return spec, expected_format


PREPARATION_ACTIONS = frozenset({
    "list_sources", "outline_source", "search_source", "read_source", "get_observation",
    "record_project_knowledge", "read_project_topic", "query_project_relations",
})


def preparation_policy(value):
    if (not isinstance(value, dict)
            or set(value) != {"version", "mode", "max_calls", "solver_reserved_calls"}
            or type(value["version"]) is not int or value["version"] != 1
            or value["mode"] not in {"same_solver", "reader_then_solver"}
            or any(type(value[key]) is not int or not 1 <= value[key] <= 30
                   for key in ("max_calls", "solver_reserved_calls"))):
        raise RoleContractError("Invalid repository preparation policy")
    return dict(value)


def preparation_contract(config, phase):
    config = preparation_policy(config)
    if phase not in {"prepare", "solve"}:
        raise RoleContractError("Explicit preparation phase required")
    if phase == "solve":
        return "solver", "diagnostic_actions"
    role = "repository_reader" if config["mode"] == "reader_then_solver" else "solver"
    return role, "repository_context_actions"


def require_preparation_action(config, *, role, phase, action_type):
    if config is None:
        if role != "solver" or phase is not None:
            raise RoleContractError("Preparation role requires frozen policy")
        return
    expected_role, _ = preparation_contract(config, phase)
    if role != expected_role:
        raise RoleContractError("Role differs from frozen preparation phase")
    if phase == "prepare" and action_type not in PREPARATION_ACTIONS:
        raise RoleContractError("Preparation permits static source/knowledge actions only")


def _text(value: object, field: str, limit: int = 700) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or "\x00" in value
        or len(value) > limit
    ):
        raise RoleContractError(f"Invalid bounded text for {field}")
    return value


def _references(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise RoleContractError(f"{field} requires at most eight public references")
    for reference in value:
        _text(reference, field, 256)
        if not re.fullmatch(
            r"business_contract|(?:source|version|observation|historical|fact):[^\s]+",
            reference,
        ):
            raise RoleContractError(f"Invalid reference in {field}")
    return value


def validate_investigation(value: object) -> dict:
    """Investigator 只交付有来源的事实与待判别问题，不接受补丁或终态动作。"""
    required = {
        "observed_facts",
        "scope_limits",
        "remaining_hypotheses",
        "conflicts",
        "next_discriminating_action",
    }
    if not isinstance(value, dict):
        raise RoleContractError("Investigation handoff requires the exact read-only schema")
    if set(value) != required:
        extra = sorted(str(key)[:80] for key in set(value) - required)
        raise RoleContractError(
            "Investigation handoff requires the exact read-only schema; invalid fields: "
            f"missing={sorted(required - set(value))}; extra={extra[:5]}; allowed={sorted(required)}. "
            "handoff does not accept issues; use remaining_hypotheses, conflicts or scope_limits."
        )
    facts = value["observed_facts"]
    if not isinstance(facts, list) or len(facts) > 12:
        raise RoleContractError("observed_facts must contain at most twelve facts")
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict) or set(fact) != {"statement", "scope", "evidence_refs"}:
            raise RoleContractError(f"Invalid observed_facts[{index}]")
        _text(fact["statement"], f"observed_facts[{index}].statement")
        _text(fact["scope"], f"observed_facts[{index}].scope")
        if not _references(fact["evidence_refs"], f"observed_facts[{index}].evidence_refs"):
            raise RoleContractError("Observed facts require evidence")
    limits = value["scope_limits"]
    if not isinstance(limits, list) or len(limits) > 8:
        raise RoleContractError("scope_limits must contain at most eight items")
    for index, limit in enumerate(limits):
        _text(limit, f"scope_limits[{index}]")
    hypotheses = value["remaining_hypotheses"]
    if not isinstance(hypotheses, list) or len(hypotheses) > 8:
        raise RoleContractError("remaining_hypotheses must contain at most eight items")
    for index, hypothesis in enumerate(hypotheses):
        if not isinstance(hypothesis, dict) or set(hypothesis) != {
            "hypothesis", "evidence_refs", "next_check"
        }:
            raise RoleContractError(f"Invalid remaining_hypotheses[{index}]")
        _text(hypothesis["hypothesis"], f"remaining_hypotheses[{index}].hypothesis")
        _references(hypothesis["evidence_refs"], f"remaining_hypotheses[{index}].evidence_refs")
        _text(hypothesis["next_check"], f"remaining_hypotheses[{index}].next_check")
    conflicts = value["conflicts"]
    if not isinstance(conflicts, list) or len(conflicts) > 8:
        raise RoleContractError("conflicts must contain at most eight items")
    for index, conflict in enumerate(conflicts):
        if not isinstance(conflict, dict) or set(conflict) != {
            "claim", "conflict", "evidence_refs"
        }:
            raise RoleContractError(f"Invalid conflicts[{index}]")
        _text(conflict["claim"], f"conflicts[{index}].claim")
        _text(conflict["conflict"], f"conflicts[{index}].conflict")
        if not _references(conflict["evidence_refs"], f"conflicts[{index}].evidence_refs"):
            raise RoleContractError("A conflict requires evidence")
    action = value["next_discriminating_action"]
    if action is not None:
        if not isinstance(action, dict) or set(action) != {"kind", "description", "evidence_refs"}:
            raise RoleContractError("Invalid next_discriminating_action")
        if action["kind"] not in INVESTIGATION_NEXT_ACTIONS:
            raise RoleContractError(
                "Unknown read-only investigation action in next_discriminating_action.kind. "
                "Allowed values: " + ", ".join(INVESTIGATION_NEXT_ACTIONS) + ". "
                "To recommend a probe or public check, use request_observation and describe it "
                "in description; run_probe is an execution tool, not a handoff kind. "
                "Resubmit the handoff with a valid kind; no handoff or execution was accepted"
            )
        _text(action["description"], "next_discriminating_action.description")
        _references(action["evidence_refs"], "next_discriminating_action.evidence_refs")
    if not (facts or limits or hypotheses or conflicts or action is not None):
        raise RoleContractError("Investigation handoff cannot be empty")
    return value


def _audit_validator(value: object) -> dict:
    from .contract_auditor import validate

    return validate(value)


ROLE_SPECS = {
    "repository_reader": RoleSpec(
        role="repository_reader", output_format="repository_context_actions", validator=None,
        candidate_write_allowed=False, terminal_write_allowed=False,
        observation_request_allowed=False, recoverable_statuses=frozenset({"action_ready"}),
    ),
    "solver": RoleSpec(
        role="solver",
        output_format="protocol_v6_actions",
        validator=None,
        candidate_write_allowed=True,
        terminal_write_allowed=True,
        observation_request_allowed=True,
        recoverable_statuses=frozenset({"pending_review", "agent_finished", "action_ready"}),
    ),
    "investigator": RoleSpec(
        role="investigator",
        output_format="investigator_actions",
        validator=validate_investigation,
        candidate_write_allowed=False,
        terminal_write_allowed=False,
        observation_request_allowed=True,
        recoverable_statuses=frozenset({"action_ready", "investigation_ready"}),
    ),
    "contract_auditor": RoleSpec(
        role="contract_auditor",
        output_format="contract_audit",
        validator=_audit_validator,
        candidate_write_allowed=False,
        terminal_write_allowed=False,
        observation_request_allowed=False,
        recoverable_statuses=frozenset({"audit_ready"}),
    ),
}


def role_spec(role: str) -> RoleSpec:
    try:
        return ROLE_SPECS[role]
    except KeyError as error:
        raise RoleContractError(f"Unknown model role {role!r}") from error
