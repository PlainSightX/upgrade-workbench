"""只注入受控响应，验证预算、归因、敏感值和候选边界。"""

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from upgrade_workbench.cases import CaseValidationError
from upgrade_workbench.generation import (
    ProposalInputError,
    complete_request,
    prepare_request,
    provider,
)

from .conftest import PATCH, provider_response

KEY = "owned-provider-secret-do-not-record"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("A unit test must not make a network call")
    monkeypatch.setattr(provider, "build_opener", fail)


def assert_saved(report):
    path = Path(report["report_path"])
    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert not path.with_name("proposal.tmp").exists()
    for item in path.parent.rglob("*"):
        if item.is_file():
            assert KEY.encode() not in item.read_bytes()
    assert report["calls"] == 1
    assert report["target_code_executed"] is False
    assert report["approved"] is False
    assert report["verification_status"] == "not_run"
    assert report["automatic_retries"] == 0


def prepare_protocol_v6(
    monkeypatch, owned_case, analysis, evidence, arguments, *, output_format,
):
    """冻结含候选、诊断和合同目录的离线 Protocol 6 请求。"""
    from upgrade_workbench import diagnostics
    from upgrade_workbench.cases import requirements
    from upgrade_workbench.generation.source_context import POLICY

    digest = owned_case.manifest.file_hashes["business-contract.md"]
    catalog = SimpleNamespace(
        path="business-contract.md",
        sha256=digest,
        public=lambda: {
            "schema_version": 1,
            "contract_path": "business-contract.md",
            "contract_sha256": digest,
            "requirements": [{
                "id": "label.default",
                "statement": "Omitted label remains None.",
                "public_check_nodeids": [],
            }],
            "development_gate_requirement_ids": [],
            "final_acceptance_requirement_ids": ["label.default"],
        },
    )
    runtime = {
        "observations": [], "navigation_assistance": "baseline",
        "workflow_profile": arguments.get("workflow_profile", "workbench"),
    }
    monkeypatch.setattr(requirements, "requirements_for_case", lambda _case: catalog)
    monkeypatch.setattr(diagnostics, "context_from_reference", lambda *_args: runtime)
    return prepare_request(
        owned_case,
        analysis,
        evidence,
        **arguments,
        output_format=output_format,
        source_policy=POLICY,
        diagnostic_context_reference={"id": "0" * 64, "path": "offline-unit-fixture"},
    )


def test_one_call_stages_original_relative_patch_without_executing_it(prepared, owned_case):
    calls = []

    def transport(request, **kwargs):
        calls.append((request, kwargs))
        assert request.get_header("Authorization") == f"Bearer {KEY}"
        assert KEY.encode() not in request.data
        return provider_response()

    report = complete_request(prepared, api_key=KEY, transport=transport)
    assert len(calls) == 1
    assert calls[0][1] == {"timeout_seconds": 10, "max_response_bytes": provider.MAX_RESPONSE_BYTES}
    assert report["status"] == "pending_review"
    assert report["candidate_origin"] == "agent_candidate"
    assert report["changed_files"] == ["model.py"]
    assert report["model_usage"] == {"availability": "reported", "input_tokens": 10, "output_tokens": 20}
    assert (Path(report["candidate_source_dir"]) / "model.py").read_bytes().endswith(b" = None\n")
    assert not (owned_case.source_dir / "model.py").read_bytes().endswith(b" = None\n")
    assert_saved(report)
    with pytest.raises(ProposalInputError, match="already attempted"):
        complete_request(prepared, api_key=KEY, transport=transport)
    assert len(calls) == 1


def test_missing_usage_remains_unavailable_instead_of_zero(prepared):
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: provider_response(usage=False))
    assert report["status"] == "pending_review"
    assert report["model_usage"] == {"availability": "unavailable", "input_tokens": None, "output_tokens": None}
    assert_saved(report)


def test_explicit_reasoning_capacity_survives_receipt_revalidation(
    owned_case, analysis, evidence, arguments,
):
    prepared = prepare_request(
        owned_case, analysis, evidence,
        **(arguments | {"max_output_tokens": 32_768, "thinking_mode": "enabled"}),
    )
    raw = json.loads(provider_response())
    raw["usage"] = {"prompt_tokens": 10, "completion_tokens": 20_000, "total_tokens": 20_010}

    def transport(request, **kwargs):
        body = json.loads(request.data)
        assert body["max_tokens"] == 32_768
        assert body["thinking"] == {"type": "enabled"}
        return json.dumps(raw).encode()

    report = complete_request(prepared, api_key=KEY, transport=transport)
    assert report["status"] == "pending_review"
    assert report["model_usage"]["output_tokens"] == 20_000
    assert_saved(report)


def test_provider_model_identity_is_recorded_separately_from_requested_alias(prepared):
    raw = provider_response(model="provider-specific-model-revision", id="provider-request-id")
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)
    assert report["model"] == "owned-model"
    assert report["returned_model"] == "provider-specific-model-revision"
    assert report["provider_response_id"] == "provider-request-id"
    assert_saved(report)


def test_bounded_wait_does_not_adopt_a_late_response_or_retry(monkeypatch):
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def slow_transport(*args, **kwargs):
        calls.append(1)
        release.wait(timeout=2)
        finished.set()
        return b"late result"

    request = Request("https://provider.example/v1/chat/completions", data=b"{}")
    try:
        with pytest.raises(TimeoutError):
            provider._bounded_transport(slow_transport, request, 0.01)
    finally:
        release.set()
        assert finished.wait(timeout=1)
    assert calls == [1]


@pytest.mark.parametrize("usage", [
    {}, {"prompt_tokens": True, "completion_tokens": 1},
    {"prompt_tokens": -1, "completion_tokens": 1},
    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 99},
    {"prompt_tokens": 1, "completion_tokens": 2048},
])
def test_invalid_or_over_budget_usage_rejects_response(prepared, usage):
    raw = json.loads(provider_response())
    raw["usage"] = usage
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: json.dumps(raw).encode())
    assert report["status"] == "provider_response_rejected"
    assert not Path(report["report_path"]).with_name("candidate").exists()
    assert_saved(report)


@pytest.mark.parametrize("change", [
    {"choices": []},
    {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": '{"summary":"x","patch":"x","extra":1}'}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": '{"summary":"x","patch":"x","patch":"y"}'}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "```json\n{}\n```"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "{}", "refusal": "refused"}}]},
])
def test_provider_schema_and_truncated_answers_are_not_candidates(prepared, change):
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: provider_response(**change))
    assert report["status"] == "provider_response_rejected"
    assert_saved(report)


@pytest.mark.parametrize("patch", [
    "not a unified diff", PATCH.replace("model.py", "../checks/test_contract.py"),
    PATCH.replace("model.py", "helper.py"), PATCH.replace("label: str | None", "WRONG_CONTEXT"),
])
def test_rejected_patches_do_not_become_reviewable_source(prepared, patch):
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: provider_response(patch))
    assert report["status"] == "candidate_rejected"
    assert not Path(report["report_path"]).with_name("candidate").exists()
    assert_saved(report)


@pytest.mark.parametrize("error, expected, reason", [
    (TimeoutError(KEY), "outcome_unknown", "provider_timeout_no_retry"),
    (URLError(KEY), "outcome_unknown", "provider_transport_error_no_retry"),
    (RuntimeError(KEY), "outcome_unknown", "unexpected_provider_or_storage_error_no_retry"),
    (KeyboardInterrupt(), "outcome_unknown", "interrupted_no_retry"),
    (HTTPError("https://example.com/?" + KEY, 401, KEY, {}, None), "provider_error", "http_status_401"),
])
def test_failures_are_durable_and_never_retry_or_log_auth(prepared, error, expected, reason):
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        raise error

    report = complete_request(prepared, api_key=KEY, transport=transport)
    assert report["status"] == expected
    assert report["reason"] == reason
    assert calls == [1]
    assert_saved(report)


def test_provider_echo_of_key_is_redacted_and_cannot_create_candidate(prepared):
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: provider_response(summary=KEY))
    assert report["reason"] == "provider_secret_echo"
    assert b"[REDACTED_PROVIDER_KEY]" in Path(report["response_path"]).read_bytes()
    assert_saved(report)


@pytest.mark.parametrize("field", ["model", "id", "summary", "patch", "dictionary_key"])
def test_unicode_escaped_provider_key_is_rejected_before_decoded_fields_are_saved(prepared, field):
    changes = {}
    if field in {"model", "id"}:
        changes[field] = KEY
        raw = provider_response(**changes)
    elif field == "dictionary_key":
        raw = provider_response(**{KEY: "value"})
    else:
        raw = provider_response(**{field: KEY})
    escaped = ("\\u%04x" % ord(KEY[0])).encode() + KEY[1:].encode()
    raw = raw.replace(KEY.encode(), escaped)
    assert KEY.encode() not in raw
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)
    assert report["reason"] == "provider_secret_echo"
    assert "returned_model" not in report
    assert "provider_response_id" not in report
    assert "summary" not in report
    assert not Path(report["report_path"]).with_name("candidate.patch").exists()
    assert report["response_sha256_kind"] == "sanitized_stored_bytes"
    assert_saved(report)


def test_second_json_layer_secret_is_redacted_before_proposal_can_become_patch(prepared):
    inner = json.dumps({"summary": "safe", "patch": KEY})
    escaped = "\\u%04x" % ord(KEY[0]) + KEY[1:]
    inner = inner.replace(KEY, escaped)
    raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": inner}}]}).encode()
    assert KEY.encode() not in raw
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)
    assert report["reason"] == "provider_secret_echo"
    assert not Path(report["report_path"]).with_name("candidate").exists()
    assert_saved(report)


def test_invalid_json_never_saves_unparsed_sensitive_response(prepared):
    raw = b'{"bad":"\\u%04x' % ord(KEY[0]) + KEY[1:].encode() + b'" broken'
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)
    assert report["status"] == "provider_response_rejected"
    assert report["response_storage"] == "metadata_only"
    assert json.loads(Path(report["response_path"]).read_text())["content_omitted"] is True
    assert_saved(report)


def test_response_byte_budget_is_enforced_for_injected_transports(prepared):
    report = complete_request(
        prepared, api_key=KEY,
        transport=lambda *a, **kw: b"x" * (provider.MAX_RESPONSE_BYTES + 1),
    )
    assert report["reason"] == "response_too_large_or_invalid"
    assert_saved(report)


def test_source_change_during_provider_call_rejects_candidate(prepared, owned_case):
    def transport(*args, **kwargs):
        (owned_case.source_dir / "model.py").write_bytes(b"case drift")
        return provider_response()

    report = complete_request(prepared, api_key=KEY, transport=transport)
    assert report["status"] == "rejected_input"
    assert not Path(report["report_path"]).with_name("candidate").exists()
    assert_saved(report)


def test_request_drift_is_rejected_before_network(prepared):
    Path(prepared["request_path"]).write_bytes(b"{}")
    with pytest.raises(ProposalInputError, match="bytes changed"):
        complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: pytest.fail("network"))
    assert not Path(prepared["report_path"]).with_name("attempt.json").exists()


def test_case_drift_is_rejected_before_network(prepared, owned_case):
    (owned_case.root / "business-contract.md").write_bytes(b"changed contract")
    with pytest.raises(CaseValidationError):
        complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: pytest.fail("network"))


def test_existing_attempt_without_final_report_cannot_be_resent(prepared):
    Path(prepared["report_path"]).with_name("attempt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ProposalInputError, match="automatic retry"):
        complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: pytest.fail("network"))


def test_synthetic_case_never_reaches_live_provider(prepared):
    with pytest.raises(ProposalInputError, match="Synthetic"):
        complete_request(prepared, api_key=KEY)
    assert not Path(prepared["report_path"]).with_name("attempt.json").exists()


@pytest.mark.parametrize("field,value", [
    ("request_path", None), ("input_hashes", []), ("input_hashes", {"path": []}),
    ("max_output_tokens", None), ("max_output_tokens", True),
    ("timeout_seconds", None), ("calls", "0"), ("live_call_eligible", 1),
])
def test_malformed_prepared_fields_are_input_errors_before_provider(prepared, field, value):
    prepared[field] = value
    Path(prepared["report_path"]).write_text(json.dumps(prepared), encoding="utf-8")
    with pytest.raises(ProposalInputError):
        complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: pytest.fail("network"))
    assert not Path(prepared["report_path"]).with_name("attempt.json").exists()


def test_minimal_receipt_does_not_raise_key_error(prepared):
    incomplete = {"report_path": prepared["report_path"], "status": "request_prepared"}
    Path(prepared["report_path"]).write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(ProposalInputError, match="required fields"):
        complete_request(incomplete, api_key=KEY, transport=lambda *a, **kw: pytest.fail("network"))


def test_redirect_handler_never_forwards_authorization():
    handler = provider._NoRedirect()
    request = Request("https://provider.example/v1/chat/completions", headers={"Authorization": KEY})
    for code in (301, 302, 303, 307, 308):
        with pytest.raises(provider._ProviderBoundaryError, match="redirect_rejected"):
            handler.redirect_request(request, None, code, "redirect", {}, "https://other.example/")


def test_default_transport_installs_no_redirect_and_reads_with_size_limit(monkeypatch):
    handlers = []
    calls = []

    class Response:
        status = 200
        headers = {"Content-Length": "999999"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Response()

    def build(*items):
        handlers.extend(items)
        return Opener()

    monkeypatch.setattr(provider, "build_opener", build)
    request = Request("https://provider.example/v1/chat/completions", data=b"{}")
    with pytest.raises(provider._ProviderBoundaryError, match="response_too_large"):
        provider._send_once(request, timeout_seconds=5, max_response_bytes=100)
    assert len(calls) == 1
    assert len(handlers) == 1 and isinstance(handlers[0], provider._NoRedirect)


@pytest.mark.parametrize("action,status", [
    ({"type": "read_source", "path": "helper.py", "start_line": 1, "end_line": 1}, "action_ready"),
    ({"type": "search_source", "query": "label", "paths": ["model.py"], "max_results": 1}, "action_ready"),
    ({"type": "finish"}, "agent_finished"),
    ({"type": "submit_candidate", "edits": [{"path": "model.py", "old": "label: str | None", "new": "label: str | None = None"}]}, "pending_review"),
])
def test_agent_actions_are_validated_but_never_execute_target(
    owned_case, analysis, evidence, arguments, action, status,
):
    from upgrade_workbench.generation import prepare_request

    prepared = prepare_request(owned_case, analysis, evidence, **arguments, output_format="agent_actions")
    raw = provider_response(choices=[{
        "finish_reason": "stop", "message": {"content": json.dumps({"summary": "bounded action", "action": action})},
    }])
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)
    assert report["status"] == status
    assert report["action"] == action
    assert_saved(report)
    with pytest.raises(ProposalInputError, match="already attempted"):
        complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: pytest.fail("replayed"))


def test_candidate_action_rejections_keep_stable_codes_and_feedback(
    owned_case, analysis, evidence, arguments,
):
    from upgrade_workbench.candidates import load_candidate

    revision = load_candidate(owned_case).revision
    cases = [
        (
            {"type": "finish", "unexpected": True},
            "provider_response_rejected",
            "action_invalid_fields",
            "unexpected",
        ),
        (
            {
                "type": "submit_candidate",
                "base_revision": "0" * 64,
                "edits": [{"path": "model.py", "old": "label", "new": "title"}],
            },
            "provider_response_rejected",
            "stale_base_revision",
            "Stale base_revision",
        ),
        (
            {
                "type": "submit_candidate",
                "base_revision": revision,
                "edits": [{"path": "model.py", "old": "not present", "new": "title"}],
            },
            "candidate_rejected",
            "edit_old_text_not_found",
            "old text was not found",
        ),
        (
            {
                "type": "submit_candidate",
                "base_revision": revision,
                "edits": [{"path": "helper.py", "old": "VALUE", "new": "OTHER"}],
            },
            "candidate_rejected",
            "edit_path_outside_allowlist",
            "outside the editable source allowlist",
        ),
        (
            {"type": "shell", "command": "cat secrets"},
            "provider_response_rejected",
            "action_outside_public_source_contract",
            "Unsupported action type",
        ),
    ]

    for action, status, reason, feedback in cases:
        prepared = prepare_request(
            owned_case,
            analysis,
            evidence,
            **arguments,
            output_format="candidate_actions",
        )
        raw = provider_response(choices=[{
            "finish_reason": "stop",
            "message": {"content": json.dumps({"summary": "bounded action", "action": action})},
        }])
        report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)
        assert report["status"] == status
        assert report["reason"] == reason
        assert feedback in report["edit_feedback"]
        # 拒绝报告保留本次请求读取的候选身份；是否产生新候选由 revision 产物判断。
        assert report["candidate_revision"] == revision
        assert not Path(report["report_path"]).with_name("revision").exists()
        assert_saved(report)


@pytest.mark.parametrize("workflow_profile", ["simple_tools", "workbench"])
@pytest.mark.parametrize("operation", [{"type": "read_file"}, ["read_file"]], ids=["object", "list"])
def test_malformed_dependency_operation_is_known_rejection_with_feedback(
    owned_case, analysis, evidence, arguments, monkeypatch, operation, workflow_profile,
):
    prepared = prepare_protocol_v6(
        monkeypatch, owned_case, analysis, evidence,
        arguments | {"workflow_profile": workflow_profile, "experiment_arm": "no_ast"},
        output_format="protocol_v6_actions",
    )
    raw = provider_response(choices=[{
        "finish_reason": "stop",
        "message": {"content": json.dumps({
            "summary": "Read the locked dependency before editing.",
            "action": {
                "type": "query_dependency", "environment": "new", "operation": operation,
            },
        })},
    }])
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return raw

    report = complete_request(prepared, api_key=KEY, transport=transport)

    assert report["status"] == "provider_response_rejected"
    assert report["reason"] == "action_invalid_fields"
    assert "query_dependency" in report["edit_feedback"]
    assert report["model_usage"]["availability"] == "reported"
    assert "action" not in report
    assert not Path(report["report_path"]).with_name("revision").exists()
    assert Path(report["response_path"]).read_bytes() == raw
    assert calls == [1]
    assert_saved(report)


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ({
            "type": "query_dependency",
            "environment": "new",
            "operation": "list_files",
            "distribution": "pydantic",
            "prefix": "pydantic",
        }, "action_ready"),
        ({
            "type": "submit_candidate",
            "base_revision": "CURRENT_REVISION",
            "edits": [{
                "path": "model.py",
                "old": "label: str | None",
                "new": "label: str | None = None",
            }],
        }, "pending_review"),
    ],
)
def test_protocol_v6_solver_actions_use_protocol_v6_validation(
    owned_case, analysis, evidence, arguments, monkeypatch, action, status,
):
    prepared = prepare_protocol_v6(
        monkeypatch,
        owned_case,
        analysis,
        evidence,
        arguments,
        output_format="protocol_v6_actions",
    )
    if action.get("base_revision") == "CURRENT_REVISION":
        action = action | {"base_revision": prepared["candidate_revision"]}
    raw = provider_response(choices=[{
        "finish_reason": "stop",
        "message": {"content": json.dumps({"summary": "Protocol 6 action", "action": action})},
    }])

    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)

    assert report["status"] == status
    assert report["action"] == action
    assert report["output_format"] == "protocol_v6_actions"
    assert (Path(report["report_path"]).with_name("revision").exists()) is (status == "pending_review")
    assert_saved(report)


def test_investigator_accepts_read_only_handoff_without_candidate_side_effect(
    owned_case, analysis, evidence, arguments, monkeypatch,
):
    prepared = prepare_protocol_v6(
        monkeypatch,
        owned_case,
        analysis,
        evidence,
        arguments,
        output_format="investigator_actions",
    )
    action = {
        "type": "handoff",
        "observed_facts": [{
            "statement": "The current field has no explicit default.",
            "scope": "Current registered candidate source only.",
            "evidence_refs": ["source:model.py"],
        }],
        "scope_limits": ["No target behavior was executed."],
        "remaining_hypotheses": [],
        "conflicts": [],
        "next_discriminating_action": None,
    }
    raw = provider_response(choices=[{
        "finish_reason": "stop",
        "message": {"content": json.dumps({"summary": "Bounded handoff", "action": action})},
    }])

    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)

    assert report["status"] == "investigation_ready"
    assert report["action"] == action
    assert not Path(report["report_path"]).with_name("revision").exists()
    assert "candidate_patch" not in report
    assert_saved(report)


def test_investigator_write_is_rejected_with_recoverable_role_feedback(
    owned_case, analysis, evidence, arguments, monkeypatch,
):
    prepared = prepare_protocol_v6(
        monkeypatch,
        owned_case,
        analysis,
        evidence,
        arguments,
        output_format="investigator_actions",
    )
    action = {
        "type": "submit_candidate",
        "base_revision": prepared["candidate_revision"],
        "edits": [{
            "path": "model.py",
            "old": "label: str | None",
            "new": "label: str | None = None",
        }],
    }
    raw = provider_response(choices=[{
        "finish_reason": "stop",
        "message": {"content": json.dumps({"summary": "Forbidden write", "action": action})},
    }])

    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)

    assert report["status"] == "provider_response_rejected"
    assert report["reason"] == "role_write_forbidden"
    assert "read-only" in report["edit_feedback"]
    assert not Path(report["report_path"]).with_name("revision").exists()
    assert_saved(report)


@pytest.mark.parametrize("output_format", ["protocol_v6_actions", "investigator_actions"])
def test_protocol_v6_root_issues_receive_fixed_recoverable_feedback(
    owned_case, analysis, evidence, arguments, monkeypatch, output_format,
):
    prepared = prepare_protocol_v6(
        monkeypatch,
        owned_case,
        analysis,
        evidence,
        arguments,
        output_format=output_format,
    )
    raw = provider_response(choices=[{
        "finish_reason": "stop",
        "message": {"content": json.dumps({
            "summary": "Misplaced issues",
            "action": {"type": "get_fact", "fact_id": "0" * 64},
            "issues": [{"private": "must not be echoed"}],
        })},
    }])

    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)

    assert report["status"] == "provider_response_rejected"
    assert report["reason"] == "diagnostic_issues_outside_action"
    assert report["edit_feedback"].startswith("Response was not executed.")
    assert "must not be echoed" not in report["edit_feedback"]
    assert_saved(report)


@pytest.mark.parametrize("action", [
    {"type": "shell", "command": "echo secret"},
    {"type": "read_source", "path": "checks/test_contract.py", "start_line": 1, "end_line": 1},
    {"type": "read_source", "path": "model.py", "start_line": 1, "end_line": 2, "cwd": "D:/"},
])
def test_provider_actions_cannot_escape_registered_source(owned_case, analysis, evidence, arguments, action):
    from upgrade_workbench.generation import prepare_request

    prepared = prepare_request(owned_case, analysis, evidence, **arguments, output_format="agent_actions")
    raw = provider_response(choices=[{
        "finish_reason": "stop", "message": {"content": json.dumps({"summary": "request tool", "action": action})},
    }])
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: raw)
    assert report["status"] == "provider_response_rejected"
    assert report["reason"] == "action_outside_public_source_contract"
    assert_saved(report)


@pytest.mark.parametrize("field,value", [("model", "other-model"), ("max_output_tokens", 8192), ("experiment_arm", "no_ast")])
def test_receipt_parameters_cannot_disagree_with_provider_payload(prepared, field, value):
    prepared[field] = value
    Path(prepared["report_path"]).write_text(json.dumps(prepared), encoding="utf-8")
    with pytest.raises(ProposalInputError):
        complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: pytest.fail("network"))
    assert not Path(prepared["report_path"]).with_name("attempt.json").exists()


def test_task_context_change_with_rehashed_request_still_fails_receipt_binding(
    owned_case, analysis, evidence, arguments,
):
    from upgrade_workbench.generation import prepare_request

    prepared = prepare_request(owned_case, analysis, evidence, **arguments,
        task_context={"task_id": "parent-task", "attempt_index": 1})
    payload = json.loads(Path(prepared["request_path"]).read_text(encoding="utf-8"))
    context = json.loads(payload["messages"][1]["content"])
    context["task_context"]["task_id"] = "wrong-parent"
    payload["messages"][1]["content"] = json.dumps(context)
    body = provider._json_bytes(payload)
    Path(prepared["request_path"]).write_bytes(body)
    prepared["request_sha256"] = provider._digest(body)
    Path(prepared["report_path"]).write_text(json.dumps(prepared), encoding="utf-8")
    with pytest.raises(ProposalInputError, match="receipt identity"):
        complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: pytest.fail("network"))


def test_public_feedback_cannot_send_provider_secret_back_to_remote(
    owned_case, analysis, evidence, arguments,
):
    from upgrade_workbench.generation import prepare_request
    from upgrade_workbench.generation.edits import edits_to_patch

    edits = [{"path": "model.py", "old": "label: str | None", "new": "label: str | None = None"}]
    digest = provider._digest(edits_to_patch(owned_case, edits))
    task = {
        "task_id": "parent-task", "attempt_index": 2,
        "previous_candidate": {"sha256": digest, "edits": edits},
        "feedback": {
            "scope": "public", "candidate_sha256": digest, "status": "failed",
            "failures": [{"category": "exception", "message": KEY}],
        },
    }
    prepared = prepare_request(owned_case, analysis, evidence, **arguments, task_context=task)
    with pytest.raises(ProposalInputError, match="provider secret"):
        complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: pytest.fail("network"))
    assert not Path(prepared["report_path"]).with_name("attempt.json").exists()
    for path in Path(prepared["report_path"]).parent.iterdir():
        assert KEY.encode() not in path.read_bytes()


@pytest.mark.parametrize("field,value", [
    ("max_source_bytes", 256_000), ("max_source_bytes", 95_000),
    ("max_request_bytes", 512_000), ("max_request_bytes", 191_000),
    ("max_source_bytes", 256_001), ("max_request_bytes", 512_001),
])
def test_receipt_capacity_cannot_change_after_preparation(prepared, field, value):
    prepared[field] = value
    Path(prepared["report_path"]).write_text(json.dumps(prepared), encoding="utf-8")
    with pytest.raises(ProposalInputError):
        complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: pytest.fail("network"))
    assert not Path(prepared["report_path"]).with_name("attempt.json").exists()


def test_old_receipt_without_explicit_capacity_uses_legacy_defaults(prepared):
    payload = json.loads(Path(prepared["request_path"]).read_text(encoding="utf-8"))
    context = json.loads(payload["messages"][1]["content"])
    context.pop("request_capacity")
    payload["messages"][1]["content"] = provider._json_bytes(context).decode()
    body = provider._json_bytes(payload)
    Path(prepared["request_path"]).write_bytes(body)
    prepared.pop("max_source_bytes")
    prepared.pop("max_request_bytes")
    prepared["request_sha256"] = provider._digest(body)
    prepared["public_context_sha256"] = provider._digest(provider._json_bytes(context))
    Path(prepared["report_path"]).write_text(json.dumps(prepared), encoding="utf-8")
    report = complete_request(prepared, api_key=KEY, transport=lambda *a, **kw: provider_response())
    assert report["status"] == "pending_review"
