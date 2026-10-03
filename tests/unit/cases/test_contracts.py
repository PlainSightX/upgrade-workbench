"""只在临时目录验证案例合同，不执行案例源代码。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from upgrade_workbench.cases import (
    CaseValidationError,
    PatchInfrastructureError,
    PatchValidationError,
    load_case,
    stage_source,
)

ORIGINAL = b"def value():\n    return 1\n"
PATCH = (
    "diff --git a/demo.py b/demo.py\n"
    "--- a/demo.py\n+++ b/demo.py\n"
    "@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n"
)


@pytest.fixture
def case_path(tmp_path: Path) -> Path:
    root = tmp_path / "case"
    files = {
        "source/demo.py": ORIGINAL,
        "source/helper.py": b"HELPER = True\n",
        "checks/test_demo.py": b"def test_placeholder():\n    assert True\n",
        "requirements/old.txt": b"example==1.0\n",
        "requirements/new.txt": b"example==2.0\n",
        "SOURCE.json": b'{"kind":"synthetic_calibration"}\n',
    }
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    manifest = {
        "schema_version": 1,
        "case_id": "demo-case",
        "title": "Temporary contract fixture",
        "source": {
            "kind": "synthetic_calibration", "repository": "local-fixture",
            "revision": "fixture-v1", "license": "MIT", "description": "Synthetic test input",
        },
        "review": {"status": "reviewed", "note": "Tests only; no capability claim"},
        "snapshot_dir": "source", "checks_dir": "checks",
        "old_lock": "requirements/old.txt", "new_lock": "requirements/new.txt",
        "allowed_changes": ["demo.py"],
        "file_hashes": {name: hashlib.sha256(contents).hexdigest() for name, contents in files.items()},
        "expected_new_original": "failed",
    }
    manifest_path = root / "case.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def change_manifest(path: Path, update) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    update(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def candidate(tmp_path: Path, text: str = PATCH) -> Path:
    path = tmp_path / "candidate.patch"
    path.write_bytes(text.encode("utf-8"))
    return path


def test_loads_verified_case_and_canonical_fingerprint(case_path: Path) -> None:
    loaded = load_case(case_path)
    assert loaded.root == case_path.parent
    assert loaded.source_dir == case_path.parent / "source"
    assert loaded.old_lock.name == "old.txt"
    assert loaded.manifest.expected_new_original == "failed"
    assert len(loaded.fingerprint) == 64
    parsed = json.loads(case_path.read_text(encoding="utf-8"))
    case_path.write_text(json.dumps(parsed, indent=4, sort_keys=True), encoding="utf-8")
    assert load_case(case_path).fingerprint == loaded.fingerprint
    change_manifest(case_path, lambda data: data.update(title="Changed identity"))
    assert load_case(case_path).fingerprint != loaded.fingerprint


def test_loads_optional_case_environment_without_changing_legacy_contract(case_path: Path) -> None:
    legacy = load_case(case_path)
    wheel_name = "wheels/legacy_package-1.0-py3-none-any.whl"
    wheel = case_path.parent / wheel_name
    wheel.parent.mkdir()
    wheel.write_bytes(b"reviewed wheel")
    environment = {
        "schema_version": 1,
        "base_image": "python@sha256:" + "d" * 64,
        "python_version": "3.9",
        "local_wheels": [wheel_name],
    }
    environment_path = case_path.parent / "environment.json"
    environment_path.write_text(json.dumps(environment), encoding="utf-8")
    change_manifest(case_path, lambda data: data["file_hashes"].update({
        "environment.json": hashlib.sha256(environment_path.read_bytes()).hexdigest(),
        wheel_name: hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }))

    loaded = load_case(case_path)

    assert legacy.environment is None
    assert loaded.environment is not None
    assert loaded.environment.python_version == "3.9"
    assert loaded.environment.local_wheels == (wheel_name,)
    assert loaded.fingerprint != legacy.fingerprint


def test_case_environment_requires_registered_wheels(case_path: Path) -> None:
    environment_path = case_path.parent / "environment.json"
    environment_path.write_text(json.dumps({
        "schema_version": 1,
        "base_image": "python@sha256:" + "d" * 64,
        "python_version": "3.9",
        "local_wheels": ["wheels/missing-1.0-py3-none-any.whl"],
    }), encoding="utf-8")
    change_manifest(case_path, lambda data: data["file_hashes"].update({
        "environment.json": hashlib.sha256(environment_path.read_bytes()).hexdigest(),
    }))
    with pytest.raises(CaseValidationError, match="Local wheel is not registered"):
        load_case(case_path)


@pytest.mark.parametrize("name", [
    "../outside.py", "/absolute.py", "C:/outside.py", "C:outside.py", "\\\\server\\file.py",
    "sub\\file.py", "sub//file.py", "./demo.py", "sub/../demo.py", "*.py",
    "demo.py/", "demo.py ", "CON.py", ".git/config.py", "bad\x00.py",
])
def test_rejects_noncanonical_allowed_paths(case_path: Path, name: str) -> None:
    change_manifest(case_path, lambda data: data.update(allowed_changes=[name]))
    with pytest.raises(CaseValidationError):
        load_case(case_path)


@pytest.mark.parametrize("name", ["../outside.py", "/absolute.py", "sub\\demo.py", "C:/outside.py"])
def test_rejects_unsafe_hash_paths(case_path: Path, name: str) -> None:
    change_manifest(case_path, lambda data: data["file_hashes"].update({name: "0" * 64}))
    with pytest.raises(CaseValidationError):
        load_case(case_path)


@pytest.mark.parametrize("extra", ["source/extra.py", "checks/test_injected.py"])
def test_rejects_unregistered_active_files(case_path: Path, extra: str) -> None:
    (case_path.parent / extra).write_text("INJECTED = True\n", encoding="utf-8")
    with pytest.raises(CaseValidationError, match="Inventory mismatch"):
        load_case(case_path)


@pytest.mark.parametrize("name", ["source/demo.py", "checks/test_demo.py", "requirements/new.txt", "SOURCE.json"])
def test_rejects_hash_mismatches(case_path: Path, name: str) -> None:
    (case_path.parent / name).write_bytes(b"changed\n")
    with pytest.raises(CaseValidationError, match="SHA-256 mismatch"):
        load_case(case_path)


@pytest.mark.parametrize("name", ["source/demo.py", "requirements/old.txt", "SOURCE.json"])
def test_rejects_missing_registered_files(case_path: Path, name: str) -> None:
    (case_path.parent / name).unlink()
    with pytest.raises(CaseValidationError):
        load_case(case_path)


@pytest.mark.parametrize("field", ["requirements/old.txt", "requirements/new.txt", "SOURCE.json"])
def test_requires_lock_and_provenance_registration(case_path: Path, field: str) -> None:
    change_manifest(case_path, lambda data: data["file_hashes"].pop(field))
    with pytest.raises(CaseValidationError):
        load_case(case_path)


@pytest.mark.parametrize("update", [
    lambda data: data.update(unrecognized=True),
    lambda data: data["source"].update(unrecognized=True),
    lambda data: data["review"].update(unrecognized=True),
    lambda data: data.update(schema_version=True),
    lambda data: data.update(allowed_changes=["missing.py"]),
    lambda data: data.update(allowed_changes=["demo.py", "DEMO.py"]),
    lambda data: data.update(case_id="../bad"),
])
def test_strict_schema(case_path: Path, update) -> None:
    change_manifest(case_path, update)
    with pytest.raises(CaseValidationError):
        load_case(case_path)


def test_duplicate_json_key_is_rejected(case_path: Path) -> None:
    text = case_path.read_text(encoding="utf-8")
    case_path.write_text(text.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'), encoding="utf-8")
    with pytest.raises(CaseValidationError, match="Duplicate JSON key"):
        load_case(case_path)


def test_rejects_symbolic_link_when_supported(case_path: Path, tmp_path: Path) -> None:
    link = case_path.parent / "source" / "injected.py"
    target = tmp_path / "external.py"
    target.write_bytes(ORIGINAL)
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Symlink creation unavailable: {error}")
    with pytest.raises(CaseValidationError, match="Links and reparse"):
        load_case(case_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_rejects_windows_junction(case_path: Path, tmp_path: Path) -> None:
    import _winapi

    target = tmp_path / "external"
    target.mkdir()
    link = case_path.parent / "source" / "junction"
    _winapi.CreateJunction(str(target), str(link))
    try:
        with pytest.raises(CaseValidationError, match="Links and reparse"):
            load_case(case_path)
    finally:
        os.rmdir(link)
    assert target.is_dir()


def test_stage_original_copies_only_verified_source(case_path: Path, tmp_path: Path) -> None:
    destination = tmp_path / "original"
    result = stage_source(load_case(case_path), destination)
    assert result == {"source_dir": str(destination), "candidate_sha256": None, "changed_files": []}
    assert sorted(path.name for path in destination.iterdir()) == ["demo.py", "helper.py"]
    assert (destination / "demo.py").read_bytes() == ORIGINAL


def test_valid_patch_does_not_mutate_original_or_host_repo(case_path: Path, tmp_path: Path, monkeypatch) -> None:
    host = tmp_path / "host-git"
    host.mkdir()
    sentinel = host / "index"
    sentinel.write_bytes(b"host index must remain unchanged")
    monkeypatch.setenv("GIT_DIR", str(host))
    monkeypatch.setenv("GIT_WORK_TREE", str(case_path.parent / "source"))
    destination = tmp_path / "patched"
    patch = candidate(tmp_path)
    result = stage_source(load_case(case_path), destination, patch)
    assert result["changed_files"] == ["demo.py"]
    assert result["candidate_sha256"] == hashlib.sha256(patch.read_bytes()).hexdigest()
    assert (destination / "demo.py").read_bytes() == ORIGINAL.replace(b"return 1", b"return 2")
    assert (case_path.parent / "source/demo.py").read_bytes() == ORIGINAL
    assert sentinel.read_bytes() == b"host index must remain unchanged"
    assert sorted(path.name for path in destination.iterdir()) == ["demo.py", "helper.py"]
    assert not list(tmp_path.glob(".upgrade-stage-*"))


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("parent_length", [198, 222, 280])
def test_deep_workspace_applies_or_rejects_patch_without_git_path_failure(case_path, tmp_path, valid, parent_length):
    # 对应真实任务的嵌套执行目录；补丁不能因为Git元数据绝对路径过长而失效。
    parent = tmp_path / "deep"
    # 服务job/task/probe比CLI多两层；更深的进程cwd限制须明确失败，不冒充补丁错误。
    while len(str(parent)) < parent_length:
        parent /= "execution-segment"
    destination = parent / "candidate"
    patch = candidate(tmp_path, PATCH if valid else PATCH.replace("return 1", "return 999"))
    case = load_case(case_path)
    if os.name == "nt" and parent_length == 280:
        with pytest.raises(PatchInfrastructureError, match="Windows Git process working directory exceeds"):
            stage_source(case, destination, patch)
        assert not destination.exists()
    elif valid:
        result = stage_source(case, destination, patch)
        assert result["changed_files"] == ["demo.py"]
        assert (destination / "demo.py").read_bytes() == ORIGINAL.replace(b"return 1", b"return 2")
    else:
        with pytest.raises(PatchValidationError, match="patch does not apply|patch failed"):
            stage_source(case, destination, patch)
        assert not destination.exists()
    assert (case.root / "source/demo.py").read_bytes() == ORIGINAL
    assert not list(parent.glob(".upgrade-stage-*"))


@pytest.mark.parametrize("target", ["helper.py", "checks/test_demo.py", "../checks/test_demo.py", "requirements/new.txt", "/outside.py", "sub\\demo.py"])
def test_patch_cannot_escape_allowlist(case_path: Path, tmp_path: Path, target: str) -> None:
    patch = candidate(tmp_path, PATCH.replace("demo.py", target))
    destination = tmp_path / "rejected"
    with pytest.raises(PatchValidationError):
        stage_source(load_case(case_path), destination, patch)
    assert not destination.exists()
    assert (case_path.parent / "source/demo.py").read_bytes() == ORIGINAL


@pytest.mark.parametrize("text", [
    "--- /dev/null\n+++ b/demo.py\n@@ -0,0 +1 @@\n+NEW = True\n",
    "--- a/demo.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def value():\n-    return 1\n",
    "diff --git a/demo.py b/other.py\nsimilarity index 100%\nrename from demo.py\nrename to other.py\n",
    "diff --git a/demo.py b/demo.py\nold mode 100644\nnew mode 100755\n",
    "diff --git a/demo.py b/demo.py\nGIT binary patch\nliteral 1\nA\n",
    "diff --git a/demo.py b/demo.py\nnew file mode 100644\n" + PATCH,
    PATCH.replace("+++ b/demo.py", "+++ b/other.py"),
    PATCH + PATCH,
    PATCH.replace("diff --git a/demo.py b/demo.py", "diff --git a/helper.py b/helper.py"),
    PATCH.replace("return 2", "return 1"),
    "not a patch\n",
])
def test_forbidden_or_ambiguous_patch_is_not_adopted(case_path: Path, tmp_path: Path, text: str) -> None:
    destination = tmp_path / "rejected"
    with pytest.raises(PatchValidationError):
        stage_source(load_case(case_path), destination, candidate(tmp_path, text))
    assert not destination.exists()
    assert not list(tmp_path.glob(".upgrade-stage-*"))


def test_git_rejects_wrong_context_and_cleans_own_temporary_directory(case_path: Path, tmp_path: Path) -> None:
    patch = candidate(tmp_path, PATCH.replace("-    return 1", "-    return 999"))
    with pytest.raises(PatchValidationError, match="Git patch validation failed"):
        stage_source(load_case(case_path), tmp_path / "invalid", patch)
    assert not (tmp_path / "invalid").exists()
    assert not list(tmp_path.glob(".upgrade-stage-*"))


@pytest.mark.parametrize("name", ["source/demo.py", "checks/test_demo.py", "requirements/old.txt", "SOURCE.json"])
def test_revalidates_inputs_before_staging(case_path: Path, tmp_path: Path, name: str) -> None:
    loaded = load_case(case_path)
    (case_path.parent / name).write_bytes(b"changed after loading\n")
    with pytest.raises(CaseValidationError):
        stage_source(loaded, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_revalidates_manifest_identity_before_staging(case_path: Path, tmp_path: Path) -> None:
    loaded = load_case(case_path)
    change_manifest(case_path, lambda data: data.update(expected_new_original="passed"))
    with pytest.raises(CaseValidationError, match="manifest changed"):
        stage_source(loaded, tmp_path / "rejected")


def test_does_not_replace_existing_destination(case_path: Path, tmp_path: Path) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(PatchValidationError, match="already exists"):
        stage_source(load_case(case_path), destination)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_does_not_stage_inside_case_root(case_path: Path) -> None:
    with pytest.raises(PatchValidationError, match="outside the immutable case root"):
        stage_source(load_case(case_path), case_path.parent / "nested")


def test_case_change_during_staging_prevents_adoption(case_path: Path, tmp_path: Path, monkeypatch) -> None:
    from upgrade_workbench.cases import patches

    original_read = patches.read_verified_file

    def read_then_change_check(root: Path, name: str, digest: str) -> bytes:
        contents = original_read(root, name, digest)
        (root / "checks/test_demo.py").write_bytes(b"changed during staging\n")
        return contents

    monkeypatch.setattr(patches, "read_verified_file", read_then_change_check)
    with pytest.raises(CaseValidationError, match="SHA-256 mismatch"):
        stage_source(load_case(case_path), tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
    assert not list(tmp_path.glob(".upgrade-stage-*"))


def test_unexpected_post_apply_change_prevents_adoption(case_path: Path, tmp_path: Path, monkeypatch) -> None:
    from upgrade_workbench.cases import patches

    original_git = patches._git

    def apply_then_modify_other_file(arguments: list[str], cwd: Path, data: bytes | None = None) -> None:
        original_git(arguments, cwd, data)
        if "apply" in arguments and "--check" not in arguments:
            (cwd / "helper.py").write_bytes(b"unexpected helper mutation\n")

    monkeypatch.setattr(patches, "_git", apply_then_modify_other_file)
    with pytest.raises(PatchValidationError, match="Actual changed files differ"):
        stage_source(load_case(case_path), tmp_path / "rejected", candidate(tmp_path))
    assert not (tmp_path / "rejected").exists()
    assert not list(tmp_path.glob(".upgrade-stage-*"))
    assert (case_path.parent / "source/demo.py").read_bytes() == ORIGINAL
