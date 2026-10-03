"""有界定位只给出可回到源码的线索，不能把同名引用当作已证明的调用。"""

from types import SimpleNamespace

from upgrade_workbench.generation.localization import focus, locations
from upgrade_workbench.generation.source_context import build_context, navigate


def snapshot():
    files = {"model.py": b"def save(item):\n    return normalize(item)\n",
             "helpers.py": b"def normalize(item):\n    return item\n"}
    return SimpleNamespace(files=files, original=files.copy(), revision="r1", origin="original", sha256="empty")


def test_failure_locates_related_definition_with_revision_and_budget():
    s = snapshot()
    obs = [{"revision": "r1", "result": {"failures": [{"location": {"path": "model.py", "line": 2}}]}}]
    requests, report = focus(s, obs, [])
    assert any(r[0] == "helpers.py" and r[3] == "name_matched_definition" for r in requests)
    assert report["seeds"][0]["revision"] == "r1"
    assert "not a resolved type graph" in report["scope"]
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=["model.py"]))
    context = build_context(case, s, observations=obs, assistance="failure_guided")
    assert context["source_selection"]["body_bytes"] <= 96000
    for block in context["source_files"]:
        assert block["text"] == "".join(s.files[block["path"]].decode().splitlines(keepends=True)[block["start_line"] - 1:block["end_line"]])


def test_stale_evidence_not_prioritized_and_deleted_hook_still_visible():
    s = snapshot()
    assert focus(s, [{"revision": "old", "result": {"path": "helpers.py", "line": 1}}], [])[1]["seeds"] == []
    s.original["model.py"] = b"def save(item):\n    hook.save_before(item)\n    return normalize(item)\n"
    report = focus(s, [], [])[1]
    assert any(r["view"] == "original" and "save_before" in r["text"] for r in report["related_behavior"])


def test_same_start_relations_have_stable_complete_order():
    code = b"\n" * 7 + (
        b"def process():\n"
        b"    a = 1\n"
        b"    b = 2\n"
        b"    c = 3\n"
        b"    d = 4\n"
        b"    process()\n"
    )
    s = SimpleNamespace(files={"model.py": code}, original={"model.py": code}, revision="r1")
    observations = [{"revision": "r1", "result": {"failures": [{
        "location": {"path": "model.py", "line": 13}
    }]}}]
    _, report = focus(s, observations, [])
    relations = [row for row in report["possible_relations"] if row["start_line"] == 8]
    assert [(row["end_line"], row["reason"]) for row in relations] == [
        (13, "name_matched_definition"),
        (23, "possible_call_relation"),
    ]


def test_search_discloses_literal_semantics():
    s = snapshot()
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=["model.py"]))
    result = navigate(case, s, {"type": "search_source", "revision": "r1", "scope": "repository", "query": "save OR normalize"})
    assert result["items"] == [] and "literal" in result["search_semantics"]
    assert "OR is literal" in result["query_feedback"]


def test_text_failure_paths_use_the_same_registered_source_identity():
    files = {"dataset/database.py": b"line\n" * 120}
    failure = {"failure_details": [{
        "message": (
            "source/dataset/table.py:306: in _reflect_table\n"
            "source/dataset/database.py:109: TypeError"
        )
    }]}

    assert locations(failure, files) == [("dataset/database.py", 109)]
    assert locations({"message": "/work/source/dataset/database.py:109: TypeError"}, files) == [
        ("dataset/database.py", 109)
    ]
    assert locations({"message": "source/unknown.py:7: TypeError"}, files) == []
    assert locations({"path": "source/dataset/database.py", "line": True}, files) == []

    snapshot = SimpleNamespace(
        files=files,
        original=files.copy(),
        revision="dataset-r1",
        origin="original",
        sha256="dataset-source",
    )
    case = SimpleNamespace(manifest=SimpleNamespace(allowed_changes=["dataset/database.py"]))
    context = build_context(
        case,
        snapshot,
        observations=[{"revision": "dataset-r1", "result": failure}],
        assistance="failure_guided",
    )
    assert context["localization"]["seeds"][0] == {
        "path": "dataset/database.py",
        "line": 109,
        "reason": "current_public_observation",
        "symbol": None,
        "revision": "dataset-r1",
    }
    assert context["source_files"][0]["path"] == "dataset/database.py"
