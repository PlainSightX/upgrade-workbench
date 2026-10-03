"""导入理解的有界复核阶段；同一 Solver 保持只读，结论不替代业务证据。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

VERSION = "project-context-v8"
COMPLETE = "complete_knowledge_review"
READ_ACTIONS = frozenset({
    "list_sources", "outline_source", "search_source", "read_source", "get_observation",
    "query_dependency", "get_fact", "read_public_check", "read_project_topic",
    "query_project_relations", "select_investigation", "revise_project_topic", COMPLETE,
})


def enabled(task):
    config = task.get("protocol", task)
    return ((config.get("project_context_policy") or {}).get("version") in {VERSION, "project-context-v9", "project-context-v10"}
            and config.get("knowledge_import") is not None)


def read_actions(context_policy=None):
    return (READ_ACTIONS | {"read_public_contract"}
            if (context_policy or {}).get("version") in {"project-context-v9", "project-context-v10"} else READ_ACTIONS)


def initial_state(snapshot, imported):
    return {"phase": "review", "start_revision": snapshot.revision,
            "import_id": imported["id"], "completion_reference": None, "consumed_by": None}


def validate_binding(value, snapshot=None):
    if (not isinstance(value, dict) or set(value) != {
            "task_id", "phase", "start_revision", "import_id", "completion_reference"}
            or value["phase"] not in {"review", "solve"}
            or not isinstance(value["task_id"], str)
            or any(not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
                   for key in ("start_revision", "import_id"))
            or (value["phase"] == "review" and value["completion_reference"] is not None)
            or (value["phase"] == "solve" and not isinstance(value["completion_reference"], dict))):
        raise ValueError("Invalid knowledge review binding")
    if snapshot is not None and value["phase"] == "review" and snapshot.revision != value["start_revision"]:
        raise ValueError("Knowledge review source changed")
    return value


def binding(task, snapshot):
    state = task["knowledge_review_state"]
    return validate_binding({"task_id": task["task_id"], **{
        key: state[key] for key in ("phase", "start_revision", "import_id", "completion_reference")}}, snapshot)


def require_action(value, action, snapshot, *, role="solver", context_policy=None):
    """在 provider 写候选前、接纳前和恢复时共用；策略身份由冻结请求核验。"""
    if value is None:
        if action.get("type") == COMPLETE:
            raise ValueError("Knowledge review completion requires a frozen review phase")
        return
    validate_binding(value, snapshot)
    if value["phase"] == "review":
        if role != "solver" or action.get("type") not in read_actions(context_policy):
            raise ValueError("Knowledge review is read-only; candidate, execution and business finish actions are unavailable")
    elif action.get("type") == COMPLETE:
        raise ValueError("Knowledge review is already complete")


def validate_completion(action):
    from ..topic_maintenance import _origin
    from .project_context import _text

    if set(action) != {"type", "revision", "topics", "remaining_scope"} or action["type"] != COMPLETE:
        raise ValueError("Knowledge review completion requires revision, topics and remaining_scope")
    _text(action["remaining_scope"], 2000)
    if not isinstance(action["topics"], list) or not 1 <= len(action["topics"]) <= 32:
        raise ValueError("Knowledge review requires bounded topic dispositions, not an update quota")
    seen = set()
    for item in action["topics"]:
        if not isinstance(item, dict) or set(item) != {"origin", "topic_sha256", "disposition", "reason", "sources"}:
            raise ValueError("Knowledge review topic disposition has invalid fields")
        key = _origin(item["origin"])
        if key in seen:
            raise ValueError("Duplicated knowledge review topic")
        seen.add(key)
        if (not isinstance(item["topic_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["topic_sha256"])
                or item["disposition"] not in {"corrected", "confirmed", "unresolved", "irrelevant"}):
            raise ValueError("Invalid knowledge review disposition")
        _text(item["reason"], 1500)
        if not isinstance(item["sources"], list) or len(item["sources"]) > 12:
            raise ValueError("Knowledge review sources must be bounded current citations")
        if item["disposition"] in {"corrected", "confirmed"} and not item["sources"]:
            raise ValueError("Corrected or confirmed explanations require current source citations")
    return action


def completion_result(case, snapshot, action, current):
    from ..topic_maintenance import _origin
    from .project_context import bind_pages

    validate_completion(action)
    if action["revision"] != snapshot.revision:
        raise ValueError("Knowledge review completion source changed")
    entries = {_origin(item["origin"]): item for item in current["topics"]}
    rows = []
    for item in action["topics"]:
        entry = entries.get(_origin(item["origin"]))
        if entry is None or entry["topic_sha256"] != item["topic_sha256"]:
            raise ValueError("Knowledge review disposition differs from the supplied explanation")
        if item["disposition"] == "corrected" and (
                entry["interpretation_revision"] != snapshot.revision or entry["update_observation_id"] is None):
            raise ValueError("Corrected disposition requires a current persisted topic update")
        sources = []
        if item["sources"]:
            # 复用引用范围校验；这里的依据不新建另一套主题解释。
            page = {"id": "review-basis", "kind": "module", "title": "Review basis",
                    "explanation": item["reason"], "sources": item["sources"], "unknowns": []}
            sources = bind_pages(case, snapshot, [page], require_complete=False)["pages"][0]["sources"]
        rows.append(item | {"sources": sources, "update_observation_id": entry["update_observation_id"]})
    return {"revision": snapshot.revision, "topics": rows, "remaining_scope": action["remaining_scope"],
            "meaning": "Source-bound model review, not behavioral proof. Confirmations do not renew unrelated topics; unresolved items remain open for the Solver."}


def verify_response(task, report, snapshot):
    """阶段转换后的重放仍绑定原 attempt；不把当前 solve 权限借给旧 review 回包。"""
    from ..diagnostics import public_observation, read
    from .roles import recoverable_attempt_contract

    value = report.get("knowledge_review_binding")
    if not enabled(task):
        if value is not None:
            raise ValueError("Unexpected knowledge review binding")
        return None
    validate_binding(value)
    attempts = [item for item in task["attempts"] if item["receipt"] == report.get("report_path")]
    frozen = read(report["diagnostic_context_reference"], Path(task["task_path"]).parent)
    state = task["knowledge_review_state"]
    if (len(attempts) != 1 or attempts[0].get("knowledge_review_binding") != value
            or frozen.get("knowledge_review_binding") != value
            or value["task_id"] != task["task_id"] or frozen["task_id"] != task["task_id"]
            or value["import_id"] != state["import_id"] or value["start_revision"] != state["start_revision"]
            or attempts[0].get("request_sha256") != report.get("request_sha256")):
        raise ValueError("Knowledge review response identity changed")
    role, output_format = recoverable_attempt_contract(task, attempts[0])
    if (frozen.get("context_role") != role.role or report.get("output_format") != output_format
            or report.get("role", role.role) != role.role
            or (value["phase"] == "review" and role.role != "solver")):
        raise ValueError("Knowledge review response role changed")
    if value["phase"] == "solve" and (state["phase"] != "solve"
            or value["completion_reference"] != state["completion_reference"]):
        raise ValueError("Solver requires the completed knowledge review")
    if value["phase"] == "review" and any(key in report for key in (
            "candidate_patch", "candidate_sha256", "base_revision", "increment_files", "patch_compilation")):
        raise ValueError("Knowledge review returned candidate output")
    if value["phase"] == "review" and state["phase"] == "solve":
        # 原回执、模型回答及原源码权限一起核验；后来源码变化不重执行该动作。
        public_observation(task, state["completion_reference"])
        completed = read(state["completion_reference"], Path(task["task_path"]).parent)
        action = {key: item for key, item in report.get("action", {}).items() if key != "issues"}
        if (completed["generation_receipt"]["path"] != report.get("report_path")
                or report.get("status") != "action_ready" or action != completed["action"]):
            raise ValueError("Old knowledge review response cannot change the completed phase")
        return value
    validate_binding(value, snapshot)
    if report.get("action") is not None:
        require_action(value, report["action"], snapshot, role=role.role,
                       context_policy=task["protocol"].get("project_context_policy"))
    return value


def validate_state(task, snapshot):
    from ..diagnostics import public_observation, read

    state = task.get("knowledge_review_state")
    if not enabled(task):
        if state is not None:
            raise ValueError("Knowledge review state requires its opt-in policy and import")
        return
    if not isinstance(state, dict) or set(state) != {
            "phase", "start_revision", "import_id", "completion_reference", "consumed_by"}:
        raise ValueError("Invalid knowledge review state")
    value = binding(task, snapshot)
    if state["import_id"] != task["knowledge_import_reference"]["id"]:
        raise ValueError("Knowledge review import changed")
    if value["phase"] == "review" and state["consumed_by"] is not None:
        raise ValueError("Unfinished knowledge review cannot be consumed")
    if value["phase"] == "solve":
        result = public_observation(task, state["completion_reference"])
        if result["action"]["type"] != COMPLETE or result["revision"] != state["start_revision"]:
            raise ValueError("Knowledge review completion changed")
    consumed = state["consumed_by"]
    if consumed is not None:
        matches = [item for item in task["attempts"] if item["request_id"] == consumed.get("request_id")]
        if (len(matches) != 1 or matches[0].get("request_sha256") != consumed.get("request_sha256")
                or matches[0].get("role") != "solver"
                or (matches[0].get("knowledge_review_binding") or {}).get("phase") != "solve"):
            raise ValueError("Knowledge review consumer must bind a Solver request")
        frozen = read(consumed["context_reference"], Path(task["task_path"]).parent)
        if (frozen.get("task_id") != task["task_id"] or frozen.get("context_role") != "solver"
                or frozen.get("knowledge_review_binding") != matches[0]["knowledge_review_binding"]
                or frozen["knowledge_review_binding"]["completion_reference"] != state["completion_reference"]):
            raise ValueError("Knowledge review consumer lost its completion")


def accept_completion(task, response, case, snapshot):
    from ..diagnostics import context_from_reference, observation_projection, observe

    state = task["knowledge_review_state"]
    if state["phase"] != "review":
        raise ValueError("Knowledge review completion already applied")
    runtime = context_from_reference(response["diagnostic_context_reference"], case, snapshot)
    result = completion_result(case, snapshot, response["action"], runtime["topic_maintenance"])
    observation_projection({"kind": "project_context", "revision": snapshot.revision,
                            "action": response["action"], "result": result}, "0" * 64)
    receipt = Path(response["report_path"])
    reference = observe(task, case, response["action"], result, kind="project_context", snapshot=snapshot,
                        generation_receipt={"path": str(receipt), "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()})
    state.update(phase="solve", completion_reference=reference)
    task.update(latest_observation=reference["id"], status="ready")
    return True


def instructions(value, *, followup=False):
    if value["phase"] == "solve":
        if followup:
            return (
                " The initial source review is complete, not the final knowledge or business delivery. "
                "Use knowledge_review_followup to locate later execution evidence and advisory handoffs. "
                "Reconcile relevant explanations with the evidence you actually read; revise them when "
                "the evidence changes their meaning, or retain uncertainty when it does not settle a claim. "
                "You choose the investigation and maintenance actions; no topic count or forced rewrite is required. "
                "The normal final delivery must answer the business contract's requested behavior, limits "
                "and advice, including a no-change decision. Initial review completion or passing checks "
                "do not replace that explanation."
            )
        return " Knowledge review is complete. Use its topic dispositions and remaining uncertainty in the business task; review completion is not behavioral evidence."
    return (
        " This request is the read-only knowledge review phase, before business solving. "
        "Do not submit/restore a candidate, finish the business task, or request runtime execution. "
        "Use current source, registered public checks and dependency facts to assess relevant imported explanations. "
        "Correct relevant outdated explanations with revise_project_topic; keep unknown or irrelevant material honest. "
        "No number of updates or investigation selections is required. Finish this review with "
        "{type:complete_knowledge_review,revision,topics:[{origin,topic_sha256,disposition,reason,sources}],remaining_scope}. "
        "Name the topics assessed (1..32 dispositions, not an update quota); disposition is corrected, confirmed, "
        "unresolved or irrelevant. Copy origin and the CURRENT topic_sha256 from topic_maintenance. "
        "Corrected requires an already persisted current topic revision. Confirmed is a source-grounded interpretation, "
        "never a runtime result. sources is 0..12 exact current {path,start_line,end_line} citations; corrected/confirmed "
        "require at least one. reason is 1..1500 characters; remaining_scope is 1..2000 characters explaining unassessed "
        "scope and remaining uncertainty. The complete public result must fit 24000 UTF-8 bytes; keep dispositions concise. "
        "Do not declare unsupported historical unknowns resolved. "
        "Completion hands the maintained explanations and review to the next ordinary Solver request."
    )
