"""生成器只处理自有离线夹具，任何真实网络调用均不属于这些测试。"""

import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench.cases import load_case
from upgrade_workbench.generation import prepare_request

ORIGINAL = b"from pydantic import BaseModel\nclass Profile(BaseModel):\n    label: str | None\n"
PATCH = (
    "--- a/model.py\n+++ b/model.py\n@@ -1,3 +1,3 @@\n"
    " from pydantic import BaseModel\n class Profile(BaseModel):\n"
    "-    label: str | None\n+    label: str | None = None\n"
)


@pytest.fixture
def owned_case(tmp_path: Path):
    root = tmp_path / "case"
    files = {
        "source/model.py": ORIGINAL,
        "source/helper.py": b"VALUE = 3\n",
        "source/tests/test_private.py": b"ORACLE_PRIVATE = 'never send this test'\n",
        "checks/test_contract.py": b"def test_oracle():\n    assert 'SECRET_ORACLE'\n",
        "requirements/old.txt": b"OLD_LOCK_PRIVATE\n",
        "requirements/new.txt": b"NEW_LOCK_PRIVATE\n",
        "SOURCE.json": b'{"origin":"PRIVATE_METADATA_NOT_REQUESTED"}\n',
        "business-contract.md": b"Omitted label must remain None.\n",
        "evidence/migration.md": b"# Pydantic migration\nNullable does not imply a default.\n",
    }
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (root / "reference.patch").write_text("PRIVATE_REFERENCE_ANSWER", encoding="utf-8")
    manifest = {
        "schema_version": 1, "case_id": "owned-proposal", "title": "Offline proposal fixture",
        "source": {
            "kind": "synthetic_calibration", "repository": "owned-fixture", "revision": "v1",
            "license": "MIT", "description": "Owned unit fixture, never a public application",
        },
        "review": {"status": "reviewed", "note": "Owned fixture only"},
        "snapshot_dir": "source", "checks_dir": "checks",
        "old_lock": "requirements/old.txt", "new_lock": "requirements/new.txt",
        "allowed_changes": ["model.py"],
        "file_hashes": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "expected_new_original": "failed",
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return load_case(path)


@pytest.fixture
def evidence(owned_case):
    return [{
        "path": "evidence/migration.md",
        "sha256": owned_case.manifest.file_hashes["evidence/migration.md"],
        "key": "pydantic-v2-required-nullable-fields",
        "start_line": 2, "end_line": 2,
        "excerpt": "Nullable does not imply a default.\n",
    }]


@pytest.fixture
def analysis(owned_case):
    return {
        "case_fingerprint": owned_case.fingerprint,
        "findings": [{
            "file": "source/model.py", "line": 3, "column": 4,
            "rule": "nullable_without_default", "symbol": "Profile.label",
            "status": "potential_impact", "evidence_key": "pydantic-v2-required-nullable-fields",
            "reason": "PRIVATE_FREEFORM_REASON_MUST_NOT_BE_SENT",
        }],
        "private_metadata": "PRIVATE_ANALYSIS_MUST_NOT_BE_SENT",
    }


@pytest.fixture
def arguments(owned_case, tmp_path):
    return {
        "model": "owned-model", "endpoint": "https://provider.example/v1/chat/completions",
        "work_root": tmp_path / "work", "max_output_tokens": 1024, "timeout_seconds": 10,
        "public_source_ack": True, "contract_path": owned_case.root / "business-contract.md",
    }


@pytest.fixture
def prepared(owned_case, analysis, evidence, arguments):
    return prepare_request(owned_case, analysis, evidence, **arguments)


def provider_response(patch=PATCH, *, usage=True, summary="Preserve omitted label.", **changes):
    response = {
        "id": "owned-response",
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps({"summary": summary, "patch": patch})},
        }],
    }
    if usage:
        response["usage"] = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    response.update(changes)
    return json.dumps(response).encode()
