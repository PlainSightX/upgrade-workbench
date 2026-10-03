"""加载案例预冻结的合同条目；身份与有限证据映射由案例作者持有。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .manifest import CaseValidationError, read_verified_file, relative_path


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ContractRequirement(_Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){1,7}$", max_length=96)
    statement: str = Field(min_length=1, max_length=700)
    public_check_nodeids: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("statement")
    @classmethod
    def bounded_statement(cls, value: str) -> str:
        if "\x00" in value or value != value.strip():
            raise ValueError("requirement statement must be canonical bounded text")
        return value

    @field_validator("public_check_nodeids")
    @classmethod
    def canonical_nodeids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("duplicate public check nodeid")
        for value in values:
            if (
                not isinstance(value, str)
                or not re.fullmatch(r"feedback/[A-Za-z0-9_./-]+\.py::[A-Za-z0-9_\[\].:-]+", value)
            ):
                raise ValueError("public checks must use feedback/<file>.py::<nodeid>")
            relative_path(value.split("::", 1)[0])
        return values


class ContractRequirements(_Contract):
    schema_version: int
    contract_path: str
    contract_sha256: str
    requirements: list[ContractRequirement] = Field(min_length=1, max_length=64)

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_one(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("contract requirements schema_version must be integer 1")
        return value

    @field_validator("contract_path")
    @classmethod
    def canonical_contract_path(cls, value: str) -> str:
        relative_path(value)
        if value != "business-contract.md":
            raise ValueError("structured requirements must bind business-contract.md")
        return value

    @field_validator("contract_sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("contract_sha256 must be lowercase SHA-256")
        return value

    @field_validator("requirements")
    @classmethod
    def unique_ids(cls, values: list[ContractRequirement]) -> list[ContractRequirement]:
        ids = [item.id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate contract requirement ID")
        return values


@dataclass(frozen=True)
class LoadedContractRequirements:
    path: str
    sha256: str
    contract: ContractRequirements

    def public(self) -> dict:
        value = self.contract.model_dump(mode="json")
        value["development_gate_requirement_ids"] = [
            item.id for item in self.contract.requirements if item.public_check_nodeids
        ]
        value["final_acceptance_requirement_ids"] = [
            item.id for item in self.contract.requirements if not item.public_check_nodeids
        ]
        return value


_ADAPTER = TypeAdapter(ContractRequirements)


def load_contract_requirements(
    root: Path,
    name: str,
    hashes: dict[str, str],
    check_files: set[str],
) -> LoadedContractRequirements:
    """核对条目文件、正文哈希及公开检查引用，不推断语义充分性。"""
    if name not in hashes:
        raise CaseValidationError("Manifest v2 must register contract-requirements.json")
    try:
        contents = read_verified_file(root, name, hashes[name])
        raw = json.loads(contents)
        contract = _ADAPTER.validate_python(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise CaseValidationError(f"Invalid contract requirements {name}: {error}") from error
    if contract.contract_path not in hashes:
        raise CaseValidationError("Structured requirements reference an unregistered business contract")
    if hashes[contract.contract_path] != contract.contract_sha256:
        raise CaseValidationError("Structured requirements contract SHA does not match the registered contract")
    for requirement in contract.requirements:
        for nodeid in requirement.public_check_nodeids:
            check = nodeid.split("::", 1)[0]
            if check not in check_files:
                raise CaseValidationError(f"Unknown public check nodeid path: {nodeid}")
    return LoadedContractRequirements(
        path=name,
        sha256=hashlib.sha256(contents).hexdigest(),
        contract=contract,
    )


def requirements_for_case(case) -> LoadedContractRequirements:
    """Protocol 5 只接受显式登记条目文件的新案例身份。"""
    name = getattr(case.manifest, "requirements_path", None)
    if name is None:
        raise CaseValidationError("Protocol 5 requires a manifest v2 case with structured requirements")
    checks = {path[len("checks/"):] for path in case.manifest.file_hashes if path.startswith("checks/")}
    return load_contract_requirements(case.root, name, case.manifest.file_hashes, checks)
