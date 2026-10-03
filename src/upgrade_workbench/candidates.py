"""不可变候选版本：内容身份由原始快照、父版本和修改字节共同确定。"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .cases import LoadedCase, PatchValidationError, load_case, stage_source
from .cases.manifest import assert_no_links, read_verified_file
from .generation.edits import diff_files, edited_sources


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encoded(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")


def public_sources(case: LoadedCase) -> dict[str, bytes]:
    from .generation.request import solver_source_names

    if load_case(case.manifest_path).fingerprint != case.fingerprint:
        raise PatchValidationError("Candidate case changed")
    return {
        name[7:]: read_verified_file(case.root, name, case.manifest.file_hashes[name])
        for name in solver_source_names(case)
    }


@dataclass(frozen=True)
class Candidate:
    revision: str
    parent: str | None
    original: dict[str, bytes]
    files: dict[str, bytes]
    patch: bytes
    origin: str
    reference: dict | None = None

    @property
    def sha256(self) -> str:
        return digest(self.patch)

    def blocks(self) -> list[dict]:
        return [{"path": name, "sha256": digest(data), "text": data.decode("utf-8")}
                for name, data in sorted(self.files.items())]


def load_candidate(case: LoadedCase, reference: dict | None = None) -> Candidate:
    """只接受公开源码的允许修改，不信任候选记录中任意宿主路径或测试内容。"""
    original = public_sources(case)
    if reference is None:
        revision = digest(_encoded({"case": case.fingerprint, "origin": "original"}))
        return Candidate(revision, None, original, dict(original), b"", "original")
    if not isinstance(reference, dict) or set(reference) != {"path", "revision"}:
        raise PatchValidationError("Invalid candidate reference")
    path = Path(reference["path"]).absolute()
    assert_no_links(path)
    if path.resolve().is_relative_to(case.root) or path.stat().st_size > 1_000_000:
        raise PatchValidationError("Candidate record outside its bounded workspace")
    raw = path.read_bytes()
    if digest(raw) != reference["revision"]:
        raise PatchValidationError("Candidate revision bytes changed")
    record = json.loads(raw)
    if (
        set(record) != {"case_fingerprint", "parent", "origin", "changes", "patch_sha256", "increment_sha256"}
        or record["case_fingerprint"] != case.fingerprint
        or not isinstance(record["parent"], str) or len(record["parent"]) != 64
        or record["origin"] not in {"official_tool", "agent_candidate"}
        or not isinstance(record["changes"], dict)
        or set(record["changes"]) - set(case.manifest.allowed_changes)
    ):
        raise PatchValidationError("Candidate record violates source contract")
    files = dict(original)
    for name, text in record["changes"].items():
        if name not in original or not isinstance(text, str) or "\x00" in text:
            raise PatchValidationError("Candidate has invalid source text")
        files[name] = text.encode("utf-8")
    patch = diff_files(original, files)
    if digest(patch) != record["patch_sha256"]:
        raise PatchValidationError("Candidate cumulative patch changed")
    increment = path.with_name("increment.patch")
    assert_no_links(increment)
    if increment.stat().st_size > 96_000 or digest(increment.read_bytes()) != record["increment_sha256"]:
        raise PatchValidationError("Candidate increment bytes changed")
    return Candidate(reference["revision"], record["parent"], original, files, patch,
                     record["origin"], dict(reference))


def publish_candidate(
    case: LoadedCase, base: Candidate, updated: dict[str, bytes], directory: Path, *, origin: str,
) -> Candidate:
    """先完整核验、写不可变版本；只有调用方持锁更新任务指针后才成为当前版本。"""
    if origin not in {"official_tool", "agent_candidate"}:
        raise PatchValidationError("Invalid candidate origin")
    if set(updated) - set(case.manifest.allowed_changes):
        raise PatchValidationError("Candidate touches protected source")
    files = {**base.files, **updated}
    changes = {name: data for name, data in files.items() if data != base.original[name]}
    for name, data in changes.items():
        try:
            ast.parse(data, filename=name)
        except SyntaxError as error:
            raise PatchValidationError(
                f"Syntax error in {name}:{error.lineno}:{error.offset}: {error.msg}"
            ) from error
    patch = diff_files(base.original, changes)
    increment = diff_files(base.files, updated)
    record = {"case_fingerprint": case.fingerprint, "parent": base.revision,
              "origin": origin, "changes": {n: b.decode("utf-8") for n, b in changes.items()},
              "patch_sha256": digest(patch), "increment_sha256": digest(increment)}
    encoded = _encoded(record)
    directory = Path(directory).absolute()
    assert_no_links(directory)
    if directory.resolve().is_relative_to(case.root):
        raise PatchValidationError("Candidate output overlaps immutable case")
    directory.mkdir(parents=True, exist_ok=False)
    patch_path = directory / "candidate.patch"
    patch_path.write_bytes(patch)
    (directory / "increment.patch").write_bytes(increment)
    # 与既有Git补丁执行器交叉核对；这里只处理字节，不导入目标应用。
    if patch:
        stage_source(case, directory / "source", patch_path)
    temporary = directory / "revision.tmp"
    temporary.write_bytes(encoded)
    temporary.replace(directory / "revision.json")
    reference = {"path": str(directory / "revision.json"), "revision": digest(encoded)}
    return load_candidate(case, reference)


def apply_increment(case: LoadedCase, base: Candidate, action: dict, directory: Path) -> Candidate:
    if action.get("base_revision") != base.revision:
        raise PatchValidationError("Stale base_revision; read the current candidate")
    updated = edited_sources(case, action["edits"], base.files)
    return publish_candidate(case, base, updated, directory, origin="agent_candidate")


def import_patch(case: LoadedCase, base: Candidate, patch: Path, root: Path) -> Candidate:
    """官方初始补丁仅能导入原始版本，不在后续修复上悄悄重跑转换。"""
    if base.origin != "original":
        raise PatchValidationError("Official seed requires the original candidate")
    staging = Path(root) / "seed-inputs" / uuid4().hex
    stage_source(case, staging, patch)
    updated = {name: (staging / name).read_bytes() for name in case.manifest.allowed_changes}
    return publish_candidate(case, base, updated, Path(root) / "candidates" / uuid4().hex,
                             origin="official_tool")
