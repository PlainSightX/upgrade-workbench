"""先冻结整批任务与全部候选，再统一揭示验收结果；不向求解器返回评分。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .budget import BudgetLedger
from .candidates import load_candidate
from .cases import load_case
from .cases.manifest import assert_no_links
from .tasks import ARMS, TERMINAL
from .workflow import run_comparison

IDENTITY = ("task_id", "task_path", "case_id", "case_fingerprint", "manifest_path",
            "arm", "phase", "repetition", "protocol_sha256", "budget_path")


def _digest(value: dict | bytes) -> str:
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> dict:
    assert_no_links(path)
    if path.stat().st_size > 2_000_000:
        raise ValueError("Batch input exceeds its byte limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Batch input must be a JSON object")
    return value


def _save(value: dict, path: Path) -> None:
    assert_no_links(path)
    temporary = path.with_suffix(".tmp")
    assert_no_links(temporary)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _task_identity(task: dict, protocol: dict) -> dict:
    if _digest(protocol) != task["protocol_sha256"] or task["protocol"] != protocol:
        raise ValueError("Task protocol differs from the batch protocol")
    if (task["arm"] not in ARMS or task["phase"] not in {"calibration", "development", "holdout"}
        or type(task["repetition"]) is not int or task["repetition"] < 1):
        raise ValueError("Task experiment identity is invalid")
    case = load_case(Path(task["manifest_path"]))
    if case.fingerprint != task["case_fingerprint"] or case.manifest.case_id != task["case_id"]:
        raise ValueError("Task case identity changed")
    if protocol.get("protocol_revision") == 3:
        if task.get("seed_strategy") != ("official" if task["arm"] == "codemod_repair" else "none"):
            raise ValueError("Task seed strategy changed from its preregistered arm")
    return {key: task[key] for key in IDENTITY}


def create_batch(tasks: list[dict], protocol: dict, output_path: Path) -> dict:
    """登记完整矩阵；已调用模型的任务不能事后冒充预登记样本。"""
    if not isinstance(tasks, list) or not tasks or len(tasks) > 1000:
        raise ValueError("A batch requires between 1 and 1000 preregistered tasks")
    output_path = Path(output_path).absolute()
    assert_no_links(output_path)
    entries, tuples, paths, ids, phases = [], set(), set(), set(), set()
    for task in tasks:
        path = Path(task["task_path"]).absolute()
        stored = _read(path)
        if stored != task or Path(stored["task_path"]) != path:
            raise ValueError("Task must match its original stored path and content")
        if stored["status"] != "ready" or stored["attempts"] or stored.get("candidate") is not None:
            raise ValueError("Only ready tasks without attempts may be preregistered")
        identity = _task_identity(stored, protocol)
        key = (identity["case_fingerprint"], identity["arm"], identity["repetition"], identity["phase"])
        if key in tuples or str(path) in paths or identity["task_id"] in ids:
            raise ValueError("Duplicate task or case/arm/repetition/phase tuple")
        if output_path.resolve().is_relative_to(Path(stored["manifest_path"]).parent.resolve()):
            raise ValueError("Batch output cannot belong to an immutable case")
        tuples.add(key)
        paths.add(str(path))
        ids.add(identity["task_id"])
        phases.add(identity["phase"])
        entries.append(identity)
    if len(phases) != 1:
        raise ValueError("Calibration, development and holdout require separate batches")
    registration = {"protocol": protocol, "protocol_sha256": _digest(protocol), "tasks": entries}
    batch = {
        "schema_version": 1, "batch_id": uuid4().hex, "batch_path": str(output_path),
        "status": "registered", "phase": next(iter(phases)), "registration": registration,
        "registration_sha256": _digest(registration), "created_at": datetime.now(UTC).isoformat(),
        "formal_matrix": phases != {"calibration"}, "final_evaluation_disclosed": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as handle:
        json.dump(batch, handle, ensure_ascii=False, indent=2)
    return batch


def _current_tasks(batch: dict) -> list[dict]:
    tasks = []
    for expected in batch["registration"]["tasks"]:
        task_path = Path(expected["task_path"])
        _task_unlocked(task_path)
        task = _read(task_path)
        if _task_identity(task, batch["registration"]["protocol"]) != expected:
            raise ValueError("A preregistered task identity changed")
        if task["status"] not in TERMINAL:
            raise ValueError("Every preregistered task must be terminal before final evaluation")
        if task.get("final_evaluation") != "not_run":
            raise ValueError("A task already exposed final evaluation outside this batch")
        candidate = task.get("candidate")
        if candidate is not None:
            if task.get("schema_version") == 2:
                snapshot = load_candidate(load_case(Path(task["manifest_path"])), task["current_candidate"])
                if snapshot.revision != candidate["revision"] or snapshot.sha256 != candidate["sha256"]:
                    raise ValueError("Candidate revision no longer matches task")
                if not any(review.get("revision") == candidate["revision"] and review["sha256"] == candidate["sha256"]
                           for review in task["review_history"]):
                    raise ValueError("Current revision has not been reviewed")
            rejected = task["status"] == "candidate_rejected"
            if rejected and (
                candidate.get("review_decision") != "rejected" or candidate.get("reviewed") is not False
                or not any(review.get("sha256") == candidate["sha256"] and review.get("decision") == "rejected"
                           and review.get("reason") == "candidate_contract_rejected" and review.get("reviewer") and review.get("note")
                           for review in task.get("review_history", []))
            ):
                raise ValueError("Rejected candidate requires an explicit hash-bound rejection")
            if not rejected and (candidate.get("reviewed") is not True or not any(
                review.get("sha256") == candidate["sha256"] and review.get("reviewer") and review.get("note")
                for review in task.get("review_history", [])
            )):
                raise ValueError("Every candidate needs an explicit hash-bound review")
            patch = Path(candidate["patch_path"])
            assert_no_links(patch)
            if patch.stat().st_size > 96_000 or _digest(patch.read_bytes()) != candidate["sha256"]:
                raise ValueError("Candidate patch changed after review")
        tasks.append(task)
    return tasks


def _task_unlocked(path: Path) -> None:
    """活跃或未核对的中断锁都不能被终态字符串覆盖，更不能触发最终评分。"""
    lock = path.with_suffix(".lock")
    assert_no_links(lock)
    if lock.exists():
        raise ValueError("A preregistered task still has an execution lock; final evaluation is forbidden")


def _unchanged_tasks(tasks: list[dict]) -> None:
    for task in tasks:
        path = Path(task["task_path"])
        _task_unlocked(path)
        if _digest(_read(path)) != _digest(task):
            raise ValueError("A task changed while freezing candidates")


def _freeze(batch: dict, tasks: list[dict], directory: Path) -> dict:
    _unchanged_tasks(tasks)
    frozen_path = directory / "candidates.json"
    if "candidates_sha256" in batch:
        frozen = _read(frozen_path)
        if _digest(frozen_path.read_bytes()) != batch["candidates_sha256"]:
            raise ValueError("Frozen candidate manifest changed")
        for task, item in zip(tasks, frozen["tasks"], strict=True):
            if _digest(task) != item["task_sha256"]:
                raise ValueError("A task changed after candidate freeze")
            snapshot = _read(Path(item["task_snapshot"]))
            if _digest(snapshot) != item["task_sha256"]:
                raise ValueError("Frozen task snapshot changed")
            for candidate in (item["candidate"], item.get("rejected_candidate")):
                if not candidate:
                    continue
                patch = Path(candidate["patch_path"])
                assert_no_links(patch)
                if _digest(patch.read_bytes()) != candidate["sha256"]:
                    raise ValueError("Frozen candidate bytes changed")
        _unchanged_tasks(tasks)
        return frozen
    directory.mkdir(parents=True, exist_ok=False)
    entries = []
    for index, task in enumerate(tasks):
        task_snapshot = directory / f"task-{index:04d}.json"
        _save(task, task_snapshot)
        entry = {**{key: task[key] for key in IDENTITY}, "task_sha256": _digest(task),
                 "task_snapshot": str(task_snapshot), "status": task["status"], "candidate": None}
        candidate = task.get("candidate")
        if candidate:
            rejected = task["status"] == "candidate_rejected"
            patch = directory / (f"rejected-{index:04d}.patch" if rejected else f"candidate-{index:04d}.patch")
            contents = Path(candidate["patch_path"]).read_bytes()
            if _digest(contents) != candidate["sha256"]:
                raise ValueError("Candidate changed during freeze")
            patch.write_bytes(contents)
            entry["rejected_candidate" if rejected else "candidate"] = {"patch_path": str(patch), "sha256": candidate["sha256"]}
        entries.append(entry)
    _unchanged_tasks(tasks)
    frozen = {"batch_id": batch["batch_id"], "registration_sha256": batch["registration_sha256"],
              "frozen_at": datetime.now(UTC).isoformat(), "tasks": entries}
    _save(frozen, frozen_path)
    batch.update(status="candidates_frozen", candidates_path=str(frozen_path),
                 candidates_sha256=_digest(frozen_path.read_bytes()))
    return frozen


def _usage(task: dict) -> dict:
    inputs = outputs = 0
    duration = 0.0
    unknown = 0
    sources = []
    for attempt in task["attempts"]:
        path = Path(attempt["receipt"])
        receipt = _read(path)
        if receipt.get("case_fingerprint") != task["case_fingerprint"] or receipt.get("model") != task["protocol"]["generation"]["model"]:
            raise ValueError("Attempt receipt differs from the frozen task identity")
        usage = receipt.get("model_usage", {})
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        if type(incoming) is int and type(outgoing) is int and min(incoming, outgoing) >= 0:
            inputs += incoming
            outputs += outgoing
        else:
            unknown += 1
        duration += receipt.get("duration_seconds", 0.0)
        sources.append({"path": str(path), "sha256": _digest(path.read_bytes())})
    return {"calls": len(task["attempts"]), "reported_input_tokens": inputs,
            "reported_output_tokens": outputs, "unknown_usage_calls": unknown,
            "provider_duration_seconds": round(duration, 6), "receipt_sources": sources}


def _markdown(report: dict) -> str:
    def safe(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = ["# 冻结批次验收报告", "", f"阶段：{report['phase']}。",
             "校准批次不计入正式矩阵。成功数按任务计算，项目数与测试数不同；重复仅呈现观察波动，不声称统计显著。",
             "全部候选在揭示最终验收前统一冻结。结果不会反馈给求解器；无候选及预算/环境失败仍计入任务总数。", "",
             "| 案例 | 实验臂 | 重复 | 结果 | 失败分类 | 输入/输出 token | 模型耗时秒 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for item in report["results"]:
        usage = item["usage"]
        lines.append("| " + " | ".join(safe(value) for value in (
            item["case_id"], item["arm"], item["repetition"], item["result"], item["failure_category"],
            f"{usage['reported_input_tokens']}/{usage['reported_output_tokens']} (未知{usage['unknown_usage_calls']}次)",
            usage["provider_duration_seconds"],
        )) + " |")
    lines.extend(["", "## 按案例与实验臂汇总", "", "| 案例 | 实验臂 | 成功/登记任务 | 重复结果 |",
                  "| --- | --- | --- | --- |"])
    for group in report["groups"]:
        lines.append(f"| {safe(group['case_id'])} | {safe(group['arm'])} | {group['succeeded']}/{group['registered']} | {safe(group['repetitions'])} |")
    lines.extend(["", "## 费用与证据", "", "费用是已报告 token 单价估算及未知请求预留，不是供应商账单；共享账本只列一次。"])
    for budget in report["budgets"]:
        lines.append(f"- 账本：`{budget['path']}`；估算/预留：{budget['snapshot']['estimated_or_reserved_usd']} USD；单价来源：{budget['pricing_source']}。")
    for item in report["results"]:
        evidence = item.get("comparison")
        if evidence:
            lines.append(f"- {safe(item['case_id'])}/{safe(item['arm'])}/{item['repetition']}：`{evidence['path']}`，SHA-256 `{evidence['sha256']}`。")
    lines.extend(["", "## 复现", "", "从仓库根目录执行 `uv sync --locked`。以下命令只重新检查已冻结批次，不调用模型：", "", "```powershell",
                  f"uv run --locked upgrade-workbench batch-finalize \"{report['batch_path']}\"", "```", "",
                  "各次模型收据摘要、冻结候选、原始比较路径和完整协议见 JSON；报告不导出原始 provider 回复。", ""])
    return "\n".join(lines)


def finalize_batch(path: Path, *, comparator=run_comparison) -> dict:
    """预登记全体任务结束后统一验收；调用中断保留冻结输入和已完成比较。"""
    path = Path(path).absolute()
    batch = _read(path)
    if (Path(batch["batch_path"]) != path
        or _digest(batch["registration"]) != batch["registration_sha256"]
        or _digest(batch["registration"]["protocol"]) != batch["registration"]["protocol_sha256"]):
        raise ValueError("Batch location or preregistered protocol changed")
    tasks = _current_tasks(batch)
    directory = path.parent / (path.stem + "-artifacts")
    assert_no_links(directory)
    lock = path.with_suffix(".lock")
    assert_no_links(lock)
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(batch["batch_id"])
    try:
        frozen = _freeze(batch, tasks, directory)
        _save(batch, path)
        results = []
        execution = batch["registration"]["protocol"].get("execution", {})
        if set(execution) - {"base_image", "prepare_timeout", "test_timeout"}:
            raise ValueError("Final evaluator parameters cannot override candidate or check scope")
        for index, (task, entry) in enumerate(zip(tasks, frozen["tasks"], strict=True)):
            result_path = directory / f"result-{index:04d}.json"
            if result_path.exists():
                item = _read(result_path)
                if item["task_sha256"] != entry["task_sha256"] or any(item[key] != entry[key] for key in IDENTITY):
                    raise ValueError("Stored evaluation belongs to another frozen task")
                comparison = item.get("comparison")
                if comparison:
                    original = _read(Path(comparison["path"]))
                    if (_digest(Path(comparison["path"]).read_bytes()) != comparison["sha256"]
                        or original.get("check_group") != "all"
                        or original.get("case_fingerprint") != entry["case_fingerprint"]
                        or original["status"] != comparison["status"]
                        or (original["status"] == "candidate_verified"
                            and original.get("candidate", {}).get("supplied_sha256") != entry["candidate"]["sha256"])
                        or item["result"] != ("solved" if original["status"] == "candidate_verified" else "not_solved")):
                        raise ValueError("Stored comparison evidence changed")
                elif entry["candidate"] or item["result"] != "not_solved":
                    raise ValueError("Stored evaluation lacks its candidate evidence")
                for source in item["usage"]["receipt_sources"]:
                    receipt_path = Path(source["path"])
                    assert_no_links(receipt_path)
                    if _digest(receipt_path.read_bytes()) != source["sha256"]:
                        raise ValueError("Stored model usage evidence changed")
                results.append(item)
                continue
            item = {**{key: entry[key] for key in IDENTITY}, "task_sha256": entry["task_sha256"],
                    "task_status": task["status"], "result": "not_solved", "comparison": None,
                    "failure_category": task.get("stop_reason") or task["status"], "usage": _usage(task)}
            if entry["candidate"]:
                candidate = entry["candidate"]
                patch = Path(candidate["patch_path"])
                assert_no_links(patch)
                if _digest(patch.read_bytes()) != candidate["sha256"]:
                    raise ValueError("Frozen candidate bytes changed")
                comparison = comparator(Path(task["manifest_path"]), directory / "verification",
                    candidate_patch=patch if patch.stat().st_size else None,
                    candidate_origin="agent_candidate" if patch.stat().st_size else None,
                    **({"expected_candidate_sha256": candidate["sha256"]} if task.get("schema_version") == 2 and patch.stat().st_size else {}),
                    check_group="all", **execution)
                report_path = Path(comparison["report_path"])
                if _read(report_path) != comparison or comparison.get("check_group") != "all":
                    raise ValueError("Comparator must return its persisted all-check report")
                if comparison.get("case_fingerprint") != task["case_fingerprint"]:
                    raise ValueError("Comparator result belongs to another case")
                if comparison["status"] == "candidate_verified" and comparison.get("candidate", {}).get("supplied_sha256") != candidate["sha256"]:
                    raise ValueError("Successful comparator result belongs to another candidate")
                item["comparison"] = {"path": str(report_path), "sha256": _digest(report_path.read_bytes()), "status": comparison["status"]}
                item["result"] = "solved" if comparison["status"] == "candidate_verified" else "not_solved"
                item["failure_category"] = "none" if item["result"] == "solved" else comparison["status"]
            _save(item, result_path)
            results.append(item)
        if batch["status"] == "evaluated":
            report_path = Path(batch["report_path"])
            existing = _read(report_path)
            if _digest(report_path.read_bytes()) != batch["report_sha256"] or existing["results"] != results:
                raise ValueError("Published batch report changed")
            return existing
        grouped = defaultdict(list)
        for item in results:
            grouped[(item["case_id"], item["arm"])].append(item)
        groups = [{"case_id": case, "arm": arm, "registered": len(items),
                   "succeeded": sum(item["result"] == "solved" for item in items),
                   "repetitions": [{"repetition": item["repetition"], "result": item["result"]} for item in sorted(items, key=lambda x: x["repetition"])]}
                  for (case, arm), items in sorted(grouped.items())]
        budgets = []
        for budget_path in sorted({task["budget_path"] for task in tasks}):
            ledger = BudgetLedger(Path(budget_path))
            budgets.append({"path": budget_path, "snapshot": ledger.snapshot(),
                            "pricing_source": ledger.spec["pricing_source"], "pricing_checked_at": ledger.spec["pricing_checked_at"]})
        report = {"schema_version": 1, "status": "batch_evaluated", "batch_path": str(path),
                  "phase": batch["phase"], "formal_matrix": batch["formal_matrix"],
                  "registration_sha256": batch["registration_sha256"], "candidates_sha256": batch["candidates_sha256"],
                  "registered_tasks": len(tasks), "registered_cases": len({task["case_fingerprint"] for task in tasks}),
                  "projects": len({load_case(Path(task["manifest_path"])).manifest.source.repository for task in tasks}),
                  "succeeded_tasks": sum(item["result"] == "solved" for item in results),
                  "results": results, "groups": groups, "budgets": budgets,
                  "solver_feedback": False, "actual_billing": "not_claimed",
                  "report_path": str(directory / "report.json"), "markdown_path": str(directory / "report.md")}
        _save(report, Path(report["report_path"]))
        Path(report["markdown_path"]).write_text(_markdown(report), encoding="utf-8")
        batch.update(status="evaluated", final_evaluation_disclosed=True, report_path=report["report_path"],
                     report_sha256=_digest(Path(report["report_path"]).read_bytes()))
        _save(batch, path)
        return report
    finally:
        lock.unlink()
