"""公开测试执行谱的纯离线评分；不读取隐藏检查，也不决定业务根因。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

Outcome = Literal["passed", "failed", "error", "skipped"]
SPECTRUM_PREFIX = "UPGRADE_WORKBENCH_SPECTRUM:"


@dataclass(frozen=True)
class SpectrumRecord:
    """一项公开测试实际执行过的允许源码行集合。"""

    nodeid: str
    outcome: Outcome
    locations: frozenset[tuple[str, int]]


@dataclass(frozen=True)
class SuspiciousLine:
    """Ochiai 分数及其可复核计数。"""

    path: str
    line: int
    score: float
    failed_covered: int
    passed_covered: int
    total_failed: int


def ochiai(*, failed_covered: int, passed_covered: int, total_failed: int) -> float:
    """计算 Ochiai；没有失败测试或该行从未执行时返回 0。"""

    values = (failed_covered, passed_covered, total_failed)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("Ochiai counts must be non-negative integers")
    if failed_covered > total_failed:
        raise ValueError("failed_covered cannot exceed total_failed")
    denominator = total_failed * (failed_covered + passed_covered)
    return failed_covered / math.sqrt(denominator) if denominator else 0.0


def rank_spectrum(
    spectra: Iterable[SpectrumRecord], *, allowed_files: Iterable[str]
) -> list[SuspiciousLine]:
    """按测试级覆盖计数生成稳定排名；error/skipped 不冒充有效谱。"""

    allowed = frozenset(allowed_files)
    records = tuple(spectra)
    total_failed = sum(record.outcome == "failed" for record in records)
    counts: dict[tuple[str, int], list[int]] = {}
    for record in records:
        if record.outcome not in {"passed", "failed"}:
            continue
        for path, line in record.locations:
            if path not in allowed or type(line) is not int or line < 1:
                continue
            pair = counts.setdefault((path, line), [0, 0])
            pair[0 if record.outcome == "failed" else 1] += 1
    ranked = [
        SuspiciousLine(
            path=path,
            line=line,
            score=ochiai(
                failed_covered=failed,
                passed_covered=passed,
                total_failed=total_failed,
            ),
            failed_covered=failed,
            passed_covered=passed,
            total_failed=total_failed,
        )
        for (path, line), (failed, passed) in counts.items()
    ]
    return sorted(
        ranked,
        key=lambda item: (
            -item.score,
            -item.failed_covered,
            item.passed_covered,
            item.path,
            item.line,
        ),
    )


def parse_spectrum_log(stdout_path: Path) -> dict[str, Any]:
    """从 pytest 输出中读取唯一采谱载荷，允许标记前存在进度字符。"""

    rows = [
        line.split(SPECTRUM_PREFIX, 1)[1]
        for line in stdout_path.read_text(encoding="utf-8").splitlines()
        if SPECTRUM_PREFIX in line
    ]
    if len(rows) != 1:
        raise ValueError("Expected exactly one bounded spectrum marker")
    value = json.loads(rows[0])
    if value.get("schema_version") != 1 or not isinstance(value.get("tests"), list):
        raise ValueError("Invalid spectrum payload")
    return value


def decide_gate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """按冻结门槛判定采谱结果；空对照不能形成正向结论。"""

    valid = all(
        result["execution"]["status"] in {"passed", "failed"}
        and result["execution"]["tests"]["errors"] == 0
        and not result["spectrum"]["truncated"]
        for result in results
    )
    labels_top_20 = all(
        label["sbfl_rank"] is not None and label["sbfl_rank"] <= 20
        for result in results
        for label in result["labels"]
    )
    current_workset_available = all(
        bool(result["current_localization"]["requests"]) for result in results
    )
    improved = current_workset_available and any(
        label["sbfl_rank"] is not None
        and label["sbfl_rank"] <= 20
        and (label["current_workset_rank"] is None or label["current_workset_rank"] > 20)
        for result in results
        for label in result["labels"]
    )
    traceback_comparison_available = all(
        bool(result["current_localization"]["traceback_locations"])
        for result in results
    )
    traceback_retained = traceback_comparison_available and all(
        row["sbfl_rank"] is not None and row["sbfl_rank"] <= 20
        for result in results
        for row in result["current_localization"]["traceback_locations"]
    )
    discriminating = all(
        {"passed", "failed"} <= set(result["spectrum"]["outcomes"])
        and result["spectrum"]["top_tie_count"] <= 20
        for result in results
    )
    admitted = valid and labels_top_20 and improved and traceback_retained and discriminating
    return {
        "admitted": admitted,
        "valid_execution": valid,
        "all_labels_top_20": labels_top_20,
        "current_workset_comparison_available": current_workset_available,
        "improves_current_workset": improved,
        "traceback_comparison_available": traceback_comparison_available,
        "traceback_locations_retained_top_20": traceback_retained,
        "discriminating": discriminating,
        "next_action": (
            "integrate_bounded_ranking_then_register_at_most_one_new_task"
            if admitted
            else "reject_or_defer_sbfl_and_stop_before_model_execution"
        ),
    }
