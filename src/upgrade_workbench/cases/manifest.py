"""验证案例身份、来源声明和可执行输入的完整性。"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from ..execution.environment import CaseEnvironment, parse_case_environment


class CaseValidationError(ValueError):
    """案例未满足路径、来源或内容完整性合同。"""


def relative_path(value: str) -> str:
    """只接受跨平台含义一致、没有别名的相对文件路径。"""
    if not isinstance(value, str) or not value:
        raise ValueError("A non-empty relative path is required")
    if any(character in value for character in '\\:*?<>|"'):
        raise ValueError(f"Non-standard relative path: {value!r}")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"Control character in path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or value == ".":
        raise ValueError(f"Non-canonical relative path: {value!r}")
    for part in path.parts:
        if part in {".", ".."} or part != part.strip() or part.endswith("."):
            raise ValueError(f"Unsafe path component: {value!r}")
        if unicodedata.normalize("NFC", part) != part:
            raise ValueError(f"Non-canonical Unicode path: {value!r}")
        stem = part.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            raise ValueError(f"Reserved Windows path: {value!r}")
        if part.casefold() == ".git":
            raise ValueError("Git metadata is not a case input")
    return value


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


NonEmpty = Annotated[str, Field(min_length=1)]


class CaseSource(_Contract):
    """来源声明保持为独立信息，不推导实验结论。"""

    kind: Literal["upstream_snapshot", "derived_snapshot", "synthetic_calibration"]
    repository: NonEmpty
    revision: NonEmpty
    license: NonEmpty
    description: NonEmpty


class CaseReview(_Contract):
    """记录案例进入执行路径之前的审阅声明。"""

    status: Literal["reviewed"]
    note: NonEmpty


class _CaseManifest(_Contract):
    """两个案例清单版本共享的不可变来源与执行边界。"""
    case_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    title: NonEmpty
    source: CaseSource
    review: CaseReview
    snapshot_dir: Literal["source"]
    checks_dir: Literal["checks"]
    old_lock: Literal["requirements/old.txt"]
    new_lock: Literal["requirements/new.txt"]
    allowed_changes: Annotated[list[str], Field(min_length=1)]
    file_hashes: Annotated[dict[str, str], Field(min_length=1)]
    expected_new_original: Literal["passed", "failed"]

    @field_validator("allowed_changes")
    @classmethod
    def safe_allowed_changes(cls, values: list[str]) -> list[str]:
        for value in values:
            relative_path(value)
            if not value.endswith(".py"):
                raise ValueError("allowed_changes must name existing Python files")
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("Duplicate or case-aliased allowed_changes")
        return values

    @field_validator("file_hashes")
    @classmethod
    def safe_file_hashes(cls, values: dict[str, str]) -> dict[str, str]:
        for path, digest in values.items():
            relative_path(path)
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Expected lowercase SHA-256 for {path}")
        if len({path.casefold() for path in values}) != len(values):
            raise ValueError("Case-aliased file_hashes paths")
        return values


class CaseManifestV1(_CaseManifest):
    """历史案例清单；不会因新合同模型而改变指纹。"""

    schema_version: Literal[1]


class CaseManifestV2(_CaseManifest):
    """显式登记结构化合同条目的新案例清单。"""

    schema_version: Literal[2]
    requirements_path: Literal["contract-requirements.json"]


CaseManifest: TypeAlias = CaseManifestV1 | CaseManifestV2
_MANIFEST_ADAPTER = TypeAdapter(Annotated[CaseManifest, Field(discriminator="schema_version")])


@dataclass(frozen=True)
class LoadedCase:
    """已核验的案例引用；暂存仍须重新检查磁盘内容。"""

    root: Path
    manifest: CaseManifest
    source_dir: Path
    checks_dir: Path
    old_lock: Path
    new_lock: Path
    environment: CaseEnvironment | None
    fingerprint: str
    manifest_path: Path


def assert_no_links(path: Path) -> None:
    """连同祖先目录检查 symlink、junction 和其他 reparse point。"""
    absolute = path.absolute()
    for component in reversed((absolute, *absolute.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise CaseValidationError(f"Links and reparse points are forbidden: {component}")


def inventory_files(directory: Path) -> set[str]:
    """枚举真实常规文件，同时拒绝目录链接和特殊文件。"""
    assert_no_links(directory)
    if not directory.is_dir():
        raise CaseValidationError(f"Required directory is missing: {directory}")
    result: set[str] = set()
    pending = [directory]
    while pending:
        for path in pending.pop().iterdir():
            assert_no_links(path)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                name = path.relative_to(directory).as_posix()
                try:
                    relative_path(name)
                except ValueError as error:
                    raise CaseValidationError(str(error)) from error
                result.add(name)
            else:
                raise CaseValidationError(f"Special files are forbidden: {path}")
    return result


def read_verified_file(root: Path, name: str, digest: str) -> bytes:
    """读取同一份字节进行校验和复制，避免校验后再盲目复制。"""
    path = root.joinpath(*PurePosixPath(name).parts)
    assert_no_links(path)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise CaseValidationError(f"Expected a regular file: {name}")
        contents = path.read_bytes()
        after = path.lstat()
    except OSError as error:
        raise CaseValidationError(f"Cannot read case file {name}: {error}") from error
    assert_no_links(path)
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise CaseValidationError(f"Case file changed while reading: {name}")
    if hashlib.sha256(contents).hexdigest() != digest:
        raise CaseValidationError(f"SHA-256 mismatch: {name}")
    return contents


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CaseValidationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_derivation(root: Path, manifest: CaseManifest) -> None:
    """核对已审阅候选的派生一致性；外部祖先真实性仍由准入审阅承担。"""
    def registered(name: str) -> bytes:
        relative_path(name)
        if name not in manifest.file_hashes or name.startswith(("source/", "checks/")):
            raise ValueError("Derivation evidence must be separately registered")
        return read_verified_file(root, name, manifest.file_hashes[name])

    def decode(raw: bytes):
        return json.loads(raw, object_pairs_hook=_unique_object)

    try:
        info = decode(registered("SOURCE.json"))
        if info["schema_version"] != 1 or info["kind"] != "derived_snapshot":
            raise ValueError("Invalid derived source identity")
        parent = _MANIFEST_ADAPTER.validate_python(decode(registered(info["parent_manifest"])))
        parent_digest = hashlib.sha256(json.dumps(
            parent.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        if (parent.source.kind == "synthetic_calibration"
                or parent_digest != info["parent_case_fingerprint"]):
            raise ValueError("Derived source requires its reviewed public parent")
        for key in ("repository", "revision", "license"):
            if getattr(parent.source, key) != getattr(manifest.source, key) or info[key] != getattr(parent.source, key):
                raise ValueError("Derived source ancestry differs from parent")
        raw = registered(info["candidate_record"])
        record = decode(raw)
        patch = registered(info["candidate_patch"])
        if (hashlib.sha256(raw).hexdigest() != info["candidate_revision"]
                or record["case_fingerprint"] != parent_digest
                or record["origin"] != "agent_candidate"
                or hashlib.sha256(patch).hexdigest() != record["patch_sha256"]
                or record["patch_sha256"] != info["candidate_sha256"]):
            raise ValueError("Derived candidate identity changed")
        changes = record["changes"]
        if not isinstance(changes, dict) or set(changes) - set(parent.allowed_changes):
            raise ValueError("Derived candidate changes exceed parent allowance")
        files = {k: v for k, v in manifest.file_hashes.items() if k.startswith("source/")}
        parent_files = {k: v for k, v in parent.file_hashes.items() if k.startswith("source/")}
        if info["files"] != files or set(files) != set(parent_files):
            raise ValueError("Derived source must preserve the complete parent inventory")
        for name, digest in files.items():
            content = changes.get(name.removeprefix("source/"))
            if name.removeprefix("source/") in changes:
                if not isinstance(content, str) or "\x00" in content:
                    raise ValueError("Invalid derived candidate text")
                expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
            else:
                expected = parent_files[name]
            if digest != expected:
                raise ValueError("Derived source differs from candidate or inherited bytes: " + name)
        for name in (manifest.old_lock, manifest.new_lock):
            if manifest.file_hashes[name] != parent.file_hashes[name]:
                raise ValueError("Derived followup must preserve its declared parent locks")
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise CaseValidationError(f"Invalid source derivation: {error}") from error


def load_case(manifest_path: Path) -> LoadedCase:
    """加载清单并核验内容；预期新环境结果只保存为案例语义。"""
    path = Path(manifest_path).absolute()
    assert_no_links(path)
    root = path.parent.resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        # Python 的 bool 是 int 子类；判别联合前显式拒绝，避免 True 被当成版本 1。
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int:
            raise CaseValidationError("schema_version must be an integer")
        manifest = _MANIFEST_ADAPTER.validate_python(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise CaseValidationError(f"Invalid case manifest {path}: {error}") from error

    source_dir = root / manifest.snapshot_dir
    checks_dir = root / manifest.checks_dir
    source_files = inventory_files(source_dir)
    check_files = inventory_files(checks_dir)
    if not source_files or not check_files:
        raise CaseValidationError("source and checks must each contain files")
    hashes = manifest.file_hashes
    for prefix, actual in (("source/", source_files), ("checks/", check_files)):
        registered = {name[len(prefix):] for name in hashes if name.startswith(prefix)}
        if actual != registered:
            raise CaseValidationError(
                f"Inventory mismatch for {prefix}: "
                f"unregistered={sorted(actual - registered)}, missing={sorted(registered - actual)}"
            )
    required_locks = {manifest.old_lock, manifest.new_lock}
    if not required_locks <= hashes.keys():
        raise CaseValidationError("Both dependency locks must be included in file_hashes")
    evidence = set(hashes) - required_locks - {
        name for name in hashes if name.startswith(("source/", "checks/"))
    }
    if not evidence or path.name in hashes:
        raise CaseValidationError("Register separate provenance evidence; do not hash the manifest itself")
    for name in manifest.allowed_changes:
        if name not in source_files:
            raise CaseValidationError(f"Allowed change is not an existing source file: {name}")
    for name, digest in sorted(hashes.items()):
        read_verified_file(root, name, digest)
    if manifest.source.kind == "derived_snapshot":
        _validate_derivation(root, manifest)
    if isinstance(manifest, CaseManifestV2):
        from .requirements import load_contract_requirements

        load_contract_requirements(root, manifest.requirements_path, hashes, check_files)
    environment = None
    environment_path = root / "environment.json"
    if environment_path.exists():
        if "environment.json" not in hashes:
            raise CaseValidationError("environment.json must be registered in file_hashes")
        try:
            environment = parse_case_environment(
                read_verified_file(root, "environment.json", hashes["environment.json"])
            )
        except ValueError as error:
            raise CaseValidationError(str(error)) from error
        for wheel in environment.local_wheels:
            relative_path(wheel)
            if wheel not in hashes:
                raise CaseValidationError(f"Local wheel is not registered in file_hashes: {wheel}")
            read_verified_file(root, wheel, hashes[wheel])
    canonical = json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    fingerprint = hashlib.sha256(canonical).hexdigest()
    return LoadedCase(
        root=root,
        manifest=manifest,
        source_dir=source_dir,
        checks_dir=checks_dir,
        old_lock=root / manifest.old_lock,
        new_lock=root / manifest.new_lock,
        environment=environment,
        fingerprint=fingerprint,
        manifest_path=path,
    )
