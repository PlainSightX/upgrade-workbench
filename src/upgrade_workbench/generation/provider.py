"""一次 provider 调用只产生待审阅补丁；不运行目标、不重试未知结果。"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, proxy_bypass

from ..cases import (
    CaseValidationError,
    PatchInfrastructureError,
    PatchValidationError,
    load_case,
    stage_source,
)
from ..cases.manifest import assert_no_links, read_verified_file
from .actions import AgentActionError, validate_action
from .diagnostics import transport_diagnostic
from .edits import edits_to_patch
from .request import (
    CONTRACT_REQUIREMENT_FORMATS,
    CURRENT_CANDIDATE_FORMATS,
    DIAGNOSTIC_ACTION_FORMATS,
    DIAGNOSTIC_CONTEXT_FORMATS,
    EDIT_FEEDBACK_FORMATS,
    EXPERIMENT_ARMS,
    MAX_EVIDENCE_BYTES,
    MAX_PATCH_BYTES,
    MAX_REQUEST_BYTES,
    MAX_REQUEST_CAPACITY,
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_CAPACITY,
    PROTOCOL_ERROR_CODES,
    PROTOCOL_V6_FORMATS,
    STRUCTURED_ACTION_FORMATS,
    SUPPORTED_OUTPUT_FORMATS,
    ProposalInputError,
    _digest,
    _endpoint,
    _instructions,
    _json_bytes,
    _located_evidence,
    _located_findings,
    _positive_limit,
    _text,
    derive_context_inputs,
    prepare_request,
    solver_source_names,
    validate_comparison_context,
    validate_task_context,
)

Transport = Callable[..., bytes]


class _ProviderBoundaryError(RuntimeError):
    """仅携带固定错误码，避免把认证头或远端响应写入错误日志。"""

    def __init__(self, code: str, feedback: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.feedback = feedback


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _ProviderBoundaryError("redirect_rejected")


@contextmanager
def provider_transport_route(endpoint: str, route: str):
    """把代理选择限制在当前 worker 调用内，并在退出时恢复原环境。"""
    if route not in {"system", "process_direct"}:
        raise ValueError("Unrecognized provider transport route")
    host = urlsplit(endpoint).hostname
    if not host:
        raise ValueError("Provider endpoint requires a host")
    previous = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
    if route == "process_direct":
        value = ",".join(dict.fromkeys(filter(None, (*previous.values(), host))))
        for key in previous:
            os.environ[key] = value
    try:
        bypassed = proxy_bypass(host)
        if route == "process_direct" and not bypassed:
            raise ValueError("Registered direct provider route is not effective")
        yield {
            "registered_route": route,
            "target_proxy_bypassed": bypassed,
            "global_proxy_modified": False,
            "certificate_verification": "enabled",
        }
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _send_once(request: Request, *, timeout_seconds: int, max_response_bytes: int) -> bytes:
    """认证只发给指定端点；不跟随跳转，没有自动重试。"""
    started = time.monotonic()
    opener = build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout_seconds) as response:
        if response.status != 200:
            raise _ProviderBoundaryError("unexpected_http_status")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                size = int(content_length)
            except ValueError as error:
                raise _ProviderBoundaryError("invalid_content_length") from error
            if size < 0 or size > max_response_bytes:
                raise _ProviderBoundaryError("response_too_large")
        # read1 避免等待填满缓冲区；总截止时间之外只容许当前一次有界 socket 读取结束。
        parts = []
        total = 0
        read = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() - started >= timeout_seconds:
                raise TimeoutError("provider deadline exceeded")
            part = read(min(16_384, max_response_bytes + 1 - total))
            if not part:
                break
            total += len(part)
            if total > max_response_bytes:
                raise _ProviderBoundaryError("response_too_large")
            parts.append(part)
        return b"".join(parts)


def _bounded_transport(transport: Transport, request: Request, timeout_seconds: int,
                       max_response_bytes: int = MAX_RESPONSE_BYTES) -> bytes:
    """限制调用方等待时长；超时后的远端结果未知，迟到回复不能成为候选。"""
    result: queue.Queue = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            response = transport(
                request, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes
            )
            result.put_nowait((True, response))
        except BaseException as error:
            result.put_nowait((False, error))

    # urllib 的 socket 超时不能限制逐字节到来的头部；守护线程只持有一次网络调用，
    # 不写运行记录。主线程退出等待后不消费迟到结果，也不启动第二次调用。
    thread = threading.Thread(target=invoke, daemon=True, name="upgrade-proposal-transport")
    thread.start()
    try:
        succeeded, value = result.get(timeout=timeout_seconds)
    except queue.Empty as error:
        raise TimeoutError("provider deadline exceeded") from error
    if not succeeded:
        if isinstance(value, (Exception, KeyboardInterrupt)):
            raise value
        raise _ProviderBoundaryError("provider_transport_aborted")
    return value


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _ProviderBoundaryError("duplicate_json_key")
        result[key] = value
    return result


def _decode_json(contents: bytes | str, *, proposal: bool = False) -> object:
    def reject_constant(value: str) -> None:
        raise _ProviderBoundaryError("nonfinite_json_number")

    try:
        return json.loads(
            contents,
            object_pairs_hook=_unique_pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        # 只返回助手正文的位置和固定指引，不回显可能含敏感信息的原文。
        feedback = None
        if proposal:
            feedback = (
                "Action not executed: invalid JSON in assistant message.content at "
                f"line {error.lineno}, column {error.colno} (1-based), "
                f"character {error.pos} (0-based). Return the complete corrected JSON object. "
                "Escape quotes, backslashes and newlines inside strings; use integer line numbers "
                "without stray quotes and check commas/brackets. No automatic repair was applied."
            )
        raise _ProviderBoundaryError("invalid_json", feedback) from error
    except (ValueError, UnicodeError, RecursionError) as error:
        raise _ProviderBoundaryError("invalid_json") from error


def _redact_tree(value: object, api_key: str, encoded_depth: int = 0) -> tuple[object, bool]:
    """检查 JSON 键、值及再次编码的 JSON 字符串，避免转义绕过字节替换。"""
    if isinstance(value, dict):
        result, found = {}, False
        for name, item in value.items():
            safe_name, name_found = _redact_tree(name, api_key, encoded_depth)
            safe_item, item_found = _redact_tree(item, api_key, encoded_depth)
            result[safe_name] = safe_item
            found = found or name_found or item_found
        return result, found
    if isinstance(value, list):
        result, found = [], False
        for item in value:
            safe_item, item_found = _redact_tree(item, api_key, encoded_depth)
            result.append(safe_item)
            found = found or item_found
        return result, found
    if not isinstance(value, str):
        return value, False
    if api_key in value:
        return value.replace(api_key, "[REDACTED_PROVIDER_KEY]"), True
    decoded = value
    for _ in range(8):
        unescaped = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match[1], 16)), decoded)
        if unescaped == decoded:
            break
        decoded = unescaped
        if api_key in decoded:
            return decoded.replace(api_key, "[REDACTED_PROVIDER_KEY]"), True
    else:
        raise _ProviderBoundaryError("response_encoding_depth_exceeded")
    # message.content 本身是 JSON 字符串；只在发现敏感值时重写它，正常回复保留原字节。
    if value.lstrip().startswith(("{", "[", '"')):
        if encoded_depth >= 8:
            raise _ProviderBoundaryError("response_encoding_depth_exceeded")
        try:
            nested = _decode_json(value)
        except _ProviderBoundaryError:
            return value, False
        safe_nested, found = _redact_tree(nested, api_key, encoded_depth + 1)
        if found:
            return json.dumps(safe_nested, ensure_ascii=True), True
    return value, False


def _sanitized_response(raw: bytes, api_key: str) -> tuple[bytes, object, bool, str | None]:
    """无法可靠检查的回复只保存元数据，绝不把未检查原文作为错误附件写出。"""
    try:
        response = _decode_json(raw)
        _, secret_echo = _redact_tree(response, api_key)
        if secret_echo:
            metadata = {
                "content_omitted": True, "response_bytes": len(raw),
                "reason": "provider_secret_echo", "provider_content": "[REDACTED_PROVIDER_KEY]",
            }
            return _json_bytes(metadata), None, True, None
        if api_key.encode() in raw:
            raise _ProviderBoundaryError("uninspectable_literal_secret")
        return raw, response, False, None
    except (_ProviderBoundaryError, RecursionError) as error:
        reason = str(error) if isinstance(error, _ProviderBoundaryError) else "response_depth_exceeded"
        metadata = {"content_omitted": True, "response_bytes": len(raw), "reason": reason}
        return _json_bytes(metadata), None, api_key.encode() in raw, reason


def _usage(response: dict) -> dict:
    value = response.get("usage")
    if value is None:
        return {"availability": "unavailable", "input_tokens": None, "output_tokens": None}
    if not isinstance(value, dict):
        raise _ProviderBoundaryError("invalid_usage")
    numbers = [value.get("prompt_tokens"), value.get("completion_tokens")]
    if any(type(number) is not int or number < 0 for number in numbers):
        raise _ProviderBoundaryError("invalid_usage")
    if "total_tokens" in value and (
        type(value["total_tokens"]) is not int or value["total_tokens"] != sum(numbers)
    ):
        raise _ProviderBoundaryError("invalid_usage")
    result = {"availability": "reported", "input_tokens": numbers[0], "output_tokens": numbers[1]}
    for field in ("prompt_tokens_details", "completion_tokens_details"):
        if value.get(field) is not None and not isinstance(value[field], dict):
            raise _ProviderBoundaryError("invalid_usage")
    # 供应商可选细分只在实际报告时记录，缺失不当成零。
    for field, value in (
        ("cached_input_tokens", value.get("prompt_cache_hit_tokens", (value.get("prompt_tokens_details") or {}).get("cached_tokens"))),
        ("uncached_input_tokens", value.get("prompt_cache_miss_tokens")),
        ("reasoning_tokens", (value.get("completion_tokens_details") or {}).get("reasoning_tokens")),
    ):
        if value is not None:
            if type(value) is not int or value < 0:
                raise _ProviderBoundaryError("invalid_usage")
            result[field] = value
    return result


def completion_metadata(response: dict, usage: dict) -> dict:
    """只标记已完整收回、可记账的长度截断；半截动作永不执行。"""
    choices = response.get("choices")
    choice = (choices[0] if isinstance(choices, list) and len(choices) == 1
              and isinstance(choices[0], dict) else {})
    value = choice.get("finish_reason")
    reason = value if value in ("stop", "length", "content_filter", "tool_calls") else "other"
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    valid = (isinstance(message, dict) and isinstance(content, str)
             and not message.get("refusal") and not message.get("tool_calls"))
    return {"finish_reason": reason,
            "content_chars": len(content) if isinstance(content, str) else None,
            "known_length": reason == "length" and valid and usage.get("availability") == "reported"}


def _proposal(response: dict, output_format: str = "unified_diff") -> dict:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise _ProviderBoundaryError("invalid_choices")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise _ProviderBoundaryError("response_not_complete")
    message = choice.get("message")
    if (
        not isinstance(message, dict)
        or message.get("refusal")
        or message.get("tool_calls")
        or not isinstance(message.get("content"), str)
    ):
        raise _ProviderBoundaryError("invalid_message")
    proposal = _decode_json(message["content"], proposal=True)
    content_key = {
        "unified_diff": "patch", "exact_edits": "edits", "single_patch_initial": "edits",
        "agent_actions": "action", "candidate_actions": "action",
        "diagnostic_actions": "action", "contract_actions": "action", "contract_audit": "audit",
        "protocol_v6_actions": "action", "investigator_actions": "action", "repository_context_actions": "action",
    }[output_format]
    if output_format in DIAGNOSTIC_ACTION_FORMATS and isinstance(proposal, dict) and "issues" in proposal:
        # 保持合同严格，返回可纠正的固定错误码；不回显模型提供的任意内容。
        issue_hint = "Move optional issues inside action, remove every other top-level field, and resend "
        action = proposal.get("action")
        if (
            output_format in PROTOCOL_V6_FORMATS
            and isinstance(action, dict) and action.get("type") in ("finish", "handoff")
        ):
            issue_hint = (
                "finish and handoff do not accept issues; do not move issues inside them. "
                "Remove issues, keep caveats in the action's declared fields, and resend "
            )
        raise _ProviderBoundaryError(
            "diagnostic_issues_outside_action",
            "Response was not executed. Top-level keys must be exactly summary and action. "
            + issue_hint + "the complete corrected object.",
        )
    if not isinstance(proposal, dict) or set(proposal) != {"summary", content_key}:
        raise _ProviderBoundaryError("invalid_proposal_schema")
    if not isinstance(proposal["summary"], str) or not proposal["summary"].strip():
        raise _ProviderBoundaryError("invalid_proposal_schema")
    if len(proposal["summary"]) > 12_000:
        raise _ProviderBoundaryError("proposal_too_large")
    if output_format == "unified_diff":
        if not isinstance(proposal["patch"], str) or not proposal["patch"].strip():
            raise _ProviderBoundaryError("invalid_proposal_schema")
        if len(proposal["patch"].encode()) > MAX_PATCH_BYTES:
            raise _ProviderBoundaryError("proposal_too_large")
    return proposal


def _save(report: dict, path: Path) -> None:
    report["updated_at"] = datetime.now(UTC).isoformat()
    temporary = path.with_name("proposal.tmp")
    assert_no_links(temporary)
    temporary.write_bytes(_json_bytes(report))
    temporary.replace(path)


def _load_prepared(prepared: dict) -> tuple[dict, bytes, Path]:
    if not isinstance(prepared, dict) or not isinstance(prepared.get("report_path"), str):
        raise ProposalInputError("A prepared request receipt is required")
    report_path = Path(prepared["report_path"]).absolute()
    assert_no_links(report_path)
    if report_path.stat().st_size > MAX_REQUEST_BYTES:
        raise ProposalInputError("Prepared receipt is too large")
    try:
        stored = _decode_json(report_path.read_bytes())
    except _ProviderBoundaryError as error:
        raise ProposalInputError("Prepared receipt is not valid JSON") from error
    if not isinstance(stored, dict) or stored != prepared:
        raise ProposalInputError("Prepared receipt changed or this request was already attempted")
    required_text = (
        "request_path", "request_sha256", "manifest_path", "case_fingerprint",
        "source_kind", "model", "endpoint", "contract_path",
    )
    if any(not isinstance(stored.get(key), str) or not stored[key] for key in required_text):
        raise ProposalInputError("Prepared receipt has missing or invalid required fields")
    if stored.get("status") != "request_prepared":
        raise ProposalInputError("Prepared receipt changed or this request was already attempted")
    if (
        type(stored.get("schema_version")) is not int or stored["schema_version"] != 1
        or type(stored.get("calls")) is not int or stored["calls"] != 0
        or stored.get("public_source_ack") is not True
        or stored.get("target_code_executed") is not False
        or type(stored.get("live_call_eligible")) is not bool
        or stored["source_kind"] not in {"upstream_snapshot", "derived_snapshot", "synthetic_calibration"}
        or stored["live_call_eligible"] != (stored["source_kind"] in {"upstream_snapshot", "derived_snapshot"})
        or stored.get("thinking_mode") not in (None, "enabled", "disabled")
        or stored.get("output_format", "unified_diff") not in SUPPORTED_OUTPUT_FORMATS
        or not isinstance(stored.get("experiment_arm", "full"), str)
        or stored.get("experiment_arm", "full") not in EXPERIMENT_ARMS
        or type(stored.get("max_response_bytes")) is not int
        or not re.fullmatch(r"[0-9a-f]{64}", stored["request_sha256"])
        or not re.fullmatch(r"[0-9a-f]{64}", stored["case_fingerprint"])
    ):
        raise ProposalInputError("Prepared receipt violates the request contract")
    hashes = stored.get("input_hashes")
    if (
        not isinstance(hashes, dict) or not hashes
        or any(
            not isinstance(name, str) or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for name, digest in hashes.items()
        )
        or stored["contract_path"] not in hashes
    ):
        raise ProposalInputError("Prepared receipt has invalid public input hashes")
    request_path = Path(stored["request_path"]).absolute()
    assert_no_links(request_path)
    if request_path.parent != report_path.parent or request_path.name != "request.json":
        raise ProposalInputError("Request must belong to its prepared operation")
    max_source_bytes = stored.get("max_source_bytes", MAX_SOURCE_BYTES)
    max_request_bytes = stored.get("max_request_bytes", MAX_REQUEST_BYTES)
    _positive_limit(max_source_bytes, "max_source_bytes", MAX_SOURCE_CAPACITY)
    _positive_limit(max_request_bytes, "max_request_bytes", MAX_REQUEST_CAPACITY)
    if request_path.stat().st_size > max_request_bytes:
        raise ProposalInputError("Request exceeds its byte limit")
    contents = request_path.read_bytes()
    if _digest(contents) != stored["request_sha256"]:
        raise ProposalInputError("Prepared request bytes changed")
    _endpoint(stored["endpoint"])
    from .capacity import from_record

    try:
        from_record(stored)
    except (ValueError, KeyError) as error:
        raise ProposalInputError(str(error)) from error
    _verify_inputs(stored)
    _verify_payload(stored, contents)
    return stored, contents, report_path


def _verify_payload(record: dict, contents: bytes) -> None:
    """收据参数必须与真实发送内容一致，不能只核对一个可同步改写的请求摘要。"""
    try:
        from .capacity import from_record
        from .protocol_v6 import validate_workflow_profile

        envelope = from_record(record)

        workflow_profile = validate_workflow_profile(record.get("workflow_profile", "workbench"))
        if workflow_profile != "workbench" and record.get("output_format") != "protocol_v6_actions":
            raise ProposalInputError("Simple tools workflow requires Protocol 6 Solver actions")
        if workflow_profile == "simple_tools" and record.get("experiment_arm") != "no_ast":
            raise ProposalInputError("Simple tools requires official version evidence without AST assistance")
        payload = _decode_json(contents)
        messages = payload["messages"]
        context = _decode_json(messages[1]["content"])
        expected = {
            "model": record["model"], "messages": messages,
            "response_format": {"type": "json_object"}, "max_tokens": record["max_output_tokens"],
            "stream": False, "n": 1,
        }
        if record.get("thinking_mode") is not None:
            expected["thinking"] = {"type": record["thinking_mode"]}
        if (
            payload != expected or len(messages) != 2
            or messages[0] != {"role": "system", "content": _instructions(record.get("output_format", "unified_diff"), workflow_profile, record.get("investigator_context_policy"), record.get("delivery_review_policy"), record.get("semantic_risk_policy"))}
            or set(messages[1]) != {"role", "content"} or messages[1]["role"] != "user"
            or not isinstance(context, dict)
        ):
            raise ProposalInputError("Prepared payload violates its frozen generation protocol")
        case = load_case(Path(record["manifest_path"]))
        arm = record.get("experiment_arm", "full")
        single_initial = record.get("output_format") == "single_patch_initial"
        if single_initial and (
            arm != "no_ast" or record.get("candidate_reference") is not None
            or "diagnostic_context_reference" in record or "task_context" in context
            or "decision_objective" in record or "comparison_binding" in record
        ):
            raise ProposalInputError("Single patch initial input cannot include runtime or candidate history")
        from ..analysis.static import enabled, visible

        static_enabled = enabled(record.get("output_format", "unified_diff"), arm)
        required = {
            "case_id", "case_fingerprint", "analysis_case_fingerprint", "potential_impacts",
            "allowed_changes", "business_contract", "source_files", "version_evidence",
        }
        comparison_keys = {"decision_objective", "comparison_binding"}
        comparison_present = bool(comparison_keys & set(record))
        allowed_comparison_context = comparison_keys if comparison_present else set()
        if (
            not required <= set(context) or set(context) - required - {"task_context", "migration_versions", "request_capacity", "candidate",
                *({"static_diagnostics"} if static_enabled else set()),
                *({"source_state", "source_inventory", "source_selection", "diagnostic_state", "localization"}
                  if record.get("output_format") in DIAGNOSTIC_CONTEXT_FORMATS else set()),
                *({"source_state", "source_inventory", "source_selection"} if single_initial else set()),
                *({"contract_requirements"} if record.get("output_format") in CONTRACT_REQUIREMENT_FORMATS else set()),
                *allowed_comparison_context,
                *({"runtime_capacity"} if record.get("runtime_capacity") is not None else set()),
                *({"project_context", "context_plan"} if record.get("project_context_policy") is not None else set())}
            or context["case_id"] != case.manifest.case_id
            or context["case_fingerprint"] != case.fingerprint
            or context["analysis_case_fingerprint"] != case.fingerprint
            or context["allowed_changes"] != case.manifest.allowed_changes
        ):
            raise ProposalInputError("Prepared payload does not match its case")
        if comparison_present:
            if not comparison_keys <= set(record):
                raise ProposalInputError("Prepared comparison receipt is incomplete")
            objective, public_binding = validate_comparison_context(
                record["decision_objective"], record["comparison_binding"]
            )
            from .protocol_v6 import decision_objective_sha256

            if (
                context.get("decision_objective") != objective
                or context.get("comparison_binding") != public_binding
                or record.get("decision_objective_sha256")
                != decision_objective_sha256(objective)
                or record.get("pair_input_sha256") != public_binding["pair_input_sha256"]
            ):
                raise ProposalInputError("Prepared comparison identity differs from its receipt")
        elif (
            comparison_keys & set(context)
            or "decision_objective_sha256" in record
            or "pair_input_sha256" in record
        ):
            raise ProposalInputError("Prepared comparison identity is incomplete")
        capacity_keys = {"max_source_bytes", "max_request_bytes"}
        if record.get("runtime_capacity") is not None and context.get("runtime_capacity") != {
            "policy": record["runtime_capacity"], **envelope
        }:
            raise ProposalInputError("Prepared runtime capacity differs from its receipt")
        if capacity_keys & set(record) or "request_capacity" in context:
            if (
                not capacity_keys <= set(record)
                or context.get("request_capacity") != {key: record[key] for key in capacity_keys}
            ):
                raise ProposalInputError("Prepared capacity differs from its receipt")
        if arm in {"no_ast", "generic"} and context["potential_impacts"]:
            raise ProposalInputError("Prepared payload leaks disabled AST findings")
        if arm in {"no_evidence", "generic"} and context["version_evidence"]:
            raise ProposalInputError("Prepared payload leaks disabled version evidence")
        if arm == "no_evidence" and any("evidence_keys" in item for item in context["potential_impacts"]):
            raise ProposalInputError("Prepared payload leaks disabled evidence references")
        source_blocks = [
            {"path": name[7:], "sha256": digest, "text": _text(read_verified_file(case.root, name, digest), name)}
            for name in solver_source_names(case)
            for digest in [case.manifest.file_hashes[name]]
        ]
        snapshot = _candidate_snapshot(record, case)
        if snapshot is not None:
            source_blocks = snapshot.blocks()
            expected_candidate = {"revision": snapshot.revision, "parent": snapshot.parent,
                                  "origin": snapshot.origin, "patch_sha256": snapshot.sha256}
            if context.get("candidate") != expected_candidate:
                raise ProposalInputError("Prepared candidate metadata changed")
            if snapshot.reference is not None and context["potential_impacts"] and "context_derivation" not in record:
                raise ProposalInputError("Original AST findings cannot describe the current revision")
        elif "candidate" in context:
            raise ProposalInputError("Unexpected candidate metadata in legacy request")
        if not single_initial and record.get("output_format") not in DIAGNOSTIC_CONTEXT_FORMATS and context["source_files"] != source_blocks:
            raise ProposalInputError("Prepared source is not the registered public snapshot")
        if sum(len(block["text"].encode("utf-8")) for block in source_blocks) > record.get("max_source_bytes", MAX_SOURCE_BYTES):
            raise ProposalInputError("Prepared source exceeds its explicit byte limit")
        name = record["contract_path"]
        digest = case.manifest.file_hashes[name]
        expected_contract = {
            "path": name, "sha256": digest,
            "text": _text(read_verified_file(case.root, name, digest), name),
        }
        if context["business_contract"] != expected_contract:
            raise ProposalInputError("Prepared business contract differs from registered bytes")
        runtime = None
        if record.get("output_format") in DIAGNOSTIC_CONTEXT_FORMATS:
            from ..diagnostics import context_from_reference

            runtime = context_from_reference(record["diagnostic_context_reference"], case, snapshot)
            if runtime.get("workflow_profile", "workbench") != workflow_profile:
                raise ProposalInputError("Prepared workflow differs from frozen diagnostic context")
            if record.get("investigator_context_policy") != runtime.get("investigator_context_policy"):
                raise ProposalInputError("Investigator context policy differs from frozen runtime")
            if record.get("delivery_review_policy") != runtime.get("delivery_review_policy"):
                raise ProposalInputError("Delivery review policy differs from frozen runtime")
            if record.get("semantic_risk_policy") != runtime.get("semantic_risk_policy"):
                raise ProposalInputError("Semantic risk policy differs from frozen runtime")
            if runtime.get("investigator_context_policy") is not None:
                from .investigator_context import project, task_view

                if record.get("context_role") != runtime.get("context_role"):
                    raise ProposalInputError("Investigator context role differs from frozen runtime")
                runtime = project(runtime, record["output_format"])
                if task_view(context.get("task_context"), runtime) != context.get("task_context"):
                    raise ProposalInputError("Prepared task context leaks withheld author feedback")
            if runtime.get("delivery_review_policy") is not None:
                from .delivery_review import independent_view

                runtime = independent_view(runtime)
        failure_query_policy = record.get("failure_query_policy")
        if failure_query_policy is not None:
            from ..retrieval import PUBLIC_FAILURE_QUERY_POLICY

            if (failure_query_policy != PUBLIC_FAILURE_QUERY_POLICY or runtime is None
                    or record.get("output_format") not in PROTOCOL_V6_FORMATS):
                raise ProposalInputError("Unknown public failure retrieval policy")
        if "context_derivation" in record:
            from ..candidates import load_candidate

            derived = derive_context_inputs(
                case, snapshot or load_candidate(case),
                max_evidence_bytes=MAX_EVIDENCE_BYTES - len(expected_contract["text"].encode("utf-8")),
                semantic_config=record.get("semantic_config"),
                include_static=static_enabled,
                diagnostic_runtime=runtime if failure_query_policy is not None else None,
            )
            binding = record["context_derivation"]
            path = Path(record["report_path"]).with_name("context-analysis.json")
            assert_no_links(path)
            if (binding != {"schema_version": 1, "path": str(path), "sha256": _digest(_json_bytes(derived))}
                or _digest(path.read_bytes()) != binding["sha256"]):
                raise ProposalInputError("Derived source/evidence context changed; prepare a new request")
            if static_enabled and context.get("static_diagnostics") != visible(derived["static_diagnostics"]):
                raise ProposalInputError("Prepared static diagnostics differ from current-source analysis")
            if derived["retrieval"] is not None:
                blocks = [
                    _located_evidence(item, read_verified_file(case.root, item["path"], item["sha256"]), item["path"])
                    for item in derived["retrieval"]["entries"]
                ]
                expected_evidence = blocks if arm not in {"no_evidence", "generic"} else []
                if context["version_evidence"] != expected_evidence:
                    raise ProposalInputError("Prepared retrieval differs from its recomputed selection")
            else:
                blocks = context["version_evidence"]
            # 旧无 bundle 夹具仍支持 no_evidence；真实任务始终可以重算完整证据。
            if derived["retrieval"] is not None or arm not in {"no_evidence", "generic"}:
                impacts, _ = _located_findings(derived["analysis"], source_blocks, blocks)
                if arm in {"no_ast", "generic"}:
                    impacts = []
                elif arm == "no_evidence":
                    impacts = [{key: value for key, value in item.items() if key != "evidence_keys"}
                               for item in impacts]
                if context["potential_impacts"] != impacts:
                    raise ProposalInputError("Prepared impacts differ from current-source analysis")
        elif static_enabled:
            raise ProposalInputError("Static diagnostics require recomputable source binding")
        if single_initial:
            from .request import initial_source_context

            selected = initial_source_context(case, snapshot, record["source_policy"])
            if any(context.get(key) != value for key, value in selected.items()):
                raise ProposalInputError("Prepared single patch initial source selection changed")
        elif record.get("output_format") in DIAGNOSTIC_CONTEXT_FORMATS:
            from .source_context import build_context

            if record.get("project_context_policy") != runtime.get("project_context_policy"):
                raise ProposalInputError("Project context policy differs from frozen runtime")
            if record.get("source_read_retention") != runtime.get("source_read_retention"):
                raise ProposalInputError("Source read retention differs from frozen runtime")
            if record.get("investigation_policy") != runtime.get("investigation_policy"):
                raise ProposalInputError("Investigation policy differs from frozen runtime")
            expected_limits = (runtime["finish_requirements"]["format_limits"]
                               if runtime.get("investigation_policy") else None)
            if record.get("finish_limits") != expected_limits:
                raise ProposalInputError("Finish limits differ from frozen runtime")
            if record.get("incomplete_response_scope") != runtime.get("incomplete_response_scope"):
                raise ProposalInputError("Incomplete response scope changed")
            if record.get("preparation_binding") != runtime.get("preparation_binding"):
                raise ProposalInputError("Preparation identity differs from frozen runtime")
            if record.get("knowledge_review_binding") != runtime.get("knowledge_review_binding"):
                raise ProposalInputError("Knowledge review identity differs from frozen runtime")
            if runtime.get("preparation_binding") is not None:
                from .preparation import validate_binding

                expected_role, expected_format = validate_binding(runtime["preparation_binding"])
                if record.get("role") != expected_role or record["output_format"] != expected_format:
                    raise ProposalInputError("Prepared role or output differs from preparation phase")
            elif record["output_format"] == "repository_context_actions":
                raise ProposalInputError("Missing repository preparation binding")
            if runtime.get("project_context_policy") is not None:
                from .project_context import assemble

                selected = assemble(case, snapshot, context, runtime, record["source_policy"],
                    model=record["model"], system=_instructions(record["output_format"], workflow_profile, record.get("investigator_context_policy"), record.get("delivery_review_policy"), record.get("semantic_risk_policy")),
                    output_tokens=record["max_output_tokens"], thinking_mode=record.get("thinking_mode"),
                    request_limit=record["max_request_bytes"])
            else:
                retain_reads = runtime.get("source_read_retention") == "current_revision"
                selected = build_context(case, snapshot, record["source_policy"],
                    observations=runtime.get("retained_source_reads", []) + runtime["observations"] if retain_reads else runtime["observations"],
                    findings=context["potential_impacts"] + runtime.get("diagnostic_workset", {}).get("source_locations", []),
                    assistance=runtime.get("navigation_assistance", "baseline"), retain_reads_first=retain_reads)
            if "localization" in context and "localization" not in selected:
                raise ProposalInputError("Unregistered navigation assistance")
            from .project_context import diagnostic_projection

            expected_diagnostic = (selected["diagnostic_state"]
                                   if (record.get("project_context_policy") or {}).get("version") == "project-context-v10"
                                   else diagnostic_projection(runtime))
            if expected_diagnostic != context.get("diagnostic_state") or any(context.get(k) != v for k, v in selected.items()):
                raise ProposalInputError("Prepared source selection or diagnostic observation changed")
        if record.get("output_format") in CONTRACT_REQUIREMENT_FORMATS:
            from ..cases.requirements import requirements_for_case

            requirements = requirements_for_case(case)
            if (
                context.get("contract_requirements") != requirements.public()
                or record["input_hashes"].get(requirements.path) != requirements.sha256
            ):
                raise ProposalInputError("Prepared contract requirements differ from the registered catalog")
        for block in context["version_evidence"]:
            name = block["path"]
            if not name.startswith("evidence/") or record["input_hashes"].get(name) != block["sha256"]:
                raise ProposalInputError("Prepared evidence is outside the bound public inputs")
            actual = _located_evidence(block, read_verified_file(case.root, name, block["sha256"]), name)
            if actual != block:
                raise ProposalInputError("Prepared evidence differs from its registered excerpt")
        if "migration_versions" in context:
            name = "evidence/bundle.json"
            bundle = _decode_json(read_verified_file(case.root, name, case.manifest.file_hashes[name]))
            if context["migration_versions"] != {key: bundle[key] for key in ("package", "old_version", "new_version")}:
                raise ProposalInputError("Prepared migration versions differ from the registered bundle")
        task = validate_task_context(case, context.get("task_context"), arm, snapshot=snapshot)
        for key, value in (("task_context_sha256", task), ("public_context_sha256", context)):
            if key in record and record[key] != _digest(_json_bytes(value)):
                raise ProposalInputError("Prepared context does not match its receipt identity")
    except (KeyError, IndexError, TypeError, _ProviderBoundaryError) as error:
        raise ProposalInputError("Prepared payload is malformed") from error


def _verify_inputs(record: dict) -> None:
    current = load_case(Path(record["manifest_path"]))
    if current.fingerprint != record["case_fingerprint"]:
        raise ProposalInputError("Case no longer matches the prepared request")
    if record.get("source_kind") != current.manifest.source.kind:
        raise ProposalInputError("Source kind differs from the registered case")
    for name, digest in record["input_hashes"].items():
        if current.manifest.file_hashes.get(name) != digest:
            raise ProposalInputError("Public input no longer belongs to this case")
        read_verified_file(current.root, name, digest)
    _candidate_snapshot(record, current)


def _candidate_snapshot(record: dict, case):
    single_initial = record.get("output_format") == "single_patch_initial"
    if single_initial and record.get("candidate_reference") is not None:
        raise ProposalInputError("Single patch initial input requires original source")
    if not single_initial and record.get("output_format") not in CURRENT_CANDIDATE_FORMATS:
        if "candidate_reference" in record or "candidate_revision" in record:
            raise ProposalInputError("Candidate identity is not a legacy request field")
        return None
    from ..candidates import load_candidate

    try:
        snapshot = load_candidate(case, record["candidate_reference"])
        if snapshot.revision != record["candidate_revision"]:
            raise ProposalInputError("Prepared candidate revision changed")
        return snapshot
    except (KeyError, OSError, ValueError) as error:
        raise ProposalInputError("Prepared candidate is no longer valid") from error


def complete_request(prepared: dict, *, api_key: str, transport: Transport | None = None) -> dict:
    """对已冻结请求尝试一次生成；成功也只返回 pending_review，不宣称修复成立。"""
    if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
        raise ProposalInputError("A provider key is required and is never recorded")
    if any(ord(character) < 32 for character in api_key):
        raise ProposalInputError("Provider key contains invalid control characters")
    record, body, report_path = _load_prepared(prepared)
    # 密钥仅在这里可见；公开反馈即使意外包含密钥，也不能再发给远端。
    _, secret_in_request = _redact_tree(_decode_json(body), api_key)
    if secret_in_request:
        # 准备阶段不接触密钥，因此只能在发送前识别；发现后原请求不再留盘。
        omitted = _json_bytes({"content_omitted": True, "reason": "provider_secret_in_public_request"})
        Path(record["request_path"]).write_bytes(omitted)
        record.update(
            status="rejected_input", reason="provider_secret_in_public_request",
            request_storage="metadata_only", stored_request_sha256=_digest(omitted),
        )
        _save(record, report_path)
        raise ProposalInputError("Prepared public request contains the provider secret")
    if not record["live_call_eligible"] and transport is None:
        raise ProposalInputError("Synthetic calibration requests only support injected offline tests")
    directory = report_path.parent
    attempt_path = directory / "attempt.json"
    assert_no_links(attempt_path)
    try:
        # 独占创建后绝不自动重试；进程中断时未知结果仍算一次调用尝试。
        with attempt_path.open("x", encoding="utf-8") as handle:
            json.dump({"request_sha256": record["request_sha256"], "calls": 1}, handle)
    except FileExistsError as error:
        raise ProposalInputError("This request already has an attempt; automatic retry is forbidden") from error
    report = {
        **record,
        "status": "calling_provider",
        "calls": 1,
        "model_usage": {"availability": "unavailable", "input_tokens": None, "output_tokens": None},
        "candidate_origin": "agent_candidate",
        "approved": False,
        "target_code_executed": False,
        "verification_status": "not_run",
        "automatic_retries": 0,
        "provider_transport": "injected" if transport is not None else "https_once",
    }
    _save(report, report_path)
    started = time.monotonic()
    try:
        request = Request(
            record["endpoint"],
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            raw = _bounded_transport(transport or _send_once, request, record["timeout_seconds"],
                                     record["max_response_bytes"])
        except Exception as error:
            # 只在传输调用内分类；后续文件写入失败不能冒充网络故障。
            report["transport_diagnostic"] = transport_diagnostic(error)
            raise
        if not isinstance(raw, bytes) or len(raw) > record["max_response_bytes"]:
            raise _ProviderBoundaryError("response_too_large_or_invalid")
        if time.monotonic() - started > record["timeout_seconds"]:
            raise TimeoutError("provider deadline exceeded")
        # 必须在记录 returned_model、summary 或候选之前检查解码后的所有键和值。
        sanitized, response, secret_echo, response_error = _sanitized_response(raw, api_key)
        response_path = directory / "response.sanitized.json"
        assert_no_links(response_path)
        response_path.write_bytes(sanitized)
        report["response_path"] = str(response_path)
        report["response_sha256"] = _digest(sanitized)
        report["provider_response_sha256"] = _digest(raw)
        report["response_sha256_kind"] = "sanitized_stored_bytes"
        report["response_storage"] = (
            "metadata_only" if response_error or secret_echo else "sanitized_provider_json"
        )
        report["response_redacted"] = secret_echo
        if secret_echo:
            raise _ProviderBoundaryError("provider_secret_echo")
        if response_error:
            raise _ProviderBoundaryError(response_error)
        if not isinstance(response, dict):
            raise _ProviderBoundaryError("invalid_provider_schema")
        for source, target in (("model", "returned_model"), ("id", "provider_response_id")):
            value = response.get(source)
            if (
                isinstance(value, str) and 0 < len(value) <= 512
                and not any(ord(character) < 32 for character in value)
            ):
                report[target] = value
        report["model_usage"] = _usage(response)
        if (
            report["model_usage"]["output_tokens"] is not None
            and report["model_usage"]["output_tokens"] > record["max_output_tokens"]
        ):
            raise _ProviderBoundaryError("provider_exceeded_output_budget")
        output_format = record.get("output_format", "unified_diff")
        report["response_completion"] = completion_metadata(response, report["model_usage"])
        report["response_bytes"] = len(raw)
        report["response_elapsed_seconds"] = round(time.monotonic() - started, 6)
        proposal = _proposal(response, output_format)
        _verify_inputs(record)
        assert_no_links(Path(record["request_path"]))
        if _digest(Path(record["request_path"]).read_bytes()) != record["request_sha256"]:
            raise ProposalInputError("Request changed during provider execution")
        patch_path = directory / "candidate.patch"
        assert_no_links(patch_path)
        case = load_case(Path(record["manifest_path"]))
        if output_format == "contract_audit":
            from .contract_auditor import validate

            report.update(
                status="audit_ready",
                summary=proposal["summary"],
                audit=validate(proposal["audit"]),
                reason="Read-only advisory result; no candidate, probe, observation, or terminal state changed.",
            )
            return report
        if output_format in STRUCTURED_ACTION_FORMATS:
            snapshot = _candidate_snapshot(record, case)
            if output_format in PROTOCOL_V6_FORMATS:
                from .protocol_v6 import validate

                role = "investigator" if output_format == "investigator_actions" else "solver"
                from .investigator_context import require_action

                require_action(proposal["action"], record.get("investigator_context_policy"), role)
                action = validate(case, proposal["action"], snapshot, role=role,
                                  workflow_profile=record.get("workflow_profile", "workbench"),
                                  project_context_policy=record.get("project_context_policy"),
                                  knowledge_review_binding=record.get("knowledge_review_binding"),
                                  finish_limits=record.get("finish_limits"))
            elif output_format in {"diagnostic_actions", "contract_actions", "repository_context_actions"}:
                if output_format in {"diagnostic_actions", "repository_context_actions"}:
                    from .protocol_v4 import validate
                else:
                    from .protocol_v5 import validate

                options = {}
                if output_format == "contract_actions":
                    options["finish_limits"] = record.get("finish_limits")
                if output_format in {"diagnostic_actions", "repository_context_actions"}:
                    options["project_context_policy"] = record.get("project_context_policy")
                    if record.get("preparation_binding") is not None:
                        from .preparation import action_options

                        options.update(action_options(record["preparation_binding"]))
                action = validate(case, proposal["action"], snapshot, **options)
            else:
                action = validate_action(case, proposal["action"], candidate=snapshot)
            report["action"] = action
            report["summary"] = proposal["summary"]
            if output_format == "investigator_actions" and action["type"] == "handoff":
                report.update(
                    status="investigation_ready",
                    reason="Validated read-only Investigator handoff; no candidate or terminal state changed.",
                )
                return report
            if action["type"] != "submit_candidate":
                report.update(
                    status="agent_finished" if action["type"] == "finish" else "action_ready",
                    reason="Validated agent action; no target code or read-only tool was executed.",
                )
                return report
            from .project_context import requires_knowledge

            if requires_knowledge(record.get("project_context_policy")):
                from ..diagnostics import context_from_reference

                runtime = context_from_reference(record["diagnostic_context_reference"], case, snapshot)
                if runtime.get("project_knowledge") is None:
                    raise AgentActionError("Record project knowledge before the first edit", code="action_invalid_fields")
            proposal["edits"] = action["edits"]
            if snapshot is not None:
                from ..candidates import apply_increment

                updated = apply_increment(case, snapshot, action, directory / "revision")
                report.update(
                    status="pending_review", candidate_reference=updated.reference,
                    base_revision=snapshot.revision, candidate_revision=updated.revision,
                    candidate_patch=str(directory / "revision" / "candidate.patch"),
                    candidate_sha256=updated.sha256,
                    changed_files=sorted(name for name in updated.files if updated.files[name] != updated.original[name]),
                    increment_files=sorted({edit["path"] for edit in action["edits"]}),
                    patch_compilation="incremental_exact_edits_with_cumulative_diff",
                    edit_count=len(action["edits"]),
                    reason="Current revision updated atomically; safety and behavior await review.",
                )
                return report
        if output_format in {"exact_edits", "single_patch_initial", "agent_actions"}:
            patch_bytes = edits_to_patch(case, proposal["edits"])
            report["patch_compilation"] = "difflib_from_exact_original_edits"
            report["edit_count"] = len(proposal["edits"])
        else:
            patch_bytes = proposal["patch"].encode("utf-8")
            report["patch_compilation"] = "provider_unified_diff"
        patch_path.write_bytes(patch_bytes)
        report["candidate_patch"] = str(patch_path)
        report["candidate_sha256"] = _digest(patch_path.read_bytes())
        staged = stage_source(case, directory / "candidate", patch_path)
        report.update(
            status="pending_review",
            summary=proposal["summary"],
            changed_files=staged["changed_files"],
            candidate_source_dir=staged["source_dir"],
            reason="Patch applied within the allowlist; behavior and safety are not approved.",
        )
    except (TimeoutError, socket.timeout):
        report.update(status="outcome_unknown", reason="provider_timeout_no_retry")
    except HTTPError as error:
        code = error.code if type(error.code) is int and 100 <= error.code <= 599 else "unknown"
        report.update(status="provider_error", reason=f"http_status_{code}")
    except URLError:
        report.update(status="outcome_unknown", reason="provider_transport_error_no_retry")
    except _ProviderBoundaryError as error:
        report.update(status="provider_response_rejected", reason=error.code)
        if (
            error.feedback is not None
            and error.code in PROTOCOL_ERROR_CODES
            and record.get("output_format") in EDIT_FEEDBACK_FORMATS
        ):
            report["edit_feedback"] = error.feedback[:1000]
    except AgentActionError as error:
        if record.get("output_format") == "contract_audit":
            from .contract_auditor import ContractAuditError

            reason = (
                error.code
                if isinstance(error, ContractAuditError)
                else "contract_audit_invalid_schema"
            )
        else:
            reason = error.code
        report.update(status="provider_response_rejected", reason=reason)
        if (
            reason in PROTOCOL_ERROR_CODES
            and record.get("output_format") in EDIT_FEEDBACK_FORMATS
        ):
            report["edit_feedback"] = str(error)[:1000]
    except PatchInfrastructureError:
        report.update(status="execution_error", reason="candidate_materialization_infrastructure_failure")
    except PatchValidationError as error:
        reason = getattr(error, "code", "patch_outside_contract_or_not_applicable")
        report.update(status="candidate_rejected", reason=reason)
        if (
            reason in PROTOCOL_ERROR_CODES
            and record.get("output_format") in EDIT_FEEDBACK_FORMATS
        ):
            report["edit_feedback"] = str(error)[:1000]
    except (CaseValidationError, ProposalInputError):
        report.update(status="rejected_input", reason="frozen_input_changed")
    except KeyboardInterrupt:
        report.update(status="outcome_unknown", reason="interrupted_no_retry")
    except Exception:
        report.update(status="outcome_unknown", reason="unexpected_provider_or_storage_error_no_retry")
    finally:
        report["duration_seconds"] = round(time.monotonic() - started, 6)
        _save(report, report_path)
    return report


def generate_proposal(case, analysis, evidence, *, api_key: str, transport=None, **kwargs) -> dict:
    """便捷入口；需要先审查请求时，分开使用 prepare_request 和 complete_request。"""
    prepared = prepare_request(case, analysis, evidence, **kwargs)
    return complete_request(prepared, api_key=api_key, transport=transport)
