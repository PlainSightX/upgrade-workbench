"""当前候选的字节、动作、请求与拒绝边界；只做静态处理，不运行目标。"""

import json
from pathlib import Path

import pytest

from upgrade_workbench.candidates import apply_increment, load_candidate, publish_candidate
from upgrade_workbench.cases import PatchValidationError, load_case
from upgrade_workbench.generation import complete_request, prepare_request
from upgrade_workbench.generation.actions import execute_source_action
from upgrade_workbench.generation.provider import (
    _load_prepared,
    _proposal,
    _ProviderBoundaryError,
    _usage,
)
from upgrade_workbench.generation.request import _answer_contract


def test_candidate_prompt_examples_use_the_real_response_envelope():
    examples = [json.loads(line) for line in _answer_contract("candidate_actions").splitlines()
                if line.startswith('{"summary":')]
    assert {example["action"]["type"] for example in examples} == {
        "read_source", "search_source", "submit_candidate", "finish",
    }
    for example in examples:
        raw = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(example)}}]}
        assert _proposal(raw, "candidate_actions") == example


@pytest.mark.parametrize("bad", [{"type": "finish"}, {"type": "json_object", "action": {"type": "finish"}}])
def test_incorrect_envelope_is_rejected_not_silently_repaired(bad):
    raw = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(bad)}}]}
    with pytest.raises(_ProviderBoundaryError, match="invalid_proposal_schema"):
        _proposal(raw, "candidate_actions")


def action(base, old, new, path="model.py"):
    return {"type": "submit_candidate", "base_revision": base.revision,
            "edits": [{"path": path, "old": old, "new": new}]}


def test_two_files_keep_previous_changes_and_allow_explicit_revert(tmp_path):
    case = load_case(Path(__file__).resolve().parents[3] / "cases/bump-my-version-0.5.0-r2/manifest.json")
    base = load_candidate(case)
    first, second = case.manifest.allowed_changes[:2]
    one = apply_increment(case, base, action(base, base.files[first].decode(), "# first\n" + base.files[first].decode(), first), tmp_path / "one")
    two = apply_increment(case, one, action(one, one.files[second].decode(), "# second\n" + one.files[second].decode(), second), tmp_path / "two")
    assert two.files[first] == one.files[first]
    assert two.parent == one.revision
    assert first.encode() in two.patch and second.encode() in two.patch
    revert = apply_increment(case, two, action(two, "# first\n", "", first), tmp_path / "revert")
    assert revert.files[first] == base.files[first]
    assert revert.files[second] == two.files[second]
    assert load_candidate(case, two.reference) == two


@pytest.mark.parametrize("failure", ["stale", "syntax", "ambiguous", "protected"])
def test_invalid_increment_keeps_original_and_valid_revision(owned_case, tmp_path, failure):
    base = load_candidate(owned_case)
    valid = apply_increment(owned_case, base, action(base, "label: str | None", "label: str | None = None"), tmp_path / "valid")
    before = Path(valid.reference["path"]).read_bytes()
    change = action(valid, "label: str | None = None", "label: str | None = 'x'")
    if failure == "stale":
        change["base_revision"] = base.revision
    elif failure == "syntax":
        change["edits"][0]["new"] = "label: ("
    elif failure == "ambiguous":
        change["edits"][0]["old"] = "\n"
    else:
        change["edits"][0]["path"] = "helper.py"
    with pytest.raises(PatchValidationError):
        apply_increment(owned_case, valid, change, tmp_path / "invalid")
    assert Path(valid.reference["path"]).read_bytes() == before
    assert not (tmp_path / "invalid" / "revision.json").exists()
    assert load_candidate(owned_case).files == base.files


def test_reads_searches_and_request_project_current_revision(owned_case, analysis, evidence, arguments, tmp_path):
    base = load_candidate(owned_case)
    current = apply_increment(owned_case, base, action(base, "label: str | None", "label: str | None = None"), tmp_path / "current")
    read = {"type": "read_source", "path": "model.py", "start_line": 1, "end_line": 3}
    actual = execute_source_action(owned_case, read, candidate=current)
    assert "= None" in actual["text"] and actual["revision"] == current.revision
    original = execute_source_action(owned_case, read | {"view": "original"}, candidate=current)
    assert "= None" not in original["text"] and original["view"] == "original"
    search = execute_source_action(owned_case, {"type": "search_source", "query": "= None", "paths": ["model.py"], "max_results": 3}, candidate=current)
    assert search["matches"][0]["line"] == 3
    prepared = prepare_request(owned_case, analysis, evidence, **arguments,
                               output_format="candidate_actions", candidate_reference=current.reference)
    _load_prepared(prepared)
    context = json.loads(json.loads(Path(prepared["request_path"]).read_bytes())["messages"][1]["content"])
    assert context["source_files"] == current.blocks()
    assert context["potential_impacts"] == []
    assert "SECRET_ORACLE" not in json.dumps(context)


def test_candidate_mutation_after_preparation_never_calls_provider(owned_case, analysis, evidence, arguments, tmp_path):
    base = load_candidate(owned_case)
    current = publish_candidate(owned_case, base, {}, tmp_path / "current", origin="official_tool")
    prepared = prepare_request(owned_case, analysis, evidence, **arguments, output_format="candidate_actions", candidate_reference=current.reference)
    Path(current.reference["path"]).write_text("{}")
    with pytest.raises(ValueError, match="candidate"):
        complete_request(prepared, api_key="unused", transport=lambda *_a, **_kw: pytest.fail("must not call"))


@pytest.mark.parametrize("syntax_error", [False, True])
def test_provider_increment_rejection_or_publication(owned_case, analysis, evidence, arguments, syntax_error):
    base = load_candidate(owned_case)
    prepared = prepare_request(owned_case, analysis, evidence, **arguments, output_format="candidate_actions")
    change = action(base, "label: str | None", "label: (" if syntax_error else "label: str | None = None")
    response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"summary": "change", "action": change})}}]}
    result = complete_request(prepared, api_key="unused", transport=lambda *_a, **_kw: json.dumps(response).encode())
    if syntax_error:
        assert result["status"] == "candidate_rejected"
        assert "Syntax error in model.py" in result["edit_feedback"]
    else:
        assert result["status"] == "pending_review"
        current = load_candidate(owned_case, result["candidate_reference"])
        assert current.parent == base.revision
        assert Path(result["candidate_patch"]).read_bytes() == current.patch
    assert load_candidate(owned_case).files == base.files


def test_optional_usage_is_recorded_not_invented():
    value = _usage({"usage": {"prompt_tokens": 20, "completion_tokens": 10,
                             "prompt_cache_hit_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 3}}})
    assert value["cached_input_tokens"] == 5 and value["reasoning_tokens"] == 3
    assert "uncached_input_tokens" not in value


def test_rejected_read_includes_correctable_range_in_next_request(owned_case, analysis, evidence, arguments):
    prepared = prepare_request(owned_case, analysis, evidence, **arguments, output_format="candidate_actions")
    action = {"type": "read_source", "path": "model.py", "start_line": 200, "end_line": 400}
    raw = {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"summary": "read", "action": action}),
    }}]}
    result = complete_request(prepared, api_key="unused", transport=lambda *_a, **_kw: json.dumps(raw).encode())
    assert result["status"] == "provider_response_rejected"
    assert "201 inclusive lines" in result["edit_feedback"]
    assert "end_line must be <= 399" in result["edit_feedback"]
    next_request = prepare_request(owned_case, analysis, evidence, **arguments, output_format="candidate_actions",
        task_context={"task_id": "read-boundary", "attempt_index": 2,
                      "protocol_feedback": {"code": result["reason"], "attempt_index": 1},
                      "edit_feedback": result["edit_feedback"]})
    _load_prepared(next_request)
    context = json.loads(json.loads(Path(next_request["request_path"]).read_bytes())["messages"][1]["content"])
    assert context["task_context"]["edit_feedback"] == result["edit_feedback"]
    assert "SECRET_ORACLE" not in json.dumps(context)
