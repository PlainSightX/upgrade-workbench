"""仅观察的可用性与证据边界；合成反馈不能充当真实迁移成绩。"""

import json
from pathlib import Path

import pytest
from test_diagnostic_operations import (
    CASE,
    coverage,
    measured_run,
    probe_action,
    request_public,
    review,
    revision_action,
    run_action,
)
from test_diagnostic_operations import (
    task as task,
)

from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import (
    accept_action,
    context_from_reference,
    freeze_context,
    read,
    recover_diagnostic,
    summarize,
)
from upgrade_workbench.generation.protocol_v4 import validate
from upgrade_workbench.reporting import export_task_report
from upgrade_workbench.tasks import finalize_task


def context(task):
    case = load_case(CASE)
    return context_from_reference(freeze_context(task), case, load_candidate(case, task["current_candidate"]))


@pytest.mark.parametrize("kind,missing,extra", [
    ("propose_probe", "code", "script"),
    ("revise_probe", "code", "script"),
    ("propose_probe", "oracle", "predicate"),
    ("run_probe", "probe_id", "id"),
    ("read_source", "path", "file"),
    ("submit_candidate", "edits", "patch"),
])
def test_action_fields_identify_exact_correction_before_execution(task, kind, missing, extra):
    case = load_case(CASE)
    snapshot = load_candidate(case)
    actions = {
        "propose_probe": probe_action(task),
        "revise_probe": revision_action(task, "a" * 64),
        "run_probe": {"type": "run_probe", "revision": snapshot.revision, "probe_id": "a" * 64},
        "read_source": {"type": "read_source", "revision": snapshot.revision, "path": "bumpversion/config.py",
                        "start_line": 1, "end_line": 5},
        "submit_candidate": {"type": "submit_candidate", "base_revision": snapshot.revision, "edits": []},
    }
    action = actions[kind]
    action[extra] = action.pop(missing)
    with pytest.raises(ValueError) as error:
        validate(case, action, snapshot)
    text = str(error.value)
    assert f"missing=['{missing}']" in text and f"extra=['{extra}']" in text
    assert kind in text and "action not executed" in text and len(text) <= 1000
    assert task["probes"] == [] and task["candidate"] is None


def test_unknown_action_is_bounded_and_lists_supported_types(task):
    case = load_case(CASE)
    with pytest.raises(ValueError) as error:
        validate(case, {"type": "run_shell" + "x" * 5000}, load_candidate(case))
    assert "action not executed" in str(error.value)
    assert "propose_probe" in str(error.value) and len(str(error.value)) <= 1000


def test_explicit_null_is_not_an_omitted_or_invalid_oracle(task):
    case = load_case(CASE)
    snapshot = load_candidate(case)
    action = probe_action(task, oracle=None)
    assert validate(case, action, snapshot) == action
    del action["oracle"]
    with pytest.raises(ValueError, match="missing=\\['oracle'\\]"):
        validate(case, action, snapshot)
    for invalid in ({}, False, "none"):
        with pytest.raises(ValueError):
            validate(case, action | {"oracle": invalid}, snapshot)


def test_probe_requires_a_pytest_collected_entrypoint(task):
    case = load_case(CASE)
    snapshot = load_candidate(case)
    action = probe_action(
        task,
        code='print("UPGRADE_WORKBENCH_MEASUREMENT:{}")\n',
    )

    with pytest.raises(ValueError, match="pytest-collected test_\\*"):
        validate(case, action, snapshot)

    class_action = probe_action(
        task,
        code="class TestObservation:\n    def test_value(self):\n        assert True\n",
    )
    assert validate(case, class_action, snapshot) == class_action


@pytest.mark.parametrize("length", [2040, 3129, 3301])
def test_oversized_finish_explains_the_field_limit_without_echoing_content(task, length):
    case = load_case(CASE)
    with pytest.raises(ValueError) as error:
        validate(case, {"type": "finish", "reason": "unresolved", "explanation": "x" * length,
                        "evidence_refs": [], "contract_coverage": coverage()}, load_candidate(case))
    text = str(error.value)
    assert "finish.explanation" in text and "maximum 2000 characters" in text
    assert f"length={length}" in text and "action not executed" in text
    assert "x" * 20 not in text


def test_oversized_reference_list_gives_count_and_does_not_execute(task):
    case = load_case(CASE)
    with pytest.raises(ValueError, match="actual_count=9"):
        validate(case, {"type": "finish", "reason": "unresolved", "explanation": "bounded",
                        "evidence_refs": ["business_contract"] * 9,
                        "contract_coverage": coverage()}, load_candidate(case))


def test_finish_requires_structured_contract_coverage(task):
    case = load_case(CASE)
    snapshot = load_candidate(case)
    action = {"type": "finish", "reason": "unresolved", "explanation": "bounded",
              "evidence_refs": []}
    with pytest.raises(ValueError, match="missing=\\['contract_coverage'\\]"):
        validate(case, action, snapshot)
    invalid = action | {"contract_coverage": [{
        "requirement": "Hidden records stay hidden", "scope": "in_contract", "status": "verified",
        "evidence_refs": [], "tool_limitation": None,
    }]}
    with pytest.raises(ValueError, match="needs evidence_refs"):
        validate(case, invalid, snapshot)


@pytest.mark.parametrize("value,completed", [(0, True), (4, True), (0, False)])
def test_null_probe_never_infers_business_success_from_pytest_or_marker(task, value, completed):
    task = review(run_action(task, probe_action(task, oracle=None)),
                  probe_runner=lambda *args: measured_run(*args, value=value, completed=completed))
    result = context(task)["observations"][-1]["result"]
    assert result["status"] == "passed"
    assert result["assessment"]["conclusion"] == "observation_only"
    assert "measurements" not in result["assessment"]
    assert result["assessment"]["oracle"] is None
    assert "UPGRADE_WORKBENCH_MEASUREMENT" in result["stages"]["new_original"]["output_excerpt"]
    assert task["candidate"] is None and task["final_evaluation"] == "not_run"


def test_no_marker_and_different_object_equal_values_are_only_local_output(tmp_path):
    stdout = tmp_path / "stdout.txt"
    stdout.write_text('engine_a=0 engine_b=0\n', encoding="utf-8")
    stage = {"status": "passed", "exit_code": 0,
             "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
             "nodeids": ["test_observe"], "stdout_path": str(stdout)}
    report = {"status": "observed", "stages": {"old_original": stage, "new_original": stage}}
    result, _ = summarize(report, probe=True, observation_scope={"purpose": "two distinct engines",
                                                               "expected_observation": "local pool counts"})
    assert result["assessment"]["conclusion"] == "observation_only"
    assert "different objects" in result["assessment"]["scope_limit"]
    assert "engine_a=0" in result["stages"]["new_original"]["output_excerpt"]
    assert "assessment" not in summarize(report, probe=True)[0]
    with pytest.raises(ValueError, match="cannot be combined"):
        summarize(report, probe=True, oracle={"expected": 0}, observation_scope={})


@pytest.mark.parametrize("parent_state", ["rejected", "inconclusive"])
def test_narrowing_preserves_unestablished_oracle_and_receipt_recovery(task, parent_state):
    task = run_action(task, probe_action(task))
    if parent_state == "rejected":
        task = review(task, diagnostic_decision="reject")
    else:
        task = review(task, probe_runner=lambda *args: measured_run(*args, completed=False))
    parent = task["probes"][0]
    original = Path(parent["path"]).read_bytes()
    task = run_action(task, revision_action(task, parent["id"], oracle=None,
        purpose="Observe one local object, not the full lifecycle",
        expected_observation="One local count; full lifecycle remains unknown"))
    assert task["status"] == "pending_diagnostic_review"
    pending = json.loads(json.dumps(task))
    task = review(task, probe_runner=measured_run)
    run = dict(task["diagnostic_runs"][-1])
    run.pop("result")
    run.pop("observation")
    run["status"] = "running"
    pending.update(status="diagnosing", diagnostic_runs=pending["diagnostic_runs"] + [run],
                   diagnostic_reviews=task["diagnostic_reviews"])
    recovered = recover_diagnostic(pending)
    assert recovered["observations"] == task["observations"]
    assert len(recovered["diagnostic_runs"]) == len(task["diagnostic_runs"])
    for line in range(1, 5):
        accept_action(recovered, {"action": {"type": "read_source", "revision": probe_action(task)["revision"],
            "path": "bumpversion/config.py", "start_line": line, "end_line": line}}, load_case(CASE))
    ctx = context(recovered)
    assert all(o["kind"] == "source" for o in ctx["observations"])
    assert ctx["runtime_findings"][-1]["conclusion"] == "observation_only"
    assert ctx["probes"][-1]["unestablished_oracles"][0]["oracle"] == json.loads(original)["oracle"]
    assert not ctx["probes"][0]["can_revise"] and ctx["probes"][-1]["remaining_revisions"] == 1
    assert Path(parent["path"]).read_bytes() == original
    accept_action(recovered, {"action": {"type": "finish", "reason": "unresolved",
        "explanation": "broader requirement remains unknown", "evidence_refs": [],
        "contract_coverage": coverage(
            "Broader lifecycle behavior remains unverified", status="unverified", evidence=[],
            tool_limitation="The synthetic fixture exposes no broader lifecycle object to a reviewed probe"
        )}}, load_case(CASE))
    assert recovered["finish"]["diagnostic_scope"]["unestablished_oracles"]


def test_narrowing_and_new_null_probe_cannot_erase_measured_counterexample(task):
    task = review(request_public(task))
    task = review(run_action(task, probe_action(task)), probe_runner=lambda *args: measured_run(*args, value=4))
    root = task["probes"][0]["id"]
    with pytest.raises(ValueError, match="eligible latest"):
        accept_action(task, {"action": revision_action(task, root, oracle=None)}, load_case(CASE))
    task = review(run_action(task, probe_action(task, oracle=None)), probe_runner=measured_run)
    with pytest.raises(ValueError, match="counterexample"):
        accept_action(task, {"action": {"type": "finish", "reason": "no_change_claimed",
            "explanation": "local probe passed", "evidence_refs": ["observation:" + r["id"] for r in task["observations"]],
            "contract_coverage": coverage()}}, load_case(CASE))


def test_observation_only_revision_cannot_silently_introduce_new_numeric_predicate(task):
    task = review(run_action(task, probe_action(task, oracle=None)), diagnostic_decision="reject")
    parent = task["probes"][0]["id"]
    with pytest.raises(ValueError, match="preserve requirement"):
        accept_action(task, {"action": revision_action(task, parent)}, load_case(CASE))
    task = run_action(task, revision_action(task, parent, oracle=None))
    assert task["status"] == "pending_diagnostic_review" and len(task["probes"]) == 2


def test_successful_finish_and_export_keep_observation_limits_but_not_acceptance(task, tmp_path):
    task = review(request_public(task))
    task = review(run_action(task, probe_action(task, oracle=None)), probe_runner=measured_run)
    action = {"type": "finish", "reason": "no_change_claimed", "explanation": "Local observation only, broader behavior unknown",
              "evidence_refs": [], "contract_coverage": coverage()}
    with pytest.raises(ValueError, match="observation-only limits") as error:
        accept_action(task, {"action": action}, load_case(CASE))
    assert "observation:" + task["observations"][-1]["id"] in str(error.value)
    assert "not a request for another execution" in str(error.value)
    task = run_action(task, action | {"evidence_refs": ["observation:" + task["observations"][-1]["id"]]})
    assert task["status"] == "no_change_claimed" and task["final_evaluation"] == "not_run"
    scope = task["finish"]["diagnostic_scope"]
    assert scope["runtime_findings"][-1]["conclusion"] == "observation_only"
    exported = export_task_report(Path(task["task_path"]), tmp_path / "export")
    assert json.loads(Path(exported["json_path"]).read_bytes())["finish"]["diagnostic_scope"] == scope

    def reject(manifest, directory, **kwargs):
        from test_diagnostic_operations import public_run

        report = public_run(manifest, directory, **kwargs)
        report["stages"]["new_original"] = {"status": "failed"}
        report["status"] = "migration_required"
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    final = finalize_task(Path(task["task_path"]), comparator=reject)
    assert final["final_result"]["status"] == "no_change_claim_not_accepted"


def test_failed_observation_is_revisable_and_not_business_counterexample(task):
    def failed(*args):
        report = measured_run(*args)
        report["stages"]["new_original"] = {"status": "failed"}
        Path(report["report_path"]).write_text(json.dumps(report))
        return report

    task = review(run_action(task, probe_action(task, oracle=None)), probe_runner=failed)
    ctx = context(task)
    assert ctx["probes"][-1]["can_revise"]
    assert ctx["runtime_findings"][-1]["execution_status"] == "failed"
    assert ctx["runtime_findings"][-1]["conclusion"] == "observation_only"
    assert read(task["probes"][-1], Path(task["task_path"]).parent)["oracle"] is None
