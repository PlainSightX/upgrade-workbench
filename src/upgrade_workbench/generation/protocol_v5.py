"""结构化合同协议：Solver 引用冻结条款，宿主计算有限覆盖状态。"""

from __future__ import annotations

import re

from . import protocol_v4
from .actions import AgentActionError, action_field_error_code

FORMAT = "contract_actions"
DIAGNOSTIC_ACTIONS = protocol_v4.DIAGNOSTIC_ACTIONS | {"restore_candidate"}
DIAGNOSTIC_POLICY = {
    "max_runs": 8,
    "max_probes": 3,
    "max_revisions_per_probe": 2,
    "max_observation_revisits": 4,
}
REQUIREMENT_ID = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+){1,7}")


def _requirement_id(value, field="requirement_id"):
    if not isinstance(value, str) or not REQUIREMENT_ID.fullmatch(value) or len(value) > 96:
        raise ValueError(f"Invalid {field}; use an exact ID from contract_requirements")


def _coverage(value, *, allow_facts=False, evidence_limit=8):
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        count = len(value) if isinstance(value, list) else None
        raise ValueError(f"Invalid contract_coverage; expected one to 64 declarations; actual_count={count}")
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {
            "requirement_id", "state", "evidence_refs", "tool_limitation"
        }:
            raise ValueError(f"Invalid contract_coverage[{index}] fields")
        _requirement_id(item["requirement_id"], f"contract_coverage[{index}].requirement_id")
        if item["requirement_id"] in seen:
            raise ValueError("Duplicate contract_coverage requirement_id")
        seen.add(item["requirement_id"])
        if item["state"] not in {"supported", "counterexample", "unobserved"}:
            raise ValueError("contract_coverage.state must be supported, counterexample or unobserved")
        protocol_v4.evidence_refs(item["evidence_refs"], allow_facts=allow_facts, maximum=evidence_limit)
        limitation = item["tool_limitation"]
        if item["state"] in {"supported", "counterexample"} and limitation is not None:
            raise ValueError(
                f"contract_coverage[{index}].tool_limitation must be JSON null for "
                f"state={item['state']}; keep the required key, use null, not an empty string or text."
            )
        if isinstance(limitation, str) and not limitation.strip():
            raise ValueError(
                f"contract_coverage[{index}].tool_limitation must be JSON null when no reviewed route "
                "is unavailable, or nonempty text identifying a genuinely unavailable route; empty text is invalid."
            )
        if limitation is not None:
            protocol_v4._text(limitation, 500, field=f"contract_coverage[{index}].tool_limitation")
        if item["state"] in {"supported", "counterexample"} and (
            not item["evidence_refs"] or limitation is not None
        ):
            raise ValueError("Observed contract state needs evidence_refs and cannot declare a tool limitation")
        if limitation is not None and item["state"] != "unobserved":
            raise ValueError("tool_limitation only applies to an unobserved requirement")


def _issues(value, *, allow_facts=False):
    """保持 Protocol 4 的问题陈述边界，同时允许恢复动作携带短问题列表。"""
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("At most four diagnostic issues")
    for issue in value:
        if not isinstance(issue, dict) or set(issue) != {
            "hypothesis", "evidence_refs", "unknown", "next_observation"
        }:
            raise ValueError("Invalid diagnostic issue")
        for key in ("hypothesis", "unknown", "next_observation"):
            protocol_v4._text(issue[key], 1000, field=f"issues.{key}")
        protocol_v4.evidence_refs(issue["evidence_refs"], allow_facts=allow_facts)


def validate(case, value, snapshot, *, allow_facts=False, finish_limits=None):
    """复用协议4动作，仅替换探针条款身份和 finish 合同。"""
    try:
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise ValueError("One typed action is required")
        kind = value["type"]
        if kind in {"propose_probe", "revise_probe"}:
            if "requirement_id" not in value:
                raise ValueError(f"{kind} requires requirement_id")
            _requirement_id(value["requirement_id"])
            reduced = {key: item for key, item in value.items() if key != "requirement_id"}
            protocol_v4.validate(case, reduced, snapshot, allow_facts=allow_facts)
            return value
        if kind == "restore_candidate":
            required = {"type", "revision", "reason", "evidence_refs"}
            optional = {"issues"}
            missing = sorted(required - set(value))
            extra = sorted(set(value) - required - optional)
            if missing or extra:
                raise AgentActionError(
                    "Invalid restore_candidate fields; action not executed. "
                    f"missing={missing}; extra={extra}",
                    code=action_field_error_code(extra),
                )
            if not isinstance(value["revision"], str) or not protocol_v4.DIGEST.fullmatch(value["revision"]):
                raise ValueError("Restore revision must be an exact SHA256")
            protocol_v4._text(value["reason"], 500, field="restore_candidate.reason")
            protocol_v4.evidence_refs(value["evidence_refs"], allow_facts=allow_facts)
            if not value["evidence_refs"]:
                raise ValueError("restore_candidate requires one exposed public-pass observation reference")
            if "issues" in value:
                _issues(value["issues"], allow_facts=allow_facts)
            return value
        if kind != "finish":
            return protocol_v4.validate(case, value, snapshot, allow_facts=allow_facts)
        required = {"type", "reason", "explanation", "evidence_refs", "contract_coverage"}
        if set(value) != required:
            extra = sorted(set(value) - required)
            issue_hint = (
                " finish does not accept issues; remove that field and keep any bounded caveat in "
                "explanation or contract_coverage."
                if "issues" in extra else ""
            )
            raise AgentActionError(
                "Invalid finish fields; action not executed. "
                f"missing={sorted(required - set(value))}; extra={extra}; allowed={sorted(required)}."
                f"{issue_hint} Allowed fields are exactly "
                "type, reason, explanation, evidence_refs, contract_coverage.",
                code=action_field_error_code(extra),
            )
        if value["reason"] not in {"candidate_ready", "no_change_claimed", "unresolved"}:
            raise ValueError(
                "Invalid finish.reason; action not executed. reason is an enum token, not prose. "
                "Use exactly candidate_ready, no_change_claimed, or unresolved; put the explanation "
                "in finish.explanation. Minimal shape: "
                '{"type":"finish","reason":"candidate_ready","explanation":"...",'
                '"evidence_refs":[],"contract_coverage":[...]}'
            )
        limits = finish_limits or {"explanation_max_characters": 2000, "evidence_refs_max_items": 8}
        protocol_v4._text(value["explanation"], limits["explanation_max_characters"], field="finish.explanation")
        protocol_v4.evidence_refs(value["evidence_refs"], allow_facts=allow_facts,
                                  maximum=limits["evidence_refs_max_items"])
        _coverage(value["contract_coverage"], allow_facts=allow_facts, evidence_limit=limits["evidence_refs_max_items"])
        return value
    except AgentActionError:
        raise
    except (ValueError, KeyError, TypeError, SyntaxError) as error:
        raise AgentActionError(str(error)[:1000], code="action_invalid_fields") from error


def instructions() -> str:
    """沿用已验证的调查动作说明，只替换合同身份与退出格式。"""
    base = protocol_v4.instructions()
    base = base.replace(
        "{type:propose_probe,revision,code,purpose,expected_observation,evidence_refs,oracle};",
        "{type:propose_probe,revision,requirement_id,code,purpose,expected_observation,evidence_refs,oracle};",
    ).replace(
        "{type:revise_probe,revision,parent_probe_id,revision_reason,code,purpose,expected_observation,evidence_refs,oracle};",
        "{type:revise_probe,revision,parent_probe_id,requirement_id,revision_reason,code,purpose,expected_observation,evidence_refs,oracle};",
    )
    prefix = base.split("Finish with {type:finish", 1)[0]
    return prefix + (
        "When restore_candidates is nonempty, a reviewed historical revision may be restored after a later "
        "candidate regresses. Use {type:restore_candidate,revision,reason,evidence_refs}; revision must be one "
        "exact exposed revision and evidence_refs must include one of its exposed public_pass_observation_refs. "
        "Restoration only reselects immutable reviewed bytes and preserves every failed revision and observation; "
        "it is not permission to rewrite results, claim final acceptance, or import an unlisted patch. "
        "Finish with {type:finish,reason,explanation,evidence_refs,contract_coverage}. "
        "contract_requirements is the complete frozen in-contract requirement set; do not omit, rename, "
        "merge, add, or re-scope IDs. contract_coverage must contain every ID exactly once with fields "
        "{requirement_id,state,evidence_refs,tool_limitation}. state is supported, counterexample, or unobserved. "
        "tool_limitation is a required nullable field: use JSON null for supported/counterexample and "
        "whenever no reviewed tool route is unavailable; never use an empty string or the string 'null'. "
        'For example: {"requirement_id":"config.empty-defaults","state":"supported",'
        '"evidence_refs":["observation:<current-id>"],"tool_limitation":null}. '
        "Each evidence_refs list, including finish.evidence_refs, allows at most eight references. "
        "supported is only a claim for the cited current-revision public observation; source, version, or the "
        "business contract alone cannot establish it. counterexample names an observed contradiction. "
        "unobserved means no adequate current observation; use tool_limitation only for a concrete unavailable "
        "reviewed route; an unperformed check or an observation-only probe is not itself tool unavailability. "
        "A reviewed probe must carry one exact requirement_id. The host recomputes finite "
        "structural support from the frozen catalog and observations; IDs and references do not prove semantic "
        "entailment. Requirements with public_check_nodeids are development-gate requirements; "
        "candidate_ready/no_change_claimed cannot leave one of them unobserved, and no current requirement "
        "may have a counterexample. Requirements without public_check_nodeids belong to independent final "
        "acceptance: declare them unobserved unless a reviewed current probe supports them. Their honest "
        "unobserved state does not by itself block submission, because submitted is not final acceptance. "
        "unresolved may exit only when bounded diagnostic routes are exhausted or genuinely unavailable. "
        "When task_context.remaining_calls is 1 and edit_feedback contains FINAL_CALL_FINISH_ONLY, "
        "the current reviewed candidate is the last eligible revision: do not read, search, run or revise "
        "a probe, restore, or submit another candidate. Return finish with reason exactly candidate_ready "
        "or unresolved and the complete contract_coverage catalog. "
        "Finish is never final acceptance."
    )
