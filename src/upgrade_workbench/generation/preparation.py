"""可选P4准备合同；身份由宿主冻结，角色不能自授权限。"""

from __future__ import annotations

import hashlib
import json

from .roles import preparation_contract, preparation_policy, require_preparation_action


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def phase(task):
    if task["preparation_state"]["status"] == "completed":
        return "solve"
    return "prepare"


def binding(task, role):
    from pathlib import Path

    from ..candidates import load_candidate
    from ..cases import load_case

    policy = preparation_policy(task["protocol"]["repository_preparation"])
    state = task["preparation_state"]
    current_phase = phase(task)
    expected_role, _ = preparation_contract(policy, current_phase)
    if role != expected_role:
        raise ValueError("Preparation role differs from current phase")
    if current_phase == "prepare" and load_candidate(load_case(Path(task["manifest_path"])), task["current_candidate"]).revision != state["start_revision"]:
        raise ValueError("Preparation source changed before handoff")
    return {
        "policy": policy, "policy_sha256": digest(policy), "task_id": task["task_id"],
        "phase": current_phase, "role": role, "start_revision": state["start_revision"],
        "handoff_reference": state["handoff_reference"],
        "remaining_preparation_calls": policy["max_calls"] - len(state["attempt_indices"]),
    }


def validate_binding(value):
    if not isinstance(value, dict) or set(value) != {
        "policy", "policy_sha256", "task_id", "phase", "role", "start_revision",
        "handoff_reference", "remaining_preparation_calls",
    }:
        raise ValueError("Invalid frozen preparation binding")
    policy = preparation_policy(value["policy"])
    role, output = preparation_contract(policy, value["phase"])
    remaining = value["remaining_preparation_calls"]
    if (value["policy_sha256"] != digest(policy) or value["role"] != role
            or type(remaining) is not int or not 0 <= remaining <= policy["max_calls"]
            or (value["phase"] == "prepare" and value["handoff_reference"] is not None)
            or (value["phase"] == "solve" and not isinstance(value["handoff_reference"], dict))):
        raise ValueError("Frozen preparation policy, phase or handoff changed")
    return role, output


def action_options(value):
    role, _ = validate_binding(value)
    return {"role": role, "preparation_policy": value["policy"],
            "preparation_phase": value["phase"]}


def verify_response(task, report):
    """接纳与恢复共用，绑定确切的旧请求而非当前阶段猜测。"""
    from ..diagnostics import _root, read

    attempts = [a for a in task["attempts"] if a["receipt"] == report.get("report_path")]
    if len(attempts) != 1:
        raise ValueError("Preparation response requires exactly one frozen attempt")
    attempt = attempts[0]
    value = report.get("preparation_binding")
    role, output = validate_binding(value)
    frozen = read(report["diagnostic_context_reference"], _root(task))
    if (value != attempt.get("preparation_binding") or value != frozen.get("preparation_binding")
            or value["policy"] != task["protocol"]["repository_preparation"]
            or value["task_id"] != task["task_id"] or frozen["task_id"] != task["task_id"]
            or frozen.get("context_role") != role or report.get("role") != role
            or attempt.get("role") != role or report.get("output_format") != output
            or attempt.get("output_format") != output
            or attempt.get("preparation_phase") != value["phase"]
            or attempt.get("request_sha256") != report.get("request_sha256")):
        raise ValueError("Preparation response identity changed")
    state = task["preparation_state"]
    if value["start_revision"] != state["start_revision"]:
        raise ValueError("Preparation source identity changed")
    if value["phase"] == "solve" and (state["status"] != "completed"
            or value["handoff_reference"] != state["handoff_reference"]):
        raise ValueError("Solver requires the completed original handoff")
    if report.get("action"):
        require_preparation_action(value["policy"], role=role, phase=value["phase"],
                                   action_type=report["action"].get("type"))
    return value


def validate_state(task):
    """查看任务只核对持久状态，不要求旧任务使用当前工具版本。"""
    from .roles import recoverable_attempt_contract

    policy = preparation_policy(task["protocol"]["repository_preparation"])
    state = task.get("preparation_state")
    if (task.get("schema_version") != 3 or task.get("seed_strategy") != "none"
            or not isinstance(state, dict) or state.get("status") not in {"active", "completed", "exhausted"}):
        raise ValueError("Invalid repository preparation state")
    indices = []
    for index, attempt in enumerate(task["attempts"], start=1):
        recoverable_attempt_contract(task, attempt)
        value = attempt.get("preparation_binding")
        validate_binding(value)
        if value["policy"] != policy or value["task_id"] != task["task_id"] or value["start_revision"] != state["start_revision"]:
            raise ValueError("Preparation attempt state changed")
        if value["phase"] == "prepare":
            indices.append(index)
    if indices != state["attempt_indices"] or len(indices) > policy["max_calls"]:
        raise ValueError("Preparation call accounting changed")
    if state["status"] == "completed":
        if not state.get("handoff_reference") or not state.get("handoff_receipt"):
            raise ValueError("Completed preparation requires a handoff")
        attempts = [a for a in task["attempts"] if a["receipt"] == state["handoff_receipt"]]
        if len(attempts) != 1 or attempts[0]["preparation_phase"] != "prepare":
            raise ValueError("Handoff must come from preparation")
    elif state.get("handoff_reference") is not None or state.get("handoff_receipt") is not None or state.get("consumed_by") is not None:
        raise ValueError("Incomplete preparation cannot contain a handoff")
    consumption = state.get("consumed_by")
    if consumption is not None:
        matches = [a for a in task["attempts"] if a["request_id"] == consumption.get("request_id")]
        if (len(matches) != 1 or matches[0]["preparation_phase"] != "solve"
                or matches[0]["request_sha256"] != consumption.get("request_sha256")):
            raise ValueError("Handoff consumption must bind one Solver request")
