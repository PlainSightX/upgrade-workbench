"""导出已验收批次的公开结果和审阅补丁，不复制模型请求、回复或自由日志。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .batches import IDENTITY, _current_tasks, _digest, _read
from .cases import load_case
from .cases.manifest import assert_no_links, read_verified_file


def export_task_report(task_path: Path, output_dir: Path) -> dict:
    """普通任务直接导出；不创建虚假的实验批次，也不将公开反馈冒充验收。"""
    from .batches import _usage
    from .tasks import inspect_task

    task = inspect_task(task_path)
    if task.get("kind") != "operation":
        raise ValueError("Use batch report-export for experimental tasks")
    output = Path(output_dir).absolute()
    assert_no_links(output)
    if output.exists() or output.resolve().is_relative_to(Path(task["manifest_path"]).parent):
        raise ValueError("Use a new export directory outside the immutable case")
    root = Path(__file__).resolve().parents[2]
    candidate = task.get("candidate")
    final = task.get("final_result")
    comparison = None
    if final:
        _reference(Path(final["path"]), root, final["sha256"])
        comparison = _read(Path(final["path"]))
        subject = task.get("verification_subject") if task.get("schema_version") in {3, 4, 5} else None
        revision = subject["revision"] if subject else candidate["revision"]
        patch_hash = subject["patch_sha256"] if subject else candidate["sha256"]
        if (final["candidate_revision"] != revision
            or final["candidate_sha256"] != patch_hash
            or comparison["case_fingerprint"] != task["case_fingerprint"]
            or comparison["check_group"] != "all"):
            raise ValueError("Final report belongs to another candidate")
    patch = None
    if candidate and candidate["reviewed"]:
        patch = Path(candidate["patch_path"]).read_bytes() or None
    usage = _usage(task)
    usage.pop("receipt_sources")
    cost = _task_cost(task, root)
    stages = {name: {key: result[key] for key in ("status", "tests") if key in result}
              for name, result in comparison["stages"].items()} if comparison else {}
    result = {
        "schema_version": 1, "task_id": task["task_id"], "case_id": task["case_id"],
        "case_fingerprint": task["case_fingerprint"], "status": task["status"],
        "stop_reason": task.get("stop_reason"), "seed_strategy": task["seed_strategy"],
        "seed_status": (task.get("seed") or {}).get("status"),
        "candidate_revision": candidate["revision"] if candidate else None,
        "candidate_sha256": candidate["sha256"] if candidate else None,
        "candidate_exported": patch is not None, "final_evaluation": task["final_evaluation"],
        "result": comparison["status"] if comparison else "not_evaluated", "stages": stages,
        "usage": usage, "cost": cost, "protocol": _public_protocol(task["protocol"]),
        "candidate_revisions": len(task["candidate_history"]),
        "review_decisions": len(task["review_history"]), "provider_payloads_exported": False,
        "origin_note": "Official seed and model increments are distinct; review is not personal implementation.",
    }
    consumption_requests = None
    if task["protocol"].get("knowledge_import") is not None:
        from .candidates import load_candidate
        from .knowledge_transfer import public_import

        case = load_case(Path(task["manifest_path"]))
        result["knowledge_import"] = public_import(task, case, load_candidate(case, task["current_candidate"]))
        consumption_requests = list(_verified_consumption_requests(task))
        result["knowledge_import_consumption"] = _knowledge_import_consumption(task, root, consumption_requests)
    if final:
        result["comparison"] = _reference(Path(final["path"]), root, final["sha256"])
    if task.get("schema_version") in {3, 4, 5}:
        from .diagnostic_state import diagnostic_execution_summary
        from .diagnostics import public_historical_input, public_observation

        observations = [public_observation(task, ref) for ref in task["observations"]]
        if (task["protocol"].get("project_context_policy") or {}).get("version") in {"project-context-v4", "project-context-v5", "project-context-v6", "project-context-v7", "project-context-v8", "project-context-v9", "project-context-v10"}:
            result["investigation_selections"] = [
                row["result"] | {"observation_id": row["id"]}
                for row in observations if row["action"]["type"] == "select_investigation"
            ]
            result["investigation_consumption"] = _investigation_consumption(task, root, consumption_requests)
        if (task["protocol"].get("project_context_policy") or {}).get("version") in {"project-context-v5", "project-context-v6", "project-context-v7", "project-context-v8", "project-context-v9", "project-context-v10"}:
            result["topic_revisions"] = [row["result"] | {"observation_id": row["id"]}
                for row in observations if row["action"]["type"] == "revise_project_topic"]
        if task.get("knowledge_review_state") is not None:
            state = task["knowledge_review_state"]
            completion = state["completion_reference"]
            consumed = state["consumed_by"]
            index = next((i for i, attempt in enumerate(task["attempts"], start=1)
                          if consumed and attempt["request_id"] == consumed["request_id"]), None)
            result["knowledge_review"] = {"phase": state["phase"], "start_revision": state["start_revision"],
                "completion": next((row for row in observations if completion and row["id"] == completion["id"]), None),
                "consumer": next((row for row in result.get("knowledge_import_consumption", []) if row["attempt_index"] == index), None),
                "meaning": "Consumer identifies delivery of the review and effective topics; semantic use and business acceptance require separate evidence."}
        result.update(schema_version=2, finish=task.get("finish"),
                      verification_subject={k: v for k, v in task.get("verification_subject", {}).items() if k != "source_reference"},
                      result=final["status"] if final else "not_evaluated",
                      source_changes=bool(patch), diagnostic_runs=len(task["diagnostic_runs"]),
                       probes=len(task["probes"]), diagnostics=diagnostic_execution_summary(task),
                      observations=[{"id": r["id"], "kind": r["kind"], "revision": r["revision"],
                                     "status": r["result"].get("status")} for r in observations])
        if task.get("schema_version") in {4, 5}:
            result.update(schema_version=3, contract_requirements=task["contract_requirements"],
                          contract_audits=len(task.get("contract_audits", [])))
        if task.get("schema_version") == 5:
            role_calls = Counter(attempt.get("role", "solver") for attempt in task["attempts"])
            historical = [
                public_historical_input(task, reference)
                for reference in task.get("historical_inputs", [])
            ]
            result.update(
                schema_version=4,
                dependency_queries=len(task.get("dependency_queries", [])),
                dependency_facts=len(task.get("dependency_facts", [])),
                role_calls=dict(role_calls),
                investigator_sessions=[
                    {
                        "status": item["status"],
                        "attempts": len(item.get("attempt_indices", [])),
                        "start_revision": item["start_revision"],
                        **({"end_revision": item["end_revision"]} if item.get("end_revision") else {}),
                        **({"failure": item["failure"]} if item.get("failure") is not None else {}),
                    }
                    for item in task.get("investigator_sessions", [])
                ],
                investigator_handoffs=len(task.get("investigator_handoffs", [])),
                historical_inputs=[
                    {
                        "id": item["id"],
                        "classification": item["classification"],
                        "source": item["source"],
                        "candidate": item["candidate"],
                    }
                    for item in historical
                ],
            )
    output.mkdir(parents=True, exist_ok=False)
    if patch is not None:
        (output / "candidate.patch").write_bytes(patch)
    seed = task.get("seed")
    if seed and seed.get("status") in {"pending_candidate_safety_review", "no_change"}:
        seed_path = Path(seed["candidate_path"])
        _reference(seed_path, root, seed["candidate_sha256"])
        result["official_seed_sha256"] = seed["candidate_sha256"]
        # 种子文件单独导出，绝不把模型后续修改写成官方工具输出。
        (output / "official-seed.patch").write_bytes(seed_path.read_bytes())
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 升级任务结果", "", f"案例：{task['case_id']}；状态：{task['status']}。",
             f"初始策略：{task['seed_strategy']}；候选版本数：{len(task['candidate_history'])}。",
             f"独立验收：{result['result']}。公开反馈通过不代替独立验收。", "",
             "| 阶段 | 状态 | 通过 / 收集 |", "| --- | --- | --- |"]
    for name, stage in stages.items():
        tests = stage.get("tests", {})
        lines.append(f"| {name} | {stage['status']} | {tests.get('passed', '-')} / {tests.get('collected', '-')} |")
    lines += ["", f"模型请求{usage['calls']}次；报告输入/输出token：{usage['reported_input_tokens']}/{usage['reported_output_tokens']}；未知用量{usage['unknown_usage_calls']}次。",
              f"仅本任务估算/预留费用：{cost['estimated_or_reserved_usd']:.6f} USD，不是供应商账单。",
              "官方静态转换不是自研算法；模型修补通过不等于用户独立实现。范围仅限登记行为，不能外推完整应用。"]
    if patch is not None:
        manifest = Path(task["manifest_path"]).relative_to(root).as_posix() if Path(task["manifest_path"]).is_relative_to(root) else "<manifest.json>"
        execution = result["protocol"]["execution"]
        extra = f" --base-image '{execution['base_image']}'" if "base_image" in execution else ""
        extra += f" --prepare-timeout {execution.get('prepare_timeout', 600)} --test-timeout {execution.get('test_timeout', 60)}"
        lines += ["", "复现已审补丁，无需模型API。沿用已准备的项目环境；新机器由环境负责人按 docs/USAGE.md 安装所需 extras，再运行：", "", "```powershell",
                  f"uv run --no-sync upgrade-workbench verify '{manifest}' --candidate '<导出包>/candidate.patch' --candidate-origin {candidate['origin']} --check-group all{extra}", "```"]
    if task.get("schema_version") in {3, 4, 5}:
        counts = result["diagnostics"]["counts"]
        lines += ["", f"退出声明：{(task.get('finish') or {}).get('reason', task.get('stop_reason', task['status']))}。",
                  f"诊断登记{counts['registered']}次、完成{counts['completed']}次、失败{counts['failed']}次、"
                  f"未知{counts['unknown']}次、历史状态{counts['historical']}次；探针{result['probes']}份。探针不计入独立验收。",
                  "模型正常交付、部分候选、无需源码改动声明和最终验收结果分别记录。"]
        latest = result["diagnostics"]["latest"]
        evidence = ((result.get("finish") or {}).get("diagnostic_scope") or {}).get("coverage_evidence")
        if evidence:
            counts_by_basis = Counter(item["evidence_basis"] for item in evidence["items"])
            lines += ["", "覆盖自报的证据边界（不等于已证明的需求数）：",
                      f"仅静态依据 {counts_by_basis['static_evidence_only']} 项；"
                      f"引用当前已完成执行 {counts_by_basis['current_execution_cited']} 项；"
                      f"缺少当前已完成执行 {counts_by_basis['no_current_completed_execution']} 项。",
                      "源码未改不证明行为未变；已执行不证明覆盖整项需求。模型 verified 只是自报，"
                      "逐项引用、过期状态与探针局限见 report.json 的 coverage_evidence；独立验收另列。"]
        if latest and latest.get("failure"):
            failure = latest["failure"]
            lines.append(f"最近诊断失败：{failure['category']}/{failure['code']}；"
                         f"阶段{latest['last_phase']}；动作{latest['last_action']}；"
                         f"错误类别{failure['error_type']}；收据{latest['failure_receipt_id'] or 'unavailable'}。")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"status": "report_exported", "json_path": str(output / "report.json"),
            "markdown_path": str(output / "report.md"), "result": result["result"]}


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.+:/@-]{1,200}", value):
        raise ValueError("A public report identifier is invalid")
    return value


def _url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Public report URLs must be credential-free HTTPS URLs")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _verified_consumption_requests(task):
    """一次导出共享同次完整校验结果；不建立可跨任务复用的信任缓存。"""
    from .candidates import load_candidate
    from .diagnostics import read
    from .generation.provider import _verify_payload

    for index, attempt in enumerate(task["attempts"], start=1):
        receipt = _read(Path(attempt["receipt"]))
        if receipt.get("calls", 0) == 0 or receipt.get("request_storage") == "metadata_only":
            continue
        path = Path(receipt["request_path"])
        assert_no_links(path)
        body = path.read_bytes()
        # 提交回执包含新候选；请求仍绑定执行前的候选，按冻结上下文恢复输入身份。
        frozen = read(receipt["diagnostic_context_reference"], Path(task["task_path"]).parent)
        base = load_candidate(load_case(Path(task["manifest_path"])), frozen["current_candidate"])
        prepared = receipt | {"candidate_reference": frozen["current_candidate"], "candidate_revision": base.revision}
        _verify_payload(prepared, body)
        context = json.loads(json.loads(body)["messages"][1]["content"])
        yield index, receipt, path, context


def _investigation_consumption(task, root, requests=None):
    """导出实际发送请求的装载结果；选择记录本身不证明已被消费。"""
    rows = []
    for index, receipt, path, context in (requests if requests is not None else _verified_consumption_requests(task)):
        plan = context["context_plan"]["investigation"]
        selection = plan["selection"]
        if selection is None:
            continue
        rows.append({"attempt_index": index, "mode": selection["mode"],
            "delivery_state": "response_received" if receipt.get("response_sha256") else "dispatch_outcome_unknown",
            "selection_observation_id": selection["observation_id"],
            "revision": context["candidate"]["revision"], "priority_paths": plan["priority_paths"],
            "delivered_paths": sorted({row["path"] for row in context["source_files"]}),
            "source_body_bytes": context["source_selection"]["body_bytes"],
            **({"excerpt_delivery": plan["excerpt_delivery"],
                "meaning": "Actual current source ranges delivered; semantic knowledge use requires separate decision evidence."}
               if "excerpt_delivery" in plan else {}),
            "request": _reference(path, root, receipt["request_sha256"])})
    return rows


def _knowledge_import_consumption(task, root, requests=None):
    """按原请求记录送达；知识被发送不等于模型正确使用或业务通过。"""
    rows = []
    for index, receipt, path, ctx in (requests if requests is not None else _verified_consumption_requests(task)):
        view = ctx["project_context"]
        if "imported_knowledge" not in view:
            from .generation.investigator_context import policy

            # 独立核查只收到有效主张；不能把来源身份从宿主补成已发送的导入包。
            config = policy(task["protocol"].get("investigator_context_policy"))
            if (config is None
                    or receipt.get("investigator_context_policy") != config
                    or receipt.get("output_format") != "investigator_actions"
                    or ctx["diagnostic_state"].get("context_role") != "investigator"
                    or view.get("investigator_context_policy") != config):
                raise ValueError("Imported knowledge missing from an unrecognized request view")
            claims = view["topic_maintenance"]
            rows.append({"attempt_index": index, "delivery_view": "source_first_claims",
                "current_revision": claims["current_revision"],
                "topic_ids": [entry["topic"]["id"] for entry in claims["topics"]],
                "topic_claims": [{key: entry[key] for key in ("origin", "topic_sha256", "interpretation_revision")}
                                 for entry in claims["topics"]],
                "delivery_state": "response_received" if receipt.get("response_sha256") else "dispatch_outcome_unknown",
                "meaning": "Effective claims and source identity were delivered; import metadata and prior review conclusions were withheld. Delivery does not establish semantic use.",
                "request": _reference(path, root, receipt["request_sha256"])})
            continue
        imported = view["imported_knowledge"]
        rows.append({"attempt_index": index, "bundle_sha256": imported["bundle_sha256"],
            "source_task_id": imported["origin"]["task_id"], "current_revision": imported["current_revision"],
            "topic_ids": [page["id"] for page in imported["topics"]],
            "delivery_state": "response_received" if receipt.get("response_sha256") else "dispatch_outcome_unknown",
            "request": _reference(path, root, receipt["request_sha256"])})
        if imported.get("version") == "knowledge-import-v2":
            rows[-1]["exporting_task_id"] = rows[-1].pop("source_task_id")
            rows[-1]["topic_origins"] = [{"id": page["id"], "origin": page["import_origin"],
                                          "lineage": page["lineage"]} for page in imported["topics"]]
    return rows


def _reference(path: Path, root: Path, digest: str | None = None) -> dict:
    path = path.absolute()
    assert_no_links(path)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest is not None and actual != digest:
        raise ValueError("An export evidence source changed")
    try:
        location = path.relative_to(root).as_posix()
        recoverability = "workspace_relative"
    except ValueError:
        # 不把用户名、外部宿主路径或同名文件夹中的私密信息带到PR附件。
        location = "external/" + path.name
        recoverability = "resolve_via_original_local_batch"
    return {"path": location, "sha256": actual, "recoverability": recoverability}


def _number(value, *, integer=False):
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not math.isfinite(value) or value < 0:
        raise ValueError("Report counters must be finite nonnegative numbers")
    return value


def _task_cost(task: dict, root: Path) -> dict:
    """只读取该批请求行，不能把共享账本总额重复归因给每个任务。"""
    path = Path(task["budget_path"]).absolute()
    assert_no_links(path)
    try:
        location = path.relative_to(root).as_posix()
    except ValueError:
        location = "external/" + path.name
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        configuration = db.execute("SELECT body FROM configuration WHERE id=1").fetchone()
        spec = json.loads(configuration[0])
        if spec["model"] != task["protocol"]["generation"]["model"]:
            raise ValueError("Export budget model differs from task protocol")
        rows = []
        for attempt in task["attempts"]:
            request_id = attempt.get("request_id", Path(attempt["receipt"]).parent.name)
            row = db.execute("SELECT request_id,phase,request_hash,reserved,charged,state FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise ValueError("A registered model attempt has no budget ledger row")
            receipt = _read(Path(attempt["receipt"]))
            if row[1] != task["phase"] or row[2] != receipt["request_sha256"]:
                raise ValueError("Budget charge does not match the task request identity")
            rows.append({"request_id": row[0], "reserved_usd": row[3] / 1_000_000,
                         "estimated_usage_usd": None if row[4] is None else row[4] / 1_000_000,
                         "state": _identifier(row[5])})
    return {"ledger": location, "pricing_source": _url(spec["pricing_source"]),
            "pricing_checked_at": _identifier(spec["pricing_checked_at"]),
            "input_per_million_usd": str(spec["input_per_million"]),
            "output_per_million_usd": str(spec["output_per_million"]),
            "estimated_or_reserved_usd": round(sum(row["estimated_usage_usd"] if row["estimated_usage_usd"] is not None else row["reserved_usd"] for row in rows), 6),
            "calls": rows, "actual_billing": "not_claimed"}


def _public_protocol(protocol: dict) -> dict:
    generation = protocol["generation"]
    result = {
        "max_calls": protocol["max_calls"],
        "generation": {key: generation[key] for key in (
            "model", "thinking_mode", "max_output_tokens", "timeout_seconds", "max_source_bytes", "max_request_bytes",
        ) if key in generation},
        "execution": {key: protocol.get("execution", {})[key] for key in (
            "base_image", "prepare_timeout", "test_timeout",
        ) if key in protocol.get("execution", {})},
    }
    result["generation"]["model"] = _identifier(generation["model"])
    result["generation"]["endpoint"] = _url(generation["endpoint"])
    if "implementation" in protocol:
        result["implementation_identity_sha256"] = _digest(protocol["implementation"])
    if "protocol_revision" in protocol:
        result["protocol_revision"] = protocol["protocol_revision"]
    for key in (
        "source_policy", "diagnostic_policy", "contract_audit_policy", "investigator_policy",
        "investigator_context_policy",
        "dependency_query_policy", "role_budget", "context_assistance", "navigation_assistance",
        "project_context_policy", "source_read_retention",
        "workflow_profile", "finish_policy", "investigation_policy", "knowledge_import", "semantic_risk_policy",
    ):
        if key in protocol:
            result[key] = protocol[key]
    return result


def export_batch_report(batch_path: Path, output_dir: Path, *, evidence_root: Path | None = None,
                        include_reviewed_patches: bool = True) -> dict:
    """严格校验完整批次后导出；不会评分、调用模型或读取环境变量中的密钥。"""
    batch_path, output_dir = Path(batch_path).absolute(), Path(output_dir).absolute()
    root = Path(evidence_root).absolute() if evidence_root else Path(__file__).resolve().parents[2]
    assert_no_links(output_dir)
    if type(include_reviewed_patches) is not bool:
        raise ValueError("Patch export policy must be explicit")
    if output_dir.exists():
        raise ValueError("Export destination already exists; preserve earlier reports")
    batch = _read(batch_path)
    registration = batch["registration"]
    if (batch["status"] != "evaluated" or batch["final_evaluation_disclosed"] is not True
        or Path(batch["batch_path"]) != batch_path
        or _digest(registration) != batch["registration_sha256"]
        or _digest(registration["protocol"]) != registration["protocol_sha256"]):
        raise ValueError("Only a complete, unchanged evaluated batch may be exported")
    report_path, frozen_path = Path(batch["report_path"]), Path(batch["candidates_path"])
    source_report_ref = _reference(report_path, root, batch["report_sha256"])
    frozen_ref = _reference(frozen_path, root, batch["candidates_sha256"])
    summary, frozen = _read(report_path), _read(frozen_path)
    tasks = _current_tasks(batch)
    if (summary["status"] != "batch_evaluated" or summary["phase"] != batch["phase"]
        or summary["registration_sha256"] != batch["registration_sha256"]
        or summary["candidates_sha256"] != batch["candidates_sha256"]
        or summary["formal_matrix"] != (batch["phase"] != "calibration")
        or batch["formal_matrix"] != summary["formal_matrix"]
        or not (len(tasks) == len(frozen["tasks"]) == len(summary["results"]) == summary["registered_tasks"])):
        raise ValueError("Export cannot mix phases, omit tasks or change candidate identities")
    protocol_hash = registration["protocol_sha256"]
    if any(task["protocol_sha256"] != protocol_hash or task["phase"] != batch["phase"] for task in tasks):
        raise ValueError("All exported tasks must share the frozen protocol and phase")
    cases, rows, patches = {}, [], []
    for task, entry, result in zip(tasks, frozen["tasks"], summary["results"], strict=True):
        if any(task[key] != entry[key] or task[key] != result[key] for key in IDENTITY):
            raise ValueError("Exported result does not belong to the registered task")
        snapshot = _read(Path(entry["task_snapshot"]))
        if _digest(snapshot) != entry["task_sha256"] or _digest(task) != entry["task_sha256"] or result["task_sha256"] != entry["task_sha256"]:
            raise ValueError("Export task snapshot changed after candidate freeze")
        case = load_case(Path(task["manifest_path"]))
        if output_dir.resolve().is_relative_to(case.root):
            raise ValueError("Exports cannot be written inside immutable cases")
        if case.fingerprint not in cases:
            bundle_path = "evidence/bundle.json"
            bundle = json.loads(read_verified_file(case.root, bundle_path, case.manifest.file_hashes[bundle_path]))
            manifest_ref = _reference(case.manifest_path, root)
            cases[case.fingerprint] = {
                "case_id": _identifier(case.manifest.case_id), "fingerprint": case.fingerprint,
                "repository": _url(case.manifest.source.repository), "revision": _identifier(case.manifest.source.revision),
                "package": _identifier(bundle["package"]), "old_version": _identifier(bundle["old_version"]),
                "new_version": _identifier(bundle["new_version"]), "manifest": manifest_ref,
                "old_lock_sha256": case.manifest.file_hashes[case.manifest.old_lock],
                "new_lock_sha256": case.manifest.file_hashes[case.manifest.new_lock],
                "allowed_changes": case.manifest.allowed_changes,
            }
        usage = {"registered_attempts": len(task["attempts"]), "model_calls_attempted": 0,
                 "input_tokens": 0, "output_tokens": 0, "unknown_usage_calls": 0,
                 "provider_duration_seconds": 0.0, "receipts": []}
        if len(result["usage"]["receipt_sources"]) != len(task["attempts"]):
            raise ValueError("Export receipt count differs from frozen task attempts")
        for attempt, expected in zip(task["attempts"], result["usage"]["receipt_sources"], strict=True):
            path = Path(attempt["receipt"])
            if path != Path(expected["path"]):
                raise ValueError("Export receipt path changed")
            receipt_ref = _reference(path, root, expected["sha256"])
            receipt = _read(path)
            if receipt["case_fingerprint"] != case.fingerprint or receipt["model"] != task["protocol"]["generation"]["model"]:
                raise ValueError("Export receipt case or model differs")
            calls = _number(receipt.get("calls", 1), integer=True)
            if calls > 1:
                raise ValueError("A provider receipt cannot contain repeated paid calls")
            usage["model_calls_attempted"] += calls
            model_usage = receipt.get("model_usage", {})
            incoming, outgoing = model_usage.get("input_tokens"), model_usage.get("output_tokens")
            if incoming is None or outgoing is None:
                usage["unknown_usage_calls"] += calls
            else:
                usage["input_tokens"] += _number(incoming, integer=True)
                usage["output_tokens"] += _number(outgoing, integer=True)
            usage["provider_duration_seconds"] += _number(receipt.get("duration_seconds", 0.0))
            usage["receipts"].append({**receipt_ref, "request_sha256": receipt["request_sha256"],
                                      "status": _identifier(receipt["status"]), "calls": calls,
                                      "returned_model": _identifier(receipt["returned_model"]) if receipt.get("returned_model") else None})
        candidate_ref = None
        rejected_ref = None
        if entry.get("rejected_candidate"):
            rejected_ref = _reference(Path(entry["rejected_candidate"]["patch_path"]), root, entry["rejected_candidate"]["sha256"])
        if entry["candidate"]:
            candidate_ref = _reference(Path(entry["candidate"]["patch_path"]), root, entry["candidate"]["sha256"])
            if include_reviewed_patches:
                filename = "candidates/" + _identifier(task["task_id"]) + ".patch"
                patches.append((Path(entry["candidate"]["patch_path"]), filename, candidate_ref["sha256"]))
                candidate_ref["exported_path"] = filename
        comparison_ref = None
        success = False
        category = "candidate_contract_rejected" if rejected_ref else _identifier(task["status"])
        verification_seconds = 0.0
        if result["comparison"]:
            comparison_ref = _reference(Path(result["comparison"]["path"]), root, result["comparison"]["sha256"])
            comparison = _read(Path(result["comparison"]["path"]))
            if comparison["case_fingerprint"] != case.fingerprint or comparison["check_group"] != "all":
                raise ValueError("Exported comparison belongs to another case or scope")
            category = _identifier(comparison["status"])
            success = category == "candidate_verified"
            if success and (candidate_ref is None or comparison.get("candidate", {}).get("supplied_sha256") != candidate_ref["sha256"]):
                raise ValueError("Successful export requires the exact reviewed candidate")
            comparison_ref["status"] = category
            verification_seconds = _number(comparison.get("duration_seconds", 0.0))
        if result["result"] != ("solved" if success else "not_solved"):
            raise ValueError("Export summary success disagrees with underlying comparison")
        rows.append({"task_id": task["task_id"], "case_id": task["case_id"], "case_fingerprint": case.fingerprint,
                     "arm": task["arm"], "repetition": task["repetition"], "phase": task["phase"],
                     "protocol_sha256": protocol_hash, "task_status": task["status"],
                     "result": "solved" if success else "not_solved", "failure_category": "none" if success else category,
                     "candidate": candidate_ref, "rejected_candidate": rejected_ref, "comparison": comparison_ref, "usage": usage,
                     "verification_duration_seconds": verification_seconds, "cost": _task_cost(task, root),
                     "human_review_decisions": len(task["review_history"]),
                     "task_snapshot": _reference(Path(entry["task_snapshot"]), root)})
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["case_id"], row["arm"])].append(row)
    groups = [{"case_id": case, "arm": arm, "succeeded": sum(row["result"] == "solved" for row in items),
               "registered": len(items), "repetitions": [{"repetition": row["repetition"], "result": row["result"]} for row in items]}
              for (case, arm), items in sorted(grouped.items())]
    totals = {key: sum(row["usage"][key] for row in rows) for key in (
        "registered_attempts", "model_calls_attempted", "input_tokens", "output_tokens", "unknown_usage_calls", "provider_duration_seconds",
    )}
    totals.update(registered_tasks=len(rows), succeeded_tasks=sum(row["result"] == "solved" for row in rows),
                  projects=len({case["repository"] for case in cases.values()}),
                  verification_duration_seconds=sum(row["verification_duration_seconds"] for row in rows),
                  estimated_or_reserved_usd=round(sum(row["cost"]["estimated_or_reserved_usd"] for row in rows), 6))
    exported = {"schema_version": 1, "status": "report_exported", "exported_at": datetime.now(UTC).isoformat(),
                "phase": batch["phase"], "formal_matrix": batch["formal_matrix"], "protocol_sha256": protocol_hash,
                "protocol": _public_protocol(registration["protocol"]), "cases": list(cases.values()),
                "totals": totals, "groups": groups, "tasks": rows,
                "failure_groups": dict(Counter(row["failure_category"] for row in rows if row["result"] != "solved")),
                "sources": {"batch": _reference(batch_path, root), "summary": source_report_ref, "candidate_manifest": frozen_ref},
                "review_boundary": "Safety review decisions are recorded; independent human implementation and extra assistance are not inferred.",
                "actual_billing": "not_claimed", "provider_payloads_exported": False,
                "reviewed_patches_included": include_reviewed_patches}
    output_dir.mkdir(parents=True, exist_ok=False)
    for original, name, digest in patches:
        contents = original.read_bytes()
        if hashlib.sha256(contents).hexdigest() != digest:
            raise ValueError("Reviewed patch changed during export")
        target = output_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    (output_dir / "report.json").write_text(json.dumps(exported, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "report.md").write_text(_markdown(exported), encoding="utf-8")
    return {"status": "report_exported", "json_path": str(output_dir / "report.json"),
            "markdown_path": str(output_dir / "report.md"), "reviewed_patches": len(patches),
            "registered_tasks": len(rows), "formal_matrix": batch["formal_matrix"]}


def _markdown(report: dict) -> str:
    totals = report["totals"]
    generation = report["protocol"]["generation"]
    lines = ["# 依赖升级批次报告", "", f"阶段：{report['phase']}；正式矩阵：{'是' if report['formal_matrix'] else '否（开发校准）'}。",
             f"协议 SHA-256：`{report['protocol_sha256']}`。所有任务使用同一冻结协议；以下包含全部登记任务。",
             f"{totals['projects']} 个独立仓库，{totals['registered_tasks']} 次登记任务，{totals['succeeded_tasks']} 次通过最终验收。项目数、任务数和测试数不同。",
             "重复结果只说明本批观察到的波动，不代表统计显著、一般成功率或全面优于其他 Agent。", "", "## 输入与版本", ""]
    for case in report["cases"]:
        lines.append(f"- {case['case_id']}：{case['repository']} @ `{case['revision']}`；{case['package']} {case['old_version']} → {case['new_version']}；案例指纹 `{case['fingerprint']}`。")
    lines += ["", "## 实验条件", "",
              f"请求模型：`{generation['model']}`；thinking：`{generation.get('thinking_mode')}`；每任务最多 {report['protocol']['max_calls']} 次模型请求，每次输出上限 {generation['max_output_tokens']} token、超时 {generation['timeout_seconds']} 秒。实际返回模型标识保留在JSON收据摘要中。",
              "`generic`：通用源码工具与公开反馈；`no_ast`：增加版本原文；`no_evidence`：增加AST定位；`full`：AST、原文、公开反馈齐全；`no_feedback`：与full相同但生成阶段看不到执行反馈。所有臂的最终验收相同。",
              "只有实际登记的臂和重复列在下面；没有运行的臂不产生结论。", ""]
    if report["protocol"].get("protocol_revision") == 3:
        lines += ["本批codemod_repair/direct_repair都使用当前候选增量协议、固定版本原文和公开反馈，均不发送AST定位。唯一预定策略差异是是否以官方工具转换作为初始候选；不是旧原始相对协议与新接口的独立因果比较。", ""]
    lines += ["", "## 全部结果", "", "| 案例 | 实验臂 | 重复 | 任务终态 | 最终结果 | 失败分类 |",
              "| --- | --- | --- | --- | --- | --- |"]
    for row in report["tasks"]:
        lines.append(f"| {row['case_id']} | {row['arm']} | {row['repetition']} | {row['task_status']} | {row['result']} | {row['failure_category']} |")
    lines += ["", "| 案例 | 实验臂 | 成功/登记任务 |", "| --- | --- | --- |"]
    for group in report["groups"]:
        lines.append(f"| {group['case_id']} | {group['arm']} | {group['succeeded']}/{group['registered']} |")
    lines += ["", "失败分组：" + ("；".join(f"{name}={count}" for name, count in sorted(report["failure_groups"].items())) or "无") + "。"]
    lines += ["", "## 成本与时间", "",
              f"登记尝试 {totals['registered_attempts']} 次，实际尝试模型请求 {totals['model_calls_attempted']} 次；报告输入 {totals['input_tokens']} token、输出 {totals['output_tokens']} token，另有 {totals['unknown_usage_calls']} 次用量未知。",
              f"模型请求累计耗时 {totals['provider_duration_seconds']:.3f} 秒，最终隔离验收累计耗时 {totals['verification_duration_seconds']:.3f} 秒；两者相加不是端到端墙钟耗时。",
              f"仅归属于本批请求的单价估算及未知请求预留合计 **{totals['estimated_or_reserved_usd']:.6f} USD**，不是供应商账单，也不包含其他批次或Codex会话费用。", ""]
    lines += ["| 案例/实验臂/重复 | 请求次数 | 输入/输出token | 模型耗时秒 | 验收耗时秒 | 估算/预留USD |",
              "| --- | --- | --- | --- | --- | --- |"]
    for row in report["tasks"]:
        usage = row["usage"]
        lines.append(f"| {row['case_id']}/{row['arm']}/{row['repetition']} | {usage['model_calls_attempted']} | {usage['input_tokens']}/{usage['output_tokens']} | {usage['provider_duration_seconds']:.3f} | {row['verification_duration_seconds']:.3f} | {row['cost']['estimated_or_reserved_usd']:.6f} |")
    lines.append("")
    seen = set()
    for row in report["tasks"]:
        cost = row["cost"]
        if cost["ledger"] not in seen:
            seen.add(cost["ledger"])
            lines.append(f"- 单价来源：{cost['pricing_source']}（{cost['pricing_checked_at']}）；每百万输入/输出 token 为 {cost['input_per_million_usd']}/{cost['output_per_million_usd']} USD。账本：`{cost['ledger']}`。")
    lines += ["", "## 人工参与与限制", "",
              f"记录到 {sum(row['human_review_decisions'] for row in report['tasks'])} 次候选审阅决定。审阅记录不等于人工独立实现；额外协助程度未从代码或成功结果倒推。自由审阅笔记、模型请求、原始回复和日志没有复制进导出包。",
              "无候选、预算耗尽、环境未完成均保留在任务分母中。环境失败单列，不能据此断言迁移方法失败。最终验收没有返回模型继续修补。", "", "## 复现与证据", "",
              "将导出包放在任意位置，从保存了对应案例的仓库根目录执行；先核对 JSON 中的案例指纹、锁摘要、协议和补丁摘要。沿用已准备的项目环境；新机器由环境负责人按 docs/USAGE.md 安装所需 extras。所有下列命令不调用模型：", "", "```powershell"]
    by_id = {case["case_id"]: case for case in report["cases"]}
    execution = report["protocol"]["execution"]
    for row in report["tasks"]:
        patch = row["candidate"]
        if patch and patch.get("exported_path"):
            manifest = by_id[row["case_id"]]["manifest"]["path"]
            extra = f" --base-image '{execution['base_image']}'" if "base_image" in execution else ""
            extra += f" --prepare-timeout {execution.get('prepare_timeout', 600)} --test-timeout {execution.get('test_timeout', 60)}"
            lines.append(f"uv run --no-sync upgrade-workbench verify '{manifest}' --candidate '<导出包>/{patch['exported_path']}' --candidate-origin agent_candidate --check-group all{extra}")
    lines += ["```", "", "无候选任务不能重建不存在的补丁；它们的来源保留在JSON。原始本机证据按仓库相对路径与SHA-256定位，外部路径以脱敏定位标记，须经原本机批次记录恢复。", ""]
    for key, ref in report["sources"].items():
        lines.append(f"- {key}：`{ref['path']}`，SHA-256 `{ref['sha256']}`。")
    for row in report["tasks"]:
        if row["comparison"]:
            ref = row["comparison"]
            lines.append(f"- {row['case_id']}/{row['arm']}/{row['repetition']}：`{ref['path']}`，SHA-256 `{ref['sha256']}`。")
    return "\n".join(lines) + "\n"
