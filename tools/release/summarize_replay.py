"""将固定候选复验投影成不含运行路径、原始回复或日志的公开 CI 摘要。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

STAGES = ("old_original", "new_original", "new_candidate")
COUNTS = ("collected", "passed", "failed", "errors", "skipped")


def summary(result: dict) -> dict:
    stages = {}
    for name in STAGES:
        tests = result["stages"][name]["tests"]
        counts = {key: tests[key] for key in COUNTS}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise ValueError("Invalid replay test counts")
        stages[name] = counts
    return {
        "schema_version": 1,
        "method": "fixed_candidate_replay",
        "status": result["status"],
        "model_calls": 0,
        "stages": stages,
        "scope": "Reviewed fixed patch behavior only; not autonomous generation or model comparison",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    value = summary(json.loads(args.input.read_bytes()))
    # 不覆盖既有回执；仅在内容完整且通过投影后创建公开输出。
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
