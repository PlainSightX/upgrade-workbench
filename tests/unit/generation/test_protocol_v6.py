"""Protocol 6 在生成边界拒绝不安全或有别名的依赖路径。"""

from __future__ import annotations

import pytest

from upgrade_workbench.generation.actions import AgentActionError
from upgrade_workbench.generation.protocol_v6 import instructions, validate


def _dependency_action(operation: str, **updates: object) -> dict:
    fields = {
        "list_files": {"prefix": "owned_dependency"},
        "search_text": {
            "query": "Thing",
            "paths": ["owned_dependency/api.py"],
            "max_results": 5,
        },
        "read_file": {
            "path": "owned_dependency/api.py",
            "start_line": 1,
            "end_line": 4,
        },
    }
    return {
        "type": "query_dependency",
        "environment": "new",
        "operation": operation,
        "distribution": "owned-dependency",
        **fields[operation],
        **updates,
    }


@pytest.mark.parametrize(
    ("operation", "updates"),
    [
        ("list_files", {"prefix": "/usr"}),
        ("search_text", {"paths": ["/usr/api.py"]}),
        ("read_file", {"path": "/usr/api.py"}),
        ("list_files", {"prefix": "C:/secret"}),
        ("search_text", {"paths": ["C:/secret.py"]}),
        ("read_file", {"path": "C:secret.py"}),
        ("list_files", {"prefix": "owned_dependency/../secret"}),
        ("search_text", {"paths": ["owned_dependency/../../secret.py"]}),
        ("read_file", {"path": "../secret.py"}),
        ("list_files", {"prefix": "owned_dependency//api"}),
        ("search_text", {"paths": ["owned_dependency/./api.py"]}),
        ("read_file", {"path": "owned_dependency/api.py/"}),
    ],
    ids=[
        "list-absolute",
        "search-absolute",
        "read-absolute",
        "list-windows-drive",
        "search-windows-drive",
        "read-windows-drive",
        "list-traversal",
        "search-traversal",
        "read-traversal",
        "list-noncanonical",
        "search-noncanonical",
        "read-noncanonical",
    ],
)
def test_dependency_queries_reject_noncanonical_paths(
    operation: str,
    updates: dict,
) -> None:
    with pytest.raises(AgentActionError) as failure:
        validate(None, _dependency_action(operation, **updates), None)

    assert failure.value.code == "action_invalid_fields"
    assert "action not executed" in str(failure.value)


@pytest.mark.parametrize("workflow_profile", ["simple_tools", "workbench"])
@pytest.mark.parametrize("field", ["operation", "environment"])
@pytest.mark.parametrize("value", [{"type": "read_file"}, ["read_file"]], ids=["object", "list"])
def test_dependency_queries_reject_unhashable_fields(field, value, workflow_profile):
    action = _dependency_action("read_file") | {field: value}

    with pytest.raises(AgentActionError) as failure:
        validate(None, action, None, workflow_profile=workflow_profile)

    assert failure.value.code == "action_invalid_fields"
    assert "action not executed" in str(failure.value)
    assert action[field] is value


@pytest.mark.parametrize(
    "action",
    [
        _dependency_action("list_files", prefix=""),
        _dependency_action("list_files", prefix="owned_dependency/"),
        _dependency_action("search_text"),
        _dependency_action("read_file"),
    ],
    ids=["empty-prefix", "directory-prefix", "search-paths", "read-path"],
)
def test_dependency_queries_accept_canonical_relative_paths(action: dict) -> None:
    assert validate(None, action, None) == action


def test_investigator_prompt_documents_handoff_recommendation_and_last_call():
    prompt = instructions(role="investigator")
    assert "request_observation" in prompt
    assert "not execution tool names" in prompt
    assert "On the last Investigator call, return handoff" in prompt
    assert "ONE JSON object" in prompt
