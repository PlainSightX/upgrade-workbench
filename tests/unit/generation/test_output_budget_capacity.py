"""扩展显式输出容量时保持旧回执、响应字节和账本边界；不发网络请求。"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from upgrade_workbench.budget import BudgetLedger
from upgrade_workbench.generation import ProposalInputError, prepare_request, provider, request


def ledger_for(tmp_path, model):
    return BudgetLedger(tmp_path / "capacity.sqlite3", {
        "mode": "user_managed", "limit_usd": None, "model": model,
        "input_per_million": "1", "output_per_million": "1",
        "pricing_source": "https://example.test/offline-price",
        "pricing_checked_at": "offline-fixture",
    })


@pytest.mark.parametrize("output_tokens", [32_768, 65_536])
def test_explicit_output_capacity_survives_production_reconstruction(
    owned_case, analysis, evidence, arguments, output_tokens,
):
    receipt = prepare_request(
        owned_case, analysis, evidence,
        **(arguments | {
            "max_output_tokens": output_tokens, "timeout_seconds": 300,
            "thinking_mode": "enabled",
        }),
    )
    loaded, body, report_path = provider._load_prepared(receipt)
    payload = json.loads(body)

    assert loaded == receipt
    assert report_path == Path(receipt["report_path"])
    assert payload["max_tokens"] == output_tokens
    assert payload["thinking"] == {"type": "enabled"}
    assert receipt["schema_version"] == 1
    assert receipt["timeout_seconds"] == 300
    assert receipt["max_response_bytes"] == provider.MAX_RESPONSE_BYTES == 384_000
    assert receipt["request_sha256"] == hashlib.sha256(body).hexdigest()
    assert receipt["calls"] == 0 and receipt["target_code_executed"] is False


def test_32768_receipt_created_under_old_limit_remains_unchanged_and_loadable(
    owned_case, analysis, evidence, arguments, monkeypatch,
):
    # 先在旧上限下生成真实回执，再放宽上限；兼容性不能靠重写旧回执建立。
    with monkeypatch.context() as prior:
        prior.setattr(request, "MAX_OUTPUT_TOKENS", 32_768)
        receipt = prepare_request(
            owned_case, analysis, evidence,
            **(arguments | {"max_output_tokens": 32_768, "timeout_seconds": 240}),
        )
    report_path = Path(receipt["report_path"])
    request_path = Path(receipt["request_path"])
    saved = (report_path.read_bytes(), request_path.read_bytes())

    loaded, body, _ = provider._load_prepared(receipt)

    assert request.MAX_OUTPUT_TOKENS == 65_536
    assert loaded["max_output_tokens"] == 32_768
    assert loaded["timeout_seconds"] == 240
    assert loaded["max_response_bytes"] == 384_000
    assert body == saved[1]
    assert (report_path.read_bytes(), request_path.read_bytes()) == saved


def test_65536_budget_reservation_and_settlement_keep_input_bound(
    owned_case, analysis, evidence, arguments, tmp_path,
):
    receipt = prepare_request(
        owned_case, analysis, evidence,
        **(arguments | {"max_output_tokens": 65_536, "timeout_seconds": 300}),
    )
    provider._load_prepared(receipt)
    ledger = ledger_for(tmp_path, receipt["model"])
    reserved = ledger.reserve(receipt, "development")
    input_bound = len(Path(receipt["request_path"]).read_bytes()) + 8192
    with sqlite3.connect(ledger.path) as db:
        row = db.execute(
            "SELECT reserved,input_bound,output_bound,state FROM calls WHERE request_id=?",
            (reserved["request_id"],),
        ).fetchone()
    assert row == (input_bound + 65_536, input_bound, 65_536, "reserved")
    assert reserved["reserved_usd"] == (input_bound + 65_536) / 1_000_000

    ledger.reconcile_receipt(reserved["request_id"], receipt["request_sha256"], {
        "availability": "reported", "input_tokens": 100, "output_tokens": 65_536,
        "reasoning_tokens": 60_000,
    })
    with sqlite3.connect(ledger.path) as db:
        settled = db.execute("SELECT charged,state FROM calls").fetchone()
    assert settled == (65_636, "settled")


def test_provider_and_budget_reject_65537_before_a_call_or_reservation(prepared, tmp_path):
    # 两个入口独立检查上限，不能只依赖最前面的prepare校验。
    prepared["max_output_tokens"] = 65_537
    Path(prepared["report_path"]).write_text(json.dumps(prepared), encoding="utf-8")
    with pytest.raises(ProposalInputError, match="max_output_tokens.*65536"):
        provider._load_prepared(prepared)
    ledger = ledger_for(tmp_path, prepared["model"])
    with pytest.raises(ValueError, match="Output tokens must be a positive bounded integer"):
        ledger.reserve(prepared, "development")
    assert ledger.snapshot()["calls"] == 0
