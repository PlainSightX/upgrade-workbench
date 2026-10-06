"""从受审探针的运行读数计算有限判据，不把 pytest 绿灯当业务证明。"""

from __future__ import annotations

import json
import math
import operator

MARKER = "UPGRADE_WORKBENCH_MEASUREMENT:"
OPERATORS = {"eq": operator.eq, "le": operator.le, "ge": operator.ge}
LIMITS = {
    "absolute": "Only the measured post-operation value; other paths remain untested.",
    "delta": "Only change from the measured baseline; does NOT establish an absolute requirement or a valid baseline.",
    "version_delta": "Only the new-minus-old difference; equal versions can both violate a business requirement.",
}


def _number(value):
    return type(value) in {int, float} and abs(value) <= 1e100 and math.isfinite(value)


def validate_oracle(value):
    if not isinstance(value, dict) or set(value) != {"requirement", "subject", "exercise", "basis", "operator", "expected"}:
        raise ValueError("oracle requires requirement, subject, exercise, basis, operator, expected")
    for key in ("requirement", "subject", "exercise"):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 700 or "\x00" in value[key]:
            raise ValueError("oracle descriptions must be bounded nonempty text")
    if (not isinstance(value["basis"], str) or value["basis"] not in LIMITS
            or not isinstance(value["operator"], str) or value["operator"] not in OPERATORS
            or not _number(value["expected"])):
        raise ValueError("oracle needs basis=absolute/delta/version_delta, operator=eq/le/ge, finite numeric expected")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate measurement field")
        result[key] = value
    return result


def measurement(stdout):
    """只认完整stdout中的独立记录；pytest失败回显中的源码不是第二份读数。"""
    records = [line[len(MARKER):] for line in stdout.splitlines() if line.startswith(MARKER)]
    if not records:
        return {"status": "missing_measurement"}
    if len(records) != 1:
        return {"status": "ambiguous_measurement"}
    try:
        if len(records[0].encode("utf-8")) > 2048:
            raise ValueError("measurement too large")
        value = json.loads(records[0], object_pairs_hook=_unique_object)
        fields = {"before", "after", "path_completed"}
        if (not isinstance(value, dict) or set(value) not in (fields, fields | {"input", "output"})
                or type(value["path_completed"]) is not bool
                or not all(_number(value[key]) for key in ("before", "after"))):
            raise ValueError("invalid measurement")
        if "input" not in value and len(records[0]) > 1000:
            raise ValueError("legacy measurement too large")
        if "input" in value:
            from .probe_comparisons import validate_sample

            validate_sample({key: value[key] for key in ("input", "output", "path_completed")})
    except (ValueError, TypeError, RecursionError, OverflowError):
        return {"status": "invalid_measurement"}
    return {"status": "measured" if value["path_completed"] else "path_not_completed", **value}


def evaluate(oracle, stages, stdout):
    """返回受限结论和原始读数。语义匹配仍须审阅，不能由字段齐全推断。"""
    validate_oracle(oracle)
    rows = {name: measurement(stdout.get(name, "")) for name in stages}
    basis = oracle["basis"]
    compare = OPERATORS[oracle["operator"]]
    old = rows.get("old_original", {})
    for name, row in rows.items():
        if row["status"] != "measured":
            row["conclusion"] = "inconclusive"
            if stages[name].get("status") != "passed":
                row["reason"] = "execution_failed_or_incomplete_before_valid_measurement"
            continue
        if basis == "version_delta" and name == "old_original":
            row["conclusion"] = "comparison_reference_only"
            continue
        if basis == "version_delta" and (old.get("status") != "measured"
                or stages.get("old_original", {}).get("status") != "passed"):
            row.update(conclusion="inconclusive", reason="old_comparator_not_valid")
            continue
        actual = (row["after"] if basis == "absolute" else row["after"] - row["before"]
                  if basis == "delta" else row["after"] - old["after"])
        matched = compare(actual, oracle["expected"])
        row.update(actual=actual, predicate_satisfied=matched)
        # 路径已完成后发现的不满足是反例；失败进程中的满足不能当正面证据。
        if not matched:
            row["conclusion"] = "counterexample_observed"
        elif stages[name].get("status") != "passed":
            row.update(conclusion="inconclusive", reason="execution_not_passed")
        else:
            row["conclusion"] = "no_counterexample_observed"
    target = "new_candidate" if "new_candidate" in stages else "new_original"
    return {"oracle": oracle, "measurements": rows,
            "conclusion": rows.get(target, {}).get("conclusion", "inconclusive"),
            "scope_limit": LIMITS[basis],
            "interpretation": "No counterexample observed is not disproof; code-to-requirement alignment is reviewed, not machine-proven."}
