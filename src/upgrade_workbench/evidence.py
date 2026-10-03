"""把迁移规则绑定到锁定版本和可复查的官方原文位置。"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .cases.manifest import CaseValidationError, LoadedCase, read_verified_file, relative_path


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceEntry(_Record):
    evidence_key: str = Field(pattern=r"^(?:pydantic-v2|sqlalchemy-v(?:14|2)|werkzeug-v21)-[a-z-]+$")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    heading: str = Field(min_length=1)


class VersionEvidence(_Record):
    schema_version: Literal[1]
    package: Literal["pydantic", "sqlalchemy"]
    old_version: str = Field(pattern=r"^1\.[0-9]+\.[0-9]+$")
    new_version: str = Field(pattern=r"^[12]\.[0-9]+\.[0-9]+$")
    repository: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    upstream_path: str
    license_path: str
    entries: list[EvidenceEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def family_binding(self):
        if self.package == "pydantic":
            expected = ("https://github.com/pydantic/pydantic", "docs/migration.md")
            versions = self.old_version.startswith("1.") and self.new_version.startswith("2.")
            prefix = "pydantic-v2-"
        elif self.old_version.startswith("1.3.") and self.new_version.startswith("1.4."):
            expected = (
                "https://github.com/sqlalchemy/sqlalchemy",
                "doc/build/changelog/migration_14.rst",
            )
            versions = True
            prefix = "sqlalchemy-v14-"
        elif self.old_version.startswith("1.4.") and self.new_version.startswith("2.0."):
            expected = (
                "https://github.com/sqlalchemy/sqlalchemy",
                "doc/build/changelog/migration_20.rst",
            )
            versions = True
            prefix = "sqlalchemy-v2-"
        else:
            raise ValueError("Unsupported migration version family")
        if not versions or (self.repository, self.upstream_path) != expected:
            raise ValueError("Official document must match migration family")
        if any(not entry.evidence_key.startswith(prefix) for entry in self.entries):
            raise ValueError("Evidence key belongs to another migration family")
        return self


class FlaskCompanion(_Record):
    """该支持范围是已冻结的两个依赖组合，不能把仅升 Flask 当作 Werkzeug 变化。"""

    package: Literal["flask"]
    old_version: Literal["2.0.1"]
    new_version: Literal["2.1.2"]


class WerkzeugVersionEvidence(VersionEvidence):
    """独立 schema 保留旧任务的证据投影，不向历史 binding 添加新字段。"""

    schema_version: Literal[2]
    package: Literal["werkzeug"]
    old_version: Literal["2.0.1"]
    new_version: Literal["2.1.2"]
    companion: FlaskCompanion

    @model_validator(mode="after")
    def family_binding(self):
        if (self.repository, self.upstream_path) != (
            "https://github.com/pallets/werkzeug", "CHANGES.rst"
        ):
            raise ValueError("Official document must match migration family")
        if any(not entry.evidence_key.startswith("werkzeug-v21-") for entry in self.entries):
            raise ValueError("Evidence key belongs to another migration family")
        return self


def _registered(case: LoadedCase, name: str) -> bytes:
    try:
        relative_path(name)
    except ValueError as error:
        raise CaseValidationError(str(error)) from error
    if name.startswith(("source/", "checks/", "requirements/")) or name not in case.manifest.file_hashes:
        raise CaseValidationError(f"Version evidence must be registered separately: {name}")
    return read_verified_file(case.root, name, case.manifest.file_hashes[name])


def _locked_version(case: LoadedCase, lock: str, package: str = "pydantic") -> str:
    text = read_verified_file(case.root, lock, case.manifest.file_hashes[lock]).decode("utf-8")
    versions = re.findall(r"^" + re.escape(package) + r"==([0-9.]+)(?:[ \t]|$)", text, re.MULTILINE | re.IGNORECASE)
    if len(versions) != 1:
        raise CaseValidationError(f"Expected exactly one pinned {package} version in {lock}")
    return versions[0]


def load_version_evidence(
    case: LoadedCase,
    bundle_path: str = "evidence/bundle.json",
    *,
    required_keys: set[str] | None = None,
) -> dict:
    """只接受与本案例旧/新锁一致的证据；缺失规则不能用泛化摘要填补。"""
    try:
        raw = _registered(case, bundle_path)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise CaseValidationError("Invalid version evidence: expected an object")
        model = WerkzeugVersionEvidence if payload.get("schema_version") == 2 else VersionEvidence
        bundle = model.model_validate_json(raw)
    except (ValidationError, UnicodeError, json.JSONDecodeError) as error:
        raise CaseValidationError(f"Invalid version evidence: {error}") from error
    for lock, expected in (
        (case.manifest.old_lock, bundle.old_version),
        (case.manifest.new_lock, bundle.new_version),
    ):
        if _locked_version(case, lock, bundle.package) != expected:
            raise CaseValidationError(f"Version evidence does not match {lock}")
    if isinstance(bundle, WerkzeugVersionEvidence):
        for lock, expected in (
            (case.manifest.old_lock, bundle.companion.old_version),
            (case.manifest.new_lock, bundle.companion.new_version),
        ):
            if _locked_version(case, lock, bundle.companion.package) != expected:
                raise CaseValidationError(f"Companion version evidence does not match {lock}")
    _registered(case, bundle.license_path)
    entries = []
    seen: set[str] = set()
    url = f"{bundle.repository}/blob/{bundle.revision}/{bundle.upstream_path}"
    for item in bundle.entries:
        if item.evidence_key in seen:
            raise CaseValidationError(f"Duplicate evidence key: {item.evidence_key}")
        seen.add(item.evidence_key)
        contents = _registered(case, item.path)
        if case.manifest.file_hashes[item.path] != item.sha256:
            raise CaseValidationError(f"Evidence digest does not match manifest: {item.path}")
        try:
            lines = contents.decode("utf-8").splitlines(keepends=True)
        except UnicodeError as error:
            raise CaseValidationError(f"Evidence must be UTF-8: {item.path}") from error
        if not 1 <= item.start_line <= item.end_line <= len(lines):
            raise CaseValidationError(f"Evidence line range is outside {item.path}")
        if lines[item.start_line - 1].strip() != item.heading:
            raise CaseValidationError(f"Evidence heading does not match line {item.start_line}")
        entries.append({
            **item.model_dump(),
            "excerpt": "".join(lines[item.start_line - 1:item.end_line]),
            "url": f"{url}#L{item.start_line}-L{item.end_line}",
        })
    missing = (required_keys or set()) - seen
    if missing:
        raise CaseValidationError(f"Missing bound version evidence: {sorted(missing)}")
    return {
        "schema_version": 1,
        "case_fingerprint": case.fingerprint,
        "bundle_path": bundle_path,
        "bundle_sha256": case.manifest.file_hashes[bundle_path],
        "binding": bundle.model_dump(exclude={"entries"}),
        "entries": entries,
        "method": "reviewed_exact_sections_not_hybrid_retrieval",
    }


def migration_package(case: LoadedCase) -> str:
    """已有无 bundle 校准案例维持 Pydantic 行为；新家族必须携带版本合同。"""
    if "evidence/bundle.json" not in case.manifest.file_hashes:
        if case.manifest.source.kind != "synthetic_calibration":
            _locked_version(case, case.manifest.old_lock)
        return "pydantic"
    return load_version_evidence(case)["binding"]["package"]
