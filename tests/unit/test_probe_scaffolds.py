"""新条款身份复用同一公开初始化，不改变历史准入或允许身份漂移。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from upgrade_workbench.cases import load_case
from upgrade_workbench.probe_scaffolds import scaffold_for

CASE = Path(__file__).resolve().parents[2] / "cases/flaskbb-sqlalchemy-1.4.21-r2"


@pytest.mark.parametrize("directory,filename,original,public", [
    ("flaskbb-sqlalchemy-1.4.21-r2", "manifest.json",
     "b1e69a715e9076be2d1d875efb8b1aba79214cbc08348ac10aca49e8db5a2f5f",
     "3d4ba2509b46bf5d283726922b9f0d609160ed507e9b8fe4d9fbb2eeb611a6f7"),
    ("flaskbb-sqlalchemy-1.4.21-r2", "manifest-v3.json",
     "8bbe842cd9faf54d26f9efed32bc5497ec57de1aee2d785f0e9009523ea831dc",
     "54167b665adb7a2071195fbda6dde5eb04b014ef94cec3a6410caaaa287ffaaa"),
    ("flaskbb-sqlalchemy-1.4.21-r2", "manifest-v4.json",
     "a9a9891ff212e2fc0391f4d3eab7ccf50625a07fe99d433ada76d736f3fecab4",
     "5a1629585a0679c279718cb80b65291b6eaaf2da3906b3297120775b116c0b21"),
    ("flaskbb-werkzeug-2.1", "manifest.json",
     "be4cd0df52e971e9ed56fcc8aa1f069a25664353edcd07735fde82891c3d476a",
     "45df1510d27a934c829623469d0f5bc282873d4f263e19aa56657c7471235d2d"),
])
def test_current_public_fixture_alias_preserves_payload_and_identity_gate(
    directory, filename, original, public,
):
    case = load_case(CASE.parent / directory / filename)
    assert case.fingerprint in {original, public}
    scaffold = scaffold_for(case)
    assert scaffold is not None
    assert scaffold.case_fingerprint == case.fingerprint
    assert scaffold.public() == scaffold_for(replace(case, fingerprint=original)).public()
    assert scaffold_for(replace(case, fingerprint="f" * 64)) is None
    renamed = case.manifest.model_copy(update={"case_id": "unreviewed-flaskbb"})
    assert scaffold_for(replace(case, manifest=renamed)) is None


def test_reviewed_thin_manifest_reuses_exact_public_scaffold():
    old = load_case(CASE / "manifest.json")
    current = load_case(CASE / "manifest-v3.json")
    assert current.manifest.file_hashes == old.manifest.file_hashes | {
        "contract-requirements.json": current.manifest.file_hashes["contract-requirements.json"]
    }
    previous, scaffold = scaffold_for(old), scaffold_for(current)
    assert previous.case_fingerprint == old.fingerprint
    assert scaffold.case_fingerprint == current.fingerprint
    assert previous.code == scaffold.code
    assert previous.sha256 == scaffold.sha256
    assert previous.public() == scaffold.public()


def test_unreviewed_fingerprint_is_rejected():
    current = load_case(CASE / "manifest-v3.json")
    assert scaffold_for(replace(current, fingerprint="0" * 64)) is None


def test_corrected_advertisement_preserves_inputs_and_historical_payload():
    old = load_case(CASE / "manifest-v3.json")
    current = load_case(CASE / "manifest-v4.json")
    prior, fixed = scaffold_for(old), scaffold_for(current)
    assert current.manifest.model_dump(exclude={"review"}) == old.manifest.model_dump(exclude={"review"})
    assert fixed.scaffold_id == "flaskbb-public-fixtures-v3"
    assert fixed.case_fingerprint == current.fingerprint != old.fingerprint
    assert fixed.code == prior.code and fixed.sha256 == prior.sha256
    assert len(fixed.fixtures) == 10 and "post" not in fixed.fixtures
    assert fixed.fixtures == tuple(name for name in prior.fixtures if name != "post")
    expected = "266347e80d8b90ad6216042983a2fbe9f14843d471a6d091554b36387ab50d33"
    for manifest in ("manifest.json", "manifest-v3.json"):
        payload = scaffold_for(load_case(CASE / manifest)).public()
        assert hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest() == expected


def test_case_name_cannot_bypass_identity_gate():
    current = load_case(CASE / "manifest-v3.json")
    manifest = current.manifest.model_copy(update={"case_id": "unreviewed-flaskbb"})
    assert scaffold_for(replace(current, manifest=manifest)) is None


def test_catalog_never_maps_hidden_checks_to_public_feedback():
    from upgrade_workbench.cases.requirements import requirements_for_case

    current = load_case(CASE / "manifest-v3.json")
    rows = requirements_for_case(current).contract.requirements
    assert len(rows) == 10
    assert all(node.startswith("feedback/") for row in rows for node in row.public_check_nodeids)
    assert not next(row for row in rows if row.id == "plugins.reply-edit-hooks").public_check_nodeids
