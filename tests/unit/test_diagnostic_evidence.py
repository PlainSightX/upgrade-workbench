"""关键失败证据、探针启动脚手架和业务观测边界。"""

from dataclasses import replace
from pathlib import Path

import pytest

from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostic_evidence import failure_fingerprint
from upgrade_workbench.diagnostics import run_probe, summarize
from upgrade_workbench.generation.source_context import encoded
from upgrade_workbench.probe_scaffolds import scaffold_for

ROOT = Path(__file__).resolve().parents[2]
FLASKBB = ROOT / "cases/flaskbb-sqlalchemy-1.4.21-r2/manifest.json"
CONSTRAINT = "NOT NULL constraint failed: topics.username"
AUTOFLUSH = "Query-invoked autoflush; consider using a session.no_autoflush block"


def failure_message(marker: str) -> str:
    return (
        "trace head\n" + "source frame\n" * 220
        + f"E sqlalchemy.exc.IntegrityError: (raised as a result of {AUTOFLUSH})\n"
        + f"E (sqlite3.IntegrityError) {CONSTRAINT}\n"
        + f"E [parameters: ({marker!r}, None, 1)]\n"
        + "trace tail\n" * 220
    )


def failed_stage(message: str) -> dict:
    node = "feedback/test_upstream_forum_behavior.py::test_topic_save"
    return {
        "status": "failed",
        "tests": {"collected": 1, "passed": 0, "failed": 1, "errors": 0, "skipped": 0},
        "nodeids": [node],
        "failure_details": [{
            "nodeid": node,
            "when": "call",
            "outcome": "failed",
            "message": message,
            "location": {"path": "feedback/test_upstream_forum_behavior.py", "line": 37},
        }],
    }


def test_priority_evidence_survives_whole_report_budget():
    passed = {
        "status": "passed",
        "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
        "nodeids": ["feedback/test_upstream_forum_behavior.py::test_topic_save"],
        "failure_details": [],
    }
    failed = failed_stage(failure_message("dynamic value"))
    result, _ = summarize({"status": "candidate_not_accepted", "stages": {
        "old_original": passed,
        "new_original": failed,
        "new_candidate": failed,
    }})

    serialized = encoded(result).decode("utf-8")
    assert len(serialized.encode("utf-8")) <= 20_000
    assert CONSTRAINT in serialized
    assert "Query-invoked autoflush" in serialized
    stage = result["stages"]["new_candidate"]
    assert stage["critical_evidence"][0]["kind"] == "database_constraint"
    assert stage["failure_clusters"][0]["occurrences"] == 1


def test_failure_fingerprint_ignores_dynamic_tail_when_semantic_failure_matches():
    first = failure_message("first runtime value") + " object at 0x1234"
    second = failure_message("second runtime value") + " object at 0x9876"
    assert failure_fingerprint(first) == failure_fingerprint(second)


def test_setup_failure_is_not_reported_as_business_observation():
    node = "test_probe.py::test_topic_save"
    stage = {
        "status": "failed",
        "tests": {"collected": 1, "passed": 0, "failed": 0, "errors": 1, "skipped": 0},
        "nodeids": [node],
        "failure_details": [{
            "nodeid": node,
            "when": "setup",
            "outcome": "error",
            "message": "fixture 'application' not found",
        }],
    }
    result, _ = summarize(
        {"status": "observed", "stages": {"old_original": stage, "new_original": stage}},
        probe=True,
        observation_scope={"purpose": "exercise save path", "expected_observation": "insert order"},
    )

    assert result["assessment"]["conclusion"] == "setup_failed"
    assert all(row["business_observation_obtained"] is False
               for row in result["probe_validity"]["affected_stages"])
    assert "not evidence" in result["assessment"]["scope_limit"]


def probe_stage(tmp_path, name, *, setup_failed=False, after=0):
    stdout = tmp_path / f"{name}.txt"
    stdout.write_text("" if setup_failed else (
        'UPGRADE_WORKBENCH_MEASUREMENT:{"before":0,"after":' + str(after)
        + ',"path_completed":true}\nobserved configuration\n'
    ), encoding="utf-8")
    return {
        "status": "failed" if setup_failed else "passed",
        "exit_code": 1 if setup_failed else 0,
        "tests": {"collected": 0 if setup_failed else 1, "passed": 0 if setup_failed else 1,
                  "failed": 0, "errors": 1 if setup_failed else 0, "skipped": 0},
        "nodeids": [] if setup_failed else ["test_probe.py::test_observation"],
        "stdout_path": str(stdout),
        "failure_details": ([{"nodeid": "test_probe.py", "when": "collect", "outcome": "failed",
                             "message": "ImportError: obsolete API"}] if setup_failed else []),
    }


def test_direct_upgrade_import_failure_preserves_other_environment_observations(tmp_path):
    stages = {name: probe_stage(tmp_path, name, setup_failed=name == "new_original")
              for name in ("old_original", "new_original", "new_candidate")}
    result, _ = summarize({"status": "observed", "stages": stages}, probe=True,
                          observation_scope={"purpose": "inspect configuration",
                                             "expected_observation": "accepted input types"})
    assert result["assessment"]["conclusion"] == "observation_only"
    assert result["status"] == "passed"
    assert [row["stage"] for row in result["probe_validity"]["affected_stages"]] == ["new_original"]
    for name in ("old_original", "new_candidate"):
        assert "observed configuration" in result["stages"][name]["output_excerpt"]


@pytest.mark.parametrize("failed_stage,basis,after,expected", [
    ("new_original", "absolute", 0, "no_counterexample_observed"),
    ("new_original", "absolute", 2, "counterexample_observed"),
    ("new_original", "delta", 2, "counterexample_observed"),
    ("new_original", "version_delta", 2, "counterexample_observed"),
    ("old_original", "version_delta", 0, "inconclusive"),
    ("old_original", "absolute", 2, "counterexample_observed"),
    ("new_candidate", "absolute", 0, "setup_failed"),
])
def test_setup_failure_blocks_only_required_numeric_observations(tmp_path, failed_stage, basis, after, expected):
    stages = {name: probe_stage(tmp_path, name, setup_failed=name == failed_stage,
                               after=after if name == "new_candidate" else 0)
              for name in ("old_original", "new_original", "new_candidate")}
    oracle = {"requirement": "No resources outstanding", "subject": "outstanding resources",
              "exercise": "open and close", "basis": basis, "operator": "eq", "expected": 0}
    result, _ = summarize({"status": "observed", "stages": stages}, probe=True, oracle=oracle)
    assert result["assessment"]["conclusion"] == expected
    if failed_stage == "old_original" and basis == "version_delta":
        assert result["assessment"]["measurements"]["new_candidate"]["reason"] == "old_comparator_not_valid"


def test_partial_setup_failure_does_not_erase_completed_test(tmp_path):
    stage = probe_stage(tmp_path, "partial", setup_failed=True)
    stage["tests"].update(collected=2, passed=1)
    result, _ = summarize({"status": "observed", "stages": {"old_original": stage, "new_original": stage}},
                          probe=True, observation_scope={"purpose": "inspect", "expected_observation": "value"})
    assert result["status"] == "failed"
    assert result["assessment"]["conclusion"] == "observation_only"
    assert "probe_validity" not in result


def test_scaffold_is_bound_to_exact_reviewed_case_identity():
    case = load_case(FLASKBB)
    scaffold = scaffold_for(case)
    assert scaffold is not None
    assert scaffold.public()["id"] == "flaskbb-public-fixtures-v2"
    assert "application" in scaffold.public()["fixtures"]
    assert "instance_path=str(instance_path)" in scaffold.code
    assert "WHOOSHEE_DIR = str(whooshee_path)" in scaffold.code
    assert scaffold_for(replace(case, fingerprint="0" * 64)) is None


def test_probe_runner_materializes_reviewed_scaffold_without_changing_probe_body(tmp_path):
    case = load_case(FLASKBB)
    snapshot = load_candidate(case)
    observed = []

    class Executor:
        def prepare_environment(self, *_args, **_kwargs):
            return {
                "image_id": "sha256:" + "1" * 64,
                "base_image_digest": "python@sha256:" + "2" * 64,
                "base_image_id": "sha256:" + "3" * 64,
            }

        def verify(self, _image, _source, checks, **_kwargs):
            observed.append((checks / "conftest.py").read_text(encoding="utf-8"))
            assert (checks / "test_probe.py").read_text(encoding="utf-8") == code
            return {
                "status": "passed",
                "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
                "nodeids": ["test_probe.py::test_scaffold"],
                "failure_details": [],
                "warning_details": [],
            }

    code = "def test_scaffold(application, database, user):\n    assert user.username\n"
    report = run_probe(case, snapshot, code, tmp_path / "probe", {}, executor=Executor())

    assert report["status"] == "observed"
    assert report["probe_scaffold"]["id"] == "flaskbb-public-fixtures-v2"
    assert len(observed) == 2
    assert all("writable isolated instance directory" in item for item in observed)
