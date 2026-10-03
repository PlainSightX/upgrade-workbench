"""可选的来源优先核查视图；隔离先前判断，不改写宿主的冻结证据。"""

from __future__ import annotations

from copy import deepcopy

POLICY = {"version": "source-first-v1"}
FOLLOWUP_POLICY = {"version": "source-first-v2"}
RESILIENT_POLICY = {"version": "source-first-v3"}
SOURCE_ACTIONS = frozenset({
    "list_sources", "outline_source", "search_source", "read_source",
    "query_project_relations", "read_public_check", "read_public_contract",
    "run_public_checks",
})
PROBE_ACTIONS = frozenset({"propose_probe", "revise_probe", "run_probe"})
ROLE_BOUNDARY = (
    " In the explicitly selected source-first Investigator view, authored-probe actions "
    "propose_probe, revise_probe and run_probe are unavailable. Use current source, locked dependency "
    "queries or registered public checks; recommend any new probe to the Solver in handoff. "
    "This role's own host tool rejections appear in diagnostic_state.investigator_action_feedback. "
    "Those errors are operational feedback, not source evidence or an acceptance decision."
)


def policy(value):
    if value is None:
        return None
    if value not in (POLICY, FOLLOWUP_POLICY, RESILIENT_POLICY):
        raise ValueError("Unknown Investigator context policy")
    return dict(value)


def isolated(runtime):
    return (runtime.get("investigator_context_policy") in (POLICY, FOLLOWUP_POLICY, RESILIENT_POLICY)
            and runtime.get("context_role") == "investigator")


def solver_followup(runtime):
    """只派生初始复核之后的证据入口，不新增完成状态或主题修改配额。"""
    binding = runtime.get("knowledge_review_binding") or {}
    if (runtime.get("investigator_context_policy") not in (FOLLOWUP_POLICY, RESILIENT_POLICY)
            or runtime.get("context_role") != "solver" or binding.get("phase") != "solve"):
        return None
    completion_id = binding["completion_reference"]["id"]
    index = runtime["observation_index"]
    positions = [i for i, row in enumerate(index) if row["id"] == completion_id]
    if len(positions) != 1:
        raise ValueError("Initial knowledge review must have one frozen observation")
    return {
        "initial_review_observation_id": completion_id,
        "current_revision": runtime["source_state"]["revision"],
        "subsequent_execution_observations": [deepcopy(row) for row in index[positions[0] + 1:]
                                              if row["kind"] in {"public_checks", "probe"}],
        "available_handoffs": [{key: row[key] for key in ("id", "revision", "stale")}
                               for row in runtime.get("investigator_handoffs", [])],
        "meaning": (
            "Evidence registered after the initial source review, not a list of resolved claims. "
            "Read observation details with get_observation and follow registered output cursors when "
            "the excerpt is incomplete. Handoff contents remain in diagnostic_state.investigator_handoffs "
            "and are advisory; source changes may make either kind stale. Choose which relevant "
            "explanations need correction, further checking or retained uncertainty. A passing check "
            "does not by itself satisfy the business contract's requested final explanation."
        ),
    }


def require_action(action, config, role):
    if not isinstance(action, dict):
        return  # 原协议校验器仍负责报告格式错误，不能变成宿主 AttributeError。
    if policy(config) is not None and role == "investigator" and action.get("type") in PROBE_ACTIONS:
        from .actions import AgentActionError

        raise AgentActionError(
            "Source-first Investigator cannot author or run probes. Read source/dependency facts, "
            "use registered public checks, or recommend a probe in handoff; action not executed.",
            code="action_invalid_fields")


def source_observation(row):
    """保留原文和公开执行结果；带作者解释的观察另行明确隐藏。"""
    if row.get("action", {}).get("type") == "read_project_topic":
        # 该结果已由原请求和工具执行核验；只读本视图的主张不会重新带入旧复核标签。
        return set(row.get("result", {})) == {
            "origin", "topic_sha256", "interpretation_revision", "topic", "current_revision", "meaning"}
    return row.get("action", {}).get("type") in SOURCE_ACTIONS


def project(runtime, output_format):
    """生成与重放共用；白名单避免新增内部叙述字段意外进入独立核查。"""
    config = policy(runtime.get("investigator_context_policy"))
    if config is None:
        return runtime
    formats = {"solver": "protocol_v6_actions", "investigator": "investigator_actions",
               "contract_auditor": "contract_audit"}
    if formats.get(runtime.get("context_role")) != output_format:
        raise ValueError("Investigator context role differs from its frozen output format")
    if not isolated(runtime):
        return runtime
    fields = {
        "task_id", "source_state", "workflow_profile", "execution_environment",
        "project_context_policy", "investigator_context_policy", "context_role", "delivery_review_policy", "semantic_risk_policy",
        "knowledge_review_binding", "incomplete_response_scope", "source_read_retention",
        "retained_source_reads", "contract_requirements", "dependency_queries",
        "dependency_facts", "dependency_fact_index", "dependency_environment_identities",
        "remaining_dependency_queries", "latest_fact_id", "latest_dependency_query_id",
        "dependency_query_feedback", "investigator_action_feedback",
        "role_budget", "investigator_sessions", "remaining_diagnostic_runs", "remaining_probes",
    }
    if config == RESILIENT_POLICY:
        fields.update({"latest_public_execution", "investigator_failure_note"})
    result = deepcopy({key: value for key, value in runtime.items() if key in fields})
    result["navigation_assistance"] = "baseline"
    result["observations"] = deepcopy([row for row in runtime["observations"] if source_observation(row)])
    visible_ids = set(runtime["investigator_source_observation_ids"])
    result["observation_index"] = deepcopy([
        row for row in runtime["observation_index"] if row["id"] in visible_ids])
    latest = runtime.get("latest_observation_id")
    result["latest_observation_id"] = latest if latest in visible_ids else None
    if latest in visible_ids and runtime.get("observation_output") is not None:
        result["observation_output"] = deepcopy(runtime["observation_output"])
    result["withheld_observations"] = {
        "count": len(runtime["observation_index"]) - len(result["observation_index"]),
        "requested_latest_id": latest if latest and latest not in visible_ids else None,
        "meaning": "Prior interpretation, review and authored-probe observations are withheld in this "
                   "role view, including get_observation rereads. Their original bytes remain stored. "
                   "No redacted result is presented as the original observation. Current claims are "
                   "separately supplied in project_context.topic_maintenance; ordinary source and "
                   "locked dependency reading remain available.",
    }
    navigation = runtime.get("navigation_candidates", {"index": [], "observations": []})
    result["navigation_candidates"] = deepcopy({
        "index": [row for row in navigation["index"] if row["id"] in visible_ids],
        "observations": [row for row in navigation["observations"] if source_observation(row)],
    })
    # 仅保留待核查正文与身份，不携带作者的确认标签、理由、更新结论或整段旧对话。
    result["topic_maintenance"] = {
        "current_revision": runtime["source_state"]["revision"],
        "topics": [{
            "origin": deepcopy(entry["origin"]), "topic_sha256": entry["topic_sha256"],
            "interpretation_revision": entry["interpretation_revision"],
            "topic": deepcopy({key: entry["topic"][key] for key in
                               ("id", "kind", "title", "explanation", "unknowns", "sources")}),
        } for entry in runtime.get("topic_maintenance", {}).get("topics", [])],
        "meaning": "Claims to check, not confirmed facts. Selection and review outcomes are hidden; "
                   "choose the claims relevant to the current task, not a whole-page quota.",
    }
    return result


def task_view(value, runtime):
    """自由反馈可能复述上一位作者；核查预算从冻结角色状态重新生成。"""
    if not isolated(runtime) or value is None:
        return value
    result = deepcopy({key: item for key, item in value.items() if key in {
        "task_id", "attempt_index", "remaining_calls", "previous_candidate", "protocol_feedback"}})
    if result["task_id"] != runtime["task_id"]:
        raise ValueError("Investigator task context belongs to another task")
    active = next((row for row in reversed(runtime["investigator_sessions"])
                   if row["status"] == "active"), None)
    if active is None:
        raise ValueError("Investigator context requires an active frozen session")
    budget = runtime["role_budget"]
    remaining = min(budget["investigator_max_calls"] - active["attempts"],
                    result["remaining_calls"] - budget["solver_reserved_calls"])
    result["edit_feedback"] = (
        f"INVESTIGATOR_CALL_BUDGET: {remaining} Investigator call(s) remain, including this call. "
        "This is part of the total task allowance. Prior free-form feedback is withheld. "
        + ("Return handoff now with available facts, limits and unknowns; another tool action "
           "would leave no call to hand off." if remaining == 1 else "")
    )
    return result


def project_view(snapshot, runtime):
    from .project_context import repository_catalog

    return {
        "policy": runtime["project_context_policy"],
        "investigator_context_policy": runtime["investigator_context_policy"],
        "map": repository_catalog(snapshot), "preparation_required": False,
        "topic_maintenance": runtime["topic_maintenance"],
        "tools_available": sorted(SOURCE_ACTIONS | {"get_observation", "query_dependency", "get_fact",
                                                     "read_project_topic", "handoff"}),
        "instructions": (
            "Read-only source-first claim checking. The supplied explanations are claims, not facts. "
            "Choose relevant claims and test them against current source, the public business contract "
            "and locked dependency facts. Check object identity, inherited behavior, trigger conditions "
            "and later overriding operations. Retain unknowns when these sources cannot settle a claim. "
            "No need to inspect every page. Source, outline and dependency tools remain available; "
            "get_observation of authored interpretations is explicitly withheld in this view. "
            "Authored probes are unavailable in this selected role; recommend them to the Solver. "
            "Return advisory handoff using the existing schema; do not revise topics or decide business "
            "acceptance. A second perspective is not a correctness guarantee. The Solver independently "
            "decides whether to use the findings, revise an explanation or retain uncertainty."
        ),
        "limitations": "Prior author confirmations and narrative are hidden, not erased from history. "
                      "Source-first review may miss errors and does not establish behavior or acceptance.",
    }
