"""通用资源、队列和跨版本反例；不使用目标应用的隐藏验收答案。"""

import json

import pytest

from upgrade_workbench.diagnostic_oracles import MARKER, evaluate, measurement, validate_oracle
from upgrade_workbench.diagnostic_state import failed_public_nodes
from upgrade_workbench.diagnostics import _diagnostic_excerpt, summarize

ORACLE = {"requirement": "All temporary resources returned after the operation",
          "subject": "outstanding resources", "exercise": "create then close, before fixture cleanup",
          "basis": "absolute", "operator": "eq", "expected": 0}


def output(before, after, completed=True):
    return "\n" + MARKER + json.dumps({"before": before, "after": after, "path_completed": completed}) + "\n"


def assess(before, after, **changes):
    return evaluate(ORACLE | changes,
                    {"old_original": {"status": "passed"}, "new_original": {"status": "passed"}},
                    {"old_original": output(0, 0), "new_original": output(before, after)})


def test_unchanged_bad_baseline_is_not_absolute_success():
    absolute = assess(3, 3)
    relative = assess(3, 3, basis="delta", operator="le")
    assert absolute["conclusion"] == "counterexample_observed"
    assert relative["conclusion"] == "no_counterexample_observed"
    assert "does NOT establish an absolute requirement" in relative["scope_limit"]
    assert absolute["measurements"]["new_original"]["actual"] == 3
    assert relative["measurements"]["new_original"]["actual"] == 0


def test_version_difference_is_not_correctness_and_bad_old_is_not_comparator():
    stages = {"old_original": {"status": "passed"}, "new_original": {"status": "passed"}}
    data = {name: output(5, 5) for name in stages}
    result = evaluate(ORACLE | {"basis": "version_delta"}, stages, data)
    assert result["conclusion"] == "no_counterexample_observed"
    assert result["measurements"]["old_original"]["conclusion"] == "comparison_reference_only"
    assert "both violate" in result["scope_limit"]
    stages["old_original"]["status"] = "failed"
    assert evaluate(ORACLE | {"basis": "version_delta"}, stages, data)["conclusion"] == "inconclusive"
    assert assess(0, 2, basis="version_delta")["conclusion"] == "counterexample_observed"


@pytest.mark.parametrize("text,status", [
    ("all passed", "missing_measurement"),
    (output(0, 0) * 2, "ambiguous_measurement"),
    (output(0, 0, False), "path_not_completed"),
    (output(0, True), "invalid_measurement"),
    (output(0, float("nan")), "invalid_measurement"),
    (output(0, float("inf")), "invalid_measurement"),
    (output(0, "0"), "invalid_measurement"),
    (MARKER + '{"before":0,"after":0,"after":2,"path_completed":true}', "invalid_measurement"),
    (MARKER + "[]", "invalid_measurement"),
    (MARKER + "not JSON", "invalid_measurement"),
])
def test_unusable_measurement_never_proves_success(text, status):
    assert measurement(text)["status"] == status
    assert evaluate(ORACLE, {"new_original": {"status": "passed"}},
                    {"new_original": text})["conclusion"] == "inconclusive"


@pytest.mark.parametrize("changes", [{"input": "actual"}, {"output": "actual"},
    {"input": "actual", "output": float("nan")},
    {"input": "actual", "output": "x" * 2100},
    {"input": "actual", "output": {"bad": "\x00"}},
])
def test_incomplete_or_invalid_unified_sample_cannot_pass_numeric_assessment(changes):
    stdout = MARKER + json.dumps({"before": 0, "after": 0, "path_completed": True, **changes})
    assert measurement(stdout)["status"] == "invalid_measurement"
    assert evaluate(ORACLE, {"new_original": {"status": "passed"}},
                    {"new_original": stdout})["conclusion"] == "inconclusive"


def test_failed_test_with_satisfying_readout_is_not_positive_evidence():
    result = evaluate(ORACLE, {"new_original": {"status": "failed"}}, {"new_original": output(0, 0)})
    assert result["conclusion"] == "inconclusive"
    assert result["measurements"]["new_original"]["reason"] == "execution_not_passed"


def test_pytest_success_and_predicate_failure_remain_separate(tmp_path):
    stdout = tmp_path / "stdout.txt"
    stdout.write_text(output(8, 8) + "x" * 9000)
    stage = {"status": "passed", "exit_code": 0, "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
             "nodeids": ["test_resource"], "stdout_path": str(stdout)}
    report = {"status": "observed", "stages": {"old_original": stage, "new_original": stage,
                                               "new_candidate": {"status": "not_supplied"}}}
    result, dependencies = summarize(report, probe=True, oracle=ORACLE)
    assert result["status"] == "passed"
    assert result["assessment"]["conclusion"] == "counterexample_observed"
    # 摘要现在保留开头业务输出；判定仍来自完整日志的独立解析。
    assert MARKER in result["stages"]["new_original"]["output_excerpt"]
    assert result["stages"]["new_original"]["output_excerpt_truncated"] is True
    assert dependencies[0]["sha256"]
    # 历史探针没有oracle，读取时不补造业务结论。
    assert "assessment" not in summarize(report, probe=True)[0]


@pytest.mark.parametrize("changes", [{"expected": True}, {"expected": float("nan")},
    {"expected": 10**400}, {"basis": []}, {"operator": "eval"}, {"exercise": ""}])
def test_oracle_rejects_invalid_predicates(changes):
    with pytest.raises(ValueError):
        validate_oracle(ORACLE | changes)


def test_wrapped_exception_keeps_original_cause_without_expanding_output():
    cause = "sqlalchemy.exc.TimeoutError: QueuePool limit of size 1 overflow 0 reached"
    outer = "app.StorageInternalError: commit failed"
    text = "E    " + cause + "\n" + "source traceback\n" * 600 + "E    " + outer + "\n"
    excerpt, truncated = _diagnostic_excerpt(text)
    assert truncated
    assert len(excerpt) <= 2500
    assert cause in excerpt and outer in excerpt
    assert excerpt.index(cause) < excerpt.index(outer)


def test_exception_excerpt_is_bounded_for_many_long_exceptions():
    text = "\n".join("E package.WrappedError: " + str(n) + "a" * 1000 for n in range(20))
    excerpt, truncated = _diagnostic_excerpt(text)
    assert truncated
    assert len(excerpt) <= 2500
    assert "WrappedError: 0" in excerpt and "WrappedError: 19" in excerpt


def _projection_stage(tmp_path, *, status="failed", failures=None, warnings=None, output=""):
    stdout = tmp_path / "stdout.txt"
    stdout.write_text(output, encoding="utf-8")
    return {
        "status": status,
        "exit_code": 1 if status == "failed" else 0,
        "tests": {"collected": 1, "passed": 0 if status == "failed" else 1,
                   "failed": 1 if status == "failed" else 0, "errors": 0, "skipped": 0},
        "nodeids": ["checks/test_contract.py::test_identity"],
        "stdout_path": str(stdout),
        "failure_details": failures or [],
        "warning_details": warnings or [],
    }


def test_structured_failure_projection_keeps_assertion_ahead_of_warning(tmp_path):
    node = "checks/test_contract.py::test_identity"
    failure = {
        "nodeid": node,
        "when": "call",
        "outcome": "failed",
        "message": "E       assert 1 == 2\nE       AssertionError",
        "location": {"path": "test_contract.py", "line": 4},
    }
    warning = {"category": "UserWarning", "message": "legacy warning", "filename": "test_contract.py",
               "line": 2, "when": "runtest", "nodeid": node}
    stage = _projection_stage(tmp_path, failures=[failure], warnings=[warning],
                              output="warnings summary\nUserWarning: legacy warning\n")
    report = {"status": "observed", "stages": {"old_original": stage, "new_original": stage,
                                                   "new_candidate": {"status": "not_supplied"}}}
    result, _ = summarize(report, probe=True)
    projected = result["stages"]["new_original"]
    assert "assert 1 == 2" in projected["output_excerpt"]
    assert "legacy warning" not in projected["output_excerpt"]
    assert projected["failure_details"][0]["location"] == {"path": "test_contract.py", "line": 4}
    assert projected["warning_summary"]["text"].startswith("UserWarning: legacy warning")


def test_structured_failure_projection_exposes_truncation(tmp_path):
    node = "checks/test_contract.py::test_identity"
    failures = [{"nodeid": node, "when": "call", "outcome": "failed",
                 "message": f"AssertionError {index}", "location": None} for index in range(17)]
    stage = _projection_stage(tmp_path, failures=failures)
    stage["failure_details_truncated"] = False
    report = {"status": "observed", "stages": {"old_original": stage, "new_original": stage,
                                                   "new_candidate": {"status": "not_supplied"}}}
    projected = summarize(report, probe=True)[0]["stages"]["new_original"]
    assert len(projected["failure_details"]) == 16
    assert projected["failure_details_truncated"] is True


def test_structured_failure_nodes_do_not_depend_on_terminal_failed_lines():
    node = "checks/test_contract.py::test_identity"
    stage = {"nodeids": [node], "failure_details": [{"nodeid": node, "when": "call"}],
             "output_excerpt": "warning-only tail"}
    assert failed_public_nodes(stage) == [node]


def test_legacy_report_without_structured_details_still_projects_failure(tmp_path):
    node = "checks/test_contract.py::test_identity"
    stage = _projection_stage(tmp_path, failures=[], warnings=[],
                              output=f"FAILED {node} - AssertionError\n")
    stage.pop("failure_details")
    stage.pop("warning_details")
    report = {"status": "observed", "stages": {"old_original": stage, "new_original": stage,
                                                   "new_candidate": {"status": "not_supplied"}}}
    result, _ = summarize(report, probe=True)
    projected = result["stages"]["new_original"]
    assert projected["failure_details"][0]["nodeid"] == node
    assert "AssertionError" in projected["output_excerpt"]


def test_legacy_absolute_failure_location_is_redacted(tmp_path):
    node = "checks/test_contract.py::test_identity"
    stage = _projection_stage(tmp_path, failures=[{
        "nodeid": node, "when": "call", "outcome": "failed", "message": "AssertionError",
        "location": {"path": "C:/Users/example/private/test_contract.py", "line": 9},
    }])
    report = {"status": "observed", "stages": {"old_original": stage, "new_original": stage,
                                                   "new_candidate": {"status": "not_supplied"}}}
    projected = summarize(report, probe=True)[0]["stages"]["new_original"]
    assert projected["failure_details"][0]["location"]["path"] == "<host-path>"


@pytest.mark.parametrize("message", ["AssertionError: " + "x" * 3000, "断言失败：" * 800], ids=["ascii", "unicode"])
def test_many_failures_fit_one_public_observation_without_stopping_execution(message):
    from upgrade_workbench.generation.source_context import encoded

    nodes = [f"test_behavior.py::test_{i}" for i in range(32)]
    failed = {"status": "failed", "exit_code": 1, "nodeids": nodes,
              "tests": {"collected": 32, "passed": 0, "failed": 32, "errors": 0, "skipped": 0},
              "failure_details": [{"nodeid": node, "when": "call", "outcome": "failed",
                                   "message": message} for node in nodes]}
    old = dict(failed, status="passed", exit_code=0, failure_details=[],
               tests={"collected": 32, "passed": 32, "failed": 0, "errors": 0, "skipped": 0})
    result, _ = summarize({"status": "candidate_not_accepted", "stages": {
        "old_original": old, "new_original": failed, "new_candidate": failed}})

    assert len(encoded(result)) <= 20_000
    assert result["status"] == "failed"
    for name in ("new_original", "new_candidate"):
        row = result["stages"][name]
        assert row["tests"]["failed"] == 32
        assert row["failure_details"]
        assert row["failure_details_truncated"]
        assert row["failure_details_total"] == 32
    assert failed["failure_details"][0]["message"] == message
