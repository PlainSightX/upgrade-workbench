"""派生来源要保留完整父快照与候选身份；这些自有夹具不调用模型。"""

import hashlib
import json

import pytest

from upgrade_workbench.cases import CaseValidationError, load_case
from upgrade_workbench.generation import prepare_request
from upgrade_workbench.generation.provider import _verify_inputs, _verify_payload
from upgrade_workbench.generation.request import ProposalInputError


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write(root, name, value):
    data = json.dumps(value, sort_keys=True).encode() if isinstance(value, dict) else value
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return digest(data)


@pytest.fixture
def derived(owned_case):
    root = owned_case.root
    parent = json.loads(owned_case.manifest_path.read_bytes())
    parent["source"].update(kind="upstream_snapshot", repository="https://github.com/example/offline-fixture")
    write(root, "manifest.json", parent)
    original = load_case(root / "manifest.json")
    current = json.loads(json.dumps(parent))
    current["case_id"] = "owned-derived"
    current["source"]["kind"] = "derived_snapshot"
    contents = "from pydantic import BaseModel\nclass Profile(BaseModel):\n    label: str | None = None\n"
    patch = b"owned cumulative patch fixture\n"
    record = {"case_fingerprint": original.fingerprint, "parent": "0" * 64,
              "origin": "agent_candidate", "changes": {"model.py": contents},
              "patch_sha256": digest(patch), "increment_sha256": digest(patch)}
    hashes = current["file_hashes"]
    hashes["provenance/parent.json"] = write(root, "provenance/parent.json", parent)
    hashes["provenance/revision.json"] = write(root, "provenance/revision.json", record)
    hashes["provenance/candidate.patch"] = write(root, "provenance/candidate.patch", patch)
    hashes["source/model.py"] = write(root, "source/model.py", contents.encode())
    info = {"schema_version": 1, "kind": "derived_snapshot",
            **{key: parent["source"][key] for key in ("repository", "revision", "license")},
            "parent_manifest": "provenance/parent.json", "parent_case_fingerprint": original.fingerprint,
            "candidate_record": "provenance/revision.json", "candidate_revision": hashes["provenance/revision.json"],
            "candidate_patch": "provenance/candidate.patch", "candidate_sha256": digest(patch),
            "files": {k: v for k, v in hashes.items() if k.startswith("source/")}}
    hashes["SOURCE.json"] = write(root, "SOURCE.json", info)
    write(root, "manifest.json", current)
    return load_case(root / "manifest.json"), info, current


def test_derived_request_has_public_eligibility_bound_to_real_kind(derived, analysis, evidence, arguments):
    case, _, _ = derived
    analysis["case_fingerprint"] = case.fingerprint
    prepared = prepare_request(case, analysis, evidence, **arguments)
    assert prepared["source_kind"] == "derived_snapshot" and prepared["live_call_eligible"] is True
    from pathlib import Path

    _verify_payload(prepared, Path(prepared["request_path"]).read_bytes())
    _verify_inputs(prepared)
    with pytest.raises(ProposalInputError, match="Source kind"):
        _verify_inputs(prepared | {"source_kind": "upstream_snapshot"})


@pytest.mark.parametrize("change", ["parent", "candidate", "inherited", "lock", "inventory", "scope"])
def test_derived_rejects_rehashed_inconsistent_lineage(derived, change):
    case, info, manifest = derived
    hashes, root = manifest["file_hashes"], case.root
    if change == "parent":
        info["parent_case_fingerprint"] = "0" * 64
    elif change == "candidate":
        info["candidate_revision"] = "0" * 64
    elif change in {"inherited", "lock"}:
        name = "source/helper.py" if change == "inherited" else "requirements/new.txt"
        hashes[name] = write(root, name, b"CHANGED\n")
    elif change == "inventory":
        hashes["source/extra.html"] = write(root, "source/extra.html", b"extra")
    else:
        record = json.loads((root / info["candidate_record"]).read_bytes())
        record["changes"]["helper.py"] = "VALUE = 4\n"
        hashes[info["candidate_record"]] = write(root, info["candidate_record"], record)
        info["candidate_revision"] = hashes[info["candidate_record"]]
        hashes["source/helper.py"] = write(root, "source/helper.py", b"VALUE = 4\n")
    info["files"] = {k: v for k, v in hashes.items() if k.startswith("source/")}
    hashes["SOURCE.json"] = write(root, "SOURCE.json", info)
    write(root, "manifest.json", manifest)
    with pytest.raises(CaseValidationError, match="source derivation"):
        load_case(case.manifest_path)
