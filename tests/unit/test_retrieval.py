"""检索质量小样本与原文合同分开验证，不声称完整迁移成功率。"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import CaseValidationError, load_case
from upgrade_workbench.cli import main
from upgrade_workbench.retrieval import EvidenceIndex, evidence_for_sources, retrieve_evidence

CASE = Path(__file__).resolve().parents[2] / "cases/copier-6.2.0-r2/manifest.json"


@pytest.fixture
def index():
    return EvidenceIndex(load_case(CASE))


@pytest.mark.parametrize("query,line", [
    ("dataclasses", 296), ("__get_validators__", 771),
    ("required optional nullable", 647), ("update_forward_refs", 129),
    ("parse_obj_as TypeAdapter", 737), ("BaseSettings", 827),
])
def test_named_development_queries_find_expected_section_first(index, query, line):
    report = index.search(query, top_k=3)
    assert report["hits"][0]["parent"]["start_line"] == line
    assert report == index.search(query, top_k=3)
    assert report["binding"]["new_version"] == "2.11.7"


def test_every_chunk_is_exact_and_covers_parent_once(index):
    case = load_case(CASE)
    for parent in index.parents:
        children = [child for child in index.children if child["parent"] == parent]
        assert children[0]["start_line"] == parent["start_line"]
        assert children[-1]["end_line"] == parent["end_line"]
        assert "".join(child["excerpt"] for child in children) == parent["excerpt"]
        for left, right in zip(children, children[1:]):
            assert left["end_line"] + 1 == right["start_line"]
        for item in [parent, *children]:
            raw = (case.root / item["path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == item["sha256"]
            lines = raw.decode().splitlines(keepends=True)
            assert item["excerpt"] == "".join(lines[item["start_line"] - 1:item["end_line"]])
            assert f'#L{item["start_line"]}-L{item["end_line"]}' in item["url"]


@pytest.mark.parametrize("query", ["", "***", "___", "zzzz_no_such_migration_word"])
def test_empty_and_unmatched_queries_never_fabricate_fallback(index, query):
    assert index.search(query)["status"] == "no_match"


def test_fts_operators_are_text_not_program(index):
    assert isinstance(index.search('" OR body:dataclass NOT xyz; DROP TABLE chunks; --')["hits"], list)
    assert index.search("dataclasses")["hits"][0]["parent"]["start_line"] == 296


@pytest.mark.parametrize("query,top_k", [("a" * 1025, 3), ("a", True), ("a", 21), (None, 3)])
def test_query_capacity_is_explicit(index, query, top_k):
    with pytest.raises(ValueError):
        index.search(query, top_k=top_k)


def test_fake_heading_in_code_fence_is_not_parent(index):
    index.parents.clear()
    index.children.clear()
    text = "# Root\r\n\r\n## Real\r\n```python\r\n# Fake\r\nx = 1\r\n```\r\n\r\n## Next\r\nEnd\r\n"
    index._add_document("evidence/test.md", "a" * 64, text)
    assert [parent["heading"] for parent in index.parents] == ["Root", "Real", "Next"]
    assert "# Fake\r\n" in index.parents[1]["excerpt"]


def test_wrong_version_cannot_be_indexed(tmp_path):
    root = tmp_path / "case"
    shutil.copytree(CASE.parent, root)
    bundle_path = root / "evidence/bundle.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["new_version"] = "2.11.6"
    bundle_path.write_text(json.dumps(bundle))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_hashes"]["evidence/bundle.json"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(CaseValidationError, match="does not match"):
        retrieve_evidence(load_case(manifest_path), "dataclasses")


def test_source_lexical_queries_ignore_comments_templates_and_test_data():
    case = load_case(CASE)
    text = '# dataclass\nx = "__get_validators__"\n'
    blocks = [{"path": "module.py", "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text}]
    report = evidence_for_sources(case, blocks, max_bytes=48000)
    assert report["symbols"] == [] and report["entries"] == []
    with pytest.raises(ValueError, match="Required symbol evidence"):
        evidence_for_sources(case, load_candidate(case).blocks(), max_bytes=1)


def test_missing_optional_capacity_is_recorded_not_truncated():
    case = load_case(CASE)
    text = "from pydantic import parse_obj_as\n"
    blocks = [{"path": "module.py", "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text}]
    report = evidence_for_sources(case, blocks, max_bytes=1)
    assert report["entries"] == [] and report["omitted"]
    assert all(item["reason"] == "capacity" for item in report["omitted"])


def test_cli_retrieve_needs_no_provider_or_target_execution(capsys):
    assert main(["retrieve", str(CASE), "--query", "__get_validators__", "--top-k", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["hits"]) == 1 and report["hits"][0]["parent"]["start_line"] == 771
