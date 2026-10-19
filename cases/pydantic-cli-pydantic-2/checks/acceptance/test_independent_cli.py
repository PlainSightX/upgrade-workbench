"""独立验收检查：冻结后不向修复过程公开；只在任务终态后运行。

覆盖 README 列明的布尔四种情形、自定义前缀、多值参数、枚举、校验失败、退出码与异常处理、
prologue/epilogue 钩子、帮助与版本、JSON 预设的环境变量与自定义键。
模型只使用在 Pydantic 1 与 2 中语义一致的写法（可空字段显式写默认值 None）。
"""

from __future__ import annotations

import enum
import json
from typing import List, Optional, Set

from pydantic import BaseModel, Field, validator

from pydantic_cli import (
    DefaultConfig,
    default_minimal_exception_handler,
    pydantic_class_to_parser,
    to_runner,
)


def _recorder(code: int = 0):
    calls = []

    def runner(opts) -> int:
        calls.append(opts)
        return code

    return runner, calls


def _run(model, args, code: int = 0, **kwargs):
    runner, calls = _recorder(code)
    kwargs.setdefault("exception_handler", default_minimal_exception_handler)
    return to_runner(model, runner, **kwargs)(args), calls


class RequiredBool(BaseModel):
    alpha: bool


def test_required_bool_uses_enable_disable_pair():
    exit_code, calls = _run(RequiredBool, ["--enable-alpha"])
    assert exit_code == 0 and calls[0].alpha is True
    exit_code, calls = _run(RequiredBool, ["--disable-alpha"])
    assert exit_code == 0 and calls[0].alpha is False
    exit_code, calls = _run(RequiredBool, [])
    assert exit_code != 0 and calls == []


class OptionalBool(BaseModel):
    gamma: Optional[bool] = None
    zeta: Optional[bool] = Field(None, cli=("--yes-zeta", "--no-zeta"))


def test_optional_bool_defaults_to_none_and_accepts_custom_pair():
    exit_code, calls = _run(OptionalBool, [])
    assert exit_code == 0 and calls[0].gamma is None and calls[0].zeta is None
    exit_code, calls = _run(OptionalBool, ["--disable-gamma", "--yes-zeta"])
    assert exit_code == 0 and calls[0].gamma is False and calls[0].zeta is True


class NegatedDefault(BaseModel):
    debug: bool = Field(True, description="Disable debug mode", cli=("-d", "--disable-debug"))
    verbose: bool = True


def test_bool_default_true_flag_negates():
    exit_code, calls = _run(NegatedDefault, [])
    assert exit_code == 0 and calls[0].debug is True and calls[0].verbose is True
    exit_code, calls = _run(NegatedDefault, ["-d", "--disable-verbose"])
    assert exit_code == 0 and calls[0].debug is False and calls[0].verbose is False


class PrefixOptions(BaseModel):
    class Config:
        CLI_BOOL_PREFIX = ("--yes-", "--no-")

    debug: bool = False


def test_config_bool_prefix_is_respected():
    exit_code, calls = _run(PrefixOptions, ["--yes-debug"])
    assert exit_code == 0 and calls[0].debug is True
    exit_code, calls = _run(PrefixOptions, ["--enable-debug"])
    assert exit_code != 0 and calls == []


class State(str, enum.Enum):
    RUNNING = "RUNNING"
    FAILED = "FAILED"


class MultiOptions(BaseModel):
    input_file: List[str]
    states: Set[State]
    mode: State = State.RUNNING


def test_list_set_and_enum_values():
    exit_code, calls = _run(
        MultiOptions, ["--input_file", "a.fa", "b.fa", "--states", "FAILED", "RUNNING", "--mode", "FAILED"])
    assert exit_code == 0
    opts = calls[0]
    assert opts.input_file == ["a.fa", "b.fa"]
    assert opts.states == {State.FAILED, State.RUNNING}
    assert opts.mode is State.FAILED
    exit_code, calls = _run(MultiOptions, ["--input_file", "a", "--states", "BOGUS"])
    assert exit_code != 0 and calls == []


class Validated(BaseModel):
    max_records: int = Field(..., gt=0)
    name: str

    @validator("name")
    def no_spaces(cls, value):
        if " " in value:
            raise ValueError("name must not contain spaces")
        return value


def test_validation_failure_reports_nonzero_without_running():
    assert _run(Validated, ["--max_records", "0", "--name", "a"]) == (1, [])
    assert _run(Validated, ["--max_records", "3", "--name", "a b"]) == (1, [])
    exit_code, calls = _run(Validated, ["--max_records", "3", "--name", "ab"])
    assert exit_code == 0 and calls[0].max_records == 3


class Simple(BaseModel):
    n: int = 1


def test_runner_exit_code_and_exception_mapping():
    assert _run(Simple, [], code=3)[0] == 3

    def boom(opts) -> int:
        raise RuntimeError("runner failed")

    seen = []

    def handler(error: BaseException) -> int:
        seen.append(error)
        return 7

    assert to_runner(Simple, boom, exception_handler=handler)([]) == 7
    assert isinstance(seen[0], RuntimeError)


def test_prologue_and_epilogue_hooks_run():
    events = []
    exit_code, calls = _run(
        Simple, ["--n", "4"], code=2,
        prologue_handler=lambda opts: events.append(("pro", opts.n)),
        epilogue_handler=lambda code, seconds: events.append(("epi", code, seconds >= 0)))
    assert exit_code == 2
    assert events == [("pro", 4), ("epi", 2, True)]


class Described(BaseModel):
    max_records: int = Field(10, description="Max records to process")


def test_help_and_version_are_eager(capsys):
    exit_code, calls = _run(Described, ["--help"])
    assert exit_code == 0 and calls == []
    out = capsys.readouterr().out
    assert "--max_records" in out and "Max records to process" in out and "default:10" in out
    exit_code, calls = _run(Described, ["--version"], version="9.8.7")
    assert exit_code == 0 and calls == []
    assert "9.8.7" in capsys.readouterr().out
    help_text = pydantic_class_to_parser(Described, description="Tool", default_value_override={}).format_help()
    assert "Tool" in help_text and "Max records to process" in help_text


class EnvPreset(BaseModel):
    class Config(DefaultConfig):
        CLI_JSON_ENABLE = True
        CLI_JSON_KEY = "preset"
        CLI_JSON_CONFIG_ENV_VAR = "UW_CHECK_PRESET"

    hdf_file: str
    alpha: float = 0.5


def test_json_preset_from_env_var_and_custom_key(tmp_path, monkeypatch):
    preset = tmp_path / "p.json"
    preset.write_text(json.dumps({"hdf_file": "env.h5"}))
    monkeypatch.setenv("UW_CHECK_PRESET", str(preset))
    exit_code, calls = _run(EnvPreset, ["--alpha", "2.5"])
    assert exit_code == 0 and (calls[0].hdf_file, calls[0].alpha) == ("env.h5", 2.5)
    other = tmp_path / "q.json"
    other.write_text(json.dumps({"hdf_file": "cli.h5", "alpha": 1.0}))
    exit_code, calls = _run(EnvPreset, ["--preset", str(other)])
    assert exit_code == 0 and (calls[0].hdf_file, calls[0].alpha) == ("cli.h5", 1.0)


class FilePreset(BaseModel):
    class Config(DefaultConfig):
        CLI_JSON_ENABLE = True

    hdf_file: str


def test_missing_json_preset_fails(tmp_path):
    exit_code, calls = _run(FilePreset, ["--json-config", str(tmp_path / "missing.json")])
    assert exit_code != 0 and calls == []
