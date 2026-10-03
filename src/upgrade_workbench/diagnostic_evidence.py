"""从长测试输出中提取可稳定复核的失败证据与重复签名。"""

from __future__ import annotations

import hashlib
import re

_SIGNAL_PATTERNS = (
    ("database_constraint", re.compile(
        r"(?i)(?:NOT NULL|UNIQUE|CHECK|FOREIGN KEY) constraint failed:\s*[^\r\n\]]+"
    )),
    ("database_constraint", re.compile(
        r"(?i)(?:null value in column [^\r\n]+ violates not-null constraint|"
        r"duplicate key value violates unique constraint [^\r\n]+|"
        r"insert or update on table [^\r\n]+ violates foreign key constraint [^\r\n]+)"
    )),
    ("autoflush_trigger", re.compile(r"(?i)Query-invoked autoflush[^)\r\n]*")),
    ("assertion", re.compile(r"(?m)^\s*E\s+(assert\s+[^\r\n]+|AssertionError(?::[^\r\n]+)?)")),
    ("exception", re.compile(
        r"(?m)^\s*E\s+((?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception):[^\r\n]+)"
    )),
)
_EXCEPTION_TYPE = re.compile(r"(?:[A-Za-z_]\w*\.)*([A-Z]\w*(?:Error|Exception)):")


def _bounded_line(value: str, limit: int = 600) -> str:
    value = " ".join(str(value or "").strip().split())
    if len(value) <= limit:
        return value
    return value[: limit - 18] + " ... <truncated>"


def failure_signals(message: str) -> list[dict[str, str]]:
    """按诊断价值而非日志位置提取约束、触发器、断言和终端异常。"""
    signals = []
    seen = set()
    for kind, pattern in _SIGNAL_PATTERNS:
        for match in pattern.finditer(str(message or "")):
            text = _bounded_line(match.group(1) if match.lastindex else match.group(0))
            key = (kind, text.casefold())
            if not text or key in seen:
                continue
            seen.add(key)
            signals.append({
                "kind": kind,
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })
            if len(signals) >= 8:
                return signals
    return signals


def failure_fingerprint(message: str, signals: list[dict[str, str]] | None = None) -> str:
    """生成跨运行稳定的失败签名；相同签名不等于已经证明相同根因。"""
    signals = failure_signals(message) if signals is None else signals
    basis = []
    for item in signals:
        if item["kind"] in {"database_constraint", "autoflush_trigger", "assertion"}:
            basis.append(f"{item['kind']}:{item['text'].casefold()}")
    exception_types = _EXCEPTION_TYPE.findall(str(message or ""))
    if exception_types:
        basis.append("exception_type:" + exception_types[-1].casefold())
    if not basis:
        value = _bounded_line(str(message or "")[-800:]).casefold()
        value = re.sub(r"0x[0-9a-f]+", "0x<address>", value)
        basis.append("fallback:" + value)
    return hashlib.sha256("\n".join(dict.fromkeys(basis)).encode("utf-8")).hexdigest()


def enrich_failure_detail(detail: dict, raw_message: str) -> dict:
    """给有界展示文本补充来自完整原消息的身份和关键证据。"""
    signals = failure_signals(raw_message)
    return detail | {
        "message_sha256": hashlib.sha256(str(raw_message or "").encode("utf-8")).hexdigest(),
        "message_chars": len(str(raw_message or "")),
        "failure_fingerprint": failure_fingerprint(raw_message, signals),
        "critical_evidence": signals,
    }


def aggregate_failure_evidence(details: list[dict]) -> tuple[list[dict], list[dict]]:
    """去重关键证据并按失败签名聚类，避免相同长 traceback 重复占用预算。"""
    evidence = []
    evidence_seen = set()
    clusters = {}
    for detail in details:
        fingerprint = detail.get("failure_fingerprint")
        if fingerprint:
            cluster = clusters.setdefault(fingerprint, {
                "fingerprint": fingerprint,
                "occurrences": 0,
                "nodeids": [],
                "phases": [],
            })
            cluster["occurrences"] += 1
            if detail.get("nodeid") and detail["nodeid"] not in cluster["nodeids"]:
                cluster["nodeids"].append(detail["nodeid"])
            phase = detail.get("when", "call")
            if phase not in cluster["phases"]:
                cluster["phases"].append(phase)
        for item in detail.pop("critical_evidence", []):
            key = (item["kind"], item["sha256"])
            if key not in evidence_seen:
                evidence_seen.add(key)
                evidence.append(item)
    ordered = sorted(
        clusters.values(),
        key=lambda row: (-row["occurrences"], row["fingerprint"]),
    )
    return evidence[:12], ordered[:8]


def setup_failure(stages: dict) -> dict | None:
    """逐环境识别未进入 call 的失败，不否定其他环境已取得的观测。"""
    affected = []
    for name, stage in stages.items():
        phases = sorted({
            detail.get("when", "call")
            for detail in stage.get("failure_details", [])
            if isinstance(detail, dict)
        })
        setup_phases = [phase for phase in phases if phase in {"collect", "setup"}]
        tests = stage.get("tests", {})
        called = tests.get("passed", 0) + tests.get("failed", 0) > 0
        if stage.get("status") == "failed" and setup_phases and "call" not in phases and not called:
            affected.append({"stage": name, "phases": setup_phases,
                             "business_observation_obtained": False})
    if not affected:
        return None
    return {
        "classification": "probe_stage_setup_failed",
        "affected_stages": affected,
        "scope_limit": "Only the listed stages failed collection or setup before the probe body ran; "
                       "their failure is not evidence for or against the business hypothesis. "
                       "Observations from other stages remain available and require their own assessment.",
    }
