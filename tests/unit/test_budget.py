"""只用本地账本验证并发额度与未知费用保护，不发起模型请求。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from upgrade_workbench.budget import BudgetExceeded, BudgetLedger


@pytest.fixture
def specification():
    return {
        "limit_usd": "1", "checkpoint_usd": "0.5", "calibration_usd": "0.2",
        "model": "owned-model", "input_per_million": "1", "output_per_million": "1",
        "pricing_source": "https://example.test/prices", "pricing_checked_at": "2026-09-16",
        "quotas": {"calibration": 5, "development": 5, "holdout": 5},
        "holdout_reserve_usd": "0.2",
    }


def request(root: Path, identity: str, *, model="owned-model", output=100):
    directory = root / identity
    directory.mkdir()
    path = directory / "request.json"
    path.write_bytes(b"{}")
    return {
        "model": model, "max_output_tokens": output, "request_path": str(path),
        "report_path": str(directory / "proposal.json"),
        "request_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_duplicate_reservation_does_not_consume_twice(tmp_path, specification):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    prepared = request(tmp_path, "one")
    reservation = ledger.reserve(prepared, "development")
    with pytest.raises(BudgetExceeded, match="already reserved"):
        ledger.reserve(prepared, "development")
    assert ledger.snapshot()["calls"] == 1
    assert ledger.snapshot()["estimated_or_reserved_usd"] == reservation["reserved_usd"]


def test_large_explicit_request_retains_full_cost_bound_and_identity(tmp_path, specification):
    from upgrade_workbench.generation.request import MAX_REQUEST_CAPACITY

    specification.update(limit_usd="10", checkpoint_usd="8", calibration_usd="2")
    ledger = BudgetLedger(tmp_path / "large.sqlite", specification)
    prepared = request(tmp_path, "large")
    path = Path(prepared["request_path"])
    body = json.dumps({"source": "x" * 1_260_000}).encode()
    path.write_bytes(body)
    prepared["request_sha256"] = hashlib.sha256(body).hexdigest()
    reserved = ledger.reserve(prepared, "development")
    assert reserved["reserved_usd"] > 1.26
    bad = request(tmp_path, "changed")
    Path(bad["request_path"]).write_bytes(body)
    with pytest.raises(ValueError, match="identity"):
        ledger.reserve(bad, "development")
    oversized = request(tmp_path, "too-large")
    Path(oversized["request_path"]).write_bytes(b"x" * (MAX_REQUEST_CAPACITY + 1))
    oversized["request_sha256"] = hashlib.sha256(Path(oversized["request_path"]).read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="size"):
        ledger.reserve(oversized, "development")
    assert ledger.snapshot()["calls"] == 1


def test_monetary_policy_change_keeps_legacy_unknown_costs_and_ignores_old_quota(tmp_path, specification):
    specification["quotas"]["development"] = 1
    path = tmp_path / "legacy.sqlite"
    ledger = BudgetLedger(path, specification)
    first = ledger.reserve(request(tmp_path, "legacy-unknown"), "development")
    ledger.settle(first["request_id"], {})
    before = ledger.snapshot()
    ledger.amend_policy(mode="user_managed", expected_configuration_sha256=before["configuration_sha256"], reason="explicit new user policy")
    after = BudgetLedger(path).snapshot()
    assert before["groups"] == after["groups"]
    assert before["estimated_or_reserved_usd"] == after["estimated_or_reserved_usd"]
    assert after["limit_usd"] is None
    ledger.reserve(request(tmp_path, "new-call"), "development")
    assert ledger.snapshot()["calls"] == 2
    with sqlite3.connect(path) as db:
        old, new = db.execute("SELECT old_configuration,new_configuration FROM amendments").fetchone()
        assert "quotas" in json.loads(old) and "quotas" not in json.loads(new)


def test_unknown_usage_retains_reservation_and_call_quota(tmp_path, specification):
    specification["quotas"]["development"] = 1
    path = tmp_path / "budget.sqlite"
    ledger = BudgetLedger(path, specification)
    reservation = ledger.reserve(request(tmp_path, "unknown"), "development")
    ledger.settle("unknown", {"input_tokens": None, "output_tokens": 0})
    reopened = BudgetLedger(path)
    snapshot = reopened.snapshot()
    assert snapshot["groups"][0]["state"] == "unknown"
    assert snapshot["estimated_or_reserved_usd"] == reservation["reserved_usd"]
    with pytest.raises(BudgetExceeded, match="quota"):
        reopened.reserve(request(tmp_path, "second"), "development")


def test_development_cannot_spend_protected_holdout_funds(tmp_path, specification):
    specification.update(limit_usd="0.03", checkpoint_usd="0.02",
                         calibration_usd="0.005", holdout_reserve_usd="0.02")
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    ledger.reserve(request(tmp_path, "first"), "development")
    with pytest.raises(BudgetExceeded, match="holdout"):
        ledger.reserve(request(tmp_path, "not-funded"), "development")
    ledger.reserve(request(tmp_path, "reserved-for-holdout"), "holdout")
    assert ledger.snapshot()["calls"] == 2


def test_parallel_reservations_cannot_each_spend_the_same_balance(tmp_path, specification):
    specification.update(limit_usd="0.012", checkpoint_usd="0.01",
                         calibration_usd="0.01", holdout_reserve_usd="0.001")
    path = tmp_path / "budget.sqlite"
    BudgetLedger(path, specification)
    prepared = [request(tmp_path, "first"), request(tmp_path, "second")]

    def reserve_one(item):
        try:
            BudgetLedger(path).reserve(item, "development")
            return "reserved"
        except BudgetExceeded:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve_one, prepared)) == ["rejected", "reserved"]
    assert BudgetLedger(path).snapshot()["calls"] == 1


def test_request_model_must_match_reviewed_price_identity(tmp_path, specification):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    with pytest.raises(ValueError, match="model"):
        ledger.reserve(request(tmp_path, "different", model="unpriced-model"), "development")
    assert ledger.snapshot()["calls"] == 0


def test_existing_ledger_specification_cannot_be_replaced(tmp_path, specification):
    path = tmp_path / "budget.sqlite"
    BudgetLedger(path, specification)
    different = deepcopy(specification)
    different["limit_usd"] = "2"
    with pytest.raises(ValueError, match="replaced"):
        BudgetLedger(path, different)


def test_external_spec_mutation_cannot_raise_persisted_quota(tmp_path, specification):
    specification["quotas"]["development"] = 1
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    ledger.reserve(request(tmp_path, "first"), "development")
    specification["quotas"]["development"] = 999
    with pytest.raises((BudgetExceeded, ValueError)):
        ledger.reserve(request(tmp_path, "second"), "development")


def test_budget_object_mutation_cannot_change_persisted_model(tmp_path, specification):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    ledger.spec["model"] = "unpriced-model"
    with pytest.raises((BudgetExceeded, ValueError)):
        ledger.reserve(request(tmp_path, "other", model="unpriced-model"), "development")


@pytest.mark.parametrize("output", [-10000, 0, True, 1.5, 65537])
def test_invalid_output_bound_cannot_create_a_reservation(tmp_path, specification, output):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    with pytest.raises(ValueError):
        ledger.reserve(request(tmp_path, "invalid", output=output), "development")
    assert ledger.snapshot()["calls"] == 0


def test_changed_request_bytes_are_rejected_before_reservation(tmp_path, specification):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    prepared = request(tmp_path, "changed")
    Path(prepared["request_path"]).write_bytes(b'{"extra": "unfrozen payload"}')
    with pytest.raises(ValueError, match="identity"):
        ledger.reserve(prepared, "development")
    assert ledger.snapshot()["calls"] == 0


def test_calibration_has_its_own_spending_ceiling(tmp_path, specification):
    specification["calibration_usd"] = "0.01"
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    ledger.reserve(request(tmp_path, "first"), "calibration")
    with pytest.raises(BudgetExceeded, match="Calibration"):
        ledger.reserve(request(tmp_path, "second"), "calibration")
    assert ledger.snapshot()["calls"] == 1


def test_provider_bound_violation_blocks_more_spend_and_cannot_be_erased(tmp_path, specification):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    ledger.reserve(request(tmp_path, "violation"), "development")
    with pytest.raises(BudgetExceeded, match="exceeded"):
        ledger.settle("violation", {"input_tokens": 9000, "output_tokens": 100})
    with pytest.raises((BudgetExceeded, ValueError)):
        ledger.settle("violation", {"input_tokens": 1, "output_tokens": 1})
    with pytest.raises(BudgetExceeded):
        ledger.reserve(request(tmp_path, "blocked"), "development")


def test_settlement_uses_actual_usage_once_and_retains_total_call_count(tmp_path, specification):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    ledger.reserve(request(tmp_path, "one"), "development")
    ledger.settle("one", {"input_tokens": 200, "output_tokens": 10})
    snapshot = ledger.snapshot()
    assert snapshot["estimated_or_reserved_usd"] == 0.00021
    assert snapshot["calls"] == 1
    assert snapshot["actual_billing"] == "not_claimed"
    with pytest.raises(ValueError, match="settled"):
        ledger.settle("one", {"input_tokens": 0, "output_tokens": 0})


def database_state(path):
    with sqlite3.connect(path) as db:
        return {
            "configuration": db.execute("SELECT body FROM configuration WHERE id=1").fetchone()[0],
            "calls": db.execute("SELECT * FROM calls ORDER BY request_id").fetchall(),
            "amendments": db.execute("SELECT * FROM amendments ORDER BY id").fetchall(),
        }


def amend(ledger, amount, *, expected=None, reason="提高长输出协议的留出集保护额"):
    return ledger.amend_holdout_reserve(
        amount, expected_configuration_sha256=expected or ledger.snapshot()["configuration_sha256"],
        reason=reason,
    )


def test_32768_output_is_reserved_without_changing_input_overhead(tmp_path, specification):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    ledger.reserve(request(tmp_path, "large", output=32768), "development")
    row = database_state(ledger.path)["calls"][0]
    assert row[3] == 2 + 8192 + 32768
    assert row[5:7] == (8194, 32768)
    ledger.settle("large", {"input_tokens": 100, "output_tokens": 20_000})
    assert ledger.snapshot()["estimated_or_reserved_usd"] == 0.0201


def test_amendment_changes_only_holdout_and_preserves_every_prior_call(tmp_path, specification):
    specification.update(limit_usd="200", checkpoint_usd="100", calibration_usd="20",
                         holdout_reserve_usd="25",
                         quotas={"calibration": 50, "development": 300, "holdout": 150})
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    for name, phase, usage in (
        ("settled", "calibration", {"input_tokens": 300, "output_tokens": 100}),
        ("unknown", "development", {}),
        ("heldout", "holdout", {"input_tokens": 100, "output_tokens": 10}),
    ):
        ledger.reserve(request(tmp_path, name), phase)
        ledger.settle(name, usage)
    before = database_state(ledger.path)
    before_snapshot = ledger.snapshot()

    audit = amend(ledger, "35")

    after = database_state(ledger.path)
    assert after["calls"] == before["calls"]
    expected_spec = deepcopy(specification)
    expected_spec["holdout_reserve_usd"] = "35"
    assert ledger.spec == expected_spec == BudgetLedger(ledger.path).spec
    assert after["amendments"][0][5:] == (before["configuration"], after["configuration"])
    assert audit["old_configuration_sha256"] == before_snapshot["configuration_sha256"]
    assert audit["new_configuration_sha256"] == hashlib.sha256(after["configuration"].encode()).hexdigest()
    assert ledger.snapshot()["amendments"] == 1
    for key in ("calls", "groups", "estimated_or_reserved_usd", "checkpoint_reached", "limit_usd"):
        assert ledger.snapshot()[key] == before_snapshot[key]


@pytest.mark.parametrize("amount", ["0.2", "0.1", "0", "-1", "1", "NaN", "Infinity", "bad", True, None])
def test_invalid_amendment_never_changes_configuration_calls_or_audit(tmp_path, specification, amount):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    before = database_state(ledger.path)
    with pytest.raises(ValueError):
        amend(ledger, amount)
    assert database_state(ledger.path) == before
    assert ledger.spec == specification


@pytest.mark.parametrize("reason", ["", "  ", None, "x" * 2001])
def test_amendment_requires_a_bounded_reason(tmp_path, specification, reason):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    before = database_state(ledger.path)
    with pytest.raises(ValueError, match="reason"):
        amend(ledger, "0.3", reason=reason)
    assert database_state(ledger.path) == before


def test_amendment_cannot_erase_unknown_costs_or_exhausted_call_quota(tmp_path, specification):
    specification["quotas"]["development"] = 1
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    reservation = ledger.reserve(request(tmp_path, "unknown"), "development")
    ledger.settle("unknown", {})
    amend(ledger, "0.3")
    assert ledger.snapshot()["estimated_or_reserved_usd"] == reservation["reserved_usd"]
    with pytest.raises(BudgetExceeded, match="quota"):
        ledger.reserve(request(tmp_path, "second"), "development")
    assert ledger.snapshot()["calls"] == 1


def test_amendment_protects_funds_against_cumulative_settled_and_unknown_costs(tmp_path, specification):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    prepared = request(tmp_path, "costly")
    Path(prepared["request_path"]).write_bytes(b" " * 500_000)
    prepared["request_sha256"] = hashlib.sha256(b" " * 500_000).hexdigest()
    ledger.reserve(prepared, "development")
    ledger.settle("costly", {"input_tokens": 500_000, "output_tokens": 100})
    ledger.reserve(request(tmp_path, "unknown"), "development")
    ledger.settle("unknown", {})
    before = database_state(ledger.path)
    with pytest.raises(BudgetExceeded, match="remaining budget"):
        amend(ledger, "0.5")
    assert database_state(ledger.path) == before
    amend(ledger, "0.49")
    with pytest.raises(BudgetExceeded, match="holdout reserve"):
        ledger.reserve(request(tmp_path, "cannot-spend-protected"), "development")
    assert ledger.snapshot()["calls"] == 2


@pytest.mark.parametrize("state", ["reserved", "bound_violated"])
def test_amendment_waits_for_inflight_and_billing_reconciliation(tmp_path, specification, state):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    ledger.reserve(request(tmp_path, "unresolved"), "development")
    if state == "bound_violated":
        with pytest.raises(BudgetExceeded):
            ledger.settle("unresolved", {"input_tokens": 9000, "output_tokens": 100})
    before = database_state(ledger.path)
    with pytest.raises(BudgetExceeded):
        amend(ledger, "0.3")
    assert database_state(ledger.path) == before


def test_concurrent_amendments_require_a_fresh_configuration_review(tmp_path, specification):
    path = tmp_path / "budget.sqlite"
    ledger = BudgetLedger(path, specification)
    reviewed_hash = ledger.snapshot()["configuration_sha256"]

    def try_amend(amount):
        try:
            amend(BudgetLedger(path), amount, expected=reviewed_hash)
            return "amended"
        except BudgetExceeded:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(try_amend, ["0.3", "0.4"])) == ["amended", "rejected"]
    assert BudgetLedger(path).snapshot()["amendments"] == 1
    before = database_state(path)
    with pytest.raises(BudgetExceeded, match="configuration changed"):
        amend(BudgetLedger(path), "0.5", expected=reviewed_hash)
    assert database_state(path) == before
    assert ledger.snapshot()["configuration_sha256"] != reviewed_hash
    with pytest.raises(BudgetExceeded, match="configuration changed"):
        ledger.reserve(request(tmp_path, "stale"), "development")


def test_amendment_configuration_failure_rolls_back_audit_and_instance(tmp_path, specification):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", specification)
    with sqlite3.connect(ledger.path) as db:
        db.execute("CREATE TRIGGER reject_config BEFORE UPDATE ON configuration "
                   "BEGIN SELECT RAISE(ABORT, 'simulated configuration failure'); END")
    before = database_state(ledger.path)
    with pytest.raises(sqlite3.IntegrityError, match="simulated"):
        amend(ledger, "0.3")
    assert database_state(ledger.path) == before
    assert ledger.spec == json.loads(before["configuration"])
