"""冻结可见性必须影响真实请求，默认任务仍完整辅助，恢复时禁止漂移。"""

import json
from pathlib import Path

import pytest

from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.tasks import advance_task, create_operation, inspect_task

CASE = Path(__file__).resolve().parents[2] / "cases/bump-my-version-0.5.0-r2/manifest.json"


def create(tmp_path, **options):
    ledger = tmp_path / "budget.sqlite"
    BudgetLedger(ledger, {"mode": "user_managed", "limit_usd": None, "model": "test-model",
        "input_per_million": "1", "output_per_million": "1", "pricing_source": "https://example.test",
        "pricing_checked_at": "2026-09-18"})
    return create_operation(CASE, tmp_path / "work", budget_path=ledger, seed_strategy="none",
        protocol_revision=4, max_calls=1, generation={"model": "test-model",
        "endpoint": "https://example.test/chat/completions", "thinking_mode": "disabled",
        "max_output_tokens": 1024, "timeout_seconds": 10}, **options)


@pytest.mark.parametrize("mode", [None, "full", "source_only"])
def test_context_assistance_reaches_transport_without_removing_contract_source_or_tools(tmp_path, mode):
    task = create(tmp_path, **({"context_assistance": mode} if mode else {}))
    seen = []

    def transport(request, **kwargs):
        payload = json.loads(request.data)
        context = json.loads(payload["messages"][1]["content"])
        assert bool(context["potential_impacts"]) == (mode != "source_only")
        assert bool(context["version_evidence"]) == (mode != "source_only")
        assert context["source_files"] and context["business_contract"]["text"]
        assert context["source_inventory"]["total"] > 0 and context["allowed_changes"]
        assert "diagnostic_state" in context and "read_source" in payload["messages"][0]["content"]
        seen.append(context)
        return json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "summary": "offline", "action": {"type": "list_sources", "view": "current",
                "revision": context["candidate"]["revision"]}})}}]}).encode()

    advance_task(Path(task["task_path"]), api_key="unused", transport=transport)
    assert len(seen) == 1
    assert inspect_task(Path(task["task_path"]))["protocol"]["context_assistance"] == (mode or "full")


def test_context_assistance_rejects_unknown_value_before_registration(tmp_path):
    with pytest.raises(ValueError, match="Context assistance"):
        create(tmp_path, context_assistance="typo")
    assert not list(tmp_path.rglob("task.json"))


def test_context_assistance_cannot_change_on_resume(tmp_path):
    task = create(tmp_path, context_assistance="source_only")
    task["protocol"]["context_assistance"] = "full"
    path = Path(task["task_path"])
    path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen protocol changed"):
        inspect_task(path)
