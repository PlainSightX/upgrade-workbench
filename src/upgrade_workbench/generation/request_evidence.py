"""从本次已发送请求恢复可引用证据，不把检索编号本身当作授权。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..candidates import load_candidate
from ..cases import load_case
from ..cases.manifest import assert_no_links
from .provider import _verify_payload
from .request import MAX_REQUEST_CAPACITY
from .roles import recoverable_attempt_contract


class RequestEvidenceError(RuntimeError):
    """收据或输入身份损坏应停止任务，不能当成模型可重试的引用拼写错误。"""


def verified_request_context(task: dict, response: dict) -> dict:
    """校验本任务最新回复的输入基底；生成的候选不是本次请求的输入源码。"""
    try:
        report_path = response["report_path"]
        matches = [
            (index, attempt)
            for index, attempt in enumerate(task["attempts"], start=1)
            if attempt.get("receipt") == report_path
        ]
        if len(matches) != 1:
            raise ValueError("Response receipt does not bind exactly one task attempt")
        attempt_index, attempt = matches[0]
        request_id = attempt["request_id"]
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("Invalid request identity")
        directory = Path(task["work_root"]) / "proposals" / request_id
        expected_report_path = directory / "proposal.json"
        if report_path != str(expected_report_path):
            raise ValueError("Response is not the task's current request")
        assert_no_links(expected_report_path)
        if expected_report_path.stat().st_size > 2_000_000:
            raise ValueError("Response receipt exceeds its bound")
        record = json.loads(expected_report_path.read_bytes())
        if task.get("schema_version") in {3, 4, 5}:
            spec, expected_output = recoverable_attempt_contract(task, attempt)
            expected_role = spec.role
        else:
            expected_output = "diagnostic_actions"
            expected_role = "solver"
        if response.get("role", expected_role) != expected_role:
            raise ValueError("Response role differs from its task attempt")
        if task.get("protocol", {}).get("repository_preparation") is not None:
            from .preparation import verify_response

            verify_response(task, response)
        # 旧回执的角色仅在内存附加；新回执持久化真实角色，不能删掉后再比较。
        persisted_response = dict(response)
        if "role" not in record:
            persisted_response.pop("role", None)
        if record != persisted_response or record["output_format"] != expected_output:
            raise ValueError("Response differs from its persisted diagnostic receipt")
        if (record["manifest_path"] != task["manifest_path"]
                or record["case_fingerprint"] != task["case_fingerprint"]):
            raise ValueError("Response belongs to another case")
        request_path = directory / "request.json"
        marker_path = directory / "attempt.json"
        for path in (request_path, marker_path):
            assert_no_links(path)
        if record["request_path"] != str(request_path) or request_path.stat().st_size > MAX_REQUEST_CAPACITY:
            raise ValueError("Request is outside its bound location or capacity")
        if marker_path.stat().st_size > 1024:
            raise ValueError("Attempt marker exceeds its bound")
        body = request_path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if (digest != record["request_sha256"]
                or json.loads(marker_path.read_bytes()) != {"request_sha256": digest, "calls": 1}):
            raise ValueError("Request differs from its sent attempt")
        context = json.loads(json.loads(body)["messages"][1]["content"])
        identity = context["task_context"]
        if identity["task_id"] != task["task_id"] or identity["attempt_index"] != attempt_index:
            raise ValueError("Request belongs to another task or attempt")
        frozen = record["diagnostic_context_reference"]
        if Path(frozen["path"]).parent != Path(task["task_path"]).parent / "contexts":
            raise ValueError("Diagnostic context belongs to another task directory")
        case = load_case(Path(task["manifest_path"]))
        snapshot = load_candidate(case, task["current_candidate"])
        if context["candidate"]["revision"] != snapshot.revision:
            raise ValueError("Request was prepared against a different source revision")
        # submit_candidate收据会携带输出候选，发送前验证必须仍重建当时的输入。
        input_record = record | {"candidate_reference": snapshot.reference, "candidate_revision": snapshot.revision}
        _verify_payload(input_record, body)
        return context
    except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
        raise RequestEvidenceError(f"Request evidence identity verification failed: {error}") from error


def visible_version_refs(task: dict, response: dict) -> set[str]:
    return {"version:" + block["evidence_key"]
            for block in verified_request_context(task, response)["version_evidence"]}
