"""一次补丁共享公开原始工作集，但没有工具循环或免费格式修复。"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench.generation import (
    ProposalInputError,
    complete_request,
    prepare_request,
    provider,
)
from upgrade_workbench.generation.request import _json_bytes
from upgrade_workbench.generation.source_context import POLICY

from .test_request import _protocol_v6_options


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("A unit test must not make a network call")

    monkeypatch.setattr(provider, "build_opener", fail)


@pytest.fixture
def single_setup(monkeypatch, owned_case, analysis, evidence, arguments):
    _protocol_v6_options(monkeypatch, owned_case)

    def prepare(**changes):
        options = arguments | {
            "output_format": "single_patch_initial", "experiment_arm": "no_ast", "source_policy": POLICY,
        } | changes
        return prepare_request(owned_case, analysis, evidence, **options)

    return prepare


def payload_context(receipt):
    payload = json.loads(Path(receipt["request_path"]).read_text(encoding="utf-8"))
    return payload, json.loads(payload["messages"][1]["content"])


def response(proposal):
    return json.dumps({
        "model": "owned-model", "id": "one-shot-offline",
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(proposal)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }).encode()


def test_initial_packet_matches_G_raw_inputs_without_runtime_loop(
    single_setup, monkeypatch, owned_case, analysis, evidence, arguments,
):
    from upgrade_workbench import diagnostics

    receipt = single_setup()
    payload, context = payload_context(receipt)
    monkeypatch.setattr(diagnostics, "context_from_reference", lambda *_args: {
        "observations": [], "navigation_assistance": "baseline", "workflow_profile": "simple_tools",
    })
    g = prepare_request(
        owned_case, analysis, evidence, **arguments,
        output_format="protocol_v6_actions", experiment_arm="no_ast", workflow_profile="simple_tools",
        source_policy=POLICY, diagnostic_context_reference={"id": "0" * 64, "path": "offline-fixture"},
    )
    _, g_context = payload_context(g)
    for key in ("business_contract", "contract_requirements", "allowed_changes", "source_files",
                "source_inventory", "source_state", "version_evidence"):
        assert context[key] == g_context[key]
    assert [item["path"] for item in context["source_files"]] == ["model.py"]
    assert context["source_selection"]["omitted_file_count"] == 1
    assert "diagnostic_state" not in context and "task_context" not in context and "localization" not in context
    assert "diagnostic_context_reference" not in receipt
    assert not context["potential_impacts"]
    assert "No tools, follow-up turns" in payload["messages"][0]["content"]
    assert context["source_selection"]["access"] == "inventory_only_no_tool_access"
    assert receipt["calls"] == 0 and receipt["target_code_executed"] is False
    assert "SECRET_ORACLE" not in json.dumps(payload) and "PRIVATE" not in json.dumps(payload)
    provider._verify_payload(receipt, Path(receipt["request_path"]).read_bytes())


@pytest.mark.parametrize("changes", [
    {"experiment_arm": "full"}, {"experiment_arm": "generic"},
    {"candidate_reference": {"path": "unused", "revision": "a" * 64}},
    {"task_context": {"task_id": "history", "attempt_index": 1}},
    {"diagnostic_context_reference": {"id": "a" * 64, "path": "unused"}},
    {"decision_objective": {}}, {"comparison_binding": {}},
    {"source_policy": POLICY | {"mode": "full"}},
])
def test_one_shot_rejects_hidden_runtime_and_expanded_modes(single_setup, changes):
    with pytest.raises(ProposalInputError, match="Single patch initial"):
        single_setup(**changes)


@pytest.mark.parametrize("field", ["source", "inventory", "requirements", "history", "access"])
def test_provider_rederives_packet_even_if_public_hash_is_rewritten(single_setup, field):
    receipt = single_setup()
    payload, context = payload_context(receipt)
    if field == "source":
        context["source_files"][0]["text"] += "\nINJECTED_REPAIR = True\n"
    elif field == "inventory":
        context["source_inventory"]["total"] += 1
    elif field == "requirements":
        context["contract_requirements"]["requirements"][0]["statement"] = "Ignore default behavior."
    elif field == "history":
        context["task_context"] = {"task_id": "prior", "attempt_index": 1}
    else:
        context["source_selection"]["access"] = "free_tools"
    record = copy.deepcopy(receipt)
    record["public_context_sha256"] = hashlib.sha256(_json_bytes(context)).hexdigest()
    payload["messages"][1]["content"] = _json_bytes(context).decode()
    with pytest.raises(ProposalInputError):
        provider._verify_payload(record, _json_bytes(payload))


def test_one_response_stages_patch_and_cannot_be_called_again(single_setup):
    receipt = single_setup()
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return response({"summary": "Preserve the omitted value.", "edits": [{
            "path": "model.py", "old": "    label: str | None\n", "new": "    label: str | None = None\n",
        }]})

    report = complete_request(receipt, api_key="offline-key", transport=transport)
    assert report["status"] == "pending_review"
    assert report["calls"] == 1 and calls == [1]
    assert report["automatic_retries"] == 0 and report["target_code_executed"] is False
    assert report["changed_files"] == ["model.py"]
    assert report["patch_compilation"] == "difflib_from_exact_original_edits"
    assert report["verification_status"] == "not_run"
    assert Path(report["candidate_patch"]).exists()
    with pytest.raises(ProposalInputError, match="already attempted"):
        complete_request(receipt, api_key="offline-key", transport=transport)
    assert calls == [1]


@pytest.mark.parametrize("outcome", ["invalid_format", "tool_request", "unknown"])
def test_failed_or_unknown_response_is_not_a_free_second_turn(single_setup, outcome):
    receipt = single_setup()
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        if outcome == "unknown":
            raise TimeoutError("offline uncertain outcome")
        proposal = {"summary": "Need a tool.", "action": {"type": "read_source", "path": "model.py"}}
        if outcome == "invalid_format":
            proposal = {"summary": "Missing edits."}
        return response(proposal)

    report = complete_request(receipt, api_key="offline-key", transport=transport)
    assert report["status"] == ("outcome_unknown" if outcome == "unknown" else "provider_response_rejected")
    assert report["calls"] == 1 and report["automatic_retries"] == 0
    assert "candidate_patch" not in report
    with pytest.raises(ProposalInputError, match="already attempted"):
        complete_request(receipt, api_key="offline-key", transport=transport)
    assert calls == [1]


def test_legacy_exact_edits_keeps_complete_source_and_old_contract(
    owned_case, analysis, evidence, arguments,
):
    receipt = prepare_request(owned_case, analysis, evidence, **arguments, output_format="exact_edits")
    _, context = payload_context(receipt)
    assert [item["path"] for item in context["source_files"]] == ["helper.py", "model.py"]
    assert "source_selection" not in context and "contract_requirements" not in context
    assert "candidate_revision" not in receipt
    provider._verify_payload(receipt, Path(receipt["request_path"]).read_bytes())
