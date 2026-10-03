"""公开上下文必须从当前 revision 重算，不能依赖可自行改写的摘要蒙混过关。"""

import json
from pathlib import Path

import pytest

from upgrade_workbench.candidates import apply_increment, load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.generation import ProposalInputError, complete_request
from upgrade_workbench.generation.provider import _load_prepared
from upgrade_workbench.generation.request import _digest, _json_bytes
from upgrade_workbench.planning import prepare_case_proposal

CASE = Path(__file__).resolve().parents[3] / "cases/copier-6.2.0-r2/manifest.json"


def prepare(tmp_path, **overrides):
    return prepare_case_proposal(CASE, tmp_path / "work", model="owned-model",
        endpoint="https://example.test/chat/completions", max_output_tokens=1024,
        timeout_seconds=10, public_source_ack=True, output_format="candidate_actions",
        max_source_bytes=256000, max_request_bytes=512000, **overrides)


def context_of(receipt):
    return json.loads(json.loads(Path(receipt["request_path"]).read_text(encoding="utf-8"))["messages"][1]["content"])


def test_real_candidate_request_has_current_line_and_recomputed_evidence(tmp_path):
    case = load_case(CASE)
    original = load_candidate(case)
    name = "copier/user_data.py"
    candidate = apply_increment(case, original, {"type": "submit_candidate", "base_revision": original.revision,
        "edits": [{"path": name, "old": original.files[name].decode(),
                   "new": "# shift location\n" + original.files[name].decode()}]}, tmp_path / "revision")
    receipt = prepare(tmp_path, candidate_reference=candidate.reference)
    _load_prepared(receipt)
    context = context_of(receipt)
    assert any(item["path"] == name and item["line"] == 208 and item["rule_id"] == "legacy_validator"
               for item in context["potential_impacts"])
    evidence_keys = {item["evidence_key"] for item in context["version_evidence"]}
    assert {"pydantic-v2-dataclasses", "pydantic-v2-custom-types"} <= evidence_keys
    derived = json.loads(Path(receipt["context_derivation"]["path"]).read_text(encoding="utf-8"))
    assert derived["analysis"]["source_binding"]["revision"] == candidate.revision
    assert derived["retrieval"]["source_hashes"][name] == _digest(candidate.files[name])
    assert "final_checks/" not in json.dumps(context)


@pytest.mark.parametrize("arm,ast_visible,evidence_visible", [
    ("full", True, True), ("no_ast", False, True), ("no_evidence", True, False),
    ("generic", False, False), ("no_feedback", True, True),
])
def test_derivation_respects_ablation_visibility(tmp_path, arm, ast_visible, evidence_visible):
    receipt = prepare(tmp_path, experiment_arm=arm)
    _load_prepared(receipt)
    context = context_of(receipt)
    assert bool(context["potential_impacts"]) is ast_visible
    assert bool(context["version_evidence"]) is evidence_visible
    assert "retrieval" not in context and "context_derivation" not in context
    if arm == "no_evidence":
        assert all("evidence_keys" not in item for item in context["potential_impacts"])


@pytest.mark.parametrize("field", ["potential_impacts", "version_evidence"])
def test_rehashing_forged_visible_context_still_fails_before_network(tmp_path, field):
    receipt = prepare(tmp_path)
    payload = json.loads(Path(receipt["request_path"]).read_text(encoding="utf-8"))
    context = json.loads(payload["messages"][1]["content"])
    context[field] = []
    payload["messages"][1]["content"] = _json_bytes(context).decode()
    body = _json_bytes(payload)
    Path(receipt["request_path"]).write_bytes(body)
    receipt["request_sha256"] = _digest(body)
    receipt["public_context_sha256"] = _digest(_json_bytes(context))
    Path(receipt["report_path"]).write_bytes(_json_bytes(receipt))
    with pytest.raises(ProposalInputError, match="recomputed selection|current-source analysis"):
        complete_request(receipt, api_key="unused", transport=lambda *_a, **_kw: pytest.fail("network"))
    assert not Path(receipt["report_path"]).with_name("attempt.json").exists()


def test_rehashed_derived_artifact_must_match_recomputed_sources(tmp_path):
    receipt = prepare(tmp_path)
    path = Path(receipt["context_derivation"]["path"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["analysis"]["source_binding"]["revision"] = "a" * 64
    path.write_bytes(_json_bytes(data))
    receipt["context_derivation"]["sha256"] = _digest(path.read_bytes())
    Path(receipt["report_path"]).write_bytes(_json_bytes(receipt))
    with pytest.raises(ProposalInputError, match="Derived source/evidence"):
        _load_prepared(receipt)


def test_lexical_retrieval_is_identical_with_or_without_ast(tmp_path):
    full = prepare(tmp_path / "full")
    no_ast = prepare(tmp_path / "no_ast", experiment_arm="no_ast")
    assert context_of(full)["version_evidence"] == context_of(no_ast)["version_evidence"]


def test_cross_file_context_is_bound_to_current_source_and_hidden_in_no_ast(tmp_path):
    receipt = prepare(tmp_path)
    context = context_of(receipt)
    impacts = [item for item in context["potential_impacts"] if item.get("related_locations")]
    assert impacts
    assert any(location["path"] == "copier/cli.py" and location["line"] == 60
               for item in impacts for location in item["related_locations"])
    assert all(item["related_scope"] == "static_dependency_not_proven_runtime_impact" for item in impacts)
    _load_prepared(receipt)
    assert not context_of(prepare(tmp_path / "ablated", experiment_arm="no_ast"))["potential_impacts"]


def test_semantic_configuration_is_reconstructed_before_send(tmp_path, monkeypatch):
    from upgrade_workbench import semantic

    def configured(index, query, *, top_k, config):
        return {**index.search(query, top_k=top_k), "model_test_double_mode": config["mode"]}

    monkeypatch.setattr(semantic, "configured_search", configured)
    config = {"models_root": str(tmp_path.absolute()), "device": "cpu", "mode": "hybrid"}
    receipt = prepare(tmp_path, semantic_config=config)
    _load_prepared(receipt)
    assert receipt["semantic_config"] == config
    assert str(tmp_path) not in json.dumps(context_of(receipt))
    receipt["semantic_config"]["mode"] = "rerank"
    Path(receipt["report_path"]).write_bytes(_json_bytes(receipt))
    with pytest.raises(ProposalInputError, match="Derived source/evidence"):
        _load_prepared(receipt)
