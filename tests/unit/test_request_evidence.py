"""请求可见证据的任务/版本绑定；全部离线，不执行目标库或发送API请求。"""

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from test_diagnostic_operations import CASE, response
from test_diagnostic_operations import task as task

from upgrade_workbench import tasks
from upgrade_workbench.candidates import apply_increment, load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import resolve_refs
from upgrade_workbench.generation.request_evidence import RequestEvidenceError, visible_version_refs
from upgrade_workbench.service.recovery import recover_task


def action_for(context, kind="finish", ref=None):
    ref = ref or next("version:" + item["evidence_key"] for item in context["version_evidence"]
                     if item["evidence_key"].startswith("retrieved-"))
    issue = {"hypothesis": "migration behavior needs checking", "evidence_refs": [ref],
             "unknown": "runtime behavior", "next_observation": "public checks"}
    if kind == "finish":
        return {"type": kind, "reason": "unresolved", "explanation": "offline scope", "evidence_refs": [ref],
                "contract_coverage": [{
                    "requirement": "Retrieved evidence remains bound to this request",
                    "scope": "in_contract", "status": "verified",
                    "evidence_refs": [ref], "tool_limitation": None,
                }]}
    if kind == "submit_candidate":
        block = next(b for b in context["source_files"] if b["path"] in context["allowed_changes"])
        return {"type": kind, "base_revision": context["candidate"]["revision"], "issues": [issue],
                "edits": [{"path": block["path"], "old": block["text"], "new": "# 离线候选\n" + block["text"]}]}
    if kind == "propose_probe":
        return {"type": kind, "revision": context["candidate"]["revision"], "purpose": "offline observation",
                "code": "def test_observe():\n    print('offline')\n", "expected_observation": "local path only",
                "evidence_refs": [ref], "oracle": None}
    return {"type": kind, "revision": context["candidate"]["revision"], "issues": [issue]}


def run(task, kind="finish"):
    def transport(request, **_kwargs):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        return response(action_for(context, kind))
    return tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)


def receipt_of(task):
    return json.loads(Path(task["attempts"][-1]["receipt"]).read_bytes())


@pytest.mark.parametrize("kind,status", [("run_public_checks", "pending_diagnostic_review"),
    ("propose_probe", "pending_diagnostic_review"), ("submit_candidate", "pending_review"), ("finish", "unresolved")])
def test_retrieved_refs_flow_through_normal_actions(task, kind, status):
    result = run(task, kind)
    assert result["status"] == status, result.get("stop_reason", result.get("edit_feedback"))
    assert len(result["attempts"]) == 1 and not result["diagnostic_runs"]
    assert result["final_evaluation"] == "not_run"
    if kind == "submit_candidate":
        assert not result["candidate"]["reviewed"]
        receipt = receipt_of(result)
        assert receipt["base_revision"] != receipt["candidate_revision"]
        assert len(result["candidate_history"]) == 1


def test_valid_ref_does_not_bypass_success_exit_gate(task):
    calls = []
    def transport(request, **_kwargs):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        action = action_for(context)
        if not calls:
            action["reason"] = "no_change_claimed"
        else:
            assert "completed current public checks" in context["task_context"]["edit_feedback"]
        calls.append(action)
        return response(action)
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert result["status"] == "unresolved" and len(calls) == 2
    assert result["final_evaluation"] == "not_run"


def test_forged_key_rejected_without_changing_task(task):
    done = run(task)
    before = copy.deepcopy(done)
    with pytest.raises(ValueError, match="Unknown public evidence"):
        resolve_refs(done, ["version:retrieved-forged"], response=receipt_of(done))
    assert done == before
    with pytest.raises(RequestEvidenceError, match="bound response"):
        resolve_refs(done, ["version:pydantic-v2-base-model"])


def test_schema_5_request_evidence_rejects_implicit_attempt_role(task):
    done = run(task)
    receipt = receipt_of(done)
    done["schema_version"] = 5

    with pytest.raises(RequestEvidenceError, match="attempt role and output_format"):
        visible_version_refs(done, receipt)


def test_request_evidence_rejects_explicit_cross_role_format(task):
    done = run(task)
    receipt = receipt_of(done)
    done["attempts"][-1]["role"] = "solver"
    done["attempts"][-1]["output_format"] = "contract_audit"

    with pytest.raises(RequestEvidenceError, match="requires output_format"):
        visible_version_refs(done, receipt)


def test_source_only_cannot_cite_even_registered_but_unprovided_evidence(task, tmp_path):
    plain = tasks.create_operation(CASE, tmp_path / "plain", budget_path=Path(task["budget_path"]),
        seed_strategy="none", protocol_revision=4, generation=task["protocol"]["generation"],
        context_assistance="source_only", max_calls=2)
    seen = []
    def transport(request, **_kwargs):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        assert context["version_evidence"] == []
        if seen:
            assert "Unknown public evidence" in context["task_context"]["edit_feedback"]
        action = {"type": "finish", "reason": "unresolved", "explanation": "offline",
                  "evidence_refs": ["version:pydantic-v2-base-model"] if not seen else ["business_contract"],
                  "contract_coverage": [{
                      "requirement": "Offline evidence binding boundary",
                      "scope": "in_contract", "status": "verified",
                      "evidence_refs": ["business_contract"], "tool_limitation": None,
                  }]}
        seen.append(action)
        return response(action)
    done = tasks.advance_task(Path(plain["task_path"]), api_key="unused", transport=transport)
    assert done["status"] == "unresolved" and len(seen) == 2
    assert not visible_version_refs(done, receipt_of(done))


@pytest.mark.parametrize("mutation", ["task", "attempt", "receipt", "manifest", "task_directory", "marker"])
def test_wrong_identity_is_integrity_error_not_retryable_model_error(task, mutation):
    done = run(task)
    receipt = receipt_of(done)
    if mutation == "task":
        done["task_id"] = "another-task"
    elif mutation == "attempt":
        done["attempts"].append(copy.deepcopy(done["attempts"][-1]))
    elif mutation == "receipt":
        done["attempts"][-1]["receipt"] = str(Path(receipt["report_path"]).with_name("other.json"))
    elif mutation == "manifest":
        done["manifest_path"] = str(Path(done["manifest_path"]).with_name("another.json"))
    elif mutation == "task_directory":
        done["task_path"] = str(Path(done["task_path"]).parent.parent / "other/task.json")
    else:
        Path(receipt["report_path"]).with_name("attempt.json").write_text('{}')
    with pytest.raises(RequestEvidenceError):
        visible_version_refs(done, receipt)


@pytest.mark.parametrize("rehash", [False, True])
def test_forged_request_excerpt_rejected_even_when_local_hashes_rewritten(task, rehash):
    done = run(task)
    receipt = receipt_of(done)
    path = Path(receipt["request_path"])
    payload = json.loads(path.read_bytes())
    context = json.loads(payload["messages"][1]["content"])
    context["version_evidence"][0]["text"] = "Fabricated migration instruction"
    payload["messages"][1]["content"] = json.dumps(context)
    path.write_text(json.dumps(payload), encoding="utf-8")
    if rehash:
        from upgrade_workbench.generation.request import _json_bytes

        receipt["request_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt["public_context_sha256"] = hashlib.sha256(_json_bytes(context)).hexdigest()
        Path(receipt["report_path"]).write_text(json.dumps(receipt), encoding="utf-8")
        Path(receipt["report_path"]).with_name("attempt.json").write_text(
            json.dumps({"request_sha256": receipt["request_sha256"], "calls": 1}))
    with pytest.raises(RequestEvidenceError):
        visible_version_refs(done, receipt)


def test_source_revision_change_rejected(task, tmp_path):
    done = run(task)
    receipt = receipt_of(done)
    case = load_case(CASE)
    original = load_candidate(case)
    name = case.manifest.allowed_changes[0]
    text = original.files[name].decode()
    changed = apply_increment(case, original, {"type": "submit_candidate", "base_revision": original.revision,
        "edits": [{"path": name, "old": text, "new": "# 离线改动\n" + text}]}, tmp_path / "other-revision")
    done["current_candidate"] = changed.reference
    with pytest.raises(RequestEvidenceError, match="different source revision"):
        visible_version_refs(done, receipt)


def test_changed_registered_document_rejected(task, tmp_path):
    copied = tmp_path / "copied-case"
    shutil.copytree(CASE.parent, copied)
    local = tasks.create_operation(copied / "manifest.json", tmp_path / "local-work",
        budget_path=Path(task["budget_path"]), seed_strategy="none", protocol_revision=4,
        generation=task["protocol"]["generation"])
    done = run(local)
    receipt = receipt_of(done)
    context = json.loads(json.loads(Path(receipt["request_path"]).read_bytes())["messages"][1]["content"])
    path = copied / context["version_evidence"][0]["path"]
    path.write_bytes(path.read_bytes() + b"changed source")
    with pytest.raises(RequestEvidenceError):
        visible_version_refs(done, receipt)


def test_corrupt_receipt_stops_normal_loop_without_another_call(task, monkeypatch):
    complete = tasks.complete_request
    calls = []
    def transport(request, **_kwargs):
        calls.append(1)
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        return response(action_for(context))
    def corrupt(*args, **kwargs):
        receipt = complete(*args, **kwargs)
        path = Path(receipt["request_path"])
        path.write_bytes(path.read_bytes() + b" ")
        return receipt
    monkeypatch.setattr(tasks, "complete_request", corrupt)
    result = tasks.advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert result["status"] == "execution_incomplete" and len(calls) == len(result["attempts"]) == 1
    assert "RequestEvidenceError" in result["stop_reason"]
    assert not result["candidate_history"] and result["final_evaluation"] == "not_run"


@pytest.mark.parametrize("kind,status", [("submit_candidate", "pending_review"), ("finish", "unresolved")])
def test_retrieved_reference_recovery_does_not_resend(task, tmp_path, monkeypatch, kind, status):
    owner = "a" * 32
    local = tasks.create_operation(CASE, tmp_path / "owned-work", budget_path=Path(task["budget_path"]),
        seed_strategy="none", protocol_revision=4, generation=task["protocol"]["generation"], service_owner=owner)
    class Crash(BaseException):
        pass
    complete = tasks.complete_request
    calls = []
    def transport(request, **_kwargs):
        calls.append(1)
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        return response(action_for(context, kind))
    def crash(*args, **kwargs):
        complete(*args, **kwargs)
        raise Crash()
    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        tasks.advance_task(Path(local["task_path"]), api_key="unused", transport=transport, execution_owner=owner)
    recovered = recover_task(Path(local["task_path"]), Path(local["work_root"]), owner)
    assert recovered["status"] == status and len(recovered["attempts"]) == len(calls) == 1
    assert recovered["budget"]["groups"][0]["state"] == "settled"
    assert recover_task(Path(local["task_path"]), Path(local["work_root"]), owner) == recovered
