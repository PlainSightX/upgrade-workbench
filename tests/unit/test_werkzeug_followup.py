"""固定双依赖支持、原文检索及普通请求入口；不在宿主导入 FlaskBB。"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from upgrade_workbench.analysis import analyze_case
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import CaseValidationError, load_case
from upgrade_workbench.evidence import load_version_evidence
from upgrade_workbench.planning import prepare_case_proposal
from upgrade_workbench.probe_scaffolds import scaffold_for
from upgrade_workbench.retrieval import EvidenceIndex, evidence_for_sources

CASE = Path(__file__).parents[2] / "cases/flaskbb-werkzeug-2.1/manifest.json"


def replace_file(root, name, data):
    (root / name).write_bytes(data)
    manifest = json.loads((root / "manifest.json").read_bytes())
    manifest["file_hashes"][name] = hashlib.sha256(data).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def copied(tmp_path):
    root = tmp_path / "case"
    shutil.copytree(CASE.parent, root)
    return root


def test_paired_evidence_and_generic_navigation():
    case = load_case(CASE)
    report = load_version_evidence(case)
    assert report["binding"]["package"] == "werkzeug"
    assert report["binding"]["companion"] == {"package": "flask", "old_version": "2.0.1", "new_version": "2.1.2"}
    analysis = analyze_case(case)
    assert analysis["findings"] == []
    assert "No dedicated werkzeug" in analysis["scope_limitations"][0]
    assert analysis["source_binding"]["files"]["flaskbb/management/views.py"] == case.manifest.file_hashes["source/flaskbb/management/views.py"]
    scaffold = scaffold_for(case)
    assert "post" not in scaffold.fixtures and "admin_user" in scaffold.fixtures
    assert "WTF_CSRF_ENABLED = False" in scaffold.code


@pytest.mark.parametrize("lock,package,version", [
    ("old.txt", "flask", "2.0.1"), ("new.txt", "flask", "2.1.2"),
    ("new.txt", "werkzeug", "2.1.2"),
])
def test_either_dependency_pin_mismatch_rejected(copied, lock, package, version):
    name = "requirements/" + lock
    raw = (copied / name).read_bytes()
    assert (package + "==" + version).encode() in raw
    replace_file(copied, name, raw.replace((package + "==" + version).encode(), (package + "==2.2.0").encode()))
    with pytest.raises(CaseValidationError, match="does not match"):
        load_version_evidence(load_case(copied / "manifest.json"))


@pytest.mark.parametrize("change", [
    {"schema_version": 1}, {"repository": "https://example.com/not-official"},
    {"new_version": "2.2.0"}, {"companion": None},
])
def test_unsupported_family_or_incomplete_pair_rejected(copied, change):
    value = json.loads((copied / "evidence/bundle.json").read_bytes())
    value.update(change)
    replace_file(copied, "evidence/bundle.json", json.dumps(value).encode())
    with pytest.raises(CaseValidationError, match="Invalid version evidence"):
        load_version_evidence(load_case(copied / "manifest.json"))


def test_nonobject_bundle_has_controlled_error(copied):
    replace_file(copied, "evidence/bundle.json", b"[]")
    with pytest.raises(CaseValidationError, match="expected an object"):
        load_version_evidence(load_case(copied / "manifest.json"))


def test_request_json_retrieval_uses_exact_official_source():
    case = load_case(CASE)
    index = EvidenceIndex(case)
    hits = index.search("get_json content type application json", top_k=3)["hits"]
    assert hits
    assert any("get_json" in hit["parent"]["excerpt"] for hit in hits)
    evidence = evidence_for_sources(case, load_candidate(case).blocks(), max_bytes=48000)
    assert "werkzeug-v21-request-json" in {entry["evidence_key"] for entry in evidence["entries"]}
    for hit in hits:
        parent = hit["parent"]
        lines = (case.root / parent["path"]).read_text(encoding="utf-8").splitlines(keepends=True)
        assert parent["excerpt"] == "".join(lines[parent["start_line"]-1:parent["end_line"]])


def test_ordinary_first_request_can_be_prepared_without_host_execution(tmp_path):
    prepared = prepare_case_proposal(
        CASE, tmp_path / "work", model="unit-test-model",
        endpoint="https://provider.example/v1/chat/completions",
        max_output_tokens=2048, timeout_seconds=30, public_source_ack=True,
        thinking_mode="disabled", max_source_bytes=1600000, max_request_bytes=850000,
    )
    assert prepared["status"] == "request_prepared"
    assert prepared["calls"] == 0 and prepared["target_code_executed"] is False
    request = Path(prepared["request_path"]).read_text(encoding="utf-8")
    assert "werkzeug-v21-request-json" in request
    assert "test_management_regressions.py" not in request
