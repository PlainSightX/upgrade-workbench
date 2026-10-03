"""用自有收据区分执行完成、全跳过及业务反例，不运行目标应用。"""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from upgrade_workbench import diagnostics
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.execution.docker import _apply_summary_record, _parse_summary_record
from upgrade_workbench.workflow import _fully_passed

ORACLE = {"requirement": "No rejected values", "subject": "rejected values",
          "exercise": "exercise supported inputs", "basis": "absolute", "operator": "eq", "expected": 0}


def _stage(root, name, *, skipped=False, after=None):
    root.mkdir(parents=True, exist_ok=True)
    counts = {"collected": 1, "passed": int(not skipped), "failed": 0, "errors": 0, "skipped": int(skipped)}
    record = {"nonce": name, "exit_code": 0, "tests": counts,
              "nodeids": ["test_probe.py::test_value"], "python_version": "3.12.10"}
    output = ""
    if after is not None:
        output += "UPGRADE_WORKBENCH_MEASUREMENT:" + json.dumps({"before": 0, "after": after, "path_completed": True}) + "\n"
    output += "UPGRADE_WORKBENCH_RESULT:" + json.dumps(record) + "\n"
    path = root / (name + ".stdout")
    path.write_text(output, encoding="utf-8")
    checked = _parse_summary_record(output, name, 0)
    assert checked is not None
    stage = {"status": "error", "expected_python": "3.12", "stdout_path": str(path),
             "cleanup": {key: {"ok": True} for key in ("container", "materialization_container", "snapshot_image")}}
    _apply_summary_record(stage, checked, "3.12", 0)
    return stage


def _report(root, *, skipped="new_original", candidate_after=1):
    return {"kind": "probe", "check_group": "probe", "status": "observed", "phase": "target_completed",
            "stages": {name: _stage(root, name, skipped=name == skipped,
                                    after=None if name == skipped else candidate_after if name == "new_candidate" else 0)
                       for name in ("old_original", "new_original", "new_candidate")}}


def test_completed_reference_skip_preserves_counterexample_and_phase(tmp_path):
    report = _report(tmp_path)
    skipped = report["stages"]["new_original"]
    assert skipped["status"] == "error" and not _fully_passed(skipped)
    assert skipped.get("execution_status") == "completed"
    assert skipped.get("test_outcome") == "all_skipped"
    # 人类原因文字不参与完整性判据。
    skipped["reason"] = "arbitrary translated diagnostic"
    public, _ = diagnostics.summarize(report, probe=True, oracle=ORACLE)
    assert public["status"] == "passed"
    assert public["assessment"]["conclusion"] == "counterexample_observed"
    assert public["assessment"]["measurements"]["new_original"]["conclusion"] == "inconclusive"
    assert public["stages"]["new_original"]["status"] == "error"
    assert public["stages"]["new_original"]["test_outcome"] == "all_skipped"
    assert public["stages"]["new_original"]["execution_status"] == "completed"
    assert diagnostics._execution_phase(report) == "target_completed"


@pytest.mark.parametrize("defect", [
    "no_completion", "no_outcome", "timeout", "exit", "python", "missing_python", "missing_cleanup",
    "empty_cleanup", "missing_resource", "cleanup_failed", "database_cleanup_failed", "no_tests",
    "partial_skip", "duplicate_nodeids", "boolean_count",
])
def test_incomplete_or_unbound_skip_never_bypasses_completion(tmp_path, defect):
    report = _report(tmp_path)
    stage = report["stages"]["new_original"]
    if defect == "no_completion":
        stage.pop("execution_status", None)
    elif defect == "no_outcome":
        stage.pop("test_outcome", None)
    elif defect == "timeout":
        stage["process_exit_status"] = "timeout_after_complete_result"
    elif defect == "exit":
        stage["exit_code"] = 1
    elif defect == "python":
        stage["python_version"] = "3.11.10"
    elif defect == "missing_python":
        stage.pop("expected_python")
    elif defect == "missing_cleanup":
        stage.pop("cleanup")
    elif defect == "empty_cleanup":
        stage["cleanup"] = {}
    elif defect == "missing_resource":
        stage["cleanup"].pop("snapshot_image")
    elif defect == "cleanup_failed":
        stage["cleanup"]["container"]["ok"] = False
    elif defect == "database_cleanup_failed":
        stage["cleanup"]["database"] = {"ok": False}
    elif defect == "no_tests":
        stage["tests"].update(collected=0, skipped=0)
        stage["nodeids"] = []
    elif defect == "partial_skip":
        stage["tests"]["collected"] = 2
    elif defect == "duplicate_nodeids":
        stage["tests"].update(collected=2, skipped=2)
        stage["nodeids"] *= 2
    elif defect == "boolean_count":
        stage["tests"]["skipped"] = True
    public, _ = diagnostics.summarize(report, probe=True, oracle=ORACLE)
    assert public["status"] == "execution_incomplete"
    assert diagnostics._execution_phase(report) != "target_completed"


def test_public_check_contract_does_not_gain_probe_completion_exception(tmp_path):
    report = _report(tmp_path)
    report.update(kind="public_checks", check_group="feedback")
    assert diagnostics.summarize(report)[0]["status"] == "execution_incomplete"
    assert diagnostics._execution_phase(report) != "target_completed"


@pytest.mark.parametrize("all_stages", [False, True])
def test_target_skip_is_complete_but_never_passed_or_coverage(tmp_path, all_stages):
    report = _report(tmp_path, skipped="new_candidate")
    names = report["stages"] if all_stages else ("new_candidate",)
    for name in names:
        # 跳过前打印恰好满足判据的数字，也不能制造业务支持。
        report["stages"][name] = _stage(tmp_path, name, skipped=True, after=0)
    public, _ = diagnostics.summarize(report, probe=True, oracle=ORACLE)
    assert public["status"] == "failed"
    assert public["assessment"]["conclusion"] == "inconclusive"
    assert diagnostics._execution_phase(report) == "target_completed"
    observation = {"id": "a" * 64, "revision": "r", "kind": "probe",
                   "action": {"requirement_id": "value.valid"}, "result": public}
    requirement = SimpleNamespace(id="value.valid", public_check_nodeids=[])
    states = diagnostics._contract_observation_states([observation], [requirement], "r")
    assert states["value.valid"]["support"] == []
    prior = copy.deepcopy(observation)
    prior["id"] = "b" * 64
    prior["result"]["assessment"]["conclusion"] = "counterexample_observed"
    decision = diagnostics.simple_finish_decision({"reason": "candidate_ready"}, [requirement], [prior, observation], "r")
    assert decision["accepted"] is False


def test_skipped_old_reference_cannot_validate_version_delta(tmp_path):
    public, _ = diagnostics.summarize(_report(tmp_path, skipped="old_original", candidate_after=0),
                                      probe=True, oracle=ORACLE | {"basis": "version_delta"})
    assert public["status"] == "passed"
    assert public["assessment"]["conclusion"] == "inconclusive"
    assert public["assessment"]["measurements"]["new_candidate"]["reason"] == "old_comparator_not_valid"


def test_completed_probe_receipt_returns_ready_and_preserves_counterexample(owned_case, tmp_path):
    root = tmp_path / "runtime"
    report = _report(root)
    public, dependencies = diagnostics.summarize(report, probe=True, oracle=ORACLE)
    snapshot = load_candidate(owned_case)
    task = {"task_id": "owned-probe", "task_path": str(root / "task.json"), "work_root": str(root),
            "manifest_path": str(owned_case.manifest_path), "case_fingerprint": owned_case.fingerprint,
            "current_candidate": None, "observations": [], "protocol": {"diagnostic_policy": {}}, "status": "diagnosing"}
    request = diagnostics.store(root / "requests", {"source_reference": None, "kind": "probe"})
    report_path = root / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    dependencies.append({"path": str(report_path), "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest()})
    review = {"reviewer": "unit-test", "decision": "accept"}
    receipt = diagnostics.store(root / "completed", {"task_id": task["task_id"], "revision": snapshot.revision,
        "request": request, "review": review, "public": public, "dependencies": dependencies,
        "phase": diagnostics._execution_phase(report), "last_action": "target_completed"})
    run = {"request": request, "review": review, "result": receipt, "directory": str(root),
           "action": {"type": "run_probe", "requirement_id": "value.valid"}}
    task["pending_diagnostic"] = run
    diagnostics.complete_run(task, owned_case, run)
    assert task["status"] == "ready" and task["pending_diagnostic"] is None
    assert run["status"] == "completed" and run["last_phase"] == "target_completed"
    observation = diagnostics.public_observation(task, run["observation"])
    assert observation["result"]["assessment"]["conclusion"] == "counterexample_observed"
    assert "failure" not in run
