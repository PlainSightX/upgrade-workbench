"""候选静态诊断须按实际请求源码重算，不能靠同步重签收据绕过。"""

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
    return json.loads(json.loads(Path(receipt["request_path"]).read_bytes())["messages"][1]["content"])


def changed(tmp_path):
    case = load_case(CASE)
    base = load_candidate(case)
    name = "copier/user_data.py"
    return apply_increment(case, base, {"type": "submit_candidate", "base_revision": base.revision,
        "edits": [{"path": name, "old": base.files[name].decode(),
                   "new": base.files[name].decode() + "\nprint(missing_static_name)\n"}]}, tmp_path / "revision")


def test_candidate_recomputed_without_terminal_rejection(tmp_path):
    before = prepare(tmp_path)
    candidate = changed(tmp_path)
    after = prepare(tmp_path, candidate_reference=candidate.reference)
    _load_prepared(after)
    old = context_of(before)["static_diagnostics"]
    new = context_of(after)["static_diagnostics"]
    assert new["current"]["revision"] == candidate.revision != old["current"]["revision"]
    assert new["baseline"] == old["baseline"]
    assert any(r["message"] == "Undefined name `missing_static_name`" and r["comparison"] == "newly_observed" for r in new["findings"])
    assert after["status"] == "request_prepared"
    assert str(tmp_path) not in json.dumps(new)


@pytest.mark.parametrize("arm,visible", [("full", True), ("no_evidence", True),
    ("generic", False), ("no_ast", False), ("no_feedback", False)])
def test_static_assistance_boundaries(tmp_path, arm, visible):
    receipt = prepare(tmp_path, experiment_arm=arm)
    _load_prepared(receipt)
    assert ("static_diagnostics" in context_of(receipt)) is visible


@pytest.mark.parametrize("tamper", ["revision", "findings", "hashes", "drop", "stale"])
def test_rehash_tampering_rejected_before_network(tmp_path, tamper):
    original = context_of(prepare(tmp_path))["static_diagnostics"]
    receipt = prepare(tmp_path, candidate_reference=changed(tmp_path).reference)
    payload = json.loads(Path(receipt["request_path"]).read_bytes())
    context = json.loads(payload["messages"][1]["content"])
    report = context["static_diagnostics"]
    if tamper == "revision":
        report["current"]["revision"] = "a" * 64
    elif tamper == "findings":
        report["findings"] = []
    elif tamper == "hashes":
        report["current"]["source_hashes"] = {}
    elif tamper == "stale":
        context["static_diagnostics"] = original
    else:
        del context["static_diagnostics"]
    payload["messages"][1]["content"] = _json_bytes(context).decode()
    body = _json_bytes(payload)
    Path(receipt["request_path"]).write_bytes(body)
    receipt["request_sha256"] = _digest(body)
    receipt["public_context_sha256"] = _digest(_json_bytes(context))
    Path(receipt["report_path"]).write_bytes(_json_bytes(receipt))
    with pytest.raises(ProposalInputError, match="static diagnostics"):
        complete_request(receipt, api_key="unused", transport=lambda *_a, **_kw: pytest.fail("network"))
    assert not Path(receipt["report_path"]).with_name("attempt.json").exists()
