"""复现真实失败形状，验证反馈抵达消费者；不把模拟响应计为模型改进。"""

import json
from pathlib import Path

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.generation.actions import AgentActionError
from upgrade_workbench.generation.protocol_v4 import validate
from upgrade_workbench.generation.provider import (
    _proposal,
    _ProviderBoundaryError,
    complete_request,
)

from .test_project_context import CASE, OWNER
from .test_project_context import task as task
from .test_repository_preparation import context
from .test_repository_preparation import prepared_task as prepared_task

MALFORMED = [
    '{"summary":"How "answer lookup order" works","action":{"type":"list_sources"}}',
    '{"summary":"Read source","action":{"type":"read_source","start_line":434,"end_line":474"}}',
]


def envelope(content, *, stop="stop"):
    return {"usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"finish_reason": stop, "message": {"content": content}}]}


@pytest.mark.parametrize("content", MALFORMED)
def test_invalid_json_feedback_reports_inner_position_without_echo(content):
    with pytest.raises(json.JSONDecodeError) as expected:
        json.loads(content)
    with pytest.raises(_ProviderBoundaryError) as rejected:
        _proposal(envelope(content), "repository_context_actions")
    assert rejected.value.code == "invalid_json"
    feedback = rejected.value.feedback
    assert f"line {expected.value.lineno}, column {expected.value.colno}" in feedback
    assert f"character {expected.value.pos}" in feedback
    assert "Action not executed" in feedback and "No automatic repair" in feedback
    assert "answer lookup order" not in feedback
    assert len(feedback) <= 1000


def test_escaped_quotes_and_integer_lines_are_accepted():
    proposal = {"summary": 'How "answer lookup order" works',
                "action": {"type": "read_source", "start_line": 434, "end_line": 474}}
    assert _proposal(envelope(json.dumps(proposal)), "repository_context_actions") == proposal


@pytest.mark.parametrize("content,code", [
    ('{"summary":"a","summary":"b","action":{}}', "duplicate_json_key"),
    ('{"summary":"a","action":{"value":NaN}}', "nonfinite_json_number"),
    ('{"summary":"a","action":{"value":Infinity}}', "nonfinite_json_number"),
])
def test_strict_decoding_not_repaired(content, code):
    with pytest.raises(_ProviderBoundaryError) as rejected:
        _proposal(envelope(content), "repository_context_actions")
    assert rejected.value.code == code


def test_length_response_is_not_reclassified_as_json_error():
    with pytest.raises(_ProviderBoundaryError) as rejected:
        _proposal(envelope(MALFORMED[0], stop="length"), "repository_context_actions")
    assert rejected.value.code == "response_not_complete"


def test_secret_redaction_still_precedes_json_feedback(prepared):
    secret = "owned-provider-secret-do-not-record"
    content = '{"summary":"' + secret + '"broken","action":{}}'
    report = complete_request(prepared, api_key=secret,
                              transport=lambda *_a, **_k: json.dumps(envelope(content)).encode())
    assert report["reason"] == "provider_secret_echo"
    assert not report.get("edit_feedback")
    assert report["target_code_executed"] is False
    assert secret not in Path(report["response_path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("field", ["purpose", "expected_observation", "revision_reason"])
@pytest.mark.parametrize("size", [1000, 1001, 1029, 1042])
def test_probe_prose_bounds_are_unchanged(field, size):
    case = load_case(CASE)
    snapshot = load_candidate(case)
    action = {"type": "propose_probe", "revision": snapshot.revision,
              "code": "def test_value():\n    pass\n", "purpose": "Observe a local value",
              "expected_observation": "A local value, not general correctness",
              "evidence_refs": ["business_contract"], "oracle": None}
    if field == "revision_reason":
        action.update(type="revise_probe", parent_probe_id="a" * 64)
    # 多字节文字仍按字符数计，不偷偷改为字节上限。
    action[field] = "观" * size
    if size <= 1000:
        assert validate(case, action, snapshot) == action
    else:
        with pytest.raises(AgentActionError, match=f"{field}.*maximum 1000.*length={size}"):
            validate(case, action, snapshot)


@pytest.mark.parametrize("content", MALFORMED)
def test_normal_preparation_continuation_consumes_json_feedback(prepared_task, content):
    seen = []
    def transport(request, **kwargs):
        ctx = context(request)
        seen.append(ctx)
        assert "strict JSON" in json.loads(request.data)["messages"][0]["content"]
        if len(seen) == 1:
            return json.dumps(envelope(content)).encode()
        assert ctx["task_context"]["protocol_feedback"]["code"] == "invalid_json"
        assert "assistant message.content at line" in ctx["task_context"]["edit_feedback"]
        action = {"type": "list_sources", "revision": ctx["candidate"]["revision"]}
        return json.dumps(envelope(json.dumps({"summary": "Inspect source", "action": action}))).encode()
    result = tasks.advance_task(Path(prepared_task["task_path"]), api_key="unused",
                                transport=transport, execution_owner=OWNER)
    assert len(seen) == len(result["attempts"]) == 2
    assert result["stop_reason"] == "repository_preparation_exhausted_without_handoff"
    assert result.get("project_knowledge_reference") is None
    assert result["candidate"] is None and result["diagnostic_runs"] == []
    assert len(result["observations"]) == 1  # 只有第二次合法导航真正生效。
    first = json.loads(Path(result["attempts"][0]["receipt"]).read_text(encoding="utf-8"))
    assert first["model_usage"]["availability"] == "reported"
    assert first["automatic_retries"] == 0 and first["target_code_executed"] is False
