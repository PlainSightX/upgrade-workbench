"""面向使用者的入口：检查案例、诊断环境、执行比较。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Sequence

from .batches import create_batch, finalize_batch
from .budget import BudgetLedger
from .cases import CaseValidationError, load_case
from .execution import DockerExecutor
from .generation import complete_request
from .planning import create_analysis, prepare_case_proposal
from .reporting import export_batch_report, export_task_report
from .retrieval import retrieve_evidence
from .tasks import (
    advance_task,
    build_historical_input_spec,
    create_operation,
    create_task,
    finalize_task,
    inspect_task,
)
from .workflow import run_comparison


def _task_summary(state: dict) -> dict:
    candidate = state.get("candidate") or {}
    next_action = "查看停止原因和已有收据；不要删除attempt标记重试"
    if state.get("final_evaluation") == "completed":
        next_action = "task-export导出已完成的独立验收与补丁；查看final_result，验收完成不代表候选通过"
    elif state["status"] == "pending_review":
        next_action = "task-diff查看完整补丁；task-review绑定candidate_revision及candidate_sha256，批准后只执行公开检查；再continue-task"
    elif state["status"] == "pending_diagnostic_review":
        next_action = "task-diagnostic查看冻结探针/检查与源码身份；task-diagnostic-review绑定request_id，接受后只执行公开诊断"
    elif state["status"] == "pending_dependency_query":
        next_action = "continue-task执行已登记锁环境中的只读依赖查询；该次推进不调用模型，完成后再continue-task"
    elif state["status"] == "ready":
        next_action = "continue-task；若尚未运行官方转换，需--reviewed-tool确认固定静态入口"
    elif state["status"] in {"submitted", "no_change_claimed"}:
        next_action = "普通任务用task-finalize独立验收、task-export导出；实验任务用batch-finalize"
    return {**{key: state[key] for key in ("task_id", "task_path", "case_id", "status")},
            "calls": len(state["attempts"]), "stop_reason": state.get("stop_reason"),
            "candidate_revision": candidate.get("revision"), "candidate_sha256": candidate.get("sha256"),
            "candidate_patch": candidate.get("patch_path"), "reviewed": candidate.get("reviewed", False),
            "seed_status": (state.get("seed") or {}).get("status"), "feedback": state.get("feedback"),
            "final_evaluation": state.get("final_evaluation"), "final_result": state.get("final_result"),
            "budget": state.get("budget"), "next_action": next_action,
            "transport_diagnostic": state.get("last_transport_diagnostic"),
            "finish": state.get("finish"), "verification_subject": state.get("verification_subject"),
            "pending_diagnostic": state.get("pending_diagnostic"),
            "diagnostic_runs": len(state.get("diagnostic_runs", [])), "observations": len(state.get("observations", [])),
            "historical_inputs": len(state.get("historical_inputs", [])),
            "investigator_sessions": len(state.get("investigator_sessions", []))}


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="upgrade-workbench")
    parser.add_argument("--work-root", type=Path, default=Path(".local"))
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("doctor", help="检查 Docker，不启动服务、不读取密钥")
    resources = actions.add_parser("resources", help="本项目镜像归属、引用与清理预览；不删除")
    resources.add_argument("--output", type=Path)
    cleanup = actions.add_parser("resources-cleanup", help="核对预览、原收据及当前引用后精确删除未使用缓存")
    cleanup.add_argument("preview", type=Path)
    cleanup.add_argument("--expected-sha256", required=True)
    batch_create = actions.add_parser("batch-create", help="在调用前登记完整比较矩阵")
    batch_create.add_argument("tasks", nargs="+", type=Path)
    batch_create.add_argument("--protocol", required=True, type=Path)
    batch_create.add_argument("--output", required=True, type=Path)
    batch_finalize = actions.add_parser("batch-finalize", help="全部候选提交后统一冻结、评分与报告")
    batch_finalize.add_argument("batch", type=Path)
    report_export = actions.add_parser("report-export", help="导出已验收批次及审阅补丁，不调用模型或发布")
    report_export.add_argument("batch", type=Path)
    report_export.add_argument("--output", required=True, type=Path)
    report_export.add_argument("--without-patches", action="store_true")
    budget_init = actions.add_parser("budget-init", help="按已审阅的价格和额度初始化持久费用账本")
    budget_init.add_argument("ledger", type=Path)
    budget_init.add_argument("specification", type=Path)
    budget_status = actions.add_parser("budget-status", help="显示累计估算/预留费用，不读取密钥")
    budget_status.add_argument("ledger", type=Path)
    budget_amend = actions.add_parser("budget-amend-holdout", help="经配置摘要确认后，仅提高留出集保留金")
    budget_amend.add_argument("ledger", type=Path)
    budget_amend.add_argument("--reserve-usd", required=True)
    budget_amend.add_argument("--expected-sha256", required=True)
    budget_amend.add_argument("--reason", required=True)
    policy = actions.add_parser("budget-policy", help="保留累计账本，改变可选金额限制")
    policy.add_argument("ledger", type=Path)
    policy.add_argument("--mode", required=True, choices=("user_managed", "capped"))
    policy.add_argument("--limit-usd")
    policy.add_argument("--expected-sha256", required=True)
    policy.add_argument("--reason", required=True)
    normal = actions.add_parser("task-create", help="创建日常升级任务，无需实验臂；不调用模型")
    normal.add_argument("case", type=Path)
    normal.add_argument("--config", required=True, type=Path)
    normal.add_argument("--budget", required=True, type=Path)
    normal.add_argument("--seed", choices=("official", "none"), default="official")
    normal.add_argument("--retrieval-mode", choices=("bm25", "dense", "hybrid", "rerank"))
    normal.add_argument("--models-root", type=Path)
    normal.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    historical = actions.add_parser(
        "task-historical-spec",
        help="冻结终态任务某次调用后的下一请求上下文；不创建任务、不调用模型",
    )
    historical.add_argument("task", type=Path)
    historical.add_argument("--cutoff-attempt", required=True, type=int)
    knowledge = actions.add_parser("task-knowledge-export", help="导出来源已核验的公开知识，供同仓库新任务显式导入")
    knowledge.add_argument("task", type=Path)
    knowledge.add_argument("--output", required=True, type=Path)
    diagnostic_show = actions.add_parser("task-diagnostic", help="查看待审诊断输入，不运行目标代码")
    diagnostic_show.add_argument("task", type=Path)
    diagnostic_review = actions.add_parser("task-diagnostic-review", help="审阅并执行确定的公开检查或探针")
    diagnostic_review.add_argument("task", type=Path)
    diagnostic_review.add_argument("--request-id", required=True)
    diagnostic_review.add_argument("--decision", choices=("accept", "reject"), required=True)
    diagnostic_review.add_argument("--reviewer", required=True)
    diagnostic_review.add_argument("--review-note", required=True)
    diagnostic_retry = actions.add_parser("task-diagnostic-retry", help="清理不完整诊断的本次资源并重新进入审阅，不自动执行")
    diagnostic_retry.add_argument("task", type=Path)
    diagnostic_retry.add_argument("--request-id", required=True)
    for name in ("task-status", "task-diff", "task-review", "task-finalize", "task-export", "task-analyze"):
        command = actions.add_parser(name)
        command.add_argument("task", type=Path)
        if name == "task-review":
            command.add_argument("--review-sha256", required=True)
            command.add_argument("--review-revision", required=True)
            command.add_argument("--reviewer", required=True)
            command.add_argument("--review-note", required=True)
            command.add_argument("--reject-reason", choices=("candidate_contract_rejected",))
        if name == "task-export":
            command.add_argument("--output", required=True, type=Path)
    task = actions.add_parser("run-task", help="创建并推进有界任务，遇候选审阅暂停，不运行最终评分")
    task.add_argument("case", type=Path)
    task.add_argument("--protocol", required=True, type=Path)
    task.add_argument("--budget", required=True, type=Path)
    task.add_argument("--arm", choices=("generic", "no_ast", "no_evidence", "full", "no_feedback"), required=True)
    task.add_argument("--phase", choices=("calibration", "development", "holdout"), required=True)
    task.add_argument("--repetition", type=int, default=1)
    task.add_argument("--prepare-only", action="store_true")
    task.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    continuation = actions.add_parser("continue-task", help="推进既有任务；审阅绑定确切补丁哈希")
    continuation.add_argument("task", type=Path)
    continuation.add_argument("--review-sha256")
    continuation.add_argument("--review-revision")
    continuation.add_argument("--reviewed-tool", action="store_true")
    continuation.add_argument("--reviewer")
    continuation.add_argument("--review-note")
    continuation.add_argument("--reject-reason", choices=("candidate_contract_rejected",))
    continuation.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    inspect = actions.add_parser("inspect", help="只核验案例输入，不执行目标代码")
    inspect.add_argument("case", type=Path)
    analyze = actions.add_parser("analyze", help="只读定位潜在迁移影响，保存版本依据和未知项")
    analyze.add_argument("case", type=Path)
    retrieve = actions.add_parser("retrieve", help="在锁定版本原文中检索，返回精确行号和父子块；不调用模型")
    retrieve.add_argument("case", type=Path)
    retrieve.add_argument("--query", required=True)
    retrieve.add_argument("--top-k", type=int, default=5)
    retrieve.add_argument("--mode", choices=("bm25", "dense", "hybrid", "rerank"), default="bm25")
    retrieve.add_argument("--models-root", type=Path)
    retrieve.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    propose = actions.add_parser("propose", help="准备公开源码的有界请求，默认不调用模型")
    propose.add_argument("case", type=Path)
    propose.add_argument("--model", required=True)
    propose.add_argument("--endpoint", required=True, help="完整 HTTPS chat/completions 地址")
    propose.add_argument("--max-output-tokens", type=int, default=8192)
    propose.add_argument("--timeout", type=int, default=180)
    propose.add_argument("--thinking-mode", choices=("enabled", "disabled"))
    propose.add_argument("--output-format", choices=("unified_diff", "exact_edits"))
    propose.add_argument("--public-source-ack", action="store_true")
    complete = actions.add_parser("complete-proposal", help="对已冻结的请求调用一次模型，不执行候选")
    complete.add_argument("receipt", type=Path, help="propose 返回的 proposal.json")
    complete.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    verify = actions.add_parser("verify", help="在 Docker 内对照旧、新、候选代码")
    verify.add_argument("case", type=Path)
    verify.add_argument("--candidate", type=Path)
    verify.add_argument(
        "--candidate-origin", choices=("calibration_reference", "user_candidate", "agent_candidate", "official_tool")
    )
    verify.add_argument("--prepare-timeout", type=int, default=600)
    verify.add_argument("--test-timeout", type=int, default=60)
    verify.add_argument("--base-image", help="显式选择 Python 3.12 Linux 基础镜像；记录实际摘要")
    verify.add_argument("--check-group", choices=("all", "feedback", "acceptance"), default="all")
    args = parser.parse_args(argv)
    try:
        if args.action == "task-knowledge-export":
            from .knowledge_transfer import export_bundle

            report = export_bundle(args.task, args.output)
            code = 0
        elif args.action == "task-historical-spec":
            report = build_historical_input_spec(args.task, args.cutoff_attempt)
            code = 0
        elif args.action == "retrieve":
            manifest = args.case / "manifest.json" if args.case.is_dir() else args.case
            semantic_config = None
            if args.mode != "bm25":
                if args.models_root is None:
                    raise ValueError("Semantic retrieval needs --models-root with verified local assets")
                semantic_config = {"models_root": str(args.models_root.absolute()),
                                   "device": args.device, "mode": args.mode}
            report = retrieve_evidence(load_case(manifest), args.query, top_k=args.top_k,
                                       semantic_config=semantic_config)
            code = 0
        elif args.action == "task-analyze":
            state = inspect_task(args.task)
            if state.get("schema_version") != 2:
                raise ValueError("task-analyze requires a current candidate task")
            report = create_analysis(Path(state["manifest_path"]), args.work_root,
                                     candidate_reference=state["current_candidate"])
            code = 0
        elif args.action == "task-create":
            config = json.loads(args.config.read_text(encoding="utf-8"))
            allowed = {
                "generation", "execution", "max_calls", "protocol_revision", "source_policy",
                "contract_audit_policy", "context_assistance", "navigation_assistance",
                "investigator_policy", "investigator_max_calls", "solver_reserved_calls",
                "investigator_context_policy",
                "historical_input", "decision_objective", "decision_point_id",
                "workflow_profile", "project_context_policy", "finish_policy", "investigation_policy", "semantic_risk_policy",
                "knowledge_import",
            }
            if not {"generation", "execution", "max_calls"} <= set(config) or set(config) - allowed:
                raise ValueError("Task config contains missing or unsupported operation fields")
            if args.retrieval_mode == "bm25":
                config["generation"].pop("semantic_config", None)
            elif args.retrieval_mode:
                if args.models_root is None:
                    raise ValueError("Semantic task retrieval needs --models-root")
                config["generation"]["semantic_config"] = {
                    "models_root": str(args.models_root.absolute()), "device": args.device,
                    "mode": args.retrieval_mode,
                }
            manifest = args.case / "manifest.json" if args.case.is_dir() else args.case
            report = _task_summary(create_operation(manifest, args.work_root, budget_path=args.budget,
                                                    seed_strategy=args.seed, **config))
            code = 0
        elif args.action in {"task-diagnostic", "task-diagnostic-review", "task-diagnostic-retry"}:
            from .diagnostics import read

            state = inspect_task(args.task)
            if args.action == "task-diagnostic-retry":
                state = advance_task(args.task, diagnostic_request_id=args.request_id, diagnostic_retry=True)
                report = _task_summary(state)
            elif args.action == "task-diagnostic-review":
                state = advance_task(args.task, diagnostic_request_id=args.request_id,
                    diagnostic_decision=args.decision, reviewer=args.reviewer, review_note=args.review_note,
                    review_only=True)
                report = _task_summary(state)
            else:
                pending = state.get("pending_diagnostic")
                if not pending:
                    raise ValueError("No pending diagnostic")
                root = Path(state["task_path"]).parent
                request = read(pending["request"], root)
                probe = next((p for p in state["probes"] if p["id"] == request["probe_id"]), None)
                report = {"request_id": pending["request"]["id"], "request": request,
                          "probe": read(probe, root) if probe else None}
            code = 0
        elif args.action in {"task-status", "task-diff", "task-review", "task-finalize", "task-export"}:
            if args.action == "task-review":
                state = advance_task(args.task, reviewed_sha256=args.review_sha256,
                                     reviewed_revision=args.review_revision, reviewer=args.reviewer,
                                     review_note=args.review_note, reject_reason=args.reject_reason, review_only=True)
            elif args.action == "task-finalize":
                state = finalize_task(args.task)
            else:
                state = inspect_task(args.task)
            report = _task_summary(state)
            if args.action == "task-diff":
                candidate = state.get("candidate")
                print(Path(candidate["patch_path"]).read_text(encoding="utf-8") if candidate else "尚无候选变更")
                return 0
            if args.action == "task-export":
                report = export_task_report(args.task, args.output)
            if args.action == "task-finalize":
                # submitted只表示提交；命令成功必须绑定已完成的独立验收结论。
                accepted = {"candidate_verified", "no_source_change_needed_for_registered_checks"}
                code = 0 if state.get("final_evaluation") == "completed" and (
                    state.get("final_result") or {}
                ).get("status") in accepted else 2
            elif args.action in {"task-status", "task-export"}:
                code = 0
            else:
                code = 0 if state["status"] in {"ready", "pending_review", "pending_diagnostic_review", "pending_dependency_query", "submitted", "no_change_claimed"} else 2
        elif args.action == "budget-policy":
            ledger = BudgetLedger(args.ledger)
            report = {"amendment": ledger.amend_policy(mode=args.mode, limit_usd=args.limit_usd,
                       expected_configuration_sha256=args.expected_sha256, reason=args.reason),
                      "budget": ledger.snapshot()}
            code = 0
        elif args.action == "batch-create":
            report = create_batch(
                [json.loads(path.read_text(encoding="utf-8")) for path in args.tasks],
                json.loads(args.protocol.read_text(encoding="utf-8")), args.output,
            )
            code = 0
        elif args.action == "batch-finalize":
            report = finalize_batch(args.batch)
            code = 0 if report["status"] == "batch_evaluated" else 2
        elif args.action == "report-export":
            report = export_batch_report(args.batch, args.output, include_reviewed_patches=not args.without_patches)
            code = 0
        elif args.action in {"budget-init", "budget-status"}:
            specification = (
                json.loads(args.specification.read_text(encoding="utf-8"))
                if args.action == "budget-init" else None
            )
            report = BudgetLedger(args.ledger, specification).snapshot()
            code = 0
        elif args.action == "budget-amend-holdout":
            ledger = BudgetLedger(args.ledger)
            amendment = ledger.amend_holdout_reserve(
                args.reserve_usd, expected_configuration_sha256=args.expected_sha256,
                reason=args.reason,
            )
            report = {"status": "holdout_reserve_amended", "amendment": amendment,
                      "budget": ledger.snapshot()}
            code = 0
        elif args.action in {"run-task", "continue-task"}:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
                raise ValueError("Invalid API key environment variable name")
            if args.action == "run-task":
                manifest = args.case / "manifest.json" if args.case.is_dir() else args.case
                state = create_task(
                    manifest, args.work_root, json.loads(args.protocol.read_text(encoding="utf-8")),
                    arm=args.arm, phase=args.phase, budget_path=args.budget, repetition=args.repetition,
                )
                if not args.prepare_only:
                    state = advance_task(Path(state["task_path"]), api_key=os.environ.get(args.api_key_env, ""))
            else:
                state = advance_task(
                    args.task, api_key=os.environ.get(args.api_key_env, ""),
                    reviewed_sha256=args.review_sha256, reviewer=args.reviewer, review_note=args.review_note,
                    reject_reason=args.reject_reason, reviewed_revision=args.review_revision,
                    reviewed_tool=args.reviewed_tool,
                )
            candidate = state.get("candidate") or {}
            report = {key: state[key] for key in ("task_id", "task_path", "case_id", "arm", "phase", "status")}
            report.update(
                calls=len(state["attempts"]), stop_reason=state.get("stop_reason"),
                candidate_patch=candidate.get("patch_path"), candidate_sha256=candidate.get("sha256"),
                budget=state.get("budget"),
            )
            if state.get("schema_version") in {2, 3, 4, 5}:
                report = _task_summary(state)
            code = 0 if state["status"] in {"ready", "pending_review", "pending_diagnostic_review", "pending_dependency_query", "submitted", "no_change_claimed"} else 2
        elif args.action == "resources":
            from .execution.cache import atomic_json
            from .execution.resources import default_inventory, inventory
            report = inventory(*default_inventory())
            if args.output:
                atomic_json(args.output, report)
            code = 0
        elif args.action == "resources-cleanup":
            from .execution.resources import apply_preview
            report = apply_preview(args.preview, args.expected_sha256)
            code = 0
        elif args.action == "doctor":
            report = {
                "docker": DockerExecutor(args.work_root / "executor").probe(),
                "git_available": shutil.which("git") is not None,
            }
            code = 0 if report["docker"]["ready"] and report["git_available"] else 2
        elif args.action == "complete-proposal":
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env):
                raise ValueError("API key environment variable name is invalid")
            if args.receipt.stat().st_size > 192_000:
                raise ValueError("Prepared receipt is too large")
            prepared = json.loads(args.receipt.read_text(encoding="utf-8"))
            report = complete_request(prepared, api_key=os.environ.get(args.api_key_env, ""))
            code = 0 if report["status"] == "pending_review" else 2
        else:
            manifest = args.case / "manifest.json" if args.case.is_dir() else args.case
            if args.action == "analyze":
                report = create_analysis(manifest, args.work_root)
                code = 0
            elif args.action == "propose":
                report = prepare_case_proposal(
                    manifest, args.work_root,
                    model=args.model, endpoint=args.endpoint,
                    max_output_tokens=args.max_output_tokens, timeout_seconds=args.timeout,
                    public_source_ack=args.public_source_ack, thinking_mode=args.thinking_mode,
                    **({"output_format": args.output_format} if args.output_format else {}),
                )
                code = 0
            elif args.action == "inspect":
                case = load_case(manifest)
                report = {
                    "case_id": case.manifest.case_id,
                    "title": case.manifest.title,
                    "fingerprint": case.fingerprint,
                    "source": case.manifest.source.model_dump(),
                    "allowed_changes": case.manifest.allowed_changes,
                    "verified_files": len(case.manifest.file_hashes),
                    "target_code_executed": False,
                }
                code = 0
            else:
                report = run_comparison(
                    manifest,
                    args.work_root,
                    candidate_patch=args.candidate,
                    candidate_origin=args.candidate_origin,
                    prepare_timeout=args.prepare_timeout,
                    test_timeout=args.test_timeout,
                    **({"check_group": args.check_group} if args.check_group != "all" else {}),
                    **({"base_image": args.base_image} if args.base_image is not None else {}),
                )
                code = (
                    0
                    if report["status"]
                    in {
                        "calibration_passed",
                        "candidate_verified",
                        "comparison_only",
                        "no_regression_observed",
                    }
                    else 2
                )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return code
    except (CaseValidationError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "invalid_request", "reason": str(exc)}, ensure_ascii=False, indent=2
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
