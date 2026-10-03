"""固定Ruff只解析公开源码快照；静态告警不替代行为验收。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory

VERSION = "0.16.7"
POLICY = {"tool": "ruff", "version": VERSION, "rules": ["F821"],
          "target_python": "py312", "isolated": True, "cache": False,
          "noqa": False, "fix": False}
MAX_OUTPUT_BYTES = 8_000_000
VISIBLE_LIMIT = 128


def enabled(output_format: str, arm: str) -> bool:
    return output_format in {"candidate_actions", "diagnostic_actions", "repository_context_actions"} and arm in {"full", "no_evidence"}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scan(files: dict[str, bytes]) -> dict:
    hashes = {name: _sha(data) for name, data in sorted(files.items()) if name.endswith(".py")}
    result = {"source_hashes": hashes, "status": "unavailable", "findings": [], "errors": []}
    try:
        actual_version = metadata.version("ruff")
    except metadata.PackageNotFoundError:
        return result | {"errors": [{"code": "tool_missing"}]}
    if actual_version != VERSION:
        return result | {"errors": [{"code": "tool_version_mismatch"}]}
    if not hashes or sum(len(files[n]) for n in hashes) > 1_600_000:
        return result | {"errors": [{"code": "source_capacity_invalid"}]}
    # 只在新临时目录物化已验证的字节；-I排除目标目录和PYTHONPATH模块劫持。
    with TemporaryDirectory(prefix="upgrade-static-") as temporary:
        root = Path(temporary).resolve()
        for name in hashes:
            path = root / name
            if path.is_absolute() and not path.resolve().is_relative_to(root):
                raise ValueError("Static source escapes its isolated snapshot")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[name])
        command = [sys.executable, "-I", "-m", "ruff", "check", "--isolated",
                   "--no-cache", "--ignore-noqa", "--no-fix", "--no-respect-gitignore",
                   "--target-version", "py312", "--select", "F821",
                   "--output-format", "json", "--", *hashes]
        try:
            # 输出落临时文件再有界读取；不能把工具错误当作零告警。
            with (root / "stdout.json").open("wb") as stdout, (root / "stderr.txt").open("wb") as stderr:
                environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("RUFF_")}
                process = subprocess.run(command, cwd=root, stdout=stdout, stderr=stderr,
                                         env=environment, timeout=20, check=False)
            if process.returncode not in {0, 1}:
                return result | {"errors": [{"code": "tool_execution_failed"}]}
            if (root / "stdout.json").stat().st_size > MAX_OUTPUT_BYTES:
                return result | {"errors": [{"code": "tool_output_capacity_exceeded"}]}
            rows = json.loads((root / "stdout.json").read_bytes())
            if not isinstance(rows, list):
                raise ValueError("Invalid Ruff output")
            for row in rows:
                name = Path(row["filename"]).resolve().relative_to(root).as_posix()
                if name not in hashes:
                    raise ValueError("Unexpected source in Ruff output")
                location = row["location"]
                if any(type(location[k]) is not int or location[k] < 1 for k in ("row", "column")):
                    raise ValueError("Invalid diagnostic location")
                item = {"path": name, "line": location["row"], "column": location["column"],
                        "code": row["code"], "message": row["message"]}
                if row["code"] == "F821":
                    result["findings"].append(item)
                elif row["code"] == "invalid-syntax":
                    result["errors"].append({"path": name, "line": location["row"], "code": "parse_error"})
                else:
                    raise ValueError("Unexpected Ruff rule")
        except subprocess.TimeoutExpired:
            return result | {"findings": [], "errors": [{"code": "tool_timeout"}]}
        except (OSError, ValueError, KeyError, TypeError):
            return result | {"findings": [], "errors": [{"code": "tool_output_invalid"}]}
    result["findings"].sort(key=lambda x: (x["path"], x["line"], x["column"]))
    result["status"] = "partial" if result["errors"] else "complete"
    return result


def _key(item: dict) -> tuple:
    return item["path"], item["code"], item["message"]


def diagnose(snapshot, original_revision: str) -> dict:
    """比较同一案例的名称告警存量；行移动不冒充新增，不推断语义等价。"""
    baseline = _scan(snapshot.original)
    current = baseline if snapshot.files == snapshot.original else _scan(snapshot.files)
    comparable = baseline["status"] == current["status"] == "complete"
    before = Counter(_key(item) for item in baseline["findings"])
    after = Counter(_key(item) for item in current["findings"])
    findings, disappeared = [], []
    for item in current["findings"]:
        key = _key(item)
        classification = "unclassified"
        if comparable:
            classification = "pre_existing" if before[key] else "newly_observed"
            before[key] = max(0, before[key] - 1)
        findings.append(item | {"comparison": classification})
    if comparable:
        for item in baseline["findings"]:
            key = _key(item)
            if after[key]:
                after[key] -= 1
            else:
                disappeared.append(item | {"comparison": "no_longer_observed"})
    return {"schema_version": 1, "policy": POLICY,
            "baseline": {"revision": original_revision, **baseline},
            "current": {"revision": snapshot.revision, **current},
            "comparison_available": comparable, "findings": findings, "disappeared": disappeared,
            "comparison_basis": "per_file_rule_message_occurrence_counts_ignoring_line_shifts_not_semantic_identity",
            "scope": "static_warning_not_runtime_failure_or_behavior_acceptance"}


def visible(report: dict) -> dict:
    """完整诊断保留在请求推导附件；模型视图有界且明确披露截断。"""
    result = {key: value for key, value in report.items() if key not in {"baseline", "current", "findings", "disappeared"}}
    for view in ("baseline", "current"):
        scan = report[view]
        result[view] = {key: value for key, value in scan.items() if key != "findings"}
        result[view]["observed_count"] = len(scan["findings"])
        result[view]["errors"] = scan["errors"][:VISIBLE_LIMIT]
        result[view]["omitted_errors"] = max(0, len(scan["errors"]) - VISIBLE_LIMIT)
    for key in ("findings", "disappeared"):
        result[key] = report[key][:VISIBLE_LIMIT]
        result["omitted_" + key] = max(0, len(report[key]) - VISIBLE_LIMIT)
    return result
