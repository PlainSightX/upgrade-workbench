"""公开异常驱动检索；只生成和重建请求，不调用模型或执行目标代码。"""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_contract_coverage import _create_protocol_6_task

from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostics import freeze_context, store
from upgrade_workbench.generation import ProposalInputError
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.planning import prepare_case_proposal
from upgrade_workbench.retrieval import evidence_for_sources, public_failure_queries

ROOT = Path(__file__).resolve().parents[2]
ENCODE = ROOT / "cases/encode-databases-sqlalchemy-2/manifest.json"
PYDANTIC = ROOT / "cases/copier-6.2.0-r2/manifest.json"


def observation(revision="a" * 64, text="TypeError: tuple indices must be integers or slices, not str"):
    return {
        "id": "b" * 64, "kind": "public_checks", "revision": revision, "stale": False,
        "action": {"type": "run_public_checks", "revision": revision},
        "result": {"scope": "public", "status": "failed", "stages": {
            "new_original": {
                "status": "failed", "failed_nodeids": [], "nodeids": [],
                "critical_evidence": [{"kind": "exception", "text": text,
                                       "sha256": hashlib.sha256(text.encode()).hexdigest()}],
            },
        }},
    }


def test_public_exception_adds_relevant_version_evidence_without_a_case_keyword():
    case = load_case(ENCODE)
    snapshot = load_candidate(case)
    original = evidence_for_sources(case, snapshot.blocks(), max_bytes=48000)
    queries = public_failure_queries({"observations": [observation(snapshot.revision)]}, snapshot.revision)
    adjusted = evidence_for_sources(case, snapshot.blocks(), max_bytes=48000, failure_queries=queries)
    heading = "Result rows act like named tuples"
    assert not any(heading in row["excerpt"] for row in original["entries"])
    assert any(heading in row["excerpt"] for row in adjusted["entries"])
    assert adjusted["evidence_bytes"] <= 48000
    assert adjusted["public_failure_queries"][0]["sources"][0]["stage"] == "new_original"
    assert "Row" not in queries[0]["query"]


@pytest.mark.parametrize("symbol", ["TypeAdapter", "__get_validators__"])
def test_unrelated_package_exception_symbols_use_the_same_retrieval_path(symbol):
    queries = public_failure_queries({"observations": [observation(text=f"AttributeError: module has no attribute '{symbol}'")]}, "a" * 64)
    result = evidence_for_sources(load_case(PYDANTIC), [], max_bytes=48000, failure_queries=queries)
    assert symbol in queries[0]["query"]
    assert any(symbol in item["excerpt"] for item in result["entries"])


@pytest.mark.parametrize("change", [
    {"revision": "c" * 64}, {"stale": True}, {"kind": "source"},
    {"result": {"scope": "final", "stages": observation()["result"]["stages"]}},
])
def test_stale_nonexecution_and_nonpublic_inputs_do_not_drive_queries(change):
    row = observation() | change
    assert public_failure_queries({"observations": [row]}, "a" * 64) == []


def test_hypotheses_and_unobserved_failures_are_not_promoted_to_retrieval_facts():
    row = observation()
    row["result"]["stages"]["new_original"]["status"] = "passed"
    runtime = {"observations": [row], "issues": [{"hypothesis": "TypeAdapter is broken"}],
               "diagnostic_workset": {"hypotheses": [{"hypothesis": "Row is broken"}]}}
    assert public_failure_queries(runtime, "a" * 64) == []
    case = load_case(PYDANTIC)
    blocks = load_candidate(case).blocks()
    assert evidence_for_sources(case, blocks, max_bytes=48000) == evidence_for_sources(
        case, blocks, max_bytes=48000, failure_queries=[])


def test_exception_hash_tampering_is_rejected():
    row = observation()
    row["result"]["stages"]["new_original"]["critical_evidence"][0]["text"] = "invented"
    with pytest.raises(ValueError, match="recorded hash"):
        public_failure_queries({"observations": [row]}, "a" * 64)


def prepared_with_public_failure(tmp_path):
    task = _create_protocol_6_task(ENCODE, tmp_path)
    revision = load_candidate(load_case(ENCODE)).revision
    row = observation(revision)
    stored = {key: value for key, value in row.items() if key not in {"id", "stale"}}
    stored.update(task_id=task["task_id"], case_fingerprint=task["case_fingerprint"],
                  source_reference=None, execution_reference=None)
    reference = store(Path(task["task_path"]).parent / "observations", stored)
    task["observations"].append(reference)
    task["latest_observation"] = reference["id"]
    protocol = task["protocol"]
    prepared = prepare_case_proposal(
        ENCODE, Path(task["work_root"]), public_source_ack=True,
        experiment_arm="full", output_format="protocol_v6_actions",
        candidate_reference=task["current_candidate"],
        task_context={"task_id": task["task_id"], "attempt_index": 1, "remaining_calls": 8},
        source_policy=protocol["source_policy"], diagnostic_context_reference=freeze_context(task),
        **protocol["generation"],
    )
    return prepared, Path(prepared["request_path"]).read_bytes(), reference


def test_preparation_and_send_rebuild_the_same_public_failure_retrieval(tmp_path):
    prepared, contents, _ = prepared_with_public_failure(tmp_path)
    _verify_payload(prepared, contents)
    payload = json.loads(contents)
    context = json.loads(payload["messages"][1]["content"])
    assert any("Result rows act like named tuples" in row["text"] for row in context["version_evidence"])
    derivation = json.loads(Path(prepared["context_derivation"]["path"]).read_bytes())
    assert derivation["retrieval"]["public_failure_queries"]
    assert prepared["calls"] == 0 and prepared["target_code_executed"] is False


@pytest.mark.parametrize("tamper", ["observation", "sent_evidence", "policy"])
def test_send_rebuild_rejects_changed_failure_or_evidence(tmp_path, tamper):
    prepared, contents, reference = prepared_with_public_failure(tmp_path)
    if tamper == "observation":
        path = Path(reference["path"])
        value = json.loads(path.read_bytes())
        value["result"]["scope"] = "final"
        path.write_text(json.dumps(value), encoding="utf-8")
    elif tamper == "sent_evidence":
        payload = json.loads(contents)
        context = json.loads(payload["messages"][1]["content"])
        context["version_evidence"][0]["text"] = "invented answer"
        payload["messages"][1]["content"] = json.dumps(context)
        contents = json.dumps(payload).encode()
    else:
        prepared = copy.deepcopy(prepared)
        prepared["failure_query_policy"] = "unregistered"
    with pytest.raises((ValueError, ProposalInputError)):
        _verify_payload(prepared, contents)
