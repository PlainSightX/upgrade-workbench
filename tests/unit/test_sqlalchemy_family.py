"""第二家族版本、原文、路由和生成输入的合同回归；不执行第三方目标。"""

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from upgrade_workbench.analysis import analyze_case
from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.codemod import run_official_tool
from upgrade_workbench.evidence import VersionEvidence, load_version_evidence, migration_package
from upgrade_workbench.execution.postgres import PostgresSpec, load_postgres_spec
from upgrade_workbench.generation.provider import _load_prepared
from upgrade_workbench.planning import prepare_case_proposal
from upgrade_workbench.retrieval import EvidenceIndex, evidence_for_sources

CASE = Path(__file__).resolve().parents[2] / "cases/apscheduler-3.9.1-pg-r2/manifest.json"
SQLALCHEMY14_CASE = (
    Path(__file__).resolve().parents[2]
    / "cases/flaskbb-sqlalchemy-1.4.21/manifest.json"
)
CSVSQL_CASE = Path(__file__).resolve().parents[2] / "cases/csvkit-csvsql-sqlalchemy-2-r2/manifest.json"


def test_locks_change_only_migrated_package():
    case = load_case(CASE)
    packages = []
    for path in (case.old_lock, case.new_lock):
        packages.append(dict(re.findall(r"^([\w-]+)==([\d.]+)", path.read_text(), re.M)))
    assert packages[0].pop("sqlalchemy") == "1.4.54"
    assert packages[1].pop("sqlalchemy") == "2.0.38"
    assert packages[0] == packages[1]


def test_package_routes_to_actual_supported_evidence_and_analyzer():
    case = load_case(CASE)
    assert migration_package(case) == "sqlalchemy"
    report = analyze_case(case)
    assert report["analyzer"]["name"].startswith("sqlalchemy-")
    assert len(report["findings"]) == 11
    assert {x["rule"] for x in report["findings"]} == {"sa_select_list", "sa_engine_execute"}
    bundle = load_version_evidence(case)
    assert bundle["binding"]["new_version"] == "2.0.38"


def test_sqlalchemy_14_evidence_uses_its_own_version_family():
    case = load_case(SQLALCHEMY14_CASE)
    bundle = load_version_evidence(
        case, required_keys={"sqlalchemy-v14-cascade-backrefs"}
    )
    assert migration_package(case) == "sqlalchemy"
    assert bundle["binding"]["old_version"] == "1.3.24"
    assert bundle["binding"]["new_version"] == "1.4.21"
    assert bundle["binding"]["upstream_path"].endswith("migration_14.rst")
    assert bundle["entries"][0]["heading"].startswith("cascade_backrefs")


def test_sqlalchemy_14_analysis_does_not_reuse_v2_findings():
    case = load_case(SQLALCHEMY14_CASE)
    report = analyze_case(case)
    assert report["analyzer"]["name"].startswith("sqlalchemy-1.3-to-1.4-")
    assert report["findings"] == []
    assert report["unknowns"] == []
    assert not any(
        value.startswith("sqlalchemy-v2-")
        for item in report["findings"]
        for value in [item.get("evidence_key", "")]
    )
    assert any("runtime checks" in item for item in report["scope_limitations"])


@pytest.mark.parametrize("field,value", [
    ("package", "pydantic"), ("repository", "https://github.com/other/sqlalchemy"),
    ("upstream_path", "docs/migration.md"), ("old_version", "1.3.24"),
])
def test_cross_family_or_unsupported_origin_rejected(field, value):
    data = json.loads((CASE.parent / "evidence/bundle.json").read_bytes())
    data[field] = value
    with pytest.raises(ValueError):
        VersionEvidence.model_validate(data)


def test_sqlalchemy_14_rejects_v2_document_and_evidence_keys():
    data = {
        "schema_version": 1,
        "package": "sqlalchemy",
        "old_version": "1.3.24",
        "new_version": "1.4.21",
        "repository": "https://github.com/sqlalchemy/sqlalchemy",
        "revision": "7ed27d8245d6b4cf98e67fe80ae3903680aa1453",
        "upstream_path": "doc/build/changelog/migration_20.rst",
        "license_path": "evidence/LICENSE",
        "entries": [{
            "evidence_key": "sqlalchemy-v2-connectionless",
            "path": "evidence/migration.rst",
            "sha256": "0" * 64,
            "start_line": 1,
            "end_line": 1,
            "heading": "heading",
        }],
    }
    with pytest.raises(ValueError):
        VersionEvidence.model_validate(data)


def test_complete_rst_is_covered_exactly_and_searchable():
    index = EvidenceIndex(load_case(CASE))
    raw = (CASE.parent / "evidence/migration.rst").read_bytes().decode()
    assert "".join(x["excerpt"] for x in index.parents) == raw
    assert len(index.parents) > 25
    for item in index.parents:
        assert item["excerpt"] == "".join(raw.splitlines(keepends=True)[item["start_line"] - 1:item["end_line"]])
    result = index.search("autocommit commit rollback")
    assert result["hits"] and "rst_sections" in result["method"]
    assert all("sqlalchemy/sqlalchemy" in hit["parent"]["url"] for hit in result["hits"])
    assert index.anchors["sqlalchemy-v2-connectionless"]["start_line"] == 709


def test_rst_include_cannot_read_host(tmp_path):
    path = tmp_path / "private.txt"
    path.write_text("not-a-document-secret", encoding="utf-8")
    index = EvidenceIndex(load_case(CASE))
    index.parents.clear()
    index.children.clear()
    index._add_document("evidence/test.rst", "a" * 64,
                        f"Title\n=====\n\n.. include:: {path.as_posix()}\n")
    assert "not-a-document-secret" not in json.dumps(index.parents)


def test_query_legacy_gets_document_not_forced_ast_rewrite():
    case = load_case(CASE)
    text = "from sqlalchemy.orm import Session\nsession.query(Item).all()\n"
    result = evidence_for_sources(case, [{"path": "app.py", "text": text,
                                           "sha256": hashlib.sha256(text.encode()).hexdigest()}], max_bytes=48000)
    assert "sqlalchemy-v2-query" in {x["evidence_key"] for x in result["entries"]}


@pytest.mark.parametrize("manifest, extra_anchor", [
    (CASE, "sqlalchemy-v2-execute"),
    (CSVSQL_CASE, None),
])
def test_execute_retains_connection_evidence_and_registered_sql_semantics(manifest, extra_anchor):
    case = load_case(manifest)
    text = "connection.execute(query)\n"
    result = evidence_for_sources(case, [{"path": "app.py", "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest()}], max_bytes=48000)
    keys = {item["evidence_key"] for item in result["entries"]}
    assert "sqlalchemy-v2-connectionless" in keys
    if extra_anchor is not None:
        assert extra_anchor in keys


def test_request_contains_bound_family_not_checks_or_pg_credentials(tmp_path):
    receipt = prepare_case_proposal(CASE, tmp_path, model="test-model",
        endpoint="https://example.test/chat/completions", max_output_tokens=1024,
        timeout_seconds=10, public_source_ack=True, output_format="candidate_actions",
        max_source_bytes=256000, max_request_bytes=512000)
    _load_prepared(receipt)
    payload = json.loads(Path(receipt["request_path"]).read_bytes())
    data = json.loads(payload["messages"][1]["content"])
    assert data["migration_versions"] == {"package": "sqlalchemy", "old_version": "1.4.54", "new_version": "2.0.38"}
    assert len(data["potential_impacts"]) == 11
    assert len(data["source_files"]) == 38
    assert data["allowed_changes"] == ["apscheduler/jobstores/sqlalchemy.py"]
    assert not any("checks/" in x["path"] for x in data["source_files"])
    assert "UPGRADE_WORKBENCH_PG_DSN" not in json.dumps(data)
    assert all(not x["evidence_key"].startswith("pydantic") for x in data["version_evidence"])
    assert receipt["context_derivation"]


def test_sqlalchemy_14_request_uses_only_its_bound_evidence_family(tmp_path):
    receipt = prepare_case_proposal(
        SQLALCHEMY14_CASE,
        tmp_path,
        model="test-model",
        endpoint="https://example.test/chat/completions",
        max_output_tokens=1024,
        timeout_seconds=10,
        public_source_ack=True,
        output_format="candidate_actions",
        max_source_bytes=1_600_000,
        max_request_bytes=2_400_000,
    )
    _load_prepared(receipt)
    payload = json.loads(Path(receipt["request_path"]).read_bytes())
    data = json.loads(payload["messages"][1]["content"])
    assert data["migration_versions"] == {
        "package": "sqlalchemy",
        "old_version": "1.3.24",
        "new_version": "1.4.21",
    }
    assert data["potential_impacts"] == []
    assert data["version_evidence"]
    assert all(
        item["path"] == "evidence/migration.rst"
        and item["sha256"]
        == "fea04287caa2ac1f5b1a34825db5c72d85cec015e887cc40774cf5625395249d"
        and not item["evidence_key"].startswith("sqlalchemy-v2-")
        for item in data["version_evidence"]
    )


def test_wrong_official_seed_rejected_before_any_execution(tmp_path):
    with pytest.raises(ValueError, match="No reviewed official seed"):
        run_official_tool(CASE, tmp_path / "seed", reviewed_tool=True)
    assert not (tmp_path / "seed").exists()


@pytest.mark.parametrize("extra", [{"host": "host.docker.internal"}, {"password": "host-secret"},
                                    {"image": "postgres:16-alpine"}, {"kind": "sqlite"},
                                    {"schema_version": True}])
def test_execution_cannot_expand_into_host_database(extra):
    data = load_postgres_spec(load_case(CASE))
    with pytest.raises(ValueError):
        PostgresSpec.model_validate({**data, **extra})


def test_execution_manifest_must_be_registered_and_unchanged(tmp_path):
    root = tmp_path / "case"
    shutil.copytree(CASE.parent, root)
    case = load_case(root / "manifest.json")
    (root / "execution.json").write_text("{}")
    with pytest.raises(ValueError, match="SHA-256"):
        load_postgres_spec(case)


def test_source_snapshot_keeps_all_unrelated_modules_immutable():
    case = load_case(CASE)
    snapshot = load_candidate(case)
    provenance = json.loads((CASE.parent / "SOURCE.json").read_bytes())
    for name, record in provenance["files"].items():
        if name.startswith("source/"):
            assert hashlib.sha256(snapshot.files[name[7:]]).hexdigest() == record["sha256"]
