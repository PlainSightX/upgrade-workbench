"""诊断上下文的纯状态投影；不运行探针，不改变已有执行证据。"""

from __future__ import annotations

import re


def diagnostic_execution_summary(task):
    """API与导出共用的只读投影；登记次数不是目标运行或完成次数。"""
    phases = {"started", "preparing", "target_started", "target_completed", "completed", "failed", "unknown"}
    statuses = {"running", "completed", "failed", "unknown"}
    outcomes = {"unknown", "passed", "semantic_failed", "failed", "execution_incomplete"}
    actions = {"dispatch", "report_returned", "materialize", "validate_input", "unknown", "target_completed",
               "prepare_old", "prepare_new", "prepare_old_environment", "prepare_new_environment",
               "verify_old_original", "verify_new_original", "verify_new_candidate",
               "old_original", "new_original", "new_candidate"}
    counts = dict.fromkeys(("registered", "running", "completed", "failed", "unknown", "historical", "target_completed"), 0)
    rows = []
    for run in task.get("diagnostic_runs", []):
        phase = run.get("phase") if run.get("phase") in phases else "unknown"
        status = run.get("status") if run.get("status") in statuses else "unknown"
        if "phase" not in run:
            status = "historical"
        counts["registered"] += 1
        counts[status] += 1
        last_phase = run.get("last_phase") if run.get("last_phase") in phases else phase
        if phase == "target_completed" or last_phase == "target_completed":
            counts["target_completed"] += 1
        row = {"status": status, "phase": phase, "last_phase": last_phase,
               "outcome": run.get("outcome") if run.get("outcome") in outcomes else "unknown",
               "last_action": run.get("last_action") if run.get("last_action") in actions else "unknown",
               "reconciliation_required": run.get("reconciliation_required") is True}
        for name, reference in (("request_id", run.get("request")), ("failure_receipt_id", run.get("failure_receipt"))):
            value = (reference or {}).get("id", "")
            row[name] = value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None
        if run.get("failure"):
            failure = run["failure"]
            row["failure"] = {
                "category": failure.get("category") if failure.get("category") in {"contract", "infrastructure"} else "unknown",
                "code": failure.get("code") if failure.get("code") in {
                    "diagnostic_contract_rejected", "diagnostic_execution_failed", "diagnostic_execution_incomplete"
                } else "diagnostic_execution_failed",
                "phase": failure.get("phase") if failure.get("phase") in phases else last_phase,
                "error_type": failure.get("error_type") if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}",
                                                                       str(failure.get("error_type", ""))) else "Error",
            }
        if run.get("failure_receipt_status") == "unavailable":
            row["failure_receipt_status"] = "unavailable"
        rows.append(row)
    return {"counts": counts, "latest": rows[-1] if rows else None,
            "runs": rows[-16:], "runs_truncated": len(rows) > 16}


def failed_public_nodes(stage, text=None):
    """仅关联日志摘要与已收集的检查身份；不从异常文字推断根因。"""
    def normal(value):
        for prefix in ("/work/checks/", "public-check/", "checks/"):
            if value.startswith(prefix):
                return value[len(prefix):]
        return value

    known = {normal(value): value for value in stage.get("nodeids", [])}
    structured = stage.get("failure_details") or stage.get("failure_reports")
    if isinstance(structured, list):
        found = set()
        for item in structured:
            if not isinstance(item, dict) or not isinstance(item.get("nodeid"), str):
                continue
            value = normal(item["nodeid"].strip())
            if value in known:
                found.add(known[value])
        if found:
            return sorted(found)
    text = stage.get("output_excerpt", "") if text is None else text
    found = re.findall(r"^(?:FAILED|ERROR)\s+(.+?)(?:\s+-\s+.*)?$", text, re.MULTILINE)
    return sorted({known[normal(value.strip())] for value in found if normal(value.strip()) in known})


def recurring_public_failures(observations, current_revision):
    """重复候选仍失败不是取得新诊断；只投影事实，不禁止下一次修改。"""
    active = {}
    semantic_active = {}
    for index, row in enumerate(observations):
        result = row["result"]
        if row["kind"] != "public_checks" or result.get("scope") != "public":
            continue
        if result.get("status") not in {"passed", "failed"}:
            continue
        stages = result.get("stages", {})
        stage = stages.get("new_candidate", {})
        if stage.get("status") in {None, "not_supplied"}:
            stage = stages.get("new_original", {})
        if stages.get("old_original", {}).get("status") != "passed":
            continue
        nodes = stage.get("failed_nodeids", failed_public_nodes(stage))
        # 完整公开检查通过才能清除已知重复；截断日志中没有FAILED不等于该项通过。
        if result["status"] == "passed":
            for node in stage.get("nodeids", []):
                active.pop(node, None)
            semantic_active.clear()
            continue
        if stage.get("status") != "failed":
            continue
        for node in nodes:
            if node not in stage.get("nodeids", []):
                continue
            history = active.setdefault(node, [])
            if row["revision"] not in {item["revision"] for item in history}:
                history.append({"observation_id": row["id"], "revision": row["revision"], "index": index})
        for cluster in stage.get("failure_clusters", []):
            fingerprint = cluster.get("fingerprint")
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
                continue
            history = semantic_active.setdefault(fingerprint, [])
            if row["revision"] not in {item["revision"] for item in history}:
                history.append({
                    "observation_id": row["id"],
                    "revision": row["revision"],
                    "index": index,
                    "nodeids": cluster.get("nodeids", []),
                })
    repeated = []
    for node, history in active.items():
        if len(history) < 2:
            continue
        latest = history[-1]
        probes = [row["id"] for row in observations[history[0]["index"] + 1:] if row["kind"] == "probe"]
        repeated.append({"nodeid": node, "distinct_failed_revisions": len(history),
                         "first_observation_id": history[0]["observation_id"],
                         "latest_observation_id": latest["observation_id"],
                         "latest_failed_revision": latest["revision"],
                         "stale": latest["revision"] != current_revision,
                         "probe_observations_since_first_failure": list(dict.fromkeys(probes))[-4:]})
    repeated.sort(key=lambda row: (-row["distinct_failed_revisions"], row["nodeid"]))
    semantic = []
    for fingerprint, history in semantic_active.items():
        if len(history) < 2:
            continue
        latest = history[-1]
        semantic.append({
            "fingerprint": fingerprint,
            "distinct_failed_revisions": len(history),
            "first_observation_id": history[0]["observation_id"],
            "latest_observation_id": latest["observation_id"],
            "latest_failed_revision": latest["revision"],
            "latest_nodeids": latest["nodeids"][:8],
            "stale": latest["revision"] != current_revision,
        })
    semantic.sort(key=lambda row: (-row["distinct_failed_revisions"], row["fingerprint"]))
    return {"items": repeated[:6], "total": len(repeated),
            "semantic_clusters": semantic[:6], "semantic_clusters_total": len(semantic),
            "scope": "Same public check failed on distinct revisions; not proof of the same root cause. "
                     "A semantic cluster means the same normalized failure signature recurred, not that its "
                     "root cause has been proven. Failure IDs come from bounded pytest output, so absence is "
                     "not proof of no failure.",
            "next_observation": (
                "Retrieve the linked public observations and exact critical evidence, compare relevant "
                "original/current source, then use an available reviewed probe to distinguish competing "
                "hypotheses before another speculative edit. "
                "Probe observations listed here are not automatically informative or accepted. If no new "
                "observation is feasible, explain the unresolved uncertainty; this advisory does not block edits."
                if repeated or semantic else None)}


def probe_states(probes, reviews, observations, revision, policy):
    """修订只修复拒绝或无结论探针；已有反例不能靠换探针身份消失。"""
    roots = {}
    unestablished = {}
    children = {}
    for probe in probes:
        parent = probe.get("parent_probe_id")
        if parent and parent not in roots:
            raise ValueError("Probe parent must be an earlier registered definition")
        roots[probe["id"]] = roots[parent] if parent else probe["id"]
        unestablished[probe["id"]] = list(unestablished[parent]) if parent else []
        if parent and probe.get("oracle") is None:
            ancestor = next(p for p in probes if p["id"] == parent)
            if ancestor.get("oracle") is not None:
                unestablished[probe["id"]].append({"probe_id": parent, "oracle": ancestor["oracle"],
                    "status": "not_established_by_narrowed_observation"})
        if parent:
            children.setdefault(parent, []).append(probe["id"])
    result = []
    for probe in probes:
        identity = probe["id"]
        root = roots[identity]
        history = [r for r in reviews if r["probe_id"] == identity]
        measurements = [r for r in observations if r["kind"] == "probe"
                        and r["result"].get("assessment", {}).get("probe_id") == identity]
        latest = measurements[-1] if measurements else None
        last_review = history[-1] if history else None
        used = sum(p["id"] != root and roots[p["id"]] == root for p in probes)
        remaining = max(0, policy.get("max_revisions_per_probe", 0) - used)
        counterexample = any(r["result"]["assessment"]["conclusion"] == "counterexample_observed"
                             for r in measurements)
        repairable = bool(last_review and last_review["decision"] == "reject") or bool(
            latest and (latest["result"]["assessment"]["conclusion"] in {"inconclusive", "setup_failed"}
                        or (latest["result"]["assessment"]["conclusion"] == "observation_only"
                            and latest["result"]["status"] != "passed")))
        result.append(probe | {
            "root_probe_id": root, "superseded_by": children.get(identity, []),
            "last_review_decision": last_review["decision"] if last_review else None,
            "review_history": history,
            "last_observation_id": latest["id"] if latest else None,
            "last_observation_stale": latest["revision"] != revision if latest else None,
            "last_conclusion": latest["result"]["assessment"]["conclusion"] if latest else None,
            "remaining_revisions": remaining,
            "unestablished_oracles": unestablished[identity],
            "can_revise": bool(remaining and repairable and not counterexample and identity not in children),
            "run_requires_review": True,
        })
    return result


def runtime_findings(observations):
    """运行结论不与最近三条源码片段争位置，也不把无结论改称通过。"""
    findings = {}
    for row in observations:
        if row["kind"] in {"source", "project_context"}:
            continue
        assessment = row["result"].get("assessment", {})
        key = (row["kind"], row["revision"], assessment.get("probe_id"))
        findings[key] = {"observation_id": row["id"], "kind": row["kind"],
                         "revision": row["revision"], "stale": row["stale"],
                         "execution_status": row["result"]["status"],
                         "probe_id": assessment.get("probe_id"),
                         "conclusion": assessment.get("conclusion"),
                         "scope_limit": assessment.get("scope_limit", row["result"].get("coverage"))}
    return list(findings.values())


def note_progress(task, revision, *, observation_id=None, output_cursor=None, changed=False):
    """首次取回旧观察仍可能有用；只限制同revision下连续重复取得已见结果。"""
    state = task.setdefault("diagnostic_progress", {})
    if state.get("revision") != revision:
        state.clear()
        state.update(revision=revision, seen_observations=[], consecutive_revisits=0)
    if changed:
        state["consecutive_revisits"] = 0
    if observation_id:
        # 阅读新日志页属于信息获取进展，不新增观察，也不暗示重新验证了行为。
        seen = state.setdefault("seen_output_pages", []) if output_cursor else state["seen_observations"]
        identity = [observation_id, output_cursor] if output_cursor else observation_id
        if identity in seen and not changed:
            state["consecutive_revisits"] += 1
        else:
            if identity not in seen:
                seen.append(identity)
            state["consecutive_revisits"] = 0
    count = state["consecutive_revisits"]
    state["warning"] = (
        "Repeated retrieval of already seen observations produced no new evidence. "
        "Inspect a new source range, correct an eligible probe, or explain remaining uncertainty; "
        "cached retrieval is not fresh verification." if count >= 2 else None)
    if count >= task["protocol"]["diagnostic_policy"].get("max_observation_revisits", 4):
        task.update(status="unresolved", stop_reason="repeated_observation_cycle_no_progress")
