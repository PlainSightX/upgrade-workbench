"""新条款身份复用同一公开初始化，不改变历史准入或允许身份漂移。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from upgrade_workbench.cases import load_case
from upgrade_workbench.probe_scaffolds import scaffold_for

CASE = Path(__file__).resolve().parents[2] / "cases/flaskbb-sqlalchemy-1.4.21-r2"


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
