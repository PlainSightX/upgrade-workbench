"""预登记实验矩阵并推进到人工审阅边界；不自动批准新补丁，也不揭示最终检查。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from upgrade_workbench.batches import IDENTITY, create_batch
from upgrade_workbench.tasks import TERMINAL, advance_task, create_task, implementation_identity


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def registered_batch(path: Path) -> dict:
    batch = read(path)
    registration = batch["registration"]
    if (Path(batch["batch_path"]).absolute() != path.absolute()
        or batch["registration_sha256"] != digest(registration)
        or registration["protocol_sha256"] != digest(registration["protocol"])):
        raise ValueError("Batch location or preregistered protocol changed")
    if any(entry["protocol_sha256"] != registration["protocol_sha256"]
           for entry in registration["tasks"]):
        raise ValueError("Every task must use the preregistered protocol")
    return batch


def compact(task: dict) -> dict:
    candidate = task.get("candidate") or {}
    return {
        "task_id": task["task_id"], "case_id": task["case_id"], "arm": task["arm"],
        "repetition": task["repetition"], "status": task["status"],
        "calls": len(task["attempts"]), "stop_reason": task.get("stop_reason"),
        "review_key": (f"{task['case_fingerprint']}:{candidate.get('revision', '')}:{candidate.get('sha256', '')}" if task.get("schema_version") == 2
                       else f"{task['case_fingerprint']}:{candidate.get('sha256', '')}"),
        "patch_path": candidate.get("patch_path"),
    }


def advance_registered(entry: dict, reviews: dict, *, reviewed_tool: bool = False) -> dict:
    path = Path(entry["task_path"])
    task = read(path)
    # 此预登记适配器只维护历史 P2/P3；现代任务使用普通 CLI 或服务，不猜测审阅协议。
    if task.get("schema_version", 1) not in {1, 2}:
        raise ValueError("Legacy registered runner supports task schema 1/2 only")
    for key in IDENTITY:
        if task[key] != entry[key]:
            raise ValueError("Registered task identity changed")
    while task["status"] not in TERMINAL:
        if task["status"] == "pending_diagnostic_review":
            break
        kwargs = {}
        if task["status"] == "pending_review":
            candidate = task["candidate"]
            key = (f"{task['case_fingerprint']}:{candidate['revision']}:{candidate['sha256']}" if task.get("schema_version") == 2
                   else f"{task['case_fingerprint']}:{candidate['sha256']}")
            decision = reviews.get(key)
            if decision is None:
                break
            if decision.get("decision") not in {"approved", "rejected"}:
                raise ValueError("A hash-bound explicit reviewer decision is required")
            kwargs = {
                "reviewed_sha256": candidate["sha256"], "reviewer": decision["reviewer"],
                "review_note": decision["note"],
            }
            if task.get("schema_version") == 2:
                kwargs["reviewed_revision"] = candidate["revision"]
            if decision["decision"] == "rejected":
                kwargs["reject_reason"] = "candidate_contract_rejected"
        if task.get("schema_version") == 2:
            kwargs["reviewed_tool"] = reviewed_tool
        task = advance_task(path, api_key=os.environ.get("DEEPSEEK_API_KEY", ""), **kwargs)
        if task.get("stop_reason") in {"static_tool_review_required", "provider_key_required_before_next_call"}:
            break
        # 未知结果或执行失败由任务协议保留，调度器不重放或强行改回ready。
    return compact(task)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze-protocol")
    freeze.add_argument("--template", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--id", required=True)
    freeze.add_argument("--status", choices=["development_calibration", "frozen"], required=True)
    freeze.add_argument("--base-image", required=True)
    freeze.add_argument("--thinking-mode", choices=["enabled", "disabled"])
    freeze.add_argument("--max-output-tokens", type=int)
    freeze.add_argument("--candidate-actions", action="store_true")
    register = sub.add_parser("register")
    register.add_argument("--protocol", type=Path, required=True)
    register.add_argument("--batch", type=Path, required=True)
    register.add_argument("--budget", type=Path, required=True)
    register.add_argument("--work-root", type=Path, default=Path(".local"))
    register.add_argument("--phase", choices=["calibration", "development", "holdout"], required=True)
    register.add_argument("--cases", type=Path, nargs="+", required=True)
    register.add_argument("--arms", nargs="+", required=True)
    register.add_argument("--repeats", type=int, required=True)
    for name in ("status", "advance"):
        command = sub.add_parser(name)
        command.add_argument("--batch", type=Path, required=True)
        if name == "advance":
            command.add_argument("--reviews", type=Path, nargs="+")
            command.add_argument("--workers", type=int, choices=[1, 2, 3], default=2)
            command.add_argument("--reviewed-tool", action="store_true")
    args = parser.parse_args()
    if args.command == "freeze-protocol":
        protocol = read(args.template)
        protocol.update(protocol_id=args.id, protocol_revision=3 if args.candidate_actions else 2, status=args.status,
                        implementation=implementation_identity())
        protocol["execution"]["base_image"] = args.base_image
        protocol["execution"]["test_timeout"] = 120
        if args.thinking_mode is not None:
            protocol["generation"]["thinking_mode"] = args.thinking_mode
        if args.max_output_tokens is not None:
            protocol["generation"]["max_output_tokens"] = args.max_output_tokens
        write_new(args.output, protocol)
        print(json.dumps({"protocol": str(args.output), "status": args.status}))
    elif args.command == "register":
        if args.batch.exists() or args.repeats < 1 or len(set(args.arms)) != len(args.arms):
            raise ValueError("Registration must be new with positive repeats and unique arms")
        protocol = read(args.protocol)
        if protocol.get("protocol_revision", 2) not in {2, 3}:
            raise ValueError("Legacy registered runner supports protocol revision 2/3 only")
        tasks = []
        # 交错项目和实验臂，避免先把一个策略全部跑完再运行其对照。
        for repeat in range(1, args.repeats + 1):
            for case in args.cases:
                for arm in args.arms:
                    manifest = case / "manifest.json" if case.is_dir() else case
                    tasks.append(create_task(manifest, args.work_root, protocol, arm=arm,
                                             phase=args.phase, budget_path=args.budget,
                                             repetition=repeat))
        batch = create_batch(tasks, protocol, args.batch)
        print(json.dumps({"batch": batch["batch_path"], "tasks": len(tasks), "phase": args.phase}))
    else:
        batch = registered_batch(args.batch)
        entries = batch["registration"]["tasks"]
        if args.command == "status":
            rows = [compact(read(Path(entry["task_path"]))) for entry in entries]
        else:
            if batch["status"] != "registered":
                raise ValueError("A frozen or evaluated batch cannot generate more candidates")
            reviews = {}
            for review_path in args.reviews or []:
                for key, decision in read(review_path).items():
                    if key in reviews and reviews[key] != decision:
                        raise ValueError("Conflicting review decisions for the same candidate")
                    reviews[key] = decision
            rows = []
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(advance_registered, entry, reviews, **({"reviewed_tool": True} if args.reviewed_tool else {})): entry for entry in entries}
                for future in as_completed(futures):
                    try:
                        row = future.result()
                    except Exception as exc:
                        # 不输出供应商原文、请求或密钥，只公开工程错误类型。
                        row = {"task_id": futures[future]["task_id"], "status": "driver_error",
                               "error_type": type(exc).__name__}
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
        print(json.dumps({"counts": dict(Counter(row["status"] for row in rows)), "tasks": rows},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
