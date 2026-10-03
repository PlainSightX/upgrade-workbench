"""解析案例级 Python 执行环境合同。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_IMAGE_DIGEST = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._/:\-]*@sha256:[0-9a-f]{64}\Z"
)
_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+\Z")


class _EnvironmentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: int
    base_image: str
    python_version: str
    local_wheels: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: int) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("environment schema_version must be 1")
        return value

    @field_validator("base_image")
    @classmethod
    def immutable_image(cls, value: str) -> str:
        if not _IMAGE_DIGEST.fullmatch(value):
            raise ValueError("environment base_image must use an immutable registry digest")
        return value

    @field_validator("python_version")
    @classmethod
    def supported_python_format(cls, value: str) -> str:
        if not _PYTHON_VERSION.fullmatch(value):
            raise ValueError("python_version must use major.minor form")
        return value

    @field_validator("local_wheels")
    @classmethod
    def safe_wheel_paths(cls, values: list[str]) -> list[str]:
        names: set[str] = set()
        for value in values:
            path = PurePosixPath(value)
            if (
                not value
                or path.is_absolute()
                or path.as_posix() != value
                or any(part in {"", ".", ".."} for part in path.parts)
                or not value.endswith(".whl")
            ):
                raise ValueError(f"Invalid local wheel path: {value!r}")
            key = path.name.casefold()
            if key in names:
                raise ValueError("Local wheel filenames must be unique")
            names.add(key)
        return values


@dataclass(frozen=True)
class CaseEnvironment:
    """已经过严格解析、仍以案例相对路径表达的环境输入。"""

    base_image: str
    python_version: str
    local_wheels: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "base_image": self.base_image,
            "python_version": self.python_version,
            "local_wheels": list(self.local_wheels),
        }


def parse_case_environment(contents: bytes) -> CaseEnvironment:
    """解析环境合同；调用方负责把列出的资产绑定到案例哈希。"""
    try:
        raw = json.loads(contents.decode("utf-8"))
        contract = _EnvironmentContract.model_validate(raw)
    except (UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ValueError(f"Invalid case environment contract: {error}") from error
    return CaseEnvironment(
        base_image=contract.base_image,
        python_version=contract.python_version,
        local_wheels=tuple(contract.local_wheels),
    )
