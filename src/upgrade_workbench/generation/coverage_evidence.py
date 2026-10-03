"""给旧版覆盖自报附上宿主证据边界，不把引用关联冒充语义证明。"""

from __future__ import annotations


def qualify_coverage(declarations: list[dict], observations: list[dict], revision: str) -> dict:
    """输入必须来自已核验的 public_observation；本函数不读取原文或改变退出门禁。"""
    known = {"observation:" + row["id"]: row for row in observations}
    items = []
    for declaration in declarations:
        static, runtime, unavailable = [], [], []
        for ref in declaration["evidence_refs"]:
            if not ref.startswith("observation:"):
                kind = "specification" if ref == "business_contract" else ref.split(":", 1)[0]
                static.append({"reference": ref, "kind": kind})
                continue
            row = known.get(ref)
            if row is None:
                unavailable.append(ref)
                continue
            freshness = "current" if row["revision"] == revision else "stale"
            if row["kind"] not in {"public_checks", "probe"}:
                evidence = {"reference": ref, "kind": row["kind"], "freshness": freshness,
                            "observation_revision": row["revision"]}
                if row["kind"] == "project_context" and "knowledge_revision" in row["result"]:
                    knowledge_revision = row["result"]["knowledge_revision"]
                    stale = bool(row["result"].get("stale")) or knowledge_revision != revision
                    evidence.update(knowledge_revision=knowledge_revision, knowledge_stale=stale,
                                    freshness="stale" if stale else freshness)
                static.append(evidence)
                continue
            result = row["result"]
            # 只投影实际观测范围；不按模型的 requirement 文本猜测测试覆盖了什么。
            runtime.append({
                "reference": ref, "kind": row["kind"], "revision": row["revision"],
                "freshness": freshness, "status": result.get("status"),
                "assessment": result.get("assessment"),
                "scope": result.get("scope"), "truncated": result.get("truncated", False),
                "execution_status": result.get("execution_status"),
                "probe_validity": result.get("probe_validity"),
                "stages": {
                    name: {key: stage[key] for key in (
                        "status", "tests", "nodeids", "nodeids_total", "nodeids_truncated",
                        "execution_status", "test_outcome",
                    ) if key in stage}
                    for name, stage in result.get("stages", {}).items()
                },
            })
        current = [row for row in runtime if row["freshness"] == "current"]
        completed = [row for row in current if row["status"] in {"passed", "failed"}]
        basis = (
            "current_execution_cited" if completed else
            "static_evidence_only" if static and not runtime else
            "no_current_completed_execution"
        )
        warnings = []
        if declaration["status"] == "verified" and not completed:
            warnings.append("verified_declaration_without_current_execution")
        if any(row["freshness"] == "stale" for row in runtime):
            warnings.append("stale_execution_not_current_proof")
        if any(row.get("freshness") == "stale" for row in static):
            warnings.append("stale_static_interpretation")
        if any(row["status"] != "passed" for row in current):
            warnings.append("execution_failed_or_incomplete")
        conclusions = {(row.get("assessment") or {}).get("conclusion") for row in current}
        if "observation_only" in conclusions:
            warnings.append("observation_only_not_business_acceptance")
        if "inconclusive" in conclusions:
            warnings.append("inconclusive_not_business_support")
        if "counterexample_observed" in conclusions:
            warnings.append("measured_counterexample")
        if unavailable:
            warnings.append("unknown_observation_reference")
        items.append({
            "requirement": declaration["requirement"], "scope": declaration["scope"],
            "model_declared_status": declaration["status"], "evidence_basis": basis,
            "static_references": static, "execution_references": runtime,
            "unavailable_references": unavailable, "warnings": warnings,
            "semantic_requirement_match": "not_established_by_host",
        })
    return {
        "schema_version": 1, "revision": revision, "items": items,
        "meaning": "Model-selected references classified by kind and revision, not proof that a requirement is satisfied. "
                   "Static evidence supports inference only; execution supports only its recorded paths. "
                   "Harness completion does not imply the business path completed. "
                   "No semantic entailment or independent acceptance is established here.",
    }
