"""日志回读不新增运行或业务证据；摘要与下一请求消费分别核验。"""

import copy
import json
from pathlib import Path

import pytest
from test_diagnostic_operations import CASE, public_run, request_public, review
from test_diagnostic_operations import task as task

from upgrade_workbench.candidates import load_candidate
from upgrade_workbench.cases import load_case
from upgrade_workbench.diagnostic_output import output_page, public_text
from upgrade_workbench.diagnostics import (
    accept_action,
    context_from_reference,
    freeze_context,
    read,
    store,
)
from upgrade_workbench.generation.provider import _verify_payload
from upgrade_workbench.planning import prepare_case_proposal

OUTPUT = ('business result: {"value": "雪\\\\abc"}\n' * 600
          + 'C:\\private\\cross-page-path\nUPGRADE_WORKBENCH_RESULT:{"internal":true}\n'
          + '================ warnings summary ================\n'
          + 'Deprecated: compatibility warning\n' * 500
          + '================ 1 passed, 500 warnings ================\n')


def with_output(task):
    def comparator(manifest, directory, **kwargs):
        report = public_run(manifest, directory, **kwargs)
        for name in ("old_original", "new_original"):
            path = directory / (name + "-stdout.log")
            path.write_text(OUTPUT, encoding="utf-8")
            report["stages"][name] = report["stages"][name] | {"stdout_path": str(path)}
        Path(report["report_path"]).write_text(json.dumps(report), encoding="utf-8")
        return report

    return review(request_public(task), comparator=comparator)


def selection(task):
    ref = task["observations"][-1]
    value = read(ref, Path(task["task_path"]).parent)
    cursor = value["result"]["stages"]["new_original"]["output_streams"][0]["cursor"]
    return ref, cursor


def test_business_summary_survives_warning_flood_and_reports_clipping(task):
    task = with_output(task)
    ref, _ = selection(task)
    row = read(ref, Path(task["task_path"]).parent)["result"]["stages"]["new_original"]
    assert 'business result' in row["output_excerpt"]
    assert 'Deprecated' not in row["output_excerpt"]
    assert row["output_excerpt_truncated"] is True
    assert 'Deprecated' in row["warning_summary"]["text"]
    assert '<host-path>' in row["output_excerpt"]


def test_paged_output_reassembles_cleaned_log_and_never_executes(task):
    task = with_output(task)
    ref, cursor = selection(task)
    revision = load_candidate(load_case(CASE), task["current_candidate"]).revision
    before = copy.deepcopy(task)
    pieces = []
    offset = 0
    while cursor:
        page = output_page(task, ref, cursor, current_revision=revision)
        assert page["offset"] == offset and not page["stale"]
        assert len(json.dumps(page, ensure_ascii=False).encode()) <= 20000
        pieces.append(page["text"])
        offset += len(page["text"])
        cursor = page["next_cursor"]
    assert page["eof"] and len(pieces) > 1
    assert "".join(pieces) == public_text(OUTPUT)
    assert "UPGRADE_WORKBENCH_RESULT" not in "".join(pieces)
    assert task == before


@pytest.mark.parametrize("bad", ["../stdout.log", "log-v1:new_original:stdout:" + "f" * 64 + ":0",
                                "offset", "negative"])
def test_forged_cursor_rejected(task, bad):
    task = with_output(task)
    ref, cursor = selection(task)
    if bad == "offset":
        bad = cursor[:-1] + "9999999999"
    if bad == "negative":
        bad = cursor[:-1] + "-1"
    with pytest.raises(ValueError):
        output_page(task, ref, bad, current_revision="a" * 64)


def test_page_is_consumed_by_real_request_and_summary_read_resets_it(task):
    task = with_output(task)
    ref, cursor = selection(task)
    case = load_case(CASE)
    snapshot = load_candidate(case)
    before_runs = list(task["diagnostic_runs"])
    accept_action(task, {"action": {"type": "get_observation", "observation_id": ref["id"], "cursor": cursor}}, case)
    frozen = freeze_context(task)
    prepared = prepare_case_proposal(CASE, Path(task["work_root"]), public_source_ack=True,
        output_format="diagnostic_actions", diagnostic_context_reference=frozen,
        source_policy=task["protocol"]["source_policy"], **task["protocol"]["generation"])
    body = Path(prepared["request_path"]).read_bytes()
    _verify_payload(prepared, body)
    context = json.loads(json.loads(body)["messages"][1]["content"])
    page = context["diagnostic_state"]["observation_output"]
    assert page["observation_id"] == ref["id"] and "business result" in page["text"]
    assert task["diagnostic_runs"] == before_runs
    accept_action(task, {"action": {"type": "get_observation", "observation_id": ref["id"]}}, case)
    assert "observation_output" not in context_from_reference(freeze_context(task), case, snapshot)


def test_log_drift_and_hidden_observations_are_rejected(task):
    task = with_output(task)
    ref, cursor = selection(task)
    hidden = task | {"hidden_observation_ids": [ref["id"]]}
    with pytest.raises(ValueError, match="not visible"):
        output_page(hidden, ref, cursor, current_revision="a" * 64)
    value = read(ref, Path(task["task_path"]).parent)
    receipt = read(value["execution_reference"], Path(task["task_path"]).parent)
    Path(receipt["dependencies"][0]["path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence changed"):
        output_page(task, ref, cursor, current_revision="a" * 64)


def test_legacy_summary_still_readable_without_fabricated_cursors(task):
    task = review(request_public(task))
    ref = task["observations"][-1]
    root = Path(task["task_path"]).parent
    value = read(ref, root)
    receipt = read(value["execution_reference"], root)
    for row in receipt["public"]["stages"].values():
        row.pop("output_streams", None)
        row.pop("output_excerpt_truncated", None)
    value["result"] = receipt["public"]
    value["execution_reference"] = store(root / "legacy-execution", receipt)
    ref = store(root / "observations", value)
    task["observations"] = [ref]
    case = load_case(CASE)
    assert accept_action(task, {"action": {"type": "get_observation", "observation_id": ref["id"]}}, case)
    with pytest.raises(ValueError, match="not registered"):
        output_page(task, ref, "log-v1:new_original:stdout:" + "a" * 64 + ":0", current_revision="a" * 64)


def test_output_cannot_escape_task_even_with_matching_log_hash(task):
    task = with_output(task)
    ref, cursor = selection(task)
    root = Path(task["task_path"]).parent
    value = read(ref, root)
    receipt = read(value["execution_reference"], root)
    dependency = next(row for row in receipt["dependencies"] if row["stage"] == "new_original")
    outside = Path(task["work_root"]) / "other-task.log"
    outside.write_bytes(Path(dependency["path"]).read_bytes())
    dependency["path"] = str(outside)
    value["execution_reference"] = store(root / "altered-execution", receipt)
    ref = store(root / "observations", value)
    task["observations"] = [ref]
    with pytest.raises(ValueError, match="outside this task"):
        output_page(task, ref, cursor, current_revision="a" * 64)


def test_five_new_pages_reach_model_without_false_no_progress_stop(task):
    from test_diagnostic_operations import coverage, response

    from upgrade_workbench.tasks import advance_task

    task = with_output(task)
    ref, cursor = selection(task)
    calls = []

    def transport(request, **_kwargs):
        nonlocal cursor
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        if calls:
            page = context["diagnostic_state"]["observation_output"]
            assert page["offset"] == calls[-1]["offset"]
            cursor = page["next_cursor"]
        if len(calls) == 5:
            return response({"type": "finish", "reason": "no_change_claimed", "explanation": "Offline test",
                             "evidence_refs": ["business_contract"], "contract_coverage": coverage()})
        assert cursor is not None
        calls.append(output_page(task, ref, cursor, current_revision=ref["id"]))
        return response({"type": "get_observation", "observation_id": ref["id"], "cursor": cursor})

    result = advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert result["status"] == "no_change_claimed", result.get("stop_reason")
    assert len(calls) == 5 and len(result["diagnostic_runs"]) == len(task["diagnostic_runs"])
    assert result["observations"] == task["observations"]
    assert result["diagnostic_progress"]["consecutive_revisits"] == 0


def test_repeating_same_page_still_hits_no_progress_guard(task):
    from upgrade_workbench.diagnostics import remember_action

    task = with_output(task)
    ref, cursor = selection(task)
    case = load_case(CASE)
    revision = load_candidate(case).revision
    response = {"action": {"type": "get_observation", "observation_id": ref["id"], "cursor": cursor}}
    for _ in range(5):
        accept_action(task, response, case)
        remember_action(task, response, case, before_revision=revision)
    assert task["status"] == "unresolved"
    assert task["stop_reason"] == "repeated_observation_cycle_no_progress"
