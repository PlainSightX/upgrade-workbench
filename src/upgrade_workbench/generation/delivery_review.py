"""交付草稿的可选只读复核；公开测量与解释分开，不引入验收权限。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..cases.manifest import assert_no_links

POLICY = "delivery-audit-v1"
EVIDENCE_POLICY = "delivery-audit-v2"
POLICIES = {POLICY, EVIDENCE_POLICY}


def draft(task, reference, snapshot):
    """只接收本任务已持久化的正常 finish；恢复时再次校验字节与修订。"""
    path = Path(reference["path"])
    assert_no_links(path)
    if not path.resolve().is_relative_to(Path(task["work_root"]).resolve()):
        raise ValueError("Delivery draft is outside the task workspace")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("Delivery draft receipt changed")
    value = json.loads(raw)
    request_path = Path(value["request_path"])
    assert_no_links(request_path)
    if not request_path.resolve().is_relative_to(Path(task["work_root"]).resolve()):
        raise ValueError("Delivery draft request is outside the task workspace")
    request_bytes = request_path.read_bytes()
    if hashlib.sha256(request_bytes).hexdigest() != value["request_sha256"]:
        raise ValueError("Delivery draft request changed")
    context = json.loads(json.loads(request_bytes)["messages"][1]["content"])
    if (value.get("status") != "agent_finished"
            or value.get("action", {}).get("type") != "finish"
            or value.get("candidate_revision") != snapshot.revision
            or value.get("case_fingerprint") != task["case_fingerprint"]
            or context.get("task_context", {}).get("task_id") != task["task_id"]):
        raise ValueError("Delivery draft does not match this task and revision")
    return {"receipt_sha256": reference["sha256"], "request_sha256": value["request_sha256"],
            "revision": snapshot.revision, "action": value["action"],
            "meaning": "Unaccepted Solver draft, not evidence or independent acceptance."}


def public_outputs(task, visible, revision):
    """Auditor 没有读取工具，因此完整展开最近一次可见公开执行，不截掉中间页。"""
    from ..diagnostic_output import output_page

    executions = [row for row in visible if row["kind"] == "public_checks"]
    if not executions:
        return None
    observation = executions[-1]
    reference = next(ref for ref in task["observations"] if ref["id"] == observation["id"])
    streams = []
    for stage, result in observation["result"].get("stages", {}).items():
        for stream in result.get("output_streams", []):
            cursor, pages, seen = stream["cursor"], [], set()
            while cursor is not None:
                if cursor in seen:
                    raise ValueError("Delivery review output cursor did not advance")
                seen.add(cursor)
                page = output_page(task, reference, cursor, current_revision=revision)
                pages.append(page)
                cursor = page["next_cursor"]
            if not pages[-1]["eof"]:
                raise ValueError("Delivery review output is incomplete")
            streams.append({"stage": stage, "stream": stream["stream"], "sha256": stream["sha256"],
                            "pages": pages})
    return {"observation": observation, "streams": streams,
            "meaning": "Complete registered public output only; stale/revision still apply. "
                       "Request capacity failures remain explicit; no independent checks are exposed."}


def independent_view(runtime):
    """保留待核草稿/主题正文，隐藏作者自评；原始收据和恢复状态不变。"""
    if runtime.get("delivery_review_policy") not in POLICIES or "delivery_review" not in runtime:
        return runtime
    from copy import deepcopy

    from .investigator_context import source_observation

    result = deepcopy(runtime)
    for key in ("knowledge_review_binding", "knowledge_review", "diagnostic_workset"):
        result.pop(key, None)
    for key in ("recent_actions", "issues", "historical_inputs", "investigator_handoffs", "contract_audits",
                "probes", "review_history"):
        result[key] = []
    selection = result.get("investigation_selection")
    if selection is not None:
        result["investigation_selection"] = {key: value for key, value in selection.items()
                                             if key in {"mode", "focus_paths", "revision", "current_revision", "observation_id"}}
    result["observations"] = [row for row in result["observations"] if source_observation(row)]
    maintenance = result.get("topic_maintenance")
    if maintenance is not None:
        result["topic_maintenance"] = {
            "current_revision": maintenance["current_revision"],
            "topics": [{key: row[key] for key in ("origin", "topic_sha256", "interpretation_revision", "topic")}
                       for row in maintenance["topics"]],
            "meaning": "Untrusted effective explanations; author review conclusions are withheld.",
        }
    return result


SOLVER_INSTRUCTIONS = (
    " Delivery-audit-v1 reviews the proposed final explanation as well as behavior. "
    "The requirement catalog maps executable checks; the full business_contract can also require "
    "explanations and maintained understanding. A green coverage table does not deliver those answers. "
    "Use current public evidence to answer the actual business questions, including observed limits, "
    "and distinguish source interpretation from measurement. Follow output cursors when needed. "
    "Address specific audit questions with existing reads, topic maintenance or final explanation; "
    "do not invent a patch or a new test merely to resolve an explanation omission. "
    "candidate_ready requires actual reviewed changes; when the original source satisfies the contract, "
    "use no_change_claimed with the complete requested business explanation. "
    "Independent acceptance remains separate and the advisory review is not a correctness guarantee."
)

EVIDENCE_INSTRUCTIONS = (
    " Before writing the final delivery, extract the original business_contract's explicit requests "
    "to explain, advise or report results. Answer each from current measurements and source. "
    "diagnostic_state.delivery_evidence supplies complete pages of the latest visible public execution. "
    "The short observation summary can be truncated even when those pages reach eof; read the supplied "
    "pages before declaring evidence unavailable. Distinguish configuration, actual measured events and "
    "generalization limits. Preserve unaffected legitimate behavior. Do not replace a measured maintenance "
    "operation with a different state-changing operation. Missing final-only checks remain for independent "
    "acceptance, but an available requested business answer belongs in this delivery, not a referral to logs."
)
