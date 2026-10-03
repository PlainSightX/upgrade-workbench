"""容量和完整截断用离线transport验证，不发送模型请求。"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from upgrade_workbench import tasks
from upgrade_workbench.cases import load_case
from upgrade_workbench.generation.capacity import from_record, validate
from upgrade_workbench.generation.incomplete import observation_identity
from upgrade_workbench.generation.provider import completion_metadata
from upgrade_workbench.service.recovery import recover_task

from .test_project_context import CASE, OWNER, response
from .test_repository_preparation import CAPACITY, advance, context
from .test_repository_preparation import prepared_task as prepared_task
from .test_repository_preparation import task as task


def length_response(padding=0):
    return json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 131072,
        "completion_tokens_details": {"reasoning_tokens": 131072}},
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "padding": "x" * padding}).encode()


@pytest.mark.parametrize("tokens,timeout,policy", [
    (65537, 300, None), (65536, 301, None), (True, 10, None),
    (131073, 1200, CAPACITY), (131072, 1201, CAPACITY),
    (131072, 1200, {"version": 1, "max_response_bytes": 1}),
    (131072, 1200, CAPACITY | {"max_response_bytes": 4194305}),
])
def test_capacity_invalid(tokens, timeout, policy):
    with pytest.raises(ValueError):
        validate(tokens, timeout, policy)


def test_legacy_and_extended_limits_are_not_reinterpreted():
    old = validate(65536, 300)
    new = validate(131072, 1200, CAPACITY)
    assert old["max_response_bytes"] == 384000
    assert new["max_response_bytes"] == 4194304
    assert from_record(old) == old
    assert from_record(new | {"runtime_capacity": CAPACITY}) == new
    with pytest.raises(ValueError):
        from_record(old | {"max_response_bytes": 4194304})


@pytest.mark.parametrize("change", ["refusal", "tools", "missing_usage", "invalid_message", "stop", "filter", "other"])
def test_only_accountable_length_classified(change):
    raw = json.loads(length_response())
    usage = {"availability": "reported"}
    choice = raw["choices"][0]
    if change == "refusal":
        choice["message"]["refusal"] = "no"
    elif change == "tools":
        choice["message"]["tool_calls"] = [{}]
    elif change == "missing_usage":
        usage = {"availability": "unavailable"}
    elif change == "invalid_message":
        choice["message"] = None
    else:
        choice["finish_reason"] = {"stop": "stop", "filter": "content_filter", "other": []}[change]
    assert completion_metadata(raw, usage)["known_length"] is False


def test_repeated_length_stops_and_is_idempotent(prepared_task):
    seen = []
    def transport(request, **kwargs):
        seen.append(context(request))
        return length_response()
    result = advance(prepared_task, transport)
    assert result["status"] == "unresolved", result.get("stop_reason")
    assert result["stop_reason"] == "repeated_incomplete_response_no_progress"
    assert len(seen) == 2 and result["candidate"] is None
    assert result["budget"]["calls"] == 2
    scopes = result["incomplete_response_scopes"]
    assert len(scopes) == 1 and len(next(iter(scopes.values()))["receipts"]) == 2
    report = json.loads(Path(result["attempts"][-1]["receipt"]).read_text(encoding="utf-8"))
    before = copy.deepcopy(scopes)
    assert tasks.accept_response(result, report, load_case(CASE)) is False
    assert result["incomplete_response_scopes"] == before
    assert report["model_usage"]["reasoning_tokens"] == report["model_usage"]["output_tokens"] == 131072
    # 同一请求不能通过替换已绑定回复获得第二次计数。
    path = Path(report["response_path"])
    changed = json.loads(path.read_bytes())
    changed["padding"] = "changed"
    data = json.dumps(changed).encode()
    path.write_bytes(data)
    report.update(response_sha256=hashlib.sha256(data).hexdigest(), response_bytes=len(data))
    with pytest.raises(ValueError, match="counted"):
        tasks.accept_response(result, report, load_case(CASE))


@pytest.mark.parametrize("settled", [False, True])
def test_large_length_recovery_once(prepared_task, monkeypatch, settled):
    class Crash(BaseException):
        pass
    original = tasks.complete_request
    calls = []
    def crash(prepared, **kwargs):
        report = original(prepared, **kwargs)
        if settled:
            from upgrade_workbench.budget import BudgetLedger
            BudgetLedger(Path(prepared_task["budget_path"])).settle(Path(prepared["report_path"]).parent.name, report["model_usage"])
        raise Crash()
    def transport(*args, **kwargs):
        calls.append(1)
        return length_response(2_100_000)
    monkeypatch.setattr(tasks, "complete_request", crash)
    with pytest.raises(Crash):
        advance(prepared_task, transport)
    path = Path(prepared_task["task_path"])
    result = recover_task(path, Path(prepared_task["work_root"]), OWNER)
    assert result["status"] == "ready" and result["budget"]["calls"] == 1 and len(calls) == 1
    assert len(next(iter(result["incomplete_response_scopes"].values()))["receipts"]) == 1
    assert recover_task(path, Path(prepared_task["work_root"]), OWNER) == result
    # 从磁盘续走仍使用原scope，不能借重启清零。
    monkeypatch.setattr(tasks, "complete_request", original)
    final = advance(result, transport)
    assert final["stop_reason"] == "repeated_incomplete_response_no_progress"
    assert len(calls) == 2 and final["budget"]["calls"] == 2


def test_epoch_deduplicates_receipts_but_keeps_changed_behavior():
    row = {"kind": "public_checks", "revision": "r1", "id": "first",
        "result": {"status": "failed", "receipt": "a", "stages": {
            "new_original": {"status": "failed", "tests": {"failed": 1}, "failed_nodeids": ["a"],
                "output_excerpt": "time=1"}}}}
    repeated = copy.deepcopy(row)
    repeated.update(id="second")
    repeated["result"]["receipt"] = "b"
    repeated["result"]["stages"]["new_original"]["output_excerpt"] = "time=2"
    assert observation_identity(row) == observation_identity(repeated)
    repeated["result"]["stages"]["new_original"]["failed_nodeids"] = ["b"]
    assert observation_identity(row) != observation_identity(repeated)
    assert observation_identity(row | {"kind": "source"}) is None
    assert observation_identity(row | {"kind": "project_context"}) is None


def test_source_navigation_does_not_reset_scope(prepared_task):
    protocol = copy.deepcopy(prepared_task["protocol"])
    protocol["repository_preparation"].update(max_calls=6, solver_reserved_calls=2)
    target = tasks.create_task(CASE, Path(prepared_task["work_root"]), protocol, arm="direct_repair",
        phase="operation", budget_path=Path(prepared_task["budget_path"]), kind="operation", service_owner=OWNER)
    calls = []
    def transport(request, **kwargs):
        calls.append(context(request))
        if len(calls) == 2:
            return response({"type": "read_source", "revision": calls[-1]["candidate"]["revision"],
                "path": "copier/main.py", "start_line": 1, "end_line": 10})
        return length_response()
    result = advance(target, transport)
    assert result["stop_reason"] == "repeated_incomplete_response_no_progress"
    assert len(calls) == 3 and len(result["incomplete_response_scopes"]) == 1
