"""提交义务的派生视图；保留历史，不将探针修订或提交视为最终验收。"""

from __future__ import annotations

POLICY = "probe-lineage-v1"
LEGACY = "legacy"
ORACLE_FIELDS = ("requirement", "subject", "basis", "operator", "expected")


def validate_policy(value):
    if value not in (None, LEGACY, POLICY):
        raise ValueError("Unknown finish policy")
    return value


def _equivalent(parent, child):
    """练习代码可以纠错，但不能换合同、换判据或沿途收窄后再声称解除。"""
    old, new = parent.get("oracle"), child.get("oracle")
    return bool(
        parent.get("requirement_id")
        and parent["requirement_id"] == child.get("requirement_id")
        and isinstance(old, dict) and isinstance(new, dict)
        and all(key in old and key in new and old[key] == new[key] for key in ORACLE_FIELDS)
    )


def project(probes, observations, revision, *, format_limits=None):
    """请求展示与实际提交共用；输入是已经核验身份的公开观察和探针状态。"""
    by_id = {probe["id"]: probe for probe in probes}
    measured = [row for row in observations if row["kind"] == "probe"]
    latest = {}
    for row in measured:
        identity = row["result"].get("assessment", {}).get("probe_id")
        latest[identity] = row

    def valid_success(probe):
        row = latest.get(probe["id"])
        current_reviews = [item for item in probe.get("review_history", [])
                           if item["revision"] == revision]
        return row if (
            row is not None and row["revision"] == revision
            and current_reviews and current_reviews[-1]["decision"] == "accept"
            and row["result"]["status"] == "passed"
            and row["result"].get("assessment", {}).get("conclusion") == "no_counterexample_observed"
        ) else None

    def replacement(probe):
        visited = set()
        while probe and probe["id"] not in visited:
            visited.add(probe["id"])
            children = probe.get("superseded_by", [])
            if not children:
                return valid_success(probe)
            if len(children) != 1:
                return None
            child = by_id.get(children[0])
            if child is None or not _equivalent(probe, child):
                return None
            probe = child
        return None

    # 与旧门禁一致：当前版本实测反例直接阻断；旧反例只由原探针当前重测解除。
    repaired = {row["result"]["assessment"]["probe_id"] for row in measured
                if row["revision"] == revision and row["result"]["status"] == "passed"
                and row["result"].get("assessment", {}).get("conclusion") == "no_counterexample_observed"}
    blockers, required, history = [], [], []
    for row in measured:
        assessment = row["result"].get("assessment", {})
        identity, conclusion = assessment.get("probe_id"), assessment.get("conclusion")
        reference = "observation:" + row["id"]
        current = row["revision"] == revision
        blocking = conclusion == "counterexample_observed" and (current or identity not in repaired)
        limitation = current and (row["result"]["status"] != "passed"
                                  or conclusion in {"inconclusive", "observation_only"})
        resolved = None
        # 仅无有效测量的失败可通过修订链解除引用义务，业务反例不能走这条路。
        if limitation and conclusion in {"inconclusive", "setup_failed"} and identity in by_id:
            resolved = replacement(by_id[identity])
        if blocking:
            blockers.append(reference)
        if limitation and resolved is None:
            required.append(reference)
        history.append({
            "observation_ref": reference, "probe_id": identity, "revision": row["revision"],
            "execution_status": row["result"]["status"], "conclusion": conclusion,
            "scope_limit": assessment.get("scope_limit"),
            "disposition": ("blocking_counterexample" if blocking else
                            "resolved_setup_history" if resolved else
                            "current_limitation" if limitation else
                            "current_observation" if current else "historical_observation"),
            "resolved_by": "observation:" + resolved["id"] if resolved else None,
        })
    limits = {"explanation_max_characters": 2000, "evidence_refs_max_items": 8}
    if format_limits is not None:
        limits.update(format_limits)
        # 宿主不能要求更多必引证据，同时用格式限制禁止提交这些证据。
        limits["evidence_refs_max_items"] = max(limits["evidence_refs_max_items"], len(required))
    format_instruction = (
        "finish.explanation allows at most 2000 characters; each evidence_refs list at most eight items. "
        if format_limits is None else
        f"finish.explanation allows at most {limits['explanation_max_characters']} characters; "
        f"each finish evidence_refs list at most {limits['evidence_refs_max_items']} items. "
    )
    return {
        "policy": POLICY, "revision": revision,
        "blocking_counterexample_refs": blockers,
        "required_evidence_refs": required,
        "probe_history": history,
        "format_limits": limits,
        "instructions": (
            "For candidate_ready/no_change_claimed, resolve blocking_counterexample_refs and include "
            "required_evidence_refs in finish.evidence_refs. Explain current limitations briefly. "
            + format_instruction +
            "The host attaches complete probe_history, including resolved setup failures: do not repeat "
            "those historical references merely to submit. This view covers probe obligations only; "
            "current public checks, reviewed candidate, complete contract_coverage where required, and "
            "separate independent acceptance still apply. Unobserved behavior is not a pass."
        ),
    }


def require_submission_evidence(action, view):
    """已展示的条件与执行条件一致；不修改模型回答，也不静默补写它的声明。"""
    if action["reason"] == "unresolved":
        return
    if view["blocking_counterexample_refs"]:
        raise ValueError("Unresolved measured counterexample blocks finish; repair and rerun the same "
                         "probe on the current revision, or finish unresolved: "
                         + repr(view["blocking_counterexample_refs"]))
    missing = [ref for ref in view["required_evidence_refs"] if ref not in action["evidence_refs"]]
    if missing:
        raise ValueError("Reference and explain current probe failures and observation-only limits in finish. "
                         "Missing evidence_refs: " + repr(missing)
                         + ". This is a citation/scope correction, not a request for another execution.")
