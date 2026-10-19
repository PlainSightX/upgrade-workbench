"""公开反馈检查：修复过程中可见；覆盖 README 快速入门中的必填参数、布尔开关、自定义旗标、
JSON 预设与子命令分派。

每个测试通过 `to_runner(...)(args)` 取得退出码，runner 把收到的模型实例记录到列表；
未调用 runner 即表示参数解析或校验在执行前失败。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from pydantic_cli import DefaultConfig, SubParser, default_minimal_exception_handler, to_runner, to_runner_sp


def _recorder(code: int = 0):
    """返回 (runner, calls)；calls 保存每次调用收到的选项对象。"""
    calls = []

    def runner(opts) -> int:
        calls.append(opts)
        return code

    return runner, calls


def _run(model, args):
    runner, calls = _recorder()
    exit_code = to_runner(model, runner, exception_handler=default_minimal_exception_handler)(args)
    return exit_code, calls


class MinOptions(BaseModel):
    input_file: str
    max_records: int


def test_required_typed_options_reach_runner():
    exit_code, calls = _run(MinOptions, ["--input_file", "data.txt", "--max_records", "12"])
    assert exit_code == 0
    assert len(calls) == 1
    opts = calls[0]
    assert isinstance(opts, MinOptions)
    assert opts.input_file == "data.txt"
    assert opts.max_records == 12 and isinstance(opts.max_records, int)


def test_missing_required_option_fails_without_running():
    exit_code, calls = _run(MinOptions, ["--input_file", "data.txt"])
    assert exit_code != 0
    assert calls == []


class DebugOptions(BaseModel):
    name: str
    debug: bool = False


def test_bool_default_false_gets_enable_flag():
    exit_code, calls = _run(DebugOptions, ["--name", "a"])
    assert exit_code == 0 and calls[0].debug is False
    exit_code, calls = _run(DebugOptions, ["--name", "a", "--enable-debug"])
    assert exit_code == 0 and calls[0].debug is True


class CustomFlagOptions(BaseModel):
    input_file: str = Field(..., description="Path to input", cli=("-i", "--input-file"))
    max_records: int = Field(..., description="Max records", cli=("-m", "--max-records"))


def test_custom_short_and_long_flags_from_field():
    exit_code, calls = _run(CustomFlagOptions, ["-i", "in.h5", "--max-records", "5"])
    assert exit_code == 0
    assert (calls[0].input_file, calls[0].max_records) == ("in.h5", 5)
    exit_code, calls = _run(CustomFlagOptions, ["--input-file", "x", "-m", "6"])
    assert exit_code == 0 and calls[0].max_records == 6


class PresetOptions(BaseModel):
    class Config(DefaultConfig):
        CLI_JSON_ENABLE = True

    hdf_file: str
    max_records: int = 10
    alpha: float


def test_json_config_supplies_values_and_cli_overrides(tmp_path):
    preset = tmp_path / "preset.json"
    preset.write_text(json.dumps({"hdf_file": "a.h5", "alpha": 1.5, "max_records": 5}))
    exit_code, calls = _run(PresetOptions, ["--json-config", str(preset)])
    assert exit_code == 0
    assert (calls[0].hdf_file, calls[0].alpha, calls[0].max_records) == ("a.h5", 1.5, 5)
    exit_code, calls = _run(PresetOptions, ["--json-config", str(preset), "--max_records", "7"])
    assert exit_code == 0 and calls[0].max_records == 7 and calls[0].alpha == 1.5


class AlphaOptions(BaseModel):
    input_file: str


class BetaOptions(BaseModel):
    retries: int = 3


def test_subparser_dispatches_to_selected_model():
    alpha, alpha_calls = _recorder()
    beta, beta_calls = _recorder()
    runner = to_runner_sp({
        "alpha": SubParser[AlphaOptions](AlphaOptions, alpha, "Alpha"),
        "beta": SubParser[BetaOptions](BetaOptions, beta, "Beta"),
    }, exception_handler=default_minimal_exception_handler)
    assert runner(["beta", "--retries", "5"]) == 0
    assert alpha_calls == [] and isinstance(beta_calls[0], BetaOptions) and beta_calls[0].retries == 5
    assert runner(["alpha", "--input_file", "f"]) == 0
    assert alpha_calls[0].input_file == "f"
