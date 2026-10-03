"""版本证据只绑定已注册原文；这些测试不依赖正在审阅的真实应用案例。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from upgrade_workbench.cases.manifest import CaseValidationError, load_case
from upgrade_workbench.evidence import load_version_evidence

CALIBRATION = Path(__file__).parents[2] / "cases" / "pydantic-field-contract"
KEY = "pydantic-v2-required-nullable-fields"
DOCUMENT = (
    "# Migration fixture\r\n\r\n## Required fields\r\n"
    "保留原文与行尾，不用摘要替代证据。\r\n\r\n## Other section\r\nOther text\r\n"
).encode("utf-8")


@pytest.fixture
def evidence_case(tmp_path: Path) -> Path:
    root = tmp_path / "case"
    shutil.copytree(CALIBRATION, root)
    directory = root / "evidence"
    directory.mkdir()
    (directory / "migration.md").write_bytes(DOCUMENT)
    (directory / "LICENSE").write_bytes(b"Synthetic test fixture; not official documentation.\n")
    bundle = {
        "schema_version": 1,
        "package": "pydantic",
        "old_version": "1.10.24",
        "new_version": "2.11.7",
        "repository": "https://github.com/pydantic/pydantic",
        "revision": "1" * 40,
        "upstream_path": "docs/migration.md",
        "license_path": "evidence/LICENSE",
        "entries": [{
            "evidence_key": KEY,
            "path": "evidence/migration.md",
            "sha256": hashlib.sha256(DOCUMENT).hexdigest(),
            "start_line": 3,
            "end_line": 5,
            "heading": "## Required fields",
        }],
    }
    (directory / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for path in directory.iterdir():
        manifest["file_hashes"][path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def replace_bundle(manifest_path: Path, change) -> None:
    path = manifest_path.parent / "evidence" / "bundle.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    change(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_hashes"]["evidence/bundle.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_excerpt_preserves_exact_text_lines_and_pins_source(evidence_case: Path) -> None:
    case = load_case(evidence_case)
    report = load_version_evidence(case, required_keys={KEY})
    entry = report["entries"][0]
    assert entry["excerpt"] == "".join(DOCUMENT.decode("utf-8").splitlines(keepends=True)[2:5])
    assert entry["excerpt"].endswith("\r\n\r\n")
    assert entry["url"] == (
        "https://github.com/pydantic/pydantic/blob/" + "1" * 40 + "/docs/migration.md#L3-L5"
    )
    assert report["case_fingerprint"] == case.fingerprint
    assert report["bundle_sha256"] == case.manifest.file_hashes["evidence/bundle.json"]


@pytest.mark.parametrize("field,value", [("old_version", "1.10.23"), ("new_version", "2.11.6")])
def test_rejects_evidence_from_different_locked_version(
    evidence_case: Path, field: str, value: str
) -> None:
    replace_bundle(evidence_case, lambda bundle: bundle.update({field: value}))
    with pytest.raises(CaseValidationError, match="does not match requirements/"):
        load_version_evidence(load_case(evidence_case))


def test_unregistered_original_document_is_rejected(evidence_case: Path) -> None:
    manifest = json.loads(evidence_case.read_text(encoding="utf-8"))
    manifest["file_hashes"].pop("evidence/migration.md")
    evidence_case.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CaseValidationError, match="registered separately"):
        load_version_evidence(load_case(evidence_case))


@pytest.mark.parametrize("name", [
    "evidence/migration.md", "evidence/bundle.json", "evidence/LICENSE", "requirements/new.txt",
])
def test_rechecks_bytes_after_case_was_loaded(evidence_case: Path, name: str) -> None:
    case = load_case(evidence_case)
    (case.root / name).write_bytes(b"changed\n")
    with pytest.raises(CaseValidationError, match="SHA-256 mismatch"):
        load_version_evidence(case)


def test_entry_digest_must_match_registered_original(evidence_case: Path) -> None:
    replace_bundle(evidence_case, lambda bundle: bundle["entries"][0].update(sha256="0" * 64))
    with pytest.raises(CaseValidationError, match="digest does not match manifest"):
        load_version_evidence(load_case(evidence_case))


@pytest.mark.parametrize("update,match", [
    ({"start_line": 0}, "Invalid version evidence"),
    ({"start_line": 5, "end_line": 3}, "line range"),
    ({"end_line": 100}, "line range"),
    ({"heading": "## Different heading"}, "heading does not match"),
])
def test_invalid_excerpt_locations_are_rejected(evidence_case: Path, update: dict, match: str) -> None:
    replace_bundle(evidence_case, lambda bundle: bundle["entries"][0].update(update))
    with pytest.raises(CaseValidationError, match=match):
        load_version_evidence(load_case(evidence_case))


def test_required_rule_cannot_be_filled_by_an_unrelated_section(evidence_case: Path) -> None:
    with pytest.raises(CaseValidationError, match="Missing bound version evidence"):
        load_version_evidence(load_case(evidence_case), required_keys={KEY, "pydantic-v2-config"})


def test_duplicate_rule_keys_are_rejected(evidence_case: Path) -> None:
    replace_bundle(evidence_case, lambda bundle: bundle["entries"].append(bundle["entries"][0]))
    with pytest.raises(CaseValidationError, match="Duplicate evidence key"):
        load_version_evidence(load_case(evidence_case))


def test_registered_executable_input_cannot_be_used_as_version_evidence(evidence_case: Path) -> None:
    replace_bundle(
        evidence_case, lambda bundle: bundle["entries"][0].update(path="source/profile_model.py")
    )
    with pytest.raises(CaseValidationError, match="registered separately"):
        load_version_evidence(load_case(evidence_case))


def test_unsafe_evidence_path_has_controlled_validation_error(evidence_case: Path) -> None:
    replace_bundle(evidence_case, lambda bundle: bundle["entries"][0].update(path="../outside.md"))
    with pytest.raises(CaseValidationError):
        load_version_evidence(load_case(evidence_case))


def test_invalid_document_encoding_has_controlled_validation_error(evidence_case: Path) -> None:
    document_path = evidence_case.parent / "evidence" / "migration.md"
    document_path.write_bytes(b"\xff\xfe\x00")
    digest = hashlib.sha256(document_path.read_bytes()).hexdigest()
    manifest = json.loads(evidence_case.read_text(encoding="utf-8"))
    manifest["file_hashes"]["evidence/migration.md"] = digest
    evidence_case.write_text(json.dumps(manifest), encoding="utf-8")
    replace_bundle(evidence_case, lambda bundle: bundle["entries"][0].update(sha256=digest))
    with pytest.raises(CaseValidationError):
        load_version_evidence(load_case(evidence_case))
