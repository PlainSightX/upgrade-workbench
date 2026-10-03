"""在受限模型动作、候选审阅和公开运行反馈之间推进一次迁移任务。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .budget import BudgetExceeded, BudgetLedger
from .candidates import import_patch, load_candidate, publish_candidate
from .cases import load_case
from .cases.manifest import assert_no_links
from .codemod import run_official_tool
from .evaluation import load_evaluation
from .generation import complete_request
from .generation.actions import execute_source_action
from .generation.request import PROTOCOL_ERROR_CODES, _instructions
from .planning import prepare_case_proposal
from .workflow import _fully_passed, run_comparison

ARMS = {"generic", "no_ast", "no_evidence", "full", "no_feedback", "codemod_repair", "direct_repair"}
TERMINAL = {"submitted", "no_candidate", "budget_exhausted", "execution_incomplete",
            "generation_failed", "outcome_unknown", "interrupted", "candidate_rejected", "seed_failed",
            "no_change_claimed", "unresolved"}
_COMPLETED_HISTORICAL_ATTEMPT_STATUSES = {
    "action_ready",
    "agent_finished",
    "audit_ready",
    "candidate_rejected",
    "execution_error",
    "investigation_ready",
    "pending_review",
    "provider_response_rejected",
}


def implementation_identity(protocol_revision=3) -> dict:
    """冻结实际工具实现、依赖锁和提示文本，不用一个可变协议名称代表同一实验。"""
    package = Path(__file__).resolve().parent
    root = package.parents[1]
    hashes = {}
    for path in sorted(package.rglob("*.py")):
        assert_no_links(path)
        hashes[path.relative_to(package).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    # wheel 的冻结构建输入随包分发；不能借用当前工作目录中的其他项目锁。
    lock = root / "uv.lock" if package.parent.name == "src" else package / "_resources/uv.lock"
    assert_no_links(lock)
    identity = {
        "tool_files": hashes,
        "uv_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(_instructions("agent_actions").encode("utf-8")).hexdigest(),
    }
    if protocol_revision in {4, 5, 6}:
        if protocol_revision == 4:
            from .generation.protocol_v4 import DIAGNOSTIC_POLICY
            output_format = "diagnostic_actions"
            task_schema = 3
        elif protocol_revision == 5:
            from .generation.protocol_v5 import DIAGNOSTIC_POLICY
            output_format = "contract_actions"
            task_schema = 4
        else:
            from .generation.protocol_v6 import DIAGNOSTIC_POLICY
            output_format = "protocol_v6_actions"
            task_schema = 5
        from .generation.source_context import POLICY

        identity.update(protocol_revision=protocol_revision, task_schema=task_schema, source_policy=POLICY,
                        diagnostic_policy=DIAGNOSTIC_POLICY,
                        prompt_sha256=hashlib.sha256(_instructions(output_format).encode()).hexdigest())
        if protocol_revision == 6:
            identity["workflow_prompt_sha256"] = {
                profile: hashlib.sha256(_instructions(output_format, profile).encode()).hexdigest()
                for profile in ("workbench", "simple_tools")
            }
    return identity


def _require_current_protocol(protocol: dict) -> None:
    from .generation.semantic_risk import validate_policy as risk_policy

    if risk_policy(protocol.get("semantic_risk_policy")) is not None and (
        protocol.get("protocol_revision") not in {4, 5, 6} or protocol.get("status") != "operation"
    ):
        raise ValueError("Semantic risk policy requires a diagnostic operation")
    from .finish_evidence import validate_policy
    from .investigation_policy import policy as investigation_policy

    investigation = investigation_policy(protocol.get("investigation_policy"))
    if protocol.get("knowledge_import") is not None:
        from .knowledge_transfer import MAINTAINED_VERSION, VERSION

        imported = protocol["knowledge_import"]
        if (not isinstance(imported, dict) or set(imported) != {"version", "bundle_sha256"}
                or imported["version"] not in {VERSION, MAINTAINED_VERSION}
                or not isinstance(imported["bundle_sha256"], str)
                or len(imported["bundle_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in imported["bundle_sha256"])
                or protocol.get("status") != "operation" or protocol.get("protocol_revision") != 6
                or (protocol.get("project_context_policy") or {}).get("version") not in {"project-context-v4", "project-context-v5", "project-context-v6", "project-context-v7", "project-context-v8", "project-context-v9", "project-context-v10"}):
            raise ValueError("Knowledge import requires a frozen bundle and P6 adaptive workbench")
    if investigation is not None:
        if protocol.get("protocol_revision") not in {5, 6} or protocol.get("status") != "operation":
            raise ValueError("Investigation policy requires a P5/P6 ordinary operation")
        if protocol.get("investigation_policy") != investigation:
            raise ValueError("Investigation policy must freeze its complete configuration")
        if protocol.get("diagnostic_policy") != investigation["diagnostic_limits"]:
            raise ValueError("Diagnostic limits differ from frozen investigation policy")

    finish_policy = validate_policy(protocol.get("finish_policy"))
    if investigation is not None and finish_policy != "probe-lineage-v1":
        raise ValueError("Investigation policy requires lineage-aware finish evidence")
    if finish_policy is not None and protocol.get("protocol_revision") not in {5, 6}:
        raise ValueError("Finish policy requires protocol 5 or 6")
    if type(protocol.get("protocol_revision")) is not int or protocol["protocol_revision"] not in {2, 3, 4, 5, 6}:
        raise ValueError(
            "Tasks require protocol revision 2, 3, diagnostic protocol 4, contract protocol 5, "
            "or fact-aware protocol 6"
        )
    expected = (implementation_identity(protocol["protocol_revision"])
                if protocol["protocol_revision"] in {4, 5, 6} else implementation_identity())
    if protocol.get("implementation") != expected:
        raise ValueError("Frozen implementation or prompt changed; create a new protocol revision")
    if protocol.get("context_assistance", "full") not in {"full", "source_only", "version_only"}:
        raise ValueError("Context assistance must be full, source_only or version_only")
    if protocol.get("navigation_assistance", "baseline") not in {"baseline", "failure_guided"}:
        raise ValueError("Unknown navigation assistance")
    retention = protocol.get("source_read_retention")
    if retention is not None and (
        retention != "current_revision" or protocol["protocol_revision"] != 6
        or protocol.get("workflow_profile") != "simple_tools"
    ):
        raise ValueError("Source read retention requires current_revision and P6 simple_tools")
    if protocol.get("project_context_policy") is not None:
        from .generation.project_context import policy as knowledge_policy

        knowledge_policy(protocol["project_context_policy"])
        if (protocol["project_context_policy"]["version"] in {"project-context-v4", "project-context-v5", "project-context-v6", "project-context-v7", "project-context-v8", "project-context-v9", "project-context-v10"}
                and protocol["protocol_revision"] != 6):
            raise ValueError("Adaptive project context requires P6 Solver workflow")
        if protocol["protocol_revision"] not in {4, 6} or protocol.get("workflow_profile", "workbench") != "workbench":
            raise ValueError("Project context requires P4/P6 workbench")
    audit_policy = protocol.get("contract_audit_policy")
    if audit_policy in {"delivery-audit-v1", "delivery-audit-v2"} and (
        protocol["protocol_revision"] != 6 or protocol.get("workflow_profile", "workbench") != "workbench"
    ):
        raise ValueError("Delivery audit requires P6 workbench")
    if protocol.get("repository_preparation") is not None:
        from .generation.roles import preparation_policy

        config = preparation_policy(protocol["repository_preparation"])
        if (protocol["protocol_revision"] != 4 or protocol.get("project_context_policy") is None
                or config["max_calls"] + config["solver_reserved_calls"] > protocol["max_calls"]):
            raise ValueError("Repository preparation requires P4 knowledge and reserved Solver calls")
    if protocol["protocol_revision"] in {5, 6}:
        if audit_policy not in {"bounded", "disabled", "delivery-audit-v1", "delivery-audit-v2"}:
            raise ValueError("A contract audit policy must be frozen as bounded or disabled")
    elif audit_policy is not None:
        raise ValueError("Contract audit policy is only available in protocols 5 and 6")
    investigator_policy = protocol.get("investigator_policy")
    from .generation.investigator_context import policy as investigator_context_policy

    investigator_context = investigator_context_policy(protocol.get("investigator_context_policy"))
    if investigator_context is not None and (
        protocol.get("protocol_revision") != 6 or protocol.get("status") != "operation"
        or investigator_policy != "required_once"
        or protocol.get("workflow_profile", "workbench") != "workbench"
        or (protocol.get("project_context_policy") or {}).get("version") != "project-context-v10"
        or protocol.get("decision_objective") is not None
    ):
        raise ValueError("Source-first Investigator requires an ordinary P6/v10 workbench investigation")
    from .generation.protocol_v6 import validate_workflow_profile

    workflow_profile = validate_workflow_profile(protocol.get("workflow_profile", "workbench"))
    if workflow_profile != "workbench" and protocol["protocol_revision"] != 6:
        raise ValueError("Simple tools workflow requires Protocol 6")
    if workflow_profile == "simple_tools" and (
        audit_policy != "disabled" or investigator_policy != "disabled"
        or protocol.get("context_assistance") != "version_only"
        or protocol.get("navigation_assistance") != "baseline"
        or protocol.get("decision_objective") is not None
    ):
        raise ValueError("Simple tools requires version_only, baseline navigation and disabled auxiliary roles")
    if protocol["protocol_revision"] == 6:
        from .generation.protocol_v6 import DEPENDENCY_QUERY_POLICY, validate_decision_objective

        if investigator_policy not in {"disabled", "required_once"}:
            raise ValueError("Protocol 6 requires a frozen disabled or required_once investigator policy")
        if protocol.get("dependency_query_policy") != DEPENDENCY_QUERY_POLICY:
            raise ValueError("Protocol 6 requires the frozen dependency query policy")
        role_budget = protocol.get("role_budget")
        expected_role_budget = {"investigator_max_calls": 3, "solver_reserved_calls": 2}
        if investigator_context is not None:
            if (not isinstance(role_budget, dict) or set(role_budget) != set(expected_role_budget)
                    or any(type(role_budget[key]) is not int or not 1 <= role_budget[key] <= 30
                           for key in expected_role_budget)):
                raise ValueError("Source-first role budgets require integers from 1 to 30")
        elif role_budget != expected_role_budget:
            raise ValueError("Protocol 6 requires a bounded Investigator budget and reserved Solver calls")
        objective = validate_decision_objective(protocol.get("decision_objective"))
        # 局部角色比较固定为五次；完整开发仍使用登记的总额度和相同角色上限。
        if objective is not None and protocol["max_calls"] != 5:
            raise ValueError("Protocol 6 local role comparisons require exactly 5 total calls")
        if investigator_policy == "required_once" and protocol["max_calls"] < sum(role_budget.values()):
            raise ValueError("Protocol 6 requires room for Investigator and reserved Solver calls")
        decision_point = protocol.get("decision_point_id")
        if (objective is None) != (decision_point is None):
            raise ValueError("Protocol 6 comparison tasks require both objective and decision point")
        if decision_point is not None and (
            not isinstance(decision_point, str)
            or not 1 <= len(decision_point) <= 64
            or not decision_point[0].isalnum()
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in decision_point)
        ):
            raise ValueError("Protocol 6 decision point must be a bounded lowercase identifier")
    elif investigator_policy is not None:
        raise ValueError("Investigator policy is only available in protocol 6")
    elif protocol.get("decision_objective") is not None or protocol.get("decision_point_id") is not None:
        raise ValueError("Decision objectives are only available in protocol 6")


def _digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _feedback_with_note(feedback: str | None, note: str) -> str:
    """保留明确的纠错开头和完整额度提示，不突破请求反馈字段的上限。"""
    if not feedback:
        return note
    return feedback[:1000 - len(note) - 1] + "\n" + note


def _common_comparison_protocol_sha256(protocol: dict) -> str:
    """只排除预登记的角色处理差异，其他执行身份必须保持相同。"""
    common = {
        key: value
        for key, value in protocol.items()
        if key != "investigator_policy"
    }
    return _digest(common)


def _comparison_binding(task: dict) -> dict:
    from .generation.protocol_v6 import (
        decision_objective_sha256,
        validate_comparison_binding,
    )

    historical = task["historical_binding"]
    protocol = task["protocol"]
    objective_sha256 = decision_objective_sha256(protocol["decision_objective"])
    common_protocol_sha256 = _common_comparison_protocol_sha256(protocol)
    core = {
        "schema_version": 1,
        "decision_point_id": protocol["decision_point_id"],
        "case_fingerprint": task["case_fingerprint"],
        "historical_primary_input_sha256": historical["primary_input_sha256"],
        "candidate_revision": historical["candidate_revision"],
        "candidate_sha256": historical["candidate_sha256"],
        "decision_objective_sha256": objective_sha256,
        "common_protocol_sha256": common_protocol_sha256,
    }
    return validate_comparison_binding(
        core | {"pair_input_sha256": _digest(core)},
        protocol["decision_objective"],
    )


def _proposal_comparison_options(task: dict) -> dict:
    if task.get("comparison_binding") is None:
        return {}
    return {
        "decision_objective": task["protocol"]["decision_objective"],
        "comparison_binding": task["comparison_binding"],
    }


def _save(task: dict, path: Path) -> None:
    if task.get("schema_version") in {2, 3, 4, 5} and task.get("candidate"):
        # 审阅和反馈在发布后才产生；保存时补入同一版本，保留原有增量编辑证据。
        candidate = task["candidate"]
        history = task["candidate_history"]
        if not history or history[-1]["revision"] != candidate["revision"]:
            raise ValueError("Candidate history does not end at the current revision")
        history[-1].update(candidate)
    task["updated_at"] = datetime.now(UTC).isoformat()
    temporary = path.with_suffix(".tmp")
    assert_no_links(temporary)
    temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _validate_task_configuration(
    manifest_path: Path, protocol: dict, *, arm: str,
    phase: str, repetition: int = 1, kind: str = "experiment",
    service_owner: str | None = None,
    knowledge_bundle: dict | None = None,
):
    """只读检查任务配置与案例，不创建任务、不打开可写账本。"""
    case = load_case(manifest_path)
    _require_current_protocol(protocol)
    if knowledge_bundle is not None or protocol.get("knowledge_import") is not None:
        from .knowledge_transfer import binding, validate_bundle

        validate_bundle(knowledge_bundle)
        if (binding(knowledge_bundle) != protocol.get("knowledge_import")
                or knowledge_bundle["repository"] != case.manifest.source.repository):
            raise ValueError("Knowledge bundle differs from frozen import")
    if kind not in {"experiment", "operation"}:
        raise ValueError("Unknown task kind")
    if service_owner is not None and (kind != "operation" or len(service_owner) != 32
                                     or any(c not in "0123456789abcdef" for c in service_owner)):
        raise ValueError("Service ownership requires a normal task and exact job identity")
    if kind == "operation" and (phase != "operation" or protocol["protocol_revision"] not in {3, 4, 5, 6}):
        raise ValueError("Normal operations require current candidate protocol")
    if protocol["protocol_revision"] in {4, 5, 6} and kind != "operation":
        raise ValueError("Diagnostic protocols are not enabled for experimental scoring; register a normal operation")
    if protocol["protocol_revision"] in {4, 5, 6}:
        if protocol["protocol_revision"] == 4:
            from .generation.protocol_v4 import DIAGNOSTIC_POLICY
        elif protocol["protocol_revision"] == 5:
            from .generation.protocol_v5 import DIAGNOSTIC_POLICY
        else:
            from .generation.protocol_v6 import DIAGNOSTIC_POLICY
        from .generation.source_context import policy

        if protocol.get("investigation_policy") is None and protocol.get("diagnostic_policy") != DIAGNOSTIC_POLICY:
            raise ValueError("Diagnostic protocol requires its bounded diagnostic policy")
        policy(protocol["source_policy"])
        if protocol["protocol_revision"] in {5, 6}:
            from .cases.requirements import requirements_for_case

            requirements = requirements_for_case(case)
            expected_contract = {
                "path": requirements.path,
                "sha256": requirements.sha256,
                "requirement_ids": [item.id for item in requirements.contract.requirements],
            }
            if protocol.get("contract_requirements") != expected_contract:
                raise ValueError("Contract protocols must freeze the exact structured contract catalog")
    if arm not in ARMS or (kind == "experiment" and phase not in {"calibration", "development", "holdout"}):
        raise ValueError("Unknown experiment arm or phase")
    if protocol.get("repository_preparation") is not None and arm == "codemod_repair":
        raise ValueError("Repository preparation requires original source without an official seed")
    if arm == "codemod_repair":
        from .evidence import migration_package

        if migration_package(case) != "pydantic":
            raise ValueError("No reviewed official seed for this family; explicitly select seed='none'")
    # 普通任务同样消费冻结的公开/独立分组，必须在模型计费之前核对其身份。
    evaluation = load_evaluation(case)
    if kind == "experiment":
        if (phase == "holdout") != (evaluation["split"] == "holdout"):
            raise ValueError("Case split and task phase must agree")
    if kind == "experiment" and phase != "calibration" and protocol.get("status") != "frozen":
        raise ValueError("Formal evaluation requires a frozen protocol")
    if arm in {"codemod_repair", "direct_repair"} and protocol["protocol_revision"] not in {3, 4, 5, 6}:
        raise ValueError("Seed comparison requires candidate protocol 3")
    if set(protocol.get("execution", {})) - {"base_image", "prepare_timeout", "test_timeout"}:
        raise ValueError("Execution settings cannot override verification scope")
    if (type(protocol.get("max_calls")) is not int or protocol["max_calls"] < 1
            or (protocol.get("investigation_policy") is None and protocol["max_calls"] > 30)):
        raise ValueError("A frozen positive call limit is required")
    if type(repetition) is not int or repetition < 1:
        raise ValueError("Repetition must be a positive integer")
    required = {"model", "endpoint", "thinking_mode", "max_output_tokens", "timeout_seconds"}
    fields = set(protocol.get("generation", {}))
    if not required <= fields or fields - required - {"max_source_bytes", "max_request_bytes", "semantic_config", "runtime_capacity"}:
        raise ValueError("Protocol must freeze the complete generation configuration")
    from .generation.capacity import validate as validate_capacity

    validate_capacity(protocol["generation"]["max_output_tokens"], protocol["generation"]["timeout_seconds"],
                      protocol["generation"].get("runtime_capacity"))
    if protocol["generation"].get("semantic_config") is not None:
        from .semantic import validate_config

        if validate_config(protocol["generation"]["semantic_config"]) != protocol["generation"]["semantic_config"]:
            raise ValueError("Task model asset root must be absolute before freezing")
    return case


def create_task(
    manifest_path: Path, work_root: Path, protocol: dict, *, arm: str,
    phase: str, budget_path: Path, repetition: int = 1, kind: str = "experiment",
    service_owner: str | None = None,
    knowledge_bundle: dict | None = None,
) -> dict:
    """创建不花钱的任务快照，公开动作与最终验收仍是不同入口。"""
    case = _validate_task_configuration(
        manifest_path, protocol, arm=arm, phase=phase, repetition=repetition,
        kind=kind, service_owner=service_owner, knowledge_bundle=knowledge_bundle,
    )
    ledger = BudgetLedger(budget_path)
    if ledger.spec["model"] != protocol["generation"]["model"]:
        raise ValueError("Protocol model and budget price model differ")
    root = Path(work_root).absolute()
    assert_no_links(root)
    if root.resolve().is_relative_to(case.root):
        raise ValueError("Task outputs must be outside the immutable case")
    directory = root / "tasks" / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    task = {
        "schema_version": 1, "task_id": directory.name, "status": "ready",
        "case_id": case.manifest.case_id, "case_fingerprint": case.fingerprint,
        "manifest_path": str(case.manifest_path), "work_root": str(root),
        "task_path": str(directory / "task.json"), "protocol": json.loads(json.dumps(protocol)),
        "protocol_sha256": _digest(protocol), "arm": arm, "phase": phase,
        "repetition": repetition, "budget_path": str(Path(budget_path).absolute()),
        "attempts": [], "candidate": None, "review_history": [], "tool_results": [],
        "feedback": None, "protocol_feedback": None, "final_evaluation": "not_run",
        "automatic_recovery": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if protocol["protocol_revision"] in {3, 4, 5, 6}:
        task.update(schema_version=2, kind=kind, current_candidate=None, candidate_history=[],
                    seed_strategy="official" if arm == "codemod_repair" else "none",
                    seed=None, edit_feedback=None)
    if protocol["protocol_revision"] in {4, 5, 6}:
        schema = {4: 3, 5: 4, 6: 5}[protocol["protocol_revision"]]
        task.update(schema_version=schema,
                    observations=[], issues=[], probes=[], diagnostic_runs=[],
                    diagnostic_reviews=[], pending_diagnostic=None)
    if protocol["protocol_revision"] in {5, 6}:
        task["contract_requirements"] = dict(protocol["contract_requirements"])
        task["contract_audits"] = []
    if protocol["protocol_revision"] == 6:
        task.update(
            recommended_candidate=None,
            dependency_queries=[],
            dependency_facts=[],
            pending_dependency_query=None,
            dependency_environment_identities={},
            investigator_sessions=[],
            investigator_handoffs=[],
            consumed_investigator_handoffs=[],
            historical_inputs=[],
        )
        if protocol.get("decision_objective") is not None:
            from .generation.protocol_v6 import decision_objective_sha256

            task["decision_objective_sha256"] = decision_objective_sha256(
                protocol["decision_objective"]
            )
    if service_owner is not None:
        task["service_owner"] = service_owner
    if protocol.get("repository_preparation") is not None:
        task["preparation_state"] = {"status": "active", "start_revision": load_candidate(case).revision,
            "attempt_indices": [], "handoff_reference": None, "handoff_receipt": None,
            "consumed_by": None, "shared_observation_ids": []}
    if knowledge_bundle is not None:
        from .diagnostics import store

        task["knowledge_import_reference"] = store(directory / "knowledge_import", knowledge_bundle)
        from .generation.knowledge_review import enabled, initial_state

        if enabled(task):
            task["knowledge_review_state"] = initial_state(load_candidate(case), task["knowledge_import_reference"])
    _save(task, Path(task["task_path"]))
    return task


def _operation_configuration(manifest_path: Path, *,
                     generation: dict, execution: dict | None = None, max_calls: int = 10,
                     seed_strategy: str = "official",
                     protocol_revision: int = 3, source_policy: dict | None = None,
                      context_assistance: str = "full", navigation_assistance: str = "failure_guided",
                      contract_audit_policy: str | None = None,
                      investigator_policy: str | None = None,
                      investigator_context_policy: dict | None = None,
                      investigator_max_calls: int | None = None,
                      solver_reserved_calls: int | None = None,
                      historical_input: dict | None = None,
                      decision_objective: dict | None = None,
                      decision_point_id: str | None = None,
                      workflow_profile: str = "workbench",
                      source_read_retention: str | None = None,
                      finish_policy: str | None = None,
                      semantic_risk_policy: str | None = None,
                      investigation_policy: dict | None = None,
                      project_context_policy: dict | None = None,
                      repository_preparation: dict | None = None,
                      knowledge_import: dict | None = None) -> tuple[dict, dict | None]:
    """组装普通任务配置；供准备阶段和真实创建共用，不写运行状态。"""
    if seed_strategy not in {"official", "none"}:
        raise ValueError("Seed strategy must be official or none")
    if protocol_revision not in {3, 4, 5, 6} or (source_policy is not None and protocol_revision not in {4, 5, 6}):
        raise ValueError("Source policy requires diagnostic operation protocol 4, 5 or 6")
    protocol = {"protocol_revision": protocol_revision, "status": "operation", "max_calls": max_calls,
                "implementation": implementation_identity(protocol_revision), "generation": generation,
                "execution": execution or {}, "context_assistance": context_assistance,
                "navigation_assistance": navigation_assistance}
    if semantic_risk_policy is not None:
        from .generation.semantic_risk import validate_policy as risk_policy

        if protocol_revision not in {4, 5, 6}:
            raise ValueError("Semantic risk policy requires a diagnostic operation")
        protocol["semantic_risk_policy"] = risk_policy(semantic_risk_policy)
    # 新普通任务修正提交接口；已有冻结任务缺省仍按旧协议重建。
    if protocol_revision in {5, 6}:
        from .finish_evidence import POLICY, validate_policy

        protocol["finish_policy"] = validate_policy(POLICY if finish_policy is None else finish_policy)
    elif finish_policy is not None:
        raise ValueError("Finish policy requires protocol 5 or 6")
    if source_read_retention is not None:
        protocol["source_read_retention"] = source_read_retention
    if repository_preparation is not None:
        if seed_strategy != "none":
            raise ValueError("Repository preparation starts from original source without a seed")
        protocol["repository_preparation"] = repository_preparation
    if project_context_policy is not None:
        from .generation.project_context import policy as knowledge_policy

        if protocol_revision not in {4, 6} or workflow_profile != "workbench":
            raise ValueError("Project context requires a diagnostic P4/P6 workbench operation")
        protocol["project_context_policy"] = knowledge_policy(project_context_policy)
    if protocol_revision == 6 or workflow_profile != "workbench":
        protocol["workflow_profile"] = workflow_profile
    if protocol_revision in {4, 5, 6}:
        if protocol_revision == 4:
            from .generation.protocol_v4 import DIAGNOSTIC_POLICY
        elif protocol_revision == 5:
            from .cases.requirements import requirements_for_case
            from .generation.protocol_v5 import DIAGNOSTIC_POLICY
        else:
            from .cases.requirements import requirements_for_case
            from .generation.protocol_v6 import DEPENDENCY_QUERY_POLICY, DIAGNOSTIC_POLICY
        from .generation.source_context import policy

        protocol.update(source_policy=policy(source_policy), diagnostic_policy=dict(DIAGNOSTIC_POLICY))
        if investigation_policy is not None:
            from .investigation_policy import policy as investigation_config

            if protocol_revision not in {5, 6}:
                raise ValueError("Investigation policy requires protocol 5 or 6")
            protocol["investigation_policy"] = investigation_config(investigation_policy)
            protocol["diagnostic_policy"] = dict(protocol["investigation_policy"]["diagnostic_limits"])
        if protocol_revision in {5, 6}:
            requirements = requirements_for_case(load_case(manifest_path))
            protocol["contract_requirements"] = {
                "path": requirements.path,
                "sha256": requirements.sha256,
                "requirement_ids": [item.id for item in requirements.contract.requirements],
            }
            protocol["contract_audit_policy"] = contract_audit_policy or "bounded"
            if protocol_revision == 6:
                from .generation.protocol_v6 import DEFAULT_ROLE_BUDGET

                protocol["investigator_policy"] = investigator_policy or "disabled"
                if investigator_context_policy is not None:
                    from .generation.investigator_context import policy as context_policy

                    protocol["investigator_context_policy"] = context_policy(investigator_context_policy)
                protocol["dependency_query_policy"] = dict(DEPENDENCY_QUERY_POLICY)
                protocol["role_budget"] = {
                    "investigator_max_calls": (
                        DEFAULT_ROLE_BUDGET["investigator_max_calls"]
                        if investigator_max_calls is None
                        else investigator_max_calls
                    ),
                    "solver_reserved_calls": (
                        DEFAULT_ROLE_BUDGET["solver_reserved_calls"]
                        if solver_reserved_calls is None
                        else solver_reserved_calls
                    ),
                }
                if decision_objective is not None or decision_point_id is not None:
                    protocol["decision_objective"] = decision_objective
                    protocol["decision_point_id"] = decision_point_id
        elif contract_audit_policy is not None:
            raise ValueError("Contract audit policy is only available in protocol 5 or 6")
        elif investigator_policy is not None:
            raise ValueError("Investigator policy is only available in protocol 6")
    if investigation_policy is not None and protocol_revision not in {5, 6}:
        raise ValueError("Investigation policy requires protocol 5 or 6")
    if protocol_revision != 6 and (
        investigator_context_policy is not None
        or investigator_max_calls is not None
        or solver_reserved_calls is not None
        or historical_input is not None
        or decision_objective is not None
        or decision_point_id is not None
    ):
        raise ValueError("Role budgets, historical inputs and decision objectives require protocol 6")
    if historical_input is not None and seed_strategy != "none":
        raise ValueError("A historical decision context already supplies its candidate base; use seed_strategy='none'")
    if decision_objective is not None and historical_input is None:
        raise ValueError("A decision objective requires one frozen historical decision context")
    knowledge_bundle = None
    if knowledge_import is not None:
        from .knowledge_transfer import binding, load_bundle

        if historical_input is not None or repository_preparation is not None:
            raise ValueError("Knowledge import cannot be mixed with a historical decision or preparation handoff")
        if (project_context_policy or {}).get("version") in {"project-context-v8", "project-context-v9", "project-context-v10"} and seed_strategy != "none":
            raise ValueError("Knowledge review requires an unchanged source without a seed")
        knowledge_bundle = load_bundle(knowledge_import, load_case(manifest_path))
        protocol["knowledge_import"] = binding(knowledge_bundle)
    return protocol, knowledge_bundle


def validate_operation_profile(manifest_path: Path, profile: dict, budget_spec: dict) -> None:
    """准备实例时复用真实任务的配置校验，避免登记无法使用的绑定。"""
    protocol, bundle = _operation_configuration(manifest_path, **profile)
    _validate_task_configuration(
        manifest_path, protocol,
        arm="codemod_repair" if profile.get("seed_strategy", "official") == "official" else "direct_repair",
        phase="operation", kind="operation", knowledge_bundle=bundle,
    )
    if protocol["generation"]["model"] != budget_spec["model"]:
        raise ValueError("Protocol model and budget price model differ")


def create_operation(manifest_path: Path, work_root: Path, *, budget_path: Path,
                     service_owner: str | None = None, **profile) -> dict:
    """日常任务入口；profile 至少提供 generation，其余字段由共用配置校验。"""
    protocol, bundle = _operation_configuration(manifest_path, **profile)
    task = create_task(manifest_path, work_root, protocol, budget_path=budget_path,
                       arm="codemod_repair" if profile.get("seed_strategy", "official") == "official" else "direct_repair",
                       phase="operation", kind="operation", service_owner=service_owner,
                       knowledge_bundle=bundle)
    historical_input = profile.get("historical_input")
    if historical_input is not None:
        task = bind_historical_input(Path(task["task_path"]), historical_input)
    return task


def require_owner(task: dict, execution_owner: str | None) -> None:
    """服务任务只能由持有该作业互斥权的适配器推进，普通CLI仍可只读查看。"""
    if task.get("service_owner") != execution_owner:
        raise ValueError("Task execution ownership mismatch; use its service entry point")


def inspect_task(task_path: Path) -> dict:
    """离线核对任务和当前候选；查看历史不要求密钥或当前实现身份匹配。"""
    path = Path(task_path).absolute()
    assert_no_links(path)
    if path.stat().st_size > 2_000_000:
        raise ValueError("Task record exceeds its limit")
    task = json.loads(path.read_text(encoding="utf-8"))
    if Path(task["task_path"]) != path or _digest(task["protocol"]) != task["protocol_sha256"]:
        raise ValueError("Task location or frozen protocol changed")
    if task.get("schema_version") != {2: 1, 3: 2, 4: 3, 5: 4, 6: 5}.get(
        task["protocol"].get("protocol_revision")
    ):
        raise ValueError("Task schema and protocol revision disagree")
    case = load_case(Path(task["manifest_path"]))
    if case.fingerprint != task["case_fingerprint"]:
        raise ValueError("Task case has changed")
    if task["protocol"].get("knowledge_import") is not None or task.get("knowledge_import_reference") is not None:
        from .knowledge_transfer import public_import

        public_import(task, case, load_candidate(case, task["current_candidate"]))
    if task["protocol"].get("repository_preparation") is not None:
        from .generation.preparation import validate_state

        validate_state(task)
    from .generation.knowledge_review import enabled as review_enabled
    from .generation.knowledge_review import validate_state as validate_knowledge_review

    if review_enabled(task) or "knowledge_review_state" in task:
        validate_knowledge_review(task, load_candidate(case, task["current_candidate"]))
    if task.get("schema_version") in {2, 3, 4, 5}:
        snapshot = load_candidate(case, task["current_candidate"])
        candidate = task.get("candidate")
        if candidate and (candidate["revision"] != snapshot.revision or candidate["sha256"] != snapshot.sha256):
            raise ValueError("Current candidate pointer disagrees with task state")
    if task.get("candidate"):
        patch = Path(task["candidate"]["patch_path"])
        assert_no_links(patch)
        if hashlib.sha256(patch.read_bytes()).hexdigest() != task["candidate"]["sha256"]:
            raise ValueError("Candidate changed after review")
    if task.get("schema_version") == 5 and task.get("historical_inputs"):
        binding = task.get("historical_binding")
        required = {
            "input_id", "primary_input_sha256", "source_task_id", "attempt_index",
            "candidate_reference", "candidate_revision", "candidate_sha256",
        }
        if (
            not isinstance(binding, dict)
            or set(binding) != required
            or len(task["historical_inputs"]) != 1
            or task["historical_inputs"][0].get("id") != binding["input_id"]
        ):
            raise ValueError("Historical binding identity changed")
        candidate_reference = binding["candidate_reference"]
        expected_directory = (
            path.parent / "historical_candidates" / binding["primary_input_sha256"]
        ).resolve()
        candidate_directory = Path(candidate_reference.get("path", "")).absolute().parent
        if candidate_directory.resolve() != expected_directory:
            raise ValueError("Historical candidate is outside its immutable binding directory")
        assert_no_links(candidate_directory)
        base = load_candidate(case, candidate_reference)
        if (
            base.revision != binding["candidate_revision"]
            or base.sha256 != binding["candidate_sha256"]
            or _artifact_sha256(candidate_directory / "candidate.patch", maximum=1_000_000)
            != base.sha256
        ):
            raise ValueError("Historical candidate binding changed")
        from .diagnostics import public_historical_input, read

        historical_value = read(task["historical_inputs"][0], path.parent)
        if (
            historical_value.get("candidate", {}).get("increment_patch_sha256")
            != _artifact_sha256(candidate_directory / "increment.patch", maximum=96_000)
        ):
            raise ValueError("Historical candidate increment changed")
        public_historical_input(task, task["historical_inputs"][0], snapshot=snapshot)
    if task.get("schema_version") == 5:
        from .generation.protocol_v6 import decision_objective_sha256, validate_decision_objective

        objective = validate_decision_objective(task["protocol"].get("decision_objective"))
        if objective is None:
            if task.get("decision_objective_sha256") is not None or task.get("comparison_binding") is not None:
                raise ValueError("Task has comparison identity without a frozen decision objective")
        else:
            expected_objective_sha256 = decision_objective_sha256(objective)
            if task.get("decision_objective_sha256") != expected_objective_sha256:
                raise ValueError("Task decision objective identity changed")
            if task.get("historical_inputs"):
                if task.get("comparison_binding") != _comparison_binding(task):
                    raise ValueError("Task comparison binding changed")
                expected_attempt = {
                    "decision_objective_sha256": expected_objective_sha256,
                    "pair_input_sha256": task["comparison_binding"]["pair_input_sha256"],
                    "comparison_binding_sha256": _digest(task["comparison_binding"]),
                }
                for attempt in task.get("attempts", []):
                    if any(attempt.get(key) != value for key, value in expected_attempt.items()):
                        raise ValueError("Task attempt comparison identity changed")
                    proposal_path = Path(attempt.get("receipt", "")).absolute()
                    work_root = Path(task["work_root"]).resolve()
                    if (
                        not proposal_path.resolve().is_relative_to(work_root)
                        or proposal_path.name != "proposal.json"
                    ):
                        raise ValueError("Task attempt proposal is outside its work root")
                    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
                    request_path = Path(proposal.get("request_path", "")).absolute()
                    if (
                        proposal.get("decision_objective") != objective
                        or proposal.get("comparison_binding") != task["comparison_binding"]
                        or proposal.get("decision_objective_sha256") != expected_objective_sha256
                        or proposal.get("pair_input_sha256")
                        != task["comparison_binding"]["pair_input_sha256"]
                        or proposal.get("request_sha256") != attempt.get("request_sha256")
                        or request_path.parent != proposal_path.parent
                        or request_path.name != "request.json"
                        or _artifact_sha256(request_path, maximum=2_400_000)
                        != attempt.get("request_sha256")
                    ):
                        raise ValueError("Task attempt proposal or request comparison identity changed")
    return task


def _artifact_sha256(path: Path, *, maximum: int = 2_000_000) -> str:
    path = Path(path).absolute()
    assert_no_links(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Historical source artifact is missing or exceeds its limit")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request_value_sha256(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _candidate_artifacts(reference: dict) -> tuple[object, dict[str, str]]:
    """核验三个候选文件；candidate.patch 不能只靠 revision.json 中的声明间接信任。"""
    case_path = Path(reference["case_manifest"])
    candidate_reference = reference["candidate_reference"]
    candidate = load_candidate(load_case(case_path), candidate_reference)
    directory = Path(candidate_reference["path"]).absolute().parent
    revision = directory / "revision.json"
    patch = directory / "candidate.patch"
    increment = directory / "increment.patch"
    record = json.loads(revision.read_text(encoding="utf-8"))
    hashes = {
        "revision_sha256": _artifact_sha256(revision, maximum=1_000_000),
        "candidate_patch_sha256": _artifact_sha256(patch, maximum=1_000_000),
        "increment_patch_sha256": _artifact_sha256(increment, maximum=96_000),
    }
    if (
        hashes["revision_sha256"] != candidate.revision
        or hashes["candidate_patch_sha256"] != candidate.sha256
        or hashes["candidate_patch_sha256"] != record.get("patch_sha256")
        or hashes["increment_patch_sha256"] != record.get("increment_sha256")
    ):
        raise ValueError("Historical candidate bytes changed")
    return candidate, hashes


def _read_historical_attempt(source_task_path: Path, attempt_index: int):
    """读取某次已完成请求的原始截面；不从任务终态反推当时可见内容。"""
    source_path = Path(source_task_path).absolute()
    source_sha256 = _artifact_sha256(source_path)
    source = inspect_task(source_path)
    if source.get("schema_version") not in {4, 5}:
        raise ValueError("Historical input requires a schema 4 or 5 source task")
    if source["status"] not in TERMINAL or source_path.with_suffix(".lock").exists():
        raise ValueError("Historical input requires an immutable terminal source task")
    if type(attempt_index) is not int or not 1 <= attempt_index <= len(source["attempts"]):
        raise ValueError("Historical attempt index is unknown")
    attempt = source["attempts"][attempt_index - 1]
    if attempt.get("status") not in _COMPLETED_HISTORICAL_ATTEMPT_STATUSES:
        raise ValueError("Historical attempt is incomplete or has an unknown outcome")
    request_id = attempt.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Historical attempt request identity is missing")
    proposal_path = Path(attempt.get("receipt", "")).absolute()
    work_root = Path(source["work_root"]).resolve()
    if (
        not proposal_path.resolve().is_relative_to(work_root)
        or proposal_path.name != "proposal.json"
        or proposal_path.parent.name != request_id
    ):
        raise ValueError("Historical proposal is outside its source request")
    proposal_sha256 = _artifact_sha256(proposal_path)
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    if (
        proposal.get("report_path") != str(proposal_path)
        or proposal.get("status") != attempt["status"]
        or proposal.get("calls") != 1
        or proposal.get("case_id") != source["case_id"]
        or proposal.get("case_fingerprint") != source["case_fingerprint"]
    ):
        raise ValueError("Historical proposal identity changed")
    request_path = Path(proposal.get("request_path", "")).absolute()
    if request_path.parent != proposal_path.parent or request_path.name != "request.json":
        raise ValueError("Historical request does not belong to its proposal")
    request_sha256 = _artifact_sha256(request_path)
    if proposal.get("request_sha256") != request_sha256:
        raise ValueError("Historical request bytes changed")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    messages = request.get("messages")
    if not isinstance(messages, list) or len(messages) != 2 or messages[1].get("role") != "user":
        raise ValueError("Historical request does not contain one frozen public context")
    public_text = messages[1].get("content")
    if not isinstance(public_text, str):
        raise ValueError("Historical request public context is invalid")
    public_context_sha256 = hashlib.sha256(public_text.encode("utf-8")).hexdigest()
    public = json.loads(public_text)
    task_context = public.get("task_context")
    diagnostic_state = public.get("diagnostic_state")
    if not isinstance(task_context, dict) or not isinstance(diagnostic_state, dict):
        raise ValueError("Historical request lacks task_context or diagnostic_state")
    if (
        task_context.get("task_id") != source["task_id"]
        or task_context.get("attempt_index") != attempt_index
        or proposal.get("public_context_sha256") != public_context_sha256
        or proposal.get("task_context_sha256") != _request_value_sha256(task_context)
    ):
        raise ValueError("Historical request context identity changed")
    context_reference = proposal.get("diagnostic_context_reference")
    if not isinstance(context_reference, dict) or set(context_reference) != {"id", "path"}:
        raise ValueError("Historical diagnostic context identity is missing")
    from .diagnostics import read

    frozen = read(context_reference, source_path.parent)
    if (
        frozen.get("task_id") != source["task_id"]
        or frozen.get("case_fingerprint") != source["case_fingerprint"]
    ):
        raise ValueError("Historical diagnostic context belongs to another task or case")
    candidate_reference = proposal.get("candidate_reference")
    if not isinstance(candidate_reference, dict):
        raise ValueError("Historical attempt has no immutable candidate to rebind")
    candidate, candidate_hashes = _candidate_artifacts(
        {"case_manifest": source["manifest_path"], "candidate_reference": candidate_reference}
    )
    public_candidate = public.get("candidate")
    if (
        proposal.get("candidate_revision") != candidate.revision
        or candidate_reference.get("revision") != candidate.revision
        or frozen.get("current_candidate") != candidate_reference
        or not isinstance(public_candidate, dict)
        or public_candidate.get("revision") != candidate.revision
        or public_candidate.get("patch_sha256") != candidate.sha256
    ):
        raise ValueError("Historical candidate disagrees with the selected request")
    if _artifact_sha256(source_path) != source_sha256:
        raise ValueError("Historical source task changed while it was being read")
    spec = {
        "source_task_path": str(source_path),
        "source_task_sha256": source_sha256,
        "source_task_id": source["task_id"],
        "source_schema_version": source["schema_version"],
        "source_protocol_sha256": source["protocol_sha256"],
        "case_id": source["case_id"],
        "case_fingerprint": source["case_fingerprint"],
        "attempt_index": attempt_index,
        "attempt_status": attempt["status"],
        "request_id": request_id,
        "proposal_sha256": proposal_sha256,
        "request_sha256": request_sha256,
        "public_context_sha256": public_context_sha256,
        "task_context_sha256": _request_value_sha256(task_context),
        "diagnostic_state_sha256": _request_value_sha256(diagnostic_state),
        "diagnostic_context_id": context_reference["id"],
        "diagnostic_context_sha256": _artifact_sha256(Path(context_reference["path"])),
        "candidate_revision": candidate.revision,
        "candidate_sha256": candidate.sha256,
        "candidate_increment_sha256": candidate_hashes["increment_patch_sha256"],
    }
    return spec, source, proposal, public, frozen, candidate_reference, candidate


def _historical_source(spec: dict):
    required = {
        "source_task_path", "source_task_sha256", "source_task_id", "source_schema_version",
        "source_protocol_sha256", "case_id", "case_fingerprint", "attempt_index",
        "attempt_status", "request_id", "proposal_sha256", "request_sha256",
        "public_context_sha256", "task_context_sha256", "diagnostic_state_sha256",
        "diagnostic_context_id", "diagnostic_context_sha256", "candidate_revision",
        "candidate_sha256", "candidate_increment_sha256",
    }
    if not isinstance(spec, dict) or set(spec) != required:
        raise ValueError("Historical input must freeze the exact task, attempt, request, context and candidate")
    discovered = _read_historical_attempt(Path(spec["source_task_path"]), spec["attempt_index"])
    if discovered[0] != spec:
        raise ValueError("Historical source bytes or identities changed after registration")
    return discovered[1:]


def build_historical_input_spec(source_task_path: Path, attempt_index: int) -> dict:
    """冻结所选已完成 attempt 当时真实发送的上下文，不读取它之后产生的材料。"""
    return _read_historical_attempt(source_task_path, attempt_index)[0]


def _copy_historical_candidate(case, reference: dict, directory: Path) -> dict:
    source = Path(reference["path"]).absolute().parent
    target = Path(directory).absolute()
    assert_no_links(target)
    target.mkdir(parents=True, exist_ok=False)
    for name, maximum in (
        ("revision.json", 1_000_000),
        ("candidate.patch", 1_000_000),
        ("increment.patch", 96_000),
    ):
        original = source / name
        destination = target / name
        assert_no_links(destination)
        data = original.read_bytes()
        _artifact_sha256(original, maximum=maximum)
        destination.write_bytes(data)
    rebound = {"path": str(target / "revision.json"), "revision": reference["revision"]}
    rebound_candidate = load_candidate(case, rebound)
    if hashlib.sha256((target / "candidate.patch").read_bytes()).hexdigest() != rebound_candidate.sha256:
        raise ValueError("Copied historical cumulative patch changed")
    return rebound


def _historical_observation_refs(diagnostic_state: dict, namespace: str) -> list[str]:
    identities = {
        row.get("id")
        for key in ("observations", "observation_index")
        for row in diagnostic_state.get(key, [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    return [f"{namespace}:observation:{identity}" for identity in sorted(identities)]


def _empty_historical_target(task: dict) -> bool:
    empty_lists = (
        "attempts", "candidate_history", "review_history", "tool_results", "observations",
        "issues", "probes", "diagnostic_runs", "diagnostic_reviews", "contract_audits",
        "dependency_queries", "dependency_facts", "investigator_sessions",
        "investigator_handoffs", "consumed_investigator_handoffs", "historical_inputs",
    )
    return (
        task.get("schema_version") == 5
        and task.get("status") == "ready"
        and all(not task.get(key) for key in empty_lists)
        and all(
            task.get(key) is None
            for key in ("candidate", "current_candidate", "seed", "feedback", "pending_diagnostic",
                        "pending_dependency_query")
        )
        and task.get("final_evaluation") == "not_run"
    )


def bind_historical_input(task_path: Path, spec: dict) -> dict:
    """复制历史候选和当时可见输入；不复制旧执行、预算、终态或 final 结论。"""
    task_path = Path(task_path).absolute()
    lock = task_path.with_suffix(".lock")
    assert_no_links(lock)
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(task_path.parent.name)
    try:
        task = inspect_task(task_path)
        if not _empty_historical_target(task):
            raise ValueError("Historical input can bind only to a new empty Protocol 6 task")
        source, proposal, public, _frozen, source_reference, candidate = _historical_source(spec)
        if source["case_id"] != task["case_id"] or source["case_fingerprint"] != task["case_fingerprint"]:
            raise ValueError("Historical input and new task use different cases")
        primary_fields = {key: value for key, value in spec.items() if key != "source_task_path"}
        primary_input_sha256 = _digest(primary_fields)
        case = load_case(Path(task["manifest_path"]))
        rebound = _copy_historical_candidate(
            case,
            source_reference,
            task_path.parent / "historical_candidates" / primary_input_sha256,
        )
        rebound_candidate = load_candidate(case, rebound)
        if rebound_candidate.revision != candidate.revision or rebound_candidate.sha256 != candidate.sha256:
            raise ValueError("Rebound historical candidate changed")
        namespace = f"historical:{primary_input_sha256}"
        diagnostic_state = public["diagnostic_state"]
        value = {
            "schema_version": 1,
            "kind": "historical_decision_context",
            "classification": "historical_only",
            "case_fingerprint": task["case_fingerprint"],
            "primary_input_sha256": primary_input_sha256,
            "evidence_namespace": namespace,
            "historical_observation_refs": _historical_observation_refs(diagnostic_state, namespace),
            "source": {
                key: spec[key]
                for key in spec
                if key != "source_task_path"
            },
            "candidate": {
                "revision": rebound_candidate.revision,
                "patch_sha256": rebound_candidate.sha256,
                "increment_patch_sha256": spec["candidate_increment_sha256"],
            },
            # 只有这两个对象属于所选请求的模型可见输入；不从源任务终态补材料。
            "visible": {
                "task_context": public["task_context"],
                "diagnostic_state": diagnostic_state,
            },
            "limitations": [
                "This is read-only material visible at one historical request, not a resumed task.",
                "Embedded observation IDs belong to the historical namespace and are not current observations.",
                "Old execution, budget, terminal and final states were not imported into this task.",
            ],
            "source_proposal_status": proposal["status"],
        }
        from .diagnostics import store

        reference = store(task_path.parent / "historical_inputs", value)
        task["current_candidate"] = rebound
        task["historical_inputs"].append(reference)
        task["historical_binding"] = {
            "input_id": reference["id"],
            "primary_input_sha256": primary_input_sha256,
            "source_task_id": spec["source_task_id"],
            "attempt_index": spec["attempt_index"],
            "candidate_reference": rebound,
            "candidate_revision": rebound_candidate.revision,
            "candidate_sha256": rebound_candidate.sha256,
        }
        if task["protocol"].get("decision_objective") is not None:
            task["comparison_binding"] = _comparison_binding(task)
        _save(task, task_path)
        return inspect_task(task_path)
    finally:
        lock.unlink(missing_ok=True)


def _feedback(report: dict, sha256: str) -> dict:
    """只从明确登记为 feedback 的执行结果取有限日志，不能改用最终评分结果。"""
    from .diagnostics import public_failure_details

    if report.get("check_group") != "feedback":
        raise ValueError("Only public feedback results may be sent to a solver")
    candidate = report["stages"].get("new_candidate", report["stages"].get("new_original"))
    if candidate.get("status") == "not_supplied":
        candidate = report["stages"]["new_original"]
    state = "passed" if report["status"] == "candidate_verified" else "failed"
    if (
        report["status"] in {"no_regression_observed", "comparison_only"}
        and _fully_passed(candidate)
        and _fully_passed(report["stages"]["old_original"])
        and candidate["nodeids"] == report["stages"]["old_original"]["nodeids"]
    ):
        state = "passed"
    if report["status"] in {"execution_incomplete", "blocked_environment", "execution_error", "baseline_invalid", "test_set_changed"}:
        state = "execution_incomplete"
    failures = []
    if state != "passed":
        stdout = candidate.get("stdout_path")
        if stdout and Path(stdout).is_file():
            raw = Path(stdout).read_text(encoding="utf-8", errors="replace")
            details = public_failure_details(candidate, raw)
            # 旧协议仍要求 {category,message}；结构化 nodeid/阶段嵌入有界消息，
            # 不再把 warning 尾部误当成失败原因。
            for detail in details[:16]:
                label = detail.get("nodeid") or "pytest failure"
                phase = detail.get("when") or "call"
                message = f"[{label} / {phase}]\n{detail['message']}"
                failures.append({"category": detail["category"], "message": message[:4000]})
        if not failures:
            failures = [{"category": "execution", "message": report.get("reason", state)}]
    return {"scope": "public", "candidate_sha256": sha256, "status": state, "failures": failures}


def accept_response(task: dict, response: dict, case) -> bool:
    """接纳已经校验并持久化的回复；返回是否可继续，不产生网络或目标执行。"""
    from .generation.knowledge_review import enabled, verify_response

    if enabled(task):
        value = verify_response(task, response, load_candidate(case, task["current_candidate"]))
        if value["phase"] == "review" and task["knowledge_review_state"]["phase"] == "solve":
            # 已应用完成回执的中断恢复不再次生成观察或转换阶段。
            if (task["status"] in {"processing_response", "calling_model"}
                    and task["attempts"][-1]["receipt"] == response["report_path"]):
                task["status"] = "ready"
            return True
    if task.get("protocol", {}).get("repository_preparation") is not None:
        from .generation.preparation import verify_response

        verify_response(task, response)
        if task["preparation_state"].get("handoff_receipt") == response.get("report_path"):
            from .diagnostics import public_knowledge, remember_action

            public_knowledge(task, task["preparation_state"]["handoff_reference"])
            if task["status"] in {"processing_response", "calling_model"}:
                task["status"] = "ready"
                remember_action(task, response, case, before_revision=load_candidate(case, task["current_candidate"]).revision)
            return True
    from .generation.incomplete import accept_length

    length_result = accept_length(task, response)
    if length_result is not None:
        return length_result
    if task.get("schema_version") not in {3, 4, 5}:
        return _accept_response(task, response, case)
    from .diagnostics import remember_action

    before = load_candidate(case, task["current_candidate"]).revision
    task["diagnostic_reused"] = False
    role = "solver"
    if task.get("schema_version") == 5:
        attempt = task["attempts"][-1] if task.get("attempts") else {}
        pending_finish = (task.get("pending_contract_audit") or {}).get("solver_finish")
        if pending_finish is not None and pending_finish["path"] == response.get("report_path"):
            # 审阅回包之后接纳的是先前 Solver 的 finish，不能误绑定到最新 Auditor。
            matches = [item for item in task["attempts"] if item["receipt"] == pending_finish["path"]]
            if len(matches) != 1:
                raise ValueError("Pending solver finish does not bind exactly one attempt")
            attempt = matches[0]
        attempt_role = attempt.get("role")
        reported_role = response.get("role")
        role = attempt_role or reported_role or (
            "investigator" if response.get("output_format") == "investigator_actions" else "solver"
        )
        if attempt_role is not None and reported_role is not None and attempt_role != reported_role:
            raise ValueError("Response role changed after the request was frozen")
        expected_format = {
            "solver": "protocol_v6_actions",
            "investigator": "investigator_actions",
            "contract_auditor": "contract_audit",
        }.get(role)
        if expected_format is None or (
            response.get("output_format") is not None
            and response["output_format"] != expected_format
        ):
            raise ValueError("Response output format does not match its frozen role")
        response = dict(response)
        response["role"] = role
        if task.get("comparison_binding") is not None:
            expected_identity = {
                "decision_objective_sha256": task["decision_objective_sha256"],
                "pair_input_sha256": task["comparison_binding"]["pair_input_sha256"],
            }
            if any(
                attempt.get(key) != value or response.get(key) != value
                for key, value in expected_identity.items()
            ):
                raise ValueError("Response comparison identity changed after the request was frozen")
        kind = response.get("action", {}).get("type")
        if role == "investigator" and kind in {
            "submit_candidate",
            "restore_candidate",
            "finish",
        }:
            task.update(
                status="ready",
                edit_feedback=(
                    "role_write_forbidden: Investigator is read-only; return a sourced handoff or "
                    "another permitted observation request."
                ),
            )
            remember_action(task, response, case, before_revision=before)
            return True
    read_only = None
    if role == "investigator":
        read_only = {
            key: json.loads(json.dumps(task[key]))
            for key in (
                "candidate",
                "current_candidate",
                "candidate_history",
                "review_history",
                "finish",
                "verification_subject",
                "final_evaluation",
                "final_candidate",
                "final_result",
            )
            if key in task
        }
    delivered_context = None
    if task["protocol"].get("investigation_policy") is not None and response.get("action") is not None:
        from .generation.request_evidence import verified_request_context

        delivered_context = verified_request_context(task, response)
    continuing = _accept_response(task, response, case)
    if read_only is not None:
        after = {
            key: json.loads(json.dumps(task[key]))
            for key in read_only
            if key in task
        }
        if after != read_only:
            raise ValueError("Investigator changed candidate or terminal state")
    remember_action(task, response, case, before_revision=before, delivered_context=delivered_context)
    return continuing and task["status"] != "unresolved"


def _accept_response(task: dict, response: dict, case) -> bool:
    incremental = task.get("schema_version") in {2, 3, 4, 5}
    if (response["status"] in {"provider_response_rejected", "candidate_rejected"}
            and response.get("reason") in PROTOCOL_ERROR_CODES):
        task["protocol_feedback"] = {
            "code": response["reason"], "attempt_index": len(task["attempts"]),
        }
        if incremental:
            task["edit_feedback"] = response.get("edit_feedback")
        task["status"] = "ready"
        return True
    task["protocol_feedback"] = None
    if incremental:
        task["edit_feedback"] = None
    if task.get("schema_version") in {3, 4, 5} and response["status"] in {
        "agent_finished",
        "action_ready",
        "investigation_ready",
        "pending_review",
    }:
        from .diagnostics import accept_action

        try:
            accepted = accept_action(task, response, case)
        except ValueError as error:
            task.update(status="ready", edit_feedback=str(error)[:1000])
            return True
        if accepted is not None:
            return accepted
    if response["status"] == "pending_review":
        previous = task["candidate"]
        if previous and previous["reviewed"] and previous["sha256"] == response["candidate_sha256"]:
            assert_no_links(Path(previous["patch_path"]))
            if hashlib.sha256(Path(previous["patch_path"]).read_bytes()).hexdigest() != previous["sha256"]:
                raise ValueError("Previously reviewed candidate bytes changed")
            task.update(status="unresolved" if task.get("schema_version") in {3, 4, 5} else "submitted",
                        stop_reason="repeated_candidate_no_change")
            task["duplicate_submission"] = {
                "receipt": response["report_path"], "sha256": response["candidate_sha256"],
                "reviewed_receipt": previous["receipt"],
            }
            return False
        if incremental:
            snapshot = load_candidate(case, response["candidate_reference"])
            if snapshot.parent != load_candidate(case, task["current_candidate"]).revision:
                raise ValueError("Increment was not based on the current revision")
        task["candidate"] = {
            "sha256": response["candidate_sha256"], "patch_path": response["candidate_patch"],
            "edits": response["action"]["edits"], "reviewed": False,
            "receipt": response["report_path"],
        }
        if incremental:
            task["candidate"].update(revision=snapshot.revision, origin="agent_candidate")
            task["current_candidate"] = snapshot.reference
            task["candidate_history"].append(dict(task["candidate"]))
            task["candidate"].pop("edits", None)
            task["tool_results"] = []
            task.pop("feedback_revision", None)
        task["feedback"] = None
        task["status"] = "pending_review"
        return False
    if response["status"] == "agent_finished":
        task["status"] = "submitted" if task["candidate"] else "no_candidate"
        return False
    if response["status"] == "action_ready":
        action = response["action"]
        result = execute_source_action(case, action, **({"candidate": load_candidate(case, task["current_candidate"])} if incremental else {}))
        if incremental and len(task["tool_results"]) >= 2 and all(
            item == {"action": action, "result": result} for item in task["tool_results"][-2:]
        ):
            task.update(status="submitted" if task["candidate"] else "no_candidate",
                        stop_reason="repeated_source_action_no_progress")
            return False
        task["tool_results"].append({"action": action, "result": result})
        task["status"] = "ready"
        return True
    if response["status"] == "outcome_unknown":
        task.update(status="outcome_unknown", stop_reason=response["reason"])
    elif response["status"] == "execution_error":
        task.update(status="execution_incomplete", stop_reason=response["reason"])
    else:
        task.update(status=("unresolved" if task.get("schema_version") in {3, 4, 5} and task["candidate"] else
                            "submitted" if task["candidate"] else "generation_failed"),
                    stop_reason=response.get("reason", response["status"]))
    return False


def _contract_audit_due(task: dict, response: dict) -> bool:
    """首次结束及一次处置复核可审阅；调用上限不足时不暗增预算。"""
    return (
        task.get("schema_version") in {4, 5}
        and task["protocol"].get("contract_audit_policy") in {"bounded", "delivery-audit-v1", "delivery-audit-v2"}
        and response.get("status") == "agent_finished"
        and response.get("action", {}).get("type") == "finish"
        and len(task.get("contract_audits", [])) < 2
        and len(task["attempts"]) < task["protocol"]["max_calls"]
    )


def _stagnation_contract_audit_due(task: dict, case) -> bool:
    """连续多个候选保留同一公开失败时，给 Solver 一次独立只读复核。"""
    if not (
        task.get("schema_version") in {4, 5}
        and task["protocol"].get("contract_audit_policy") in {"bounded", "delivery-audit-v1", "delivery-audit-v2"}
        and not task.get("contract_audits")
        # 先由 Solver 消费紧邻上一请求的格式反馈，再插入其他角色。
        and task.get("protocol_feedback") is None
        and len(task["attempts"]) + 1 < task["protocol"]["max_calls"]
        and (task.get("candidate") or {}).get("reviewed") is True
        and (task.get("feedback") or {}).get("status") == "failed"
    ):
        return False

    from .diagnostic_state import recurring_public_failures
    from .diagnostics import public_observation

    snapshot = load_candidate(case, task["current_candidate"])
    observations = [public_observation(task, reference) for reference in task["observations"]]
    recurring = recurring_public_failures(observations, snapshot.revision)
    rows = [*recurring["items"], *recurring["semantic_clusters"]]
    return any(not row["stale"] and row["distinct_failed_revisions"] >= 3 for row in rows)


def _set_contract_audit_feedback(task: dict) -> None:
    """把审阅问题作为建议写回下一次 Solver 上下文，不把它冒充运行证据。"""
    task["edit_feedback"] = (
        "A read-only Contract Auditor raised advisory requirement-bound questions. "
        "Inspect contract_audits in diagnostic_state. Address a concrete current-revision "
        "counterexample, stale contradictory evidence, source conflict, or source-level risk "
        "when a relevant bounded route exists. Missing current observations for requirements "
        "without public_check_nodeids does not by itself block candidate_ready: those "
        "final-acceptance-only requirements may remain unobserved and be submitted for "
        "independent final acceptance. If no current counterexample exists and no relevant "
        "bounded route remains, do not finish unresolved solely because final-only behavior is "
        "unobserved. The audit is advisory and is not execution evidence."
    )
    if task["protocol"].get("contract_audit_policy") in {"delivery-audit-v1", "delivery-audit-v2"}:
        task["edit_feedback"] = (
            "The read-only Contract Auditor raised advisory questions in diagnostic_state.contract_audits. "
            "Read the cited existing evidence and address concrete business-answer omissions, contradictions "
            "or relevant explanation errors. Use existing reads, topic maintenance and normal finish; do not "
            "invent a patch or test when evidence suffices. Unobserved final-only checks still go to separate "
            "independent acceptance. Audit suggestions are not evidence or acceptance."
        )


def _accept_contract_audit(task: dict, response: dict, case) -> dict:
    """保存只读建议及其完整身份；建议本身不是运行证据或终态决定。"""
    from .diagnostics import UnknownPublicEvidenceReference, resolve_refs, store
    from .generation.contract_auditor import ContractAuditError

    audit = response["audit"]
    requirement_ids = set(task["contract_requirements"]["requirement_ids"])
    questions = audit["questions"]
    unknown = sorted({item["requirement_id"] for item in questions} - requirement_ids)
    if unknown:
        raise ContractAuditError("contract_audit_invalid_requirement", "Unknown frozen requirement ID")
    refs = [ref for item in questions for ref in item["evidence_refs"]]
    try:
        resolve_refs(task, refs, response=response)
    except UnknownPublicEvidenceReference as error:
        raise ContractAuditError("contract_audit_invalid_reference", "Unknown public evidence reference") from error
    snapshot = load_candidate(case, task["current_candidate"])
    receipt_path = Path(response["report_path"])
    assert_no_links(receipt_path)
    receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    value = {
        "task_id": task["task_id"],
        "case_fingerprint": task["case_fingerprint"],
        "catalog": dict(task["contract_requirements"]),
        "revision": snapshot.revision,
        "patch_sha256": snapshot.sha256,
        "observation_ids": [item["id"] for item in task["observations"]],
        "request_sha256": response["request_sha256"],
        "receipt_path": str(receipt_path),
        "receipt_sha256": receipt_sha256,
        "summary": response["summary"],
        "audit": audit,
    }
    reference = store(Path(task["task_path"]).parent / "contract_audits", value)
    task["contract_audits"].append(reference)
    return value


def _contract_audit_protocol_feedback(code: str, attempt_index: int) -> dict:
    """Return only the fixed rejection code and exact preceding attempt identity."""
    return {"code": code, "attempt_index": attempt_index}


def _process_contract_audit_response(task: dict, response: dict, case) -> dict:
    """处理已收到的回复；只允许已知合同拒绝重试，收据与产物错误继续上抛。"""
    from .generation.contract_auditor import CONTRACT_AUDIT_RETRY_CODES, ContractAuditError

    pending = task["pending_contract_audit"]
    if pending.get("result") is not None:
        return pending["result"]
    attempt = task["attempts"][-1]
    if attempt.get("role") != "contract_auditor" or attempt["receipt"] != response["report_path"]:
        raise ValueError("Contract audit response does not match the current attempt")
    reason = response.get("reason", response["status"])
    if response["status"] == "audit_ready":
        try:
            audit = _accept_contract_audit(task, response, case)
        except ContractAuditError as error:
            reason = error.code
        else:
            pending["result"] = {"status": "accepted", "audit": audit}
    elif response["status"] != "provider_response_rejected":
        pending["result"] = {"status": "unavailable", "reason": reason}
    if pending.get("result") is None:
        if reason in CONTRACT_AUDIT_RETRY_CODES:
            if attempt.get("audit_rejection_code") is None:
                attempt["audit_rejection_code"] = reason
                pending["rejected_codes"].append(reason)
            elif attempt["audit_rejection_code"] != reason:
                raise ValueError("Contract audit rejection changed during response recovery")
            pending["protocol_feedback"] = _contract_audit_protocol_feedback(reason, len(task["attempts"]))
            if len(pending["rejected_codes"]) >= 2:
                pending["result"] = {
                    "status": "rejected", "reason": reason,
                    "rejected_codes": list(pending["rejected_codes"]),
                }
        else:
            pending["result"] = {"status": "unavailable", "reason": reason}
    task["status"] = "ready"
    return pending.get("result", {"status": "retry_pending"})


def _pending_audit_finish(task: dict) -> dict | None:
    """恢复只读取审阅前已经完成的 Solver 回复，不再次调用 Solver。"""
    pending = task["pending_contract_audit"]
    reference = pending.get("solver_finish")
    if reference is None:
        return None
    path = Path(reference["path"])
    assert_no_links(path)
    if (not path.resolve().is_relative_to(Path(task["work_root"]).resolve())
            or path.stat().st_size > 2_000_000):
        raise ValueError("Pending audit finish receipt is outside the task workspace")
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != reference["sha256"]:
        raise ValueError("Pending audit finish receipt changed")
    return json.loads(contents)


def _continue_after_contract_audit(task: dict, result: dict, case, finish_response=None) -> bool:
    """消费本次审阅的结果；只读质疑回到 Solver，已拒绝审阅不能静默通过。"""
    from .diagnostics import remember_action

    audit = result.get("audit")
    if audit is not None and audit["audit"]["questions"]:
        _set_contract_audit_feedback(task)
    elif finish_response is not None and result["status"] == "accepted":
        continuing = accept_response(task, finish_response, case)
        task.pop("pending_contract_audit", None)
        return continuing
    if finish_response is not None:
        remember_action(task, finish_response, case,
                        before_revision=load_candidate(case, task["current_candidate"]).revision)
        if result["status"] != "accepted":
            task["contract_audit_failure"] = {
                "status": result["status"], "reason": result["reason"],
                "solver_finish_receipt": finish_response["report_path"],
            }
            task.update(status="unresolved", stop_reason="required_contract_audit_not_accepted")
            task.pop("pending_contract_audit", None)
            return False
    task.pop("pending_contract_audit", None)
    return True


def _run_contract_audit(task: dict, case, ledger: BudgetLedger, *, api_key: str, transport,
                        finish_response: dict | None = None, reserve_solver_call: bool = False) -> dict:
    """审阅纠错最多拒绝两次；持久化预算和原始 finish，恢复不重发已完成请求。"""
    from .diagnostics import freeze_context

    protocol = task["protocol"]
    if "pending_contract_audit" not in task:
        finish = None
        if finish_response is not None:
            path = Path(finish_response["report_path"])
            assert_no_links(path)
            contents = path.read_bytes()
            if json.loads(contents) != {key: value for key, value in finish_response.items() if key != "role"}:
                raise ValueError("Solver finish differs from its persisted receipt")
            finish = {"path": str(path), "sha256": hashlib.sha256(contents).hexdigest()}
            # 合法 finish 已消费 Solver 上一次拒绝；审阅有自己的反馈，不能让旧编号跨角色残留。
            task["protocol_feedback"] = None
        task["pending_contract_audit"] = {
            "solver_finish": finish, "rejected_codes": [], "protocol_feedback": None,
            "reserved_solver_calls": int(reserve_solver_call),
        }
    pending = task["pending_contract_audit"]
    if pending.get("result") is not None:
        return pending["result"]
    # 停滞审阅的纠错也消耗总调用数；必须保留至少一次 Solver 处置机会，恢复后仍沿用此余额。
    audit_call_limit = protocol["max_calls"] - pending.get("reserved_solver_calls", 0)
    while len(task["attempts"]) < audit_call_limit and len(pending["rejected_codes"]) < 2:
        context = {
            "task_id": task["task_id"],
            "attempt_index": len(task["attempts"]) + 1,
            "remaining_calls": protocol["max_calls"] - len(task["attempts"]),
        }
        if pending["protocol_feedback"] is not None:
            context["protocol_feedback"] = pending["protocol_feedback"]
        prepared = prepare_case_proposal(
            Path(task["manifest_path"]),
            Path(task["work_root"]),
            public_source_ack=True,
            experiment_arm="full",
            output_format="contract_audit",
            candidate_reference=task["current_candidate"],
            task_context=context,
            source_policy=protocol["source_policy"],
            diagnostic_context_reference=freeze_context(task, role="contract_auditor"),
            **_proposal_comparison_options(task),
            **protocol["generation"],
        )
        reservation = ledger.reserve(prepared, task["phase"])
        attempt = {
            "receipt": prepared["report_path"],
            **reservation,
            "status": "reserved",
            "role": "contract_auditor",
            **(
                {"output_format": "contract_audit"}
                if task.get("schema_version") == 5
                else {}
            ),
        }
        if prepared.get("knowledge_review_binding") is not None:
            attempt.update(knowledge_review_binding=prepared["knowledge_review_binding"],
                           request_sha256=prepared["request_sha256"])
        for key in ("decision_objective_sha256", "pair_input_sha256"):
            if key in prepared:
                attempt[key] = prepared[key]
        if task.get("comparison_binding") is not None:
            attempt["comparison_binding_sha256"] = _digest(task["comparison_binding"])
            attempt["request_sha256"] = prepared["request_sha256"]
        task["attempts"].append(attempt)
        task["status"] = "calling_model"
        _save(task, Path(task["task_path"]))
        response = complete_request(prepared, api_key=api_key, transport=transport)
        attempt["status"] = response["status"]
        ledger.settle(reservation["request_id"], response.get("model_usage", {}))
        task["budget"] = ledger.snapshot()
        task["status"] = "processing_response"
        _save(task, Path(task["task_path"]))
        result = _process_contract_audit_response(task, response, case)
        _save(task, Path(task["task_path"]))
        if result["status"] != "retry_pending":
            return result
    task["status"] = "ready"
    pending["result"] = {
        "status": "rejected",
        "reason": pending["rejected_codes"][-1] if pending["rejected_codes"] else "contract_audit_call_budget_exhausted",
        "rejected_codes": list(pending["rejected_codes"]),
    }
    return pending["result"]


def _next_model_role(task: dict) -> str | None:
    if (task.get("knowledge_review_state") or {}).get("phase") == "review":
        return "solver"
    if task["protocol"].get("repository_preparation") is not None:
        from .generation.roles import preparation_contract

        config = task["protocol"]["repository_preparation"]
        state = task["preparation_state"]
        if state["status"] == "completed":
            return "solver"
        if (state["status"] == "exhausted" or len(state["attempt_indices"]) >= config["max_calls"]
                or task["protocol"]["max_calls"] - len(task["attempts"]) <= config["solver_reserved_calls"]):
            state["status"] = "exhausted"
            task.update(status="unresolved", stop_reason="repository_preparation_exhausted_without_handoff")
            return None
        return preparation_contract(config, "prepare")[0]
    """Protocol 6可串行运行一次独立调查；候选写入者始终只有Solver。"""
    if (
        task.get("schema_version") != 5
        or task["protocol"].get("investigator_policy") != "required_once"
    ):
        return "solver"
    active = next(
        (
            item
            for item in reversed(task.get("investigator_sessions", []))
            if item.get("status") == "active"
        ),
        None,
    )
    if active is not None:
        budget = task["protocol"]["role_budget"]
        remaining = task["protocol"]["max_calls"] - len(task["attempts"])
        if (
            len(active.get("attempt_indices", [])) >= budget["investigator_max_calls"]
            or remaining <= budget["solver_reserved_calls"]
        ):
            snapshot = load_candidate(load_case(Path(task["manifest_path"])), task["current_candidate"])
            active.update(
                status="budget_exhausted",
                end_revision=snapshot.revision,
                end_patch_sha256=snapshot.sha256,
                reason="required_investigator_handoff_missing",
            )
            from .generation.investigator_context import RESILIENT_POLICY

            if task["protocol"].get("investigator_context_policy") == RESILIENT_POLICY:
                # 辅助核查失败不冒充有效交接，也不剥夺主求解者的剩余处置机会。
                active.update(status="exhausted_without_handoff", failure={
                    "reason": "investigator_call_allowance_exhausted_without_valid_handoff",
                    "last_protocol_feedback": task.get("protocol_feedback"),
                    "last_edit_feedback": task.get("edit_feedback"),
                })
                task.update(protocol_feedback=None, edit_feedback=None)
                return "solver"
            task.update(
                status="unresolved",
                stop_reason="required_investigator_handoff_missing",
            )
            return None
        return "investigator"
    if task.get("investigator_sessions"):
        return "solver"
    if not task.get("attempts") and not task.get("historical_inputs"):
        # 普通新任务先让主Solver形成一个真实决策点；由历史截点重建的局部比较
        # 已经携带主上下文，因此可以直接进入独立调查。
        return "solver"
    remaining = task["protocol"]["max_calls"] - len(task["attempts"])
    if remaining <= task["protocol"]["role_budget"]["solver_reserved_calls"]:
        task["investigator_sessions"].append(
            {
                "id": _digest(
                    {"task_id": task["task_id"], "role": "investigator", "sequence": 1}
                ),
                "status": "not_started",
                "attempt_indices": [],
                "start_revision": load_candidate(
                    load_case(Path(task["manifest_path"])), task["current_candidate"]
                ).revision,
                "reason": "solver_call_reserve_reached",
            }
        )
        return "solver"
    snapshot = load_candidate(load_case(Path(task["manifest_path"])), task["current_candidate"])
    task["investigator_sessions"].append(
        {
            "id": _digest(
                {
                    "task_id": task["task_id"],
                    "role": "investigator",
                    "sequence": 1,
                }
            ),
            "status": "active",
            "attempt_indices": [],
            "start_revision": snapshot.revision,
            "start_patch_sha256": snapshot.sha256,
        }
    )
    return "investigator"


def _prepare_model_call(task: dict, role: str) -> tuple[dict, dict | None, str]:
    """构造并冻结本次请求；只准备文件，不预留费用或发送模型请求。"""
    protocol = task["protocol"]
    incremental = task.get("schema_version") in {2, 3, 4, 5}
    context = {
        "task_id": task["task_id"], "attempt_index": len(task["attempts"]) + 1,
        "remaining_calls": protocol["max_calls"] - len(task["attempts"]),
    }
    if task["candidate"]:
        candidate = task["candidate"]
        context["previous_candidate"] = ({"sha256": candidate["sha256"], "revision": candidate["revision"]} if incremental
                                         else {"sha256": candidate["sha256"], "edits": candidate["edits"]})
    if task["feedback"] is not None:
        if incremental and task.get("feedback_revision") != candidate["revision"]:
            raise ValueError("Stale feedback revision")
        context["feedback"] = task["feedback"]
    if incremental and task.get("edit_feedback"):
        context["edit_feedback"] = task["edit_feedback"]
    if role == "investigator":
        active = next(
            item for item in reversed(task["investigator_sessions"])
            if item["status"] == "active"
        )
        role_budget = protocol["role_budget"]
        remaining = min(
            role_budget["investigator_max_calls"] - len(active["attempt_indices"]),
            context["remaining_calls"] - role_budget["solver_reserved_calls"],
        )
        note = (
            f"INVESTIGATOR_CALL_BUDGET: {remaining} Investigator call(s) remain, "
            "including this call. Total task calls are a separate allowance. "
        )
        if remaining == 1:
            note += (
                "Return handoff now with available facts, limits and unresolved hypotheses; "
                "another tool action would leave no call to hand off. "
                "Do not invent evidence or claim acceptance to finish the investigation."
            )
        context["edit_feedback"] = _feedback_with_note(context.get("edit_feedback"), note)
    if (
        role == "solver"
        and
        incremental
        and protocol.get("investigation_policy") is None
        and context["remaining_calls"] == 1
        and (task.get("candidate") or {}).get("reviewed") is True
        and (task.get("feedback") or {}).get("status") == "passed"
    ):
        final_call = (
            "FINAL_CALL_FINISH_ONLY: exactly one model call remains and the current candidate "
            "is reviewed with passing public feedback. Do not submit another candidate or request "
            "another source/diagnostic action, because there would be no model call left to close it. "
            "Return a finish action now. finish.reason must be exactly candidate_ready or unresolved; "
            "put prose in explanation. "
            + (
                "Do not include contract_coverage in the simple tools workflow."
                if protocol.get("workflow_profile") == "simple_tools"
                else "Include the complete contract_coverage catalog."
            )
        )
        context["edit_feedback"] = _feedback_with_note(context.get("edit_feedback"), final_call)
    investigation = protocol.get("investigation_policy")
    if (investigation and role == "solver"
            and context["remaining_calls"] <= investigation["finish_notice_calls"]):
        context["edit_feedback"] = _feedback_with_note(context.get("edit_feedback"),
            f"CALL_BUDGET_NOTICE: {context['remaining_calls']} calls remain, including format "
            "corrections and finish. Budget exhaustion preserves candidates but is not repair failure "
            "or acceptance. Finish if evidence permits; otherwise use the remaining calls on the "
            "missing evidence or explain unresolved uncertainty. No automatic extra calls are granted.")
    if task.get("protocol_feedback") is not None:
        context["protocol_feedback"] = task["protocol_feedback"]
    if task["tool_results"]:
        context["tool_results"] = task["tool_results"][-3:]
    if protocol.get("repository_preparation") is not None:
        from .generation.preparation import phase

        if phase(task) == "solve" and protocol["repository_preparation"]["mode"] == "reader_then_solver" and not task["preparation_state"]["consumed_by"]:
            for key in ("tool_results", "edit_feedback", "protocol_feedback"):
                context.pop(key, None)
    diagnostic_options = {}
    diagnostic_context_reference = None
    if task.get("schema_version") in {3, 4, 5}:
        from .diagnostics import freeze_context

        # 原协议反馈绑定候选；新协议的原始/候选观察统一由不可变收据提供。
        context.pop("feedback", None)
        diagnostic_context_reference = freeze_context(task, role=role)
        diagnostic_options = {"source_policy": protocol["source_policy"],
                              "diagnostic_context_reference": diagnostic_context_reference}
    output_format = (
        "repository_context_actions"
        if protocol.get("repository_preparation") is not None and phase(task) == "prepare"
        else "investigator_actions"
        if task.get("schema_version") == 5 and role == "investigator"
        else "protocol_v6_actions"
        if task.get("schema_version") == 5
        else "contract_actions"
        if task.get("schema_version") == 4
        else "diagnostic_actions"
        if task.get("schema_version") == 3
        else "candidate_actions"
        if incremental
        else "agent_actions"
    )
    prepared = prepare_case_proposal(
        Path(task["manifest_path"]), Path(task["work_root"]),
        public_source_ack=True,
        experiment_arm=(({"source_only": "generic", "version_only": "no_ast"}.get(
                            protocol.get("context_assistance"), "full"))
                        if task.get("kind") == "operation" else
                        "no_ast" if task["arm"] in {"codemod_repair", "direct_repair"} else task["arm"]),
        output_format=output_format,
        **({"candidate_reference": task["current_candidate"]} if incremental else {}),
        task_context=context, **protocol["generation"], **diagnostic_options,
        workflow_profile=protocol.get("workflow_profile", "workbench"),
        **_proposal_comparison_options(task),
    )
    return prepared, diagnostic_context_reference, output_format


def _record_reserved_attempt(task: dict, prepared: dict, reservation: dict, *,
                             role: str, output_format: str,
                             diagnostic_context_reference: dict | None) -> dict:
    """将已预留请求绑定到本次任务；返回追加的同一个 attempt，供发送后记账。"""
    protocol = task["protocol"]
    attempt = {"receipt": prepared["report_path"], **reservation, "status": "reserved"}
    if "incomplete_response_scope" in prepared:
        attempt.update(incomplete_response_scope=prepared["incomplete_response_scope"],
                       request_sha256=prepared["request_sha256"])
    if protocol.get("repository_preparation") is not None:
        current_phase = prepared["preparation_binding"]["phase"]
        attempt.update(role=role, output_format=output_format, preparation_phase=current_phase,
                       preparation_binding=prepared["preparation_binding"], request_sha256=prepared["request_sha256"])
        state = task["preparation_state"]
        if current_phase == "prepare":
            state["attempt_indices"].append(len(task["attempts"]) + 1)
        elif state["consumed_by"] is None:
            state["consumed_by"] = {"request_id": reservation["request_id"],
                "request_sha256": prepared["request_sha256"], "context_reference": diagnostic_context_reference}
    if task.get("schema_version") == 5:
        attempt.update(role=role, output_format=output_format)
        if prepared.get("knowledge_review_binding") is not None:
            review_binding = prepared["knowledge_review_binding"]
            attempt.update(knowledge_review_binding=review_binding,
                           request_sha256=prepared["request_sha256"])
            review_state = task["knowledge_review_state"]
            if role == "solver" and review_binding["phase"] == "solve" and review_state["consumed_by"] is None:
                review_state["consumed_by"] = {"request_id": reservation["request_id"],
                    "request_sha256": prepared["request_sha256"],
                    "context_reference": diagnostic_context_reference}
        for key in ("decision_objective_sha256", "pair_input_sha256"):
            if key in prepared:
                attempt[key] = prepared[key]
        if task.get("comparison_binding") is not None:
            attempt["comparison_binding_sha256"] = _digest(task["comparison_binding"])
            attempt["request_sha256"] = prepared["request_sha256"]
    task["attempts"].append(attempt)
    if role == "investigator":
        session = next(
            item
            for item in reversed(task["investigator_sessions"])
            if item["status"] == "active"
        )
        session["attempt_indices"].append(len(task["attempts"]))
    if (
        role == "solver"
        and task.get("schema_version") == 5
        and diagnostic_context_reference is not None
    ):
        from .diagnostics import consume_investigator_handoffs

        consume_investigator_handoffs(task, diagnostic_context_reference)
    return attempt


def advance_task(
    task_path: Path, *, api_key: str = "", reviewed_sha256: str | None = None,
    reviewed_revision: str | None = None, review_only: bool = False,
    reviewed_tool: bool = False, seed_runner=run_official_tool,
    reviewer: str | None = None, review_note: str | None = None,
    reject_reason: str | None = None, transport=None, comparator=run_comparison,
    execution_owner: str | None = None,
    diagnostic_request_id: str | None = None, diagnostic_decision: str = "accept",
    probe_runner=None,
    dependency_query_runner=None,
    diagnostic_retry: bool = False,
) -> dict:
    """运行至下一次候选审阅或提交边界；从不自行打开最终验收。"""
    task_path = Path(task_path).absolute()
    assert_no_links(task_path)
    require_owner(inspect_task(task_path), execution_owner)
    # 明确不可能发送的密钥输入在建账之前拒绝，不留下未知费用预留。
    key_valid = not (
        not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096
        or any(ord(character) < 32 for character in api_key)
    )
    lock = task_path.with_suffix(".lock")
    assert_no_links(lock)
    # 必须先取得排他所有权才读写可变状态；另一个进程的 calling_model 不是崩溃。
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(task_path.parent.name)
    try:
        if task_path.stat().st_size > 2_000_000:
            raise ValueError("Task record exceeds its limit")
        task = inspect_task(task_path)
        require_owner(task, execution_owner)
        incremental = task.get("schema_version") in {2, 3, 4, 5}
        if not incremental and not key_valid:
            raise ValueError("A nonempty provider key without control characters is required")
        case = load_case(Path(task["manifest_path"]))
        if case.fingerprint != task["case_fingerprint"]:
            raise ValueError("Task case has changed")
        if diagnostic_retry:
            from .diagnostics import prepare_retry

            _require_current_protocol(task["protocol"])
            prepare_retry(task, diagnostic_request_id)
            _save(task, task_path)
            return task
        if task["status"] in TERMINAL:
            return task
        _require_current_protocol(task["protocol"])
        if task["status"] == "pending_dependency_query":
            from .diagnostics import run_pending_dependency_query

            run_pending_dependency_query(task, case, runner=dependency_query_runner)
            _save(task, task_path)
            return task
        if diagnostic_request_id is not None and (
            task["status"] != "pending_diagnostic_review"
            or diagnostic_request_id != task["pending_diagnostic"]["request"]["id"]
            or not reviewer or not reviewer.strip() or not review_note or not review_note.strip()
            or diagnostic_decision not in {"accept", "reject"}
        ):
            raise ValueError("Diagnostic review must bind the exact pending request")
        if task["status"] == "diagnosing":
            from .diagnostics import recover_diagnostic

            recover_diagnostic(task)
            _save(task, task_path)
            return task
        if reject_reason is not None and (
            reject_reason != "candidate_contract_rejected"
            or task["status"] != "pending_review" or reviewed_sha256 is None
        ):
            raise ValueError("Review rejection requires a pending exact-hash contract review")
        if task["status"] in {"calling_model", "processing_response", "seeding", "verifying"}:
            interrupted_status = "outcome_unknown" if task["status"] == "calling_model" else "interrupted"
            task.update(status=interrupted_status, stop_reason="Interrupted attempt boundary; reconcile existing receipt before continuation")
            _save(task, task_path)
            return task
        if task["status"] == "pending_review" and reviewed_sha256 is not None:
            candidate = task["candidate"]
            if incremental and reviewed_revision != candidate["revision"]:
                raise ValueError("Review must bind the exact current revision")
            if reviewed_sha256 != candidate["sha256"] or not reviewer or not review_note:
                raise ValueError("Review must bind the exact candidate hash and an explicit note")
            assert_no_links(Path(candidate["patch_path"]))
            if hashlib.sha256(Path(candidate["patch_path"]).read_bytes()).hexdigest() != reviewed_sha256:
                raise ValueError("Candidate changed after review")
        try:
            protocol = task["protocol"]
            ledger = BudgetLedger(Path(task["budget_path"]))
            prepared = None
            reservation = None
            if task["status"] == "pending_diagnostic_review":
                if diagnostic_request_id is not None:
                    from .diagnostics import review_diagnostic

                    review_diagnostic(task, case, request_id=diagnostic_request_id,
                        reviewer=reviewer, note=review_note, decision=diagnostic_decision,
                        comparator=comparator, **({"probe_runner": probe_runner} if probe_runner else {}))
                return task
            if incremental and task["seed_strategy"] == "official" and task["seed"] is None:
                if not reviewed_tool:
                    task["stop_reason"] = "static_tool_review_required"
                    return task
                task["status"] = "seeding"
                _save(task, task_path)
                seed = seed_runner(Path(task["manifest_path"]), Path(task["work_root"]) / "seeds", reviewed_tool=True)
                task["seed"] = seed
                if seed["status"] not in {"pending_candidate_safety_review", "no_change"}:
                    task.update(status="seed_failed", stop_reason="official_tool_failed_no_fallback")
                    return task
                if seed["case_fingerprint"] != case.fingerprint:
                    raise ValueError("Official tool result belongs to another case")
                patch_path = Path(seed["candidate_path"])
                assert_no_links(patch_path)
                if hashlib.sha256(patch_path.read_bytes()).hexdigest() != seed["candidate_sha256"]:
                    raise ValueError("Official tool patch changed before import")
                base = load_candidate(case)
                snapshot = (import_patch(case, base, patch_path, task_path.parent) if patch_path.stat().st_size else
                            publish_candidate(case, base, {}, task_path.parent / "empty-seed", origin="official_tool"))
                task["current_candidate"] = snapshot.reference
                task["candidate"] = {"revision": snapshot.revision, "sha256": snapshot.sha256,
                                     "patch_path": str(Path(snapshot.reference["path"]).with_name("candidate.patch")),
                                     "reviewed": False, "receipt": seed["baseline_path"], "origin": "official_tool"}
                task["candidate_history"].append(dict(task["candidate"]))
                task.update(status="pending_review", stop_reason="candidate_review_required")
                return task
            if task["status"] == "pending_review":
                candidate = task["candidate"]
                if reviewed_sha256 is None:
                    return task
                actual = candidate["sha256"]
                if reject_reason is not None:
                    task["review_history"].append({
                        "sha256": actual, "reviewer": reviewer, "note": review_note,
                        "decision": "rejected", "reason": reject_reason,
                        **({"revision": candidate["revision"]} if incremental else {}),
                    })
                    candidate["reviewed"] = False
                    candidate["review_decision"] = "rejected"
                    task.update(status="candidate_rejected", stop_reason=reject_reason)
                    return task
                task["review_history"].append({"sha256": actual, "reviewer": reviewer, "note": review_note,
                                              **({"revision": candidate["revision"]} if incremental else {})})
                candidate["reviewed"] = True
                task["status"] = "verifying"
                _save(task, task_path)
                if task["arm"] != "no_feedback":
                    comparison = comparator(
                        Path(task["manifest_path"]), Path(task["work_root"]),
                        candidate_patch=Path(candidate["patch_path"]) if Path(candidate["patch_path"]).stat().st_size else None,
                        candidate_origin=candidate.get("origin", "agent_candidate") if Path(candidate["patch_path"]).stat().st_size else None,
                        **({"expected_candidate_sha256": actual} if incremental and Path(candidate["patch_path"]).stat().st_size else {}),
                        check_group="feedback", **protocol.get("execution", {}),
                    )
                    if incremental:
                        # 比较器是共享边界：反馈不能只凭成功字符串接入另一个候选。
                        if comparison.get("case_fingerprint") != case.fingerprint:
                            raise ValueError("Feedback belongs to another case")
                        if Path(candidate["patch_path"]).stat().st_size and comparison.get("candidate", {}).get("supplied_sha256") != actual:
                            raise ValueError("Feedback belongs to another candidate")
                    task["feedback"] = _feedback(comparison, actual)
                    candidate["feedback_report"] = comparison["report_path"]
                    if incremental:
                        task["feedback_revision"] = candidate["revision"]
                    if task.get("schema_version") in {3, 4, 5}:
                        from .diagnostics import observe, store, summarize

                        public, dependencies = summarize(comparison)
                        report_path = Path(comparison["report_path"])
                        dependencies.append({"path": str(report_path), "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest()})
                        receipt = store(task_path.parent / "feedback_receipts", {
                            "task_id": task["task_id"], "revision": candidate["revision"],
                            "public": public, "dependencies": dependencies})
                        ref = observe(task, case, {"type": "candidate_feedback", "revision": candidate["revision"]},
                                      public, kind="public_checks", execution_reference=receipt)
                        task["latest_observation"] = ref["id"]
                    if task["feedback"]["status"] == "execution_incomplete":
                        task["status"] = "execution_incomplete"
                        return task
                task["status"] = "ready"
                task.pop("stop_reason", None)
            if review_only:
                return task
            if incremental and not key_valid:
                task["stop_reason"] = "provider_key_required_before_next_call"
                return task
            if task.get("pending_contract_audit") is not None:
                finish_response = _pending_audit_finish(task)
                audit_result = _run_contract_audit(task, case, ledger, api_key=api_key, transport=transport)
                if not _continue_after_contract_audit(task, audit_result, case, finish_response):
                    return task
                _save(task, task_path)
            while len(task["attempts"]) < protocol["max_calls"]:
                # 包括同一进程的下一次续答，也不得跨实现/提示版本静默继续。
                _require_current_protocol(protocol)
                role = _next_model_role(task)
                if role is None:
                    return task
                if role == "solver" and _stagnation_contract_audit_due(task, case):
                    audit_result = _run_contract_audit(
                        task,
                        case,
                        ledger,
                        api_key=api_key,
                        transport=transport,
                        reserve_solver_call=True,
                    )
                    _continue_after_contract_audit(task, audit_result, case)
                    _save(task, task_path)
                    if len(task["attempts"]) >= protocol["max_calls"]:
                        break
                prepared, diagnostic_context_reference, output_format = _prepare_model_call(task, role)
                reservation = ledger.reserve(prepared, task["phase"])
                attempt = _record_reserved_attempt(
                    task, prepared, reservation, role=role, output_format=output_format,
                    diagnostic_context_reference=diagnostic_context_reference,
                )
                # 先持久化发送身份，再联网；恢复流程不会盲目重发未知结果。
                task["status"] = "calling_model"
                _save(task, task_path)
                response = complete_request(prepared, api_key=api_key, transport=transport)
                if task.get("schema_version") == 5:
                    response["role"] = role
                if "transport_diagnostic" in response:
                    task["last_transport_diagnostic"] = response["transport_diagnostic"]
                attempt["status"] = response["status"]
                ledger.settle(reservation["request_id"], response.get("model_usage", {}))
                task["budget"] = ledger.snapshot()
                task["status"] = "processing_response"
                _save(task, task_path)
                if _contract_audit_due(task, response):
                    audit_result = _run_contract_audit(
                        task,
                        case,
                        ledger,
                        api_key=api_key,
                        transport=transport,
                        finish_response=response,
                    )
                    if not _continue_after_contract_audit(task, audit_result, case, response):
                        return task
                    _save(task, task_path)
                    continue
                if not accept_response(task, response, case):
                    return task
            task["status"] = ("budget_exhausted" if protocol.get("investigation_policy") else
                              "unresolved" if task.get("schema_version") in {3, 4, 5} else
                              "submitted" if task["candidate"] else "no_candidate")
            task["stop_reason"] = "call_limit"
            return task
        except BudgetExceeded as error:
            task.update(status="budget_exhausted", stop_reason=str(error))
            return task
        except KeyboardInterrupt:
            task.update(status="interrupted", stop_reason="No automatic replay; inspect receipts and budget")
            return task
        except Exception as error:
            from .generation.project_context import ContextCapacityError

            if isinstance(error, ContextCapacityError) and task["status"] != "calling_model":
                task.update(status="execution_incomplete", stop_reason=str(error),
                            context_capacity_failure=error.details)
                return task
            if (
                task["status"] == "calling_model" and prepared is not None and reservation is not None
                and not Path(prepared["report_path"]).with_name("attempt.json").exists()
            ):
                # complete_request 在独占 attempt 标记之前失败，按实现合同尚未联网。
                ledger.settle(reservation["request_id"], {"input_tokens": 0, "output_tokens": 0})
                task["attempts"][-1]["status"] = "not_sent"
                task["budget"] = ledger.snapshot()
                task.update(status="generation_failed", stop_reason=f"Provider request not sent: {type(error).__name__}")
                return task
            task.update(
                status="outcome_unknown" if task["status"] == "calling_model" else "execution_incomplete",
                stop_reason=f"Unhandled {type(error).__name__}; inspect recorded receipts, no automatic replay",
            )
            return task
        finally:
            _save(task, task_path)
    finally:
        lock.unlink()


def finalize_task(task_path: Path, *, comparator=run_comparison,
                  execution_owner: str | None = None) -> dict:
    """普通任务的独立验收；终态候选冻结后执行，不把结果返回模型。"""
    path = Path(task_path).absolute()
    require_owner(inspect_task(path), execution_owner)
    lock = path.with_suffix(".lock")
    assert_no_links(lock)
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(path.parent.name)
    try:
        task = inspect_task(path)
        require_owner(task, execution_owner)
        if task.get("kind") != "operation" or task["status"] not in TERMINAL:
            raise ValueError("Only terminal normal tasks can use task-finalize; experiments require batch-finalize")
        if task.get("schema_version") in {3, 4, 5}:
            from .diagnostics import finalize_subject

            return finalize_subject(task, comparator)
        candidate = task.get("candidate")
        if not candidate or not candidate["reviewed"] or not any(
            review.get("revision") == candidate["revision"] and review["sha256"] == candidate["sha256"]
            and review.get("reviewer") and review.get("note") and review.get("decision") != "rejected"
            for review in task["review_history"]
        ):
            raise ValueError("A hash- and revision-reviewed candidate is required")
        existing = task.get("final_result")
        if existing:
            result_path = Path(existing["path"])
            assert_no_links(result_path)
            if hashlib.sha256(result_path.read_bytes()).hexdigest() != existing["sha256"]:
                raise ValueError("Final result bytes changed")
            return task
        task["final_evaluation"] = "running"
        task["final_candidate"] = {"revision": candidate["revision"], "sha256": candidate["sha256"]}
        _save(task, path)
        patch = Path(candidate["patch_path"])
        comparison = comparator(Path(task["manifest_path"]), Path(task["work_root"]),
                                candidate_patch=patch if patch.stat().st_size else None,
                                candidate_origin=candidate["origin"] if patch.stat().st_size else None,
                                **({"expected_candidate_sha256": candidate["sha256"]} if patch.stat().st_size else {}),
                                check_group="all", **task["protocol"].get("execution", {}))
        result_path = Path(comparison["report_path"])
        assert_no_links(result_path)
        if (comparison.get("case_fingerprint") != task["case_fingerprint"]
            or comparison.get("check_group") != "all"
            or json.loads(result_path.read_bytes()) != comparison
            or (patch.stat().st_size and comparison.get("candidate", {}).get("supplied_sha256") != candidate["sha256"])):
            raise ValueError("Final comparison identity is invalid")
        task["final_result"] = {"path": str(result_path), "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                                "status": comparison["status"], "candidate_revision": candidate["revision"],
                                "candidate_sha256": candidate["sha256"]}
        task["final_evaluation"] = "completed"
        _save(task, path)
        return task
    finally:
        lock.unlink()
