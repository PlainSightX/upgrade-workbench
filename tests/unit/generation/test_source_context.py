"""聚焦正文不能缩小已登记源码可访问性，也不能破坏字节身份。"""

import pytest

from upgrade_workbench.candidates import Candidate, load_candidate, publish_candidate
from upgrade_workbench.generation.protocol_v4 import validate
from upgrade_workbench.generation.source_context import (
    build_context,
    navigate,
    outline,
    source_comparison,
)


def test_inventory_omission_navigation_and_current_identity(owned_case, tmp_path):
    base = load_candidate(owned_case)
    focused = build_context(owned_case, base)
    assert len(focused["source_inventory"]["items"]) == 2
    assert [f["path"] for f in focused["source_files"]] == ["model.py"]
    assert focused["source_selection"]["omitted_file_count"] == 1
    action = {"type": "search_source", "revision": base.revision, "scope": "repository", "query": "VALUE"}
    validate(owned_case, action, base)
    assert navigate(owned_case, base, action)["items"][0]["path"] == "helper.py"
    changed = publish_candidate(owned_case, base, {"model.py": b"# changed\n" + base.files["model.py"]},
                                tmp_path / "candidate", origin="agent_candidate")
    original = navigate(owned_case, changed, {"type": "read_source", "view": "original",
                        "revision": base.revision, "path": "model.py", "start_line": 1, "end_line": 200})
    assert original["revision"] == base.revision and original["end_line"] == 3 and original["eof"]
    assert original["requested_end_line"] == 200
    with pytest.raises(ValueError, match="Stale"):
        navigate(owned_case, changed, action)


def test_search_pagination_is_query_and_revision_bound(owned_case):
    base = load_candidate(owned_case)
    action = {"type": "search_source", "revision": base.revision, "scope": "repository", "query": "e", "max_results": 1}
    first = navigate(owned_case, base, action)
    assert first["next_cursor"]
    second = navigate(owned_case, base, action | {"cursor": first["next_cursor"]})
    assert second["offset"] == 1 and first["items"] != second["items"]
    with pytest.raises(ValueError, match="Stale"):
        navigate(owned_case, base, action | {"query": "x", "cursor": first["next_cursor"]})


def test_source_comparison_binds_original_current_and_changed_hunks(owned_case, tmp_path):
    base = load_candidate(owned_case)
    changed = publish_candidate(
        owned_case,
        base,
        {"model.py": b"# changed\n" + base.files["model.py"]},
        tmp_path / "comparison-candidate",
        origin="agent_candidate",
    )

    comparison = source_comparison(owned_case, changed)

    assert comparison["original_revision"] == base.revision
    assert comparison["current_revision"] == changed.revision
    assert comparison["changed_files"] == ["model.py"]
    assert [row["path"] for row in comparison["file_identities"]] == ["model.py"]
    assert "--- a/model.py" in comparison["canonical_unified_diff"]
    assert "helper.py" not in comparison["canonical_unified_diff"]


def test_source_comparison_rejects_noncanonical_patch(owned_case):
    base = load_candidate(owned_case)
    tampered = Candidate(
        revision=base.revision,
        parent=base.parent,
        original=base.original,
        files=base.files,
        patch=b"--- a/helper.py\n+++ b/helper.py\n",
        origin=base.origin,
    )

    with pytest.raises(ValueError, match="canonical"):
        source_comparison(owned_case, tampered)


def test_outline_decorators_nested_symbols_and_parse_error():
    symbols = outline(b"import os\nclass A:\n    @classmethod\n    def f(cls):\n        return 1\n")
    assert symbols[-1] == {"kind": "FunctionDef", "name": "A.f", "start_line": 3, "end_line": 5}
    assert outline(b"def broken(")[0]["kind"] == "parse_error"


@pytest.mark.parametrize("action", [
    {"type": "read_source", "path": "model.py", "start_line": 200, "end_line": 400},
    {"type": "search_source", "query": "x", "scope": "repository", "paths": ["model.py"]},
    {"type": "outline_source", "path": "../checks/test_contract.py"},
    {"type": "finish", "reason": "passed", "explanation": "done", "evidence_refs": [],
     "contract_coverage": [{"requirement": "invalid", "scope": "in_contract", "status": "verified",
                            "evidence_refs": ["business_contract"], "tool_limitation": None}]},
])
def test_invalid_actions_rejected(owned_case, action):
    base = load_candidate(owned_case)
    if action["type"] != "finish":
        action["revision"] = base.revision
    with pytest.raises(ValueError):
        validate(owned_case, action, base)


def test_read_first_preserves_recent_exact_code_before_automatic_focus():
    from types import SimpleNamespace

    from upgrade_workbench.generation.source_context import POLICY

    files = {"auto.py": b"value = 1\n" * 400,
             "read.py": b"retained = 1\n" * 100}
    snapshot = SimpleNamespace(files=files, original=files, revision="a" * 64,
                               origin="original", sha256=None)
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=["auto.py"]))

    def read(start, end, revision=snapshot.revision):
        return {"result": {"path": "read.py", "view": "current", "revision": revision,
                           "start_line": start, "end_line": end}}

    observations = [read(1, 70), read(65, 100), read(1, 100, "stale")]
    result = build_context(case, snapshot, POLICY | {"body_bytes": 1024},
        observations=observations, findings=[{"path": "auto.py", "line": 1}],
        assistance="failure_guided", prioritize_reads=True, retain_reads_first=True)
    first = result["source_files"][0]
    assert (first["path"], first["start_line"], first["end_line"]) == ("read.py", 65, 100)
    assert first["text"] == "".join(files["read.py"].decode().splitlines(keepends=True)[64:100])
    assert result["source_selection"]["read_retention"] == {
        "requested_lines": 100, "included_lines": 78, "complete": False,
        "omitted_ranges": [{"path": "read.py", "start_line": 43, "end_line": 64}],
    }
    roomy = build_context(case, snapshot, POLICY, observations=observations,
        findings=[{"path": "auto.py", "line": 1}], assistance="failure_guided", retain_reads_first=True)
    assert roomy["source_selection"]["read_retention"] == {
        "requested_lines": 100, "included_lines": 100, "complete": True, "omitted_ranges": [],
    }
    stale = build_context(case, snapshot, POLICY, observations=[read(1, 100, "stale")], retain_reads_first=True)
    assert stale["source_selection"]["read_retention"]["requested_lines"] == 0
