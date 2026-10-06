"""生成可冻结的文本输入；输入域由合同决定，工具不替代语义判断。"""

from __future__ import annotations

import json


def text_inputs(*, alphabet: str, max_length: int = 12, max_examples: int = 24) -> list[str]:
    """由 Hypothesis 生成有界输入，返回实际语料，供多个隔离环境逐项重放。"""
    if (not isinstance(alphabet, str) or not alphabet or "\x00" in alphabet
            or len(set(alphabet)) > 256
            or type(max_length) is not int or not 1 <= max_length <= 128
            or type(max_examples) is not int or not 1 <= max_examples <= 128):
        raise ValueError("Text input domain must have bounded alphabet, length and example count")
    try:
        from hypothesis import Phase, given, settings, strategies
    except ImportError as error:
        raise RuntimeError("Install the locked probe-generation extra to generate inputs") from error

    values = []

    @settings(max_examples=max_examples, derandomize=True, database=None,
              phases=(Phase.generate,), deadline=None)
    @given(strategies.text(alphabet=alphabet, max_size=max_length))
    def collect(value):
        if value not in values:
            values.append(value)

    collect()
    return values


def smallest_recorded_counterexample(comparisons: list[dict]) -> dict | None:
    """只缩减到已执行语料中的最小差异，不宣称任意输入空间的最小反例。"""
    failures = [row for row in comparisons if row.get("status") == "different"]
    if not failures:
        return None

    def order(row):
        data = json.dumps(row["input"], ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        return len(data), data

    return min(failures, key=order)
