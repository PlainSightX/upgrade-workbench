"""检查只读工具不能通过路径、动作名或返回值绕过公开源码边界。"""

import pytest

from upgrade_workbench.generation.actions import (
    AgentActionError,
    execute_source_action,
    validate_action,
)


def test_read_registered_original_source(owned_case):
    action = {"type": "read_source", "path": "model.py", "start_line": 2, "end_line": 3}
    result = execute_source_action(owned_case, action)
    assert result == {
        "path": "model.py", "start_line": 2, "end_line": 3,
        "text": "class Profile(BaseModel):\n    label: str | None\n",
    }


def test_search_is_literal_and_cannot_search_non_source(owned_case):
    result = execute_source_action(owned_case, {
        "type": "search_source", "paths": ["model.py", "helper.py"],
        "query": "=", "max_results": 1,
    })
    assert result == {"matches": [{"path": "helper.py", "line": 1, "text": "VALUE = 3"}], "truncated": False}
    empty = execute_source_action(owned_case, {
        "type": "search_source", "paths": ["model.py"], "query": ".*", "max_results": 1,
    })
    assert empty["matches"] == []


@pytest.mark.parametrize("path", [
    "../checks/test_contract.py", "checks/test_contract.py", "tests/test_private.py",
    "C:/secret.txt", "/tmp/secret", "reference.patch", "evidence/migration.md",
    "source/model.py", "model.py/../helper.py",
])
def test_read_action_rejects_paths_outside_public_source(owned_case, path):
    with pytest.raises(AgentActionError):
        validate_action(owned_case, {"type": "read_source", "path": path, "start_line": 1, "end_line": 1})


@pytest.mark.parametrize("action", [
    {"type": "shell", "command": "cat secrets"}, {"type": "finish", "message": "extra"},
    {"type": "read_source", "path": "model.py", "start_line": True, "end_line": 2},
    {"type": "read_source", "path": "model.py", "start_line": 1, "end_line": 201},
    {"type": "search_source", "query": "x", "paths": ["model.py", "model.py"], "max_results": 1},
    {"type": "search_source", "query": "x", "paths": ["model.py"], "max_results": 0},
])
def test_unbounded_actions_are_rejected(owned_case, action):
    with pytest.raises(AgentActionError):
        validate_action(owned_case, action)


def test_read_range_must_exist_and_finish_is_not_a_tool(owned_case):
    result = execute_source_action(owned_case, {"type": "read_source", "path": "model.py", "start_line": 1, "end_line": 100})
    assert result == {"error": {"code": "read_range_out_of_bounds", "available_lines": 3}}
    with pytest.raises(AgentActionError, match="Only read_source"):
        execute_source_action(owned_case, {"type": "finish"})


@pytest.mark.parametrize("start,end", [(1, 200), (201, 400), (1040, 1239)])
def test_read_limit_counts_both_endpoints(owned_case, start, end):
    action = {"type": "read_source", "path": "model.py", "start_line": start, "end_line": end}
    assert validate_action(owned_case, action) == action
    with pytest.raises(AgentActionError) as failure:
        validate_action(owned_case, action | {"end_line": end + 1})
    assert f"range {start}..{end + 1}: 201 inclusive lines" in str(failure.value)
    assert f"end_line must be <= {end}" in str(failure.value)
    assert "No source was read" in str(failure.value)


def test_action_rejections_keep_schema_stale_and_scope_categories(owned_case):
    from upgrade_workbench.candidates import load_candidate

    base = load_candidate(owned_case)
    actions = [
        ({"type": "finish", "unexpected": True}, "action_invalid_fields"),
        ({
            "type": "submit_candidate",
            "base_revision": "0" * 64,
            "edits": [{"path": "model.py", "old": "label", "new": "title"}],
        }, "stale_base_revision"),
        ({
            "type": "read_source",
            "path": "checks/test_contract.py",
            "start_line": 1,
            "end_line": 1,
        }, "action_outside_public_source_contract"),
        ({"type": "shell", "command": "cat secrets"}, "action_outside_public_source_contract"),
    ]

    for action, code in actions:
        with pytest.raises(AgentActionError) as failure:
            validate_action(owned_case, action, candidate=base)
        assert failure.value.code == code
