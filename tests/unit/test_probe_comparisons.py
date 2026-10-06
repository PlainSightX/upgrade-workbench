"""核验有限差分证据、身份和实际请求链；注入执行不算真实迁移成果。"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from upgrade_workbench.probe_comparisons import (
    MARKER,
    compare,
    compare_versions,
    project,
    readings,
    sample,
)


def output(value, *, input_value="ordinary", completed=True):
    return "\n" + MARKER + json.dumps({"input": input_value, "output": value,
                                      "path_completed": completed}) + "\n"


def stage():
    return {"status": "passed", "exit_code": 0,
            "tests": {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0},
            "nodeids": ["test_probe.py::test_observe"]}


def reading(value, *, source="a", input_value="ordinary", **updates):
    return {**sample(output(value, input_value=input_value)), "execution_passed": True,
            "identity": {"case_fingerprint": "c" * 64, "probe_sha256": "d" * 64,
                         "source_revision": source * 64, "patch_sha256": source * 64,
                         "lock_sha256": "e" * 64, "image_id": "sha256:" + "f" * 64}, **updates}


@pytest.mark.parametrize("value", [None, True, 0, 1.5, "observed:value", [1, 2], {"rows": [["ok"]]}])
def test_valid_json_observations_are_not_restricted_to_numbers(value):
    assert sample(output(value))["output"] == value


@pytest.mark.parametrize("text,status", [
    ("1 passed", "missing_sample"),
    (output(1) * 2, "ambiguous_sample"),
    (MARKER + '{"input":0,"output":1,"output":2,"path_completed":true}', "invalid_sample"),
    (output(float("nan")), "invalid_sample"),
    (output(float("inf")), "invalid_sample"),
    (output(10 ** 200), "invalid_sample"),
    (output("x" * 2100), "invalid_sample"),
    (output("中文" * 1000), "invalid_sample"),
    (output(1, completed=False), "path_not_completed"),
    (MARKER + "[]", "invalid_sample"),
    (MARKER + '{"input":0,"output":0,"path_completed":"true"}', "invalid_sample"),
    ('E assert "' + MARKER + '{}"', "missing_sample"),
])
def test_missing_or_unusable_sample_does_not_create_equality(text, status):
    assert sample(text)["status"] == status


def test_same_input_compares_observed_outputs_without_proving_correctness():
    same = compare(reading("ok"), reading("ok", source="b"))
    different = compare(reading("ok"), reading({"error": "observed"}, source="b"))
    assert same["status"] == "same"
    assert different["status"] == "different"
    assert "not which source is correct" in different["scope_limit"]
    assert "do not establish equivalence" in same["scope_limit"]
    assert compare(reading(True), reading(1, source="b"))["status"] == "different"


@pytest.mark.parametrize("change,reason", [
    ({"source_revision": "a" * 64}, "same_source_not_two_implementations"),
    ({"patch_sha256": "a" * 64}, "same_source_not_two_implementations"),
    ({"image_id": "sha256:" + "0" * 64}, "different_probe_case_or_environment"),
    ({"probe_sha256": "0" * 64}, "different_probe_case_or_environment"),
    ({"lock_sha256": "0" * 64}, "different_probe_case_or_environment"),
    ({"case_fingerprint": "0" * 64}, "different_probe_case_or_environment"),
    ({"patch_sha256": None}, "missing_or_invalid_execution_identity"),
])
def test_identity_mismatch_is_incomplete(change, reason):
    right = reading(1, source="b")
    right["identity"].update(change)
    result = compare(reading(1), right)
    assert result["status"] == "incomplete" and result["reason"] == reason


@pytest.mark.parametrize("updates", [{"execution_passed": False}, {"status": "missing_sample"},
                                    {"status": "path_not_completed"}])
def test_failed_or_incomplete_paths_cannot_prove_a_difference(updates):
    assert compare(reading(1), reading(2, source="b", **updates))["status"] == "incomplete"


def test_different_inputs_are_not_comparable():
    result = compare(reading(1), reading(2, source="b", input_value="other"))
    assert result["status"] == "incomplete" and result["reason"] == "different_recorded_inputs"


def test_single_numeric_record_supplies_both_reading_and_business_predicate():
    from upgrade_workbench.diagnostic_oracles import MARKER as NUMERIC_MARKER
    from upgrade_workbench.diagnostic_oracles import evaluate, measurement

    stdout = "\n" + NUMERIC_MARKER + json.dumps({"before": 1, "after": 0,
        "input": {"query": "SELECT ':a'"}, "output": {"rows": [[":a"]]}, "path_completed": True})
    oracle = {"requirement": "preserve row", "subject": "violations", "exercise": "run query",
              "basis": "absolute", "operator": "eq", "expected": 0}
    assert measurement(stdout)["after"] == 0
    assert sample(stdout)["output"] == {"rows": [[":a"]]}
    assert evaluate(oracle, {"new_candidate": stage()}, {"new_candidate": stdout})[
        "conclusion"] == "no_counterexample_observed"
    assert sample(stdout + output("another output"))["status"] == "ambiguous_sample"


def test_numeric_only_history_cannot_create_concrete_input_or_output():
    from upgrade_workbench.diagnostic_oracles import MARKER as NUMERIC_MARKER

    row = sample(NUMERIC_MARKER + '{"before":1,"after":0,"path_completed":true}')
    assert row["status"] == "missing_sample" and row["measurement_status"] == "measured"
    assert "input" not in row and "output" not in row


def version_readings(left_output="ok", right_output="ok"):
    pair = {"old_lock": "1" * 64, "new_lock": "2" * 64,
            "old_image": "sha256:" + "3" * 64, "new_image": "sha256:" + "4" * 64,
            "old_base": "sha256:" + "5" * 64, "new_base": "sha256:" + "5" * 64}
    left = reading(left_output, environment="old", version_pair=pair)
    right = reading(right_output, environment="new", version_pair=deepcopy(pair))
    left["identity"].update(lock_sha256=pair["old_lock"], image_id=pair["old_image"])
    right["identity"].update(lock_sha256=pair["new_lock"], image_id=pair["new_image"])
    return left, right


def test_version_comparison_accepts_bound_environment_change_and_same_source_bytes():
    left, right = version_readings()
    assert compare_versions(left, right)["status"] == "same"
    assert compare(left, right)["status"] == "incomplete"
    right["output"] = {"error": "StatementError"}
    assert compare_versions(left, right)["status"] == "different"


@pytest.mark.parametrize("kind", ["base", "pair", "lock", "image", "environment", "case",
                                  "probe", "input", "sample", "execution"])
def test_version_comparison_rejects_unbound_or_incomplete_reference(kind):
    left, right = version_readings()
    if kind == "base":
        left["version_pair"]["new_base"] = "sha256:" + "6" * 64
        right["version_pair"] = deepcopy(left["version_pair"])
    elif kind == "pair":
        right["version_pair"]["old_image"] = "sha256:" + "6" * 64
    elif kind in {"lock", "image", "case", "probe"}:
        key = {"lock": "lock_sha256", "image": "image_id", "case": "case_fingerprint",
               "probe": "probe_sha256"}[kind]
        right["identity"][key] = ("sha256:" if kind == "image" else "") + "6" * 64
    elif kind == "environment":
        right["environment"] = "old"
    elif kind == "input":
        right["input"] = "different"
    elif kind == "sample":
        left["status"] = "missing_sample"
    else:
        left["execution_passed"] = False
    assert compare_versions(left, right)["status"] == "incomplete"


def test_projection_keeps_old_behavior_separate_from_direct_upgrade():
    old, candidate = version_readings(right_output="changed")
    row = observation("1", candidate, direct=reading("exception", source="0"))
    row["result"]["probe_samples"]["old_original"] = old
    comparisons = project([row], candidate["identity"]["source_revision"])["comparisons"]
    assert comparisons[0]["kind"] == "old_original_vs_candidate"
    assert comparisons[0]["status"] == "different"
    assert comparisons[1]["kind"] == "direct_upgrade_vs_candidate"


def test_readings_require_completed_collected_tests_and_bind_report_metadata(tmp_path):
    from upgrade_workbench.diagnostics import summarize

    stdout = tmp_path / "stdout.txt"
    stdout.write_text(output("ok"), encoding="utf-8")
    passed = stage() | {"stdout_path": str(stdout)}
    report = {"status": "observed", "stages": {"old_original": passed, "new_original": passed,
                                               "new_candidate": passed},
              "case_fingerprint": "c" * 64,
              "probe_sample_identity": {"probe_sha256": "d" * 64, "lock_sha256": "e" * 64,
                                        "new_original": {"revision": "a" * 64, "patch_sha256": "a" * 64},
                                        "new_candidate": {"revision": "b" * 64, "patch_sha256": "b" * 64}},
              "environments": {"new": {"image_id": "sha256:" + "f" * 64}}}
    assert "probe_samples" not in summarize(report, probe=True)[0]
    result = summarize(report, probe=True, include_probe_samples=True)[0]
    rows = result["probe_samples"]
    assert compare(rows["new_original"], rows["new_candidate"])["status"] == "same"
    assert compare_versions(rows["old_original"], rows["new_candidate"])["status"] == "incomplete"
    report["stages"]["new_candidate"] = passed | {"status": "failed", "exit_code": 1}
    assert readings(report, {"new_candidate": output(3)})["new_candidate"]["execution_passed"] is False
    with pytest.raises(ValueError, match="reviewed probe"):
        summarize(report, include_probe_samples=True)


def observation(identity, target, *, direct=None):
    return {"id": identity * 64, "kind": "probe", "revision": target["identity"]["source_revision"],
            "result": {"assessment": {"probe_id": "9" * 64},
                       "probe_samples": {"new_candidate": target,
                                         **({"new_original": direct} if direct else {})}}}


def test_projection_distinguishes_alternative_candidates_from_direct_upgrade():
    first = observation("1", reading(1), direct=reading(0, source="0"))
    second = observation("2", reading(1, source="b"), direct=reading(0, source="0"))
    items = project([first, second], "b" * 64)["comparisons"]
    assert [(row["kind"], row["status"]) for row in items] == [
        ("direct_upgrade_vs_candidate", "different"), ("candidate_vs_candidate", "same")]
    assert items[0]["right_is_current_source"] is False
    assert items[1]["right_is_current_source"] is True
    # 调用者只交付公开观察；省略的历史参照不从别处取回。
    visible = project([second], "b" * 64)["comparisons"]
    assert len(visible) == 1 and visible[0]["kind"] == "direct_upgrade_vs_candidate"


def test_projection_is_bounded_and_does_not_reinterpret_historical_stdout():
    items = [observation(str(index), reading(index, source=str(index))) for index in range(1, 8)]
    assert len(project(items, "7" * 64)["comparisons"]) == 4
    assert project([{"kind": "probe", "result": {"stages": {}}}], "a" * 64)["comparisons"] == []


def test_reviewed_receipts_reach_actual_solver_request_and_reject_tampering(tmp_path):
    from upgrade_workbench.budget import BudgetLedger
    from upgrade_workbench.candidates import load_candidate, publish_candidate
    from upgrade_workbench.cases import load_case
    from upgrade_workbench.diagnostic_oracles import MARKER as NUMERIC_MARKER
    from upgrade_workbench.diagnostics import (
        accept_action,
        context_from_reference,
        freeze_context,
        public_observation,
        read,
        review_diagnostic,
        store,
    )
    from upgrade_workbench.generation.provider import _load_prepared
    from upgrade_workbench.generation.semantic_risk import COMPARISON_POLICY
    from upgrade_workbench.planning import prepare_case_proposal
    from upgrade_workbench.probe_comparisons import identity as sample_identity
    from upgrade_workbench.tasks import create_operation

    manifest = Path(__file__).resolve().parents[2] / "cases/csvkit-csvsql-sqlalchemy-2-a3/manifest.json"
    case = load_case(manifest)
    ledger = tmp_path / "ledger.sqlite"
    BudgetLedger(ledger, {"mode": "user_managed", "limit_usd": None, "model": "owned-model",
                         "input_per_million": "1", "output_per_million": "1",
                         "pricing_source": "https://example.test/pricing", "pricing_checked_at": "2026-10-05"})
    generation = {"model": "owned-model", "endpoint": "https://example.test/chat/completions",
                  "max_output_tokens": 1024, "timeout_seconds": 10, "thinking_mode": "disabled"}
    task = create_operation(manifest, tmp_path / "work", budget_path=ledger, seed_strategy="none",
                            protocol_revision=4, semantic_risk_policy=COMPARISON_POLICY, generation=generation)
    original = load_candidate(case)
    name = case.manifest.allowed_changes[0]
    code = 'def test_observe():\n    print("\\n' + MARKER + '{}")\n'

    def fake_runner(case, candidate, code, directory, execution):
        directory.mkdir(parents=True)
        stdout = directory / "stdout.txt"
        stdout.write_text("\n" + NUMERIC_MARKER + json.dumps({"before": 0, "after": 0,
            "input": "ordinary", "output": "same observed value", "path_completed": True}),
            encoding="utf-8")
        report = {"kind": "probe", "check_group": "probe", "case_fingerprint": case.fingerprint,
                  "revision": candidate.revision, "status": "observed",
                  "stages": {label: stage() | {"stdout_path": str(stdout)} for label in
                             ("old_original", "new_original", "new_candidate")},
                  "environments": {"old": {"image_id": "sha256:" + "0" * 64,
                                             "base_image_id": "sha256:" + "f" * 64},
                                   "new": {"image_id": "sha256:" + "1" * 64,
                                           "base_image_id": "sha256:" + "f" * 64}},
                  "probe_sample_identity": sample_identity(case, candidate, code),
                  "report_path": str(directory / "report.json")}
        Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        return report

    for index in (1, 2):
        candidate = publish_candidate(case, original,
            {name: original.files[name] + f"\n# owned offline candidate {index}\n".encode()},
            Path(task["task_path"]).parent / "calibration" / str(index), origin="agent_candidate")
        task["current_candidate"] = candidate.reference
        action = ({"type": "propose_probe", "revision": candidate.revision, "code": code,
                   "purpose": "observe owned injected output", "expected_observation": "unknown",
                   "evidence_refs": ["business_contract"], "oracle": None} if index == 1 else
                  {"type": "run_probe", "revision": candidate.revision, "probe_id": task["probes"][0]["id"]})
        accept_action(task, {"action": action, "report_path": str(tmp_path / "injected-origin.json")}, case)
        review_diagnostic(task, case, request_id=task["pending_diagnostic"]["request"]["id"],
                          reviewer="owned-test", note="offline receipt injection", decision="accept",
                          comparator=None, probe_runner=fake_runner)
        assert task["status"] == "ready"

    reference = freeze_context(task)
    runtime = context_from_reference(reference, case, candidate)
    assert runtime["probe_comparisons"]["comparisons"][-1]["status"] == "same"
    prepared = prepare_case_proposal(manifest, tmp_path / "request", public_source_ack=True,
        output_format="diagnostic_actions", diagnostic_context_reference=reference,
        candidate_reference=candidate.reference, max_source_bytes=1600000, max_request_bytes=850000,
        source_policy=task["protocol"]["source_policy"], **generation)
    _load_prepared(prepared)
    body = json.loads(Path(prepared["request_path"]).read_bytes())
    actual = json.loads(body["messages"][1]["content"])["diagnostic_state"]["probe_comparisons"]
    assert actual == runtime["probe_comparisons"]
    old_comparisons = [row for row in actual["comparisons"] if row["kind"] == "old_original_vs_candidate"]
    assert old_comparisons and all(row["status"] == "same" for row in old_comparisons)
    assert old_comparisons[-1]["left"]["lock_sha256"] == case.manifest.file_hashes["requirements/old.txt"]
    assert "candidate_vs_candidate" in body["messages"][0]["content"]
    assert "checks/acceptance" not in json.dumps(body)
    frozen = read(reference, Path(task["task_path"]).parent)
    frozen["hidden_observation_ids"] = [task["observations"][0]["id"]]
    hidden_reference = store(Path(task["task_path"]).parent / "contexts", frozen)
    hidden = context_from_reference(hidden_reference, case, candidate)
    assert all(row["kind"] != "candidate_vs_candidate" for row in hidden["probe_comparisons"]["comparisons"])
    original_receipt = deepcopy(task["observations"][-1])
    Path(task["diagnostic_runs"][-1]["report"]["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence changed"):
        public_observation(task, original_receipt)
