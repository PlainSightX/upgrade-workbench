"""两条准备路线均走真实冻结请求；离线响应不计为迁移成果。"""

import copy
import json
from pathlib import Path

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import accept_action, public_knowledge, read, store
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.generation.request_evidence import RequestEvidenceError, visible_version_refs
from upgrade_workbench.generation.roles import recoverable_attempt_contract
from upgrade_workbench.service.recovery import recover_task

from .test_project_context import CASE, OWNER, pages, response
from .test_project_context import task as task

CAPACITY = {"version": "generation-capacity-v1", "max_response_bytes": 4 * 1024 * 1024}


@pytest.fixture(params=["same_solver", "reader_then_solver"])
def prepared_task(task, request):
    protocol = copy.deepcopy(task["protocol"])
    protocol["repository_preparation"] = {"version": 1, "mode": request.param,
        "max_calls": 2, "solver_reserved_calls": 6}
    protocol["generation"].update(runtime_capacity=CAPACITY, max_output_tokens=131072, timeout_seconds=1200)
    return tasks.create_task(CASE, Path(task["work_root"]), protocol, arm="direct_repair",
        phase="operation", budget_path=Path(task["budget_path"]), kind="operation", service_owner=OWNER)


def advance(task, transport):
    return tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport, execution_owner=OWNER)


def context(request):
    return json.loads(json.loads(request.data)["messages"][1]["content"])


def test_prepare_handoff_history_and_single_writer(prepared_task):
    seen = []
    def transport(request, **kwargs):
        ctx = context(request)
        seen.append(ctx)
        assert kwargs == {"timeout_seconds": 1200, "max_response_bytes": 4194304}
        assert "contract_requirements" not in ctx
        rev = ctx["candidate"]["revision"]
        phase = ctx["diagnostic_state"]["preparation_binding"]["phase"]
        if len(seen) == 1:
            assert phase == "prepare"
            return response({"type": "read_source", "revision": rev, "path": "copier/user_data.py", "start_line": 1, "end_line": 10})
        if len(seen) == 2:
            return response({"type": "record_project_knowledge", "revision": rev, "pages": pages(),
                "issues": [{"hypothesis": "Reader-only hypothesis", "evidence_refs": ["business_contract"],
                    "unknown": "Runtime", "next_observation": "Check later"}]})
        assert phase == "solve"
        state = ctx["diagnostic_state"]
        assert state["project_knowledge"]["pages"][1]["id"] == "answers-flow"
        assert ctx["source_selection"]["omitted_file_count"] == 0
        separated = prepared_task["protocol"]["repository_preparation"]["mode"] == "reader_then_solver"
        assert bool(state["recent_actions"]) is not separated
        assert bool(state["issues"]) is not separated
        assert bool(state["retained_source_reads"]) is not separated
        return response({"type": "submit_candidate", "base_revision": rev,
            "edits": [{"path": "copier/main.py", "old": "import platform", "new": "import platform  # fixture"}]})
    result = advance(prepared_task, transport)
    assert result["status"] == "pending_review", result.get("stop_reason")
    assert len(result["candidate_history"]) == 1
    state = result["preparation_state"]
    assert state["status"] == "completed" and state["consumed_by"]
    assert state["attempt_indices"] == [1, 2]
    expected = "repository_reader" if result["protocol"]["repository_preparation"]["mode"] == "reader_then_solver" else "solver"
    assert [a["role"] for a in result["attempts"]] == [expected, expected, "solver"]
    for a in result["attempts"]:
        report = json.loads(Path(a["receipt"]).read_text(encoding="utf-8"))
        if report["status"] == "pending_review":
            report["candidate_reference"] = None
            report["candidate_revision"] = load_candidate(load_case(CASE)).revision
        _verify_payload(report, Path(report["request_path"]).read_bytes())
    public_knowledge(result, state["handoff_reference"])


@pytest.mark.parametrize("kind", ["run_public_checks", "propose_probe", "revise_probe", "run_probe", "submit_candidate", "restore_candidate", "finish"])
def test_preparation_denies_runtime_and_writes(prepared_task, kind):
    result = advance(prepared_task, lambda request, **kw: response({"type": kind}))
    assert result["status"] == "unresolved", result.get("stop_reason")
    assert result["stop_reason"] == "repository_preparation_exhausted_without_handoff"
    assert result["candidate"] is None and result["pending_diagnostic"] is None
    assert not result["probes"] and not result["diagnostic_runs"]
    assert len(result["attempts"]) == 2
    report = json.loads(Path(result["attempts"][0]["receipt"]).read_text(encoding="utf-8"))
    assert report["reason"] == "role_write_forbidden"
    # 宿主入口独立拒绝，不能只依靠provider先挡掉越权。
    before = copy.deepcopy(result)
    with pytest.raises(ValueError, match="static"):
        accept_action(result, report | {"action": {"type": kind}}, load_case(CASE))
    assert result == before


@pytest.mark.parametrize("settled", [False, True])
def test_handoff_recovery_and_identity(prepared_task, monkeypatch, settled):
    class Crash(BaseException):
        pass
    original = tasks.complete_request
    calls = []
    def crash(prepared, **kwargs):
        result = original(prepared, **kwargs)
        if settled:
            from upgrade_workbench.budget import BudgetLedger
            BudgetLedger(Path(prepared_task["budget_path"])).settle(Path(prepared["report_path"]).parent.name, result["model_usage"])
        raise Crash()
    def transport(request, **kw):
        calls.append(1)
        return response({"type": "record_project_knowledge", "revision": context(request)["candidate"]["revision"], "pages": pages()})
    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        advance(prepared_task, transport)
    path = Path(prepared_task["task_path"])
    interrupted = tasks.inspect_task(path)
    for field in ("role", "output_format", "preparation_phase", "request_sha256"):
        attempt = dict(interrupted["attempts"][0])
        attempt.pop(field)
        with pytest.raises(ValueError):
            recoverable_attempt_contract(interrupted, attempt)
    result = recover_task(path, Path(prepared_task["work_root"]), OWNER)
    assert result["status"] == "ready" and result["preparation_state"]["status"] == "completed"
    assert result["budget"]["calls"] == 1 and len(calls) == 1
    assert len(result["observations"]) == 1
    assert recover_task(path, Path(prepared_task["work_root"]), OWNER) == result
    # 已消费交接、但还停在processing_response的检查点也必须恢复为可推进状态。
    result["status"] = "processing_response"
    tasks._save(result, path)
    result = recover_task(path, Path(prepared_task["work_root"]), OWNER)
    assert result["status"] == "ready" and len(result["observations"]) == 1
    report = json.loads(Path(result["attempts"][0]["receipt"]).read_text(encoding="utf-8"))
    assert tasks.accept_response(result, report, load_case(CASE)) is True
    assert len(result["observations"]) == 1
    value = read(result["preparation_state"]["handoff_reference"], path.parent)
    assert value["author"] == result["attempts"][0]["role"]
    forged = copy.deepcopy(result)
    forged["task_id"] = "f" * 32
    with pytest.raises(ValueError):
        public_knowledge(forged, result["preparation_state"]["handoff_reference"])
    # 新身份不能通过删掉binding退化为legacy Solver作者。
    frozen = read(report["diagnostic_context_reference"], path.parent)
    frozen.pop("preparation_binding")
    frozen["context_role"] = "solver"
    report["diagnostic_context_reference"] = store(path.parent / "contexts", frozen)
    data = json.dumps(report).encode()
    Path(report["report_path"]).write_bytes(data)
    import hashlib
    value["receipt_sha256"] = hashlib.sha256(data).hexdigest()
    forged_ref = store(path.parent / "project_knowledge", value)
    with pytest.raises(ValueError, match="downgrade"):
        public_knowledge(result, forged_ref)


def test_preparation_cannot_use_official_seed(prepared_task):
    with pytest.raises(ValueError, match="original source"):
        tasks.create_task(CASE, Path(prepared_task["work_root"]), prepared_task["protocol"],
            arm="codemod_repair", phase="operation", budget_path=Path(prepared_task["budget_path"]), kind="operation")


def version_action(request, *, prepare):
    ctx = context(request)
    issue = {"hypothesis": "Check version behavior",
        "evidence_refs": ["version:" + ctx["version_evidence"][0]["evidence_key"]],
        "unknown": "Runtime", "next_observation": "Run public checks after handoff"}
    action = {"type": "record_project_knowledge" if prepare else "run_public_checks",
        "revision": ctx["candidate"]["revision"], "issues": [issue]}
    if prepare:
        action["pages"] = pages()
    return response(action)


def test_version_evidence_survives_real_preparation_to_solver(prepared_task):
    calls = []
    def transport(request, **kw):
        calls.append(1)
        return version_action(request, prepare=len(calls) == 1)
    result = advance(prepared_task, transport)
    assert result["status"] == "pending_diagnostic_review", result.get("stop_reason")
    assert len(calls) == 2
    report = json.loads(Path(result["attempts"][-1]["receipt"]).read_text(encoding="utf-8"))
    assert report["role"] == "solver"
    assert visible_version_refs(result, report)
    for mutation in ({"role": "repository_reader"}, {"output_format": "repository_context_actions"}):
        with pytest.raises(RequestEvidenceError):
            visible_version_refs(result, report | mutation)
    missing = dict(report)
    missing.pop("role")
    with pytest.raises(RequestEvidenceError):
        visible_version_refs(result, missing)
    forged = copy.deepcopy(result)
    forged["attempts"][-1]["request_sha256"] = "0" * 64
    with pytest.raises(RequestEvidenceError):
        visible_version_refs(forged, report)


def test_legacy_version_evidence_allows_only_correct_transient_role(task):
    calls = []
    def transport(request, **kw):
        calls.append(1)
        return version_action(request, prepare=len(calls) == 1)
    result = advance(task, transport)
    assert result["status"] == "pending_diagnostic_review", result.get("stop_reason")
    report = json.loads(Path(result["attempts"][-1]["receipt"]).read_text(encoding="utf-8"))
    assert "role" not in report
    assert visible_version_refs(result, report)
    assert visible_version_refs(result, report | {"role": "solver"})
    with pytest.raises(RequestEvidenceError, match="role"):
        visible_version_refs(result, report | {"role": "repository_reader"})


def test_unknown_result_is_not_replayed(prepared_task):
    calls = []
    def transport(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()
    result = advance(prepared_task, transport)
    assert result["status"] == "outcome_unknown"
    assert recover_task(Path(result["task_path"]), Path(result["work_root"]), OWNER)["status"] == "outcome_unknown"
    assert len(calls) == 1
