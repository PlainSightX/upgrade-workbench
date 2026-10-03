"""使用事务账本预留真实调用费用，未知结果不能当成免费重试。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from .cases.manifest import assert_no_links
from .generation.request import MAX_REQUEST_CAPACITY


class BudgetExceeded(ValueError):
    """已登记的费用或调用额度不足。"""


def micros(value: str | int | float) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Amounts must be finite and nonnegative")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


class BudgetLedger:
    """一个整改预算只有一个账本；重新启动任务不会重置它。"""

    def __init__(self, path: Path, specification: dict | None = None):
        self.path = Path(path).absolute()
        assert_no_links(self.path)
        if specification is None and not self.path.is_file():
            raise ValueError("Initialize a reviewed budget before calling a model")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS configuration (id INTEGER PRIMARY KEY, body TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS calls (
                request_id TEXT PRIMARY KEY, phase TEXT NOT NULL, request_hash TEXT NOT NULL,
                reserved INTEGER NOT NULL, charged INTEGER, input_bound INTEGER NOT NULL,
                output_bound INTEGER NOT NULL, state TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS amendments (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, reason TEXT NOT NULL,
                old_configuration_sha256 TEXT NOT NULL, new_configuration_sha256 TEXT NOT NULL,
                old_configuration TEXT NOT NULL, new_configuration TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS receipt_usage (
                request_id TEXT PRIMARY KEY REFERENCES calls(request_id), body TEXT NOT NULL)""")
            existing = db.execute("SELECT body FROM configuration WHERE id=1").fetchone()
            if existing is None:
                if specification is None:
                    raise ValueError("Budget configuration is missing")
                self._validate(specification)
                encoded = json.dumps(specification, sort_keys=True, separators=(",", ":"))
                db.execute("INSERT INTO configuration VALUES (1, ?)", (encoded,))
                self._spec_json = encoded
            else:
                self._spec_json = existing[0]
                if specification is not None and self.spec != specification:
                    raise ValueError("An existing budget cannot be silently replaced")
            self._validate(self.spec)

    @property
    def spec(self) -> dict:
        """返回副本，调用方不能通过修改字典扩大持久预算。"""
        return json.loads(self._spec_json)

    @staticmethod
    def _validate(spec: dict) -> None:
        if "mode" in spec:
            if (
                set(spec) != {"mode", "limit_usd", "model", "input_per_million",
                              "output_per_million", "pricing_source", "pricing_checked_at"}
                or spec["mode"] not in {"user_managed", "capped"}
                or (spec["mode"] == "user_managed" and spec["limit_usd"] is not None)
                or (spec["mode"] == "capped" and (spec["limit_usd"] is None or micros(spec["limit_usd"]) <= 0))
                or not isinstance(spec["model"], str) or not spec["model"].strip()
                or not spec["pricing_source"].startswith("https://") or not spec["pricing_checked_at"]
                or micros(spec["input_per_million"]) <= 0 or micros(spec["output_per_million"]) <= 0
            ):
                raise ValueError("Invalid accounting specification")
            return
        if (
            set(spec) != {"limit_usd", "checkpoint_usd", "calibration_usd", "model",
                          "input_per_million", "output_per_million", "pricing_source",
                          "pricing_checked_at", "quotas", "holdout_reserve_usd"}
            or not 0 < micros(spec["limit_usd"]) <= micros(200)
            or not 0 < micros(spec["checkpoint_usd"]) <= micros(spec["limit_usd"])
            or not 0 < micros(spec["calibration_usd"]) <= micros(spec["limit_usd"])
            or not 0 < micros(spec["holdout_reserve_usd"]) < micros(spec["limit_usd"])
            or set(spec["quotas"]) != {"calibration", "development", "holdout"}
            or any(type(value) is not int or value <= 0 for value in spec["quotas"].values())
            or not isinstance(spec["model"], str) or not spec["model"].strip()
            or not spec["pricing_source"].startswith("https://")
            or not spec["pricing_checked_at"]
            or micros(spec["input_per_million"]) <= 0
            or micros(spec["output_per_million"]) <= 0
        ):
            raise ValueError("Invalid or unauthorized budget specification")

    @contextmanager
    def _transaction(self):
        assert_no_links(self.path)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def cost(self, input_tokens: int, output_tokens: int) -> int:
        # 单价为每百万 token 的美元；结果直接采用百万分之一美元，向上取整。
        return int((
            Decimal(self.spec["input_per_million"]) * input_tokens
            + Decimal(self.spec["output_per_million"]) * output_tokens
        ).to_integral_value(rounding=ROUND_CEILING))

    def amend_holdout_reserve(
        self, new_reserve_usd: str | int | float, *,
        expected_configuration_sha256: str, reason: str,
    ) -> dict:
        """只提高留出集保护额；旧调用、费用、额度和价格都不能借此重置。"""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("A nonempty bounded amendment reason is required")
        if (
            not isinstance(expected_configuration_sha256, str)
            or len(expected_configuration_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_configuration_sha256)
        ):
            raise ValueError("A reviewed configuration SHA-256 is required")
        if type(new_reserve_usd) not in {str, int, float}:
            raise ValueError("Holdout reserve must be a finite amount")
        try:
            new_reserve = micros(new_reserve_usd)
        except InvalidOperation as exc:
            raise ValueError("Holdout reserve must be a finite amount") from exc
        with self._transaction() as db:
            previous = db.execute("SELECT body FROM configuration WHERE id=1").fetchone()[0]
            old_hash = hashlib.sha256(previous.encode("utf-8")).hexdigest()
            if old_hash != expected_configuration_sha256 or previous != self._spec_json:
                raise BudgetExceeded("Budget configuration changed; review the current configuration")
            old_spec = json.loads(previous)
            if new_reserve <= micros(old_spec["holdout_reserve_usd"]):
                raise ValueError("Holdout reserve amendment must strictly increase the reserve")
            if db.execute("SELECT 1 FROM calls WHERE state='reserved'").fetchone():
                raise BudgetExceeded("Reconcile in-flight reserved calls before amending the budget")
            if db.execute("SELECT 1 FROM calls WHERE state='bound_violated'").fetchone():
                raise BudgetExceeded("A provider bound violation requires billing reconciliation")
            # 沿用已有费用占用口径；unknown 调用仍占据原预留额，不能当成零花费。
            occupied = db.execute(
                "SELECT coalesce(sum(coalesce(charged,reserved)),0) FROM calls",
            ).fetchone()[0]
            holdout_allocated = db.execute(
                "SELECT coalesce(sum(reserved),0) FROM calls WHERE phase='holdout'",
            ).fetchone()[0]
            protected = max(0, new_reserve - holdout_allocated)
            if occupied + protected > micros(old_spec["limit_usd"]):
                raise BudgetExceeded("Insufficient remaining budget for increased holdout reserve")
            amended = dict(old_spec)
            amended["holdout_reserve_usd"] = str(Decimal(new_reserve) / 1_000_000)
            self._validate(amended)
            encoded = json.dumps(amended, sort_keys=True, separators=(",", ":"))
            new_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            created_at = datetime.now(UTC).isoformat()
            audit_id = db.execute(
                "INSERT INTO amendments (created_at,reason,old_configuration_sha256,"
                "new_configuration_sha256,old_configuration,new_configuration) VALUES (?,?,?,?,?,?)",
                (created_at, reason.strip(), old_hash, new_hash, previous, encoded),
            ).lastrowid
            db.execute("UPDATE configuration SET body=? WHERE id=1", (encoded,))
        # 事务提交前不改变本对象；其他旧实例仍会被 reserve 的配置一致性检查拦截。
        self._spec_json = encoded
        return {
            "amendment_id": audit_id, "kind": "increase_holdout_reserve",
            "created_at": created_at, "reason": reason.strip(),
            "old_configuration_sha256": old_hash, "new_configuration_sha256": new_hash,
            "old_holdout_reserve_usd": old_spec["holdout_reserve_usd"],
            "new_holdout_reserve_usd": amended["holdout_reserve_usd"],
        }

    def amend_policy(self, *, mode: str, limit_usd=None,
                     expected_configuration_sha256: str, reason: str) -> dict:
        """同账本有审计地改变金额策略；历史价格、调用和未知预留原样保留。"""
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ValueError("A bounded policy amendment reason is required")
        amended = {key: self.spec[key] for key in (
            "model", "input_per_million", "output_per_million", "pricing_source", "pricing_checked_at",
        )}
        amended.update(mode=mode, limit_usd=limit_usd)
        self._validate(amended)
        with self._transaction() as db:
            old = db.execute("SELECT body FROM configuration WHERE id=1").fetchone()[0]
            old_hash = hashlib.sha256(old.encode()).hexdigest()
            if old != self._spec_json or old_hash != expected_configuration_sha256:
                raise BudgetExceeded("Budget configuration changed; inspect before amendment")
            if db.execute("SELECT 1 FROM calls WHERE state IN ('reserved','bound_violated')").fetchone():
                raise BudgetExceeded("Reconcile in-flight calls or bound violations first")
            occupied = db.execute("SELECT coalesce(sum(coalesce(charged,reserved)),0) FROM calls").fetchone()[0]
            if mode == "capped" and micros(limit_usd) < occupied:
                raise BudgetExceeded("Optional cap is below already occupied costs")
            encoded = json.dumps(amended, sort_keys=True, separators=(",", ":"))
            new_hash = hashlib.sha256(encoded.encode()).hexdigest()
            audit_id = db.execute(
                "INSERT INTO amendments (created_at,reason,old_configuration_sha256,"
                "new_configuration_sha256,old_configuration,new_configuration) VALUES (?,?,?,?,?,?)",
                (datetime.now(UTC).isoformat(), reason.strip(), old_hash, new_hash, old, encoded),
            ).lastrowid
            db.execute("UPDATE configuration SET body=? WHERE id=1", (encoded,))
        self._spec_json = encoded
        return {"amendment_id": audit_id, "kind": "monetary_policy", "mode": mode,
                "old_configuration_sha256": old_hash, "new_configuration_sha256": new_hash}

    def reserve(self, prepared: dict, phase: str) -> dict:
        phases = self.spec.get("quotas", {"operation", "calibration", "development", "holdout"})
        if phase not in phases or prepared["model"] != self.spec["model"]:
            raise ValueError("Request does not match this budget's model or phase")
        request_path = Path(prepared["request_path"])
        assert_no_links(request_path)
        body = request_path.read_bytes()
        if len(body) > MAX_REQUEST_CAPACITY or hashlib.sha256(body).hexdigest() != prepared["request_sha256"]:
            raise ValueError("Request size or identity violates the reservation contract")
        # 对固定模型采用每 UTF-8 字节一个 token 加协议余量的保守上界。
        input_bound = len(body) + 8192
        output_bound = prepared["max_output_tokens"]
        from .generation.capacity import limits

        maximum_output, _, _ = limits(prepared.get("runtime_capacity"))
        if type(output_bound) is not int or not 1 <= output_bound <= maximum_output:
            raise ValueError("Output tokens must be a positive bounded integer")
        amount = self.cost(input_bound, output_bound)
        request_id = Path(prepared["report_path"]).parent.name
        with self._transaction() as db:
            if db.execute("SELECT body FROM configuration WHERE id=1").fetchone()[0] != self._spec_json:
                raise BudgetExceeded("Persisted budget configuration changed; reconcile before spending")
            if db.execute("SELECT 1 FROM calls WHERE state='bound_violated'").fetchone():
                raise BudgetExceeded("A provider bound violation requires billing reconciliation")
            if db.execute("SELECT 1 FROM calls WHERE request_id=?", (request_id,)).fetchone():
                raise BudgetExceeded("Request already reserved; recovery must reconcile its receipt")
            count, phase_spent = db.execute(
                "SELECT count(*), coalesce(sum(coalesce(charged,reserved)),0) FROM calls WHERE phase=?",
                (phase,),
            ).fetchone()
            total = db.execute("SELECT coalesce(sum(coalesce(charged,reserved)),0) FROM calls").fetchone()[0]
            holdout_allocated = db.execute(
                "SELECT coalesce(sum(reserved),0) FROM calls WHERE phase='holdout'",
            ).fetchone()[0]
            if "mode" in self.spec:
                if self.spec["mode"] == "capped" and total + amount > micros(self.spec["limit_usd"]):
                    raise BudgetExceeded("Optional monetary cap reached")
            else:
                protected = max(0, micros(self.spec["holdout_reserve_usd"]) - holdout_allocated)
                if count >= self.spec["quotas"][phase]:
                    raise BudgetExceeded("Phase call quota exhausted")
                if total + amount + (protected if phase != "holdout" else 0) > micros(self.spec["limit_usd"]):
                    raise BudgetExceeded("Insufficient budget including protected holdout reserve")
                if phase == "calibration" and phase_spent + amount > micros(self.spec["calibration_usd"]):
                    raise BudgetExceeded("Calibration spending limit reached")
            db.execute(
                "INSERT INTO calls VALUES (?,?,?,?,NULL,?,?,?)",
                (request_id, phase, prepared["request_sha256"], amount,
                 input_bound, output_bound, "reserved"),
            )
        return {"request_id": request_id, "reserved_usd": amount / 1_000_000}

    def settle(self, request_id: str, usage: dict) -> None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT input_bound,output_bound,state FROM calls WHERE request_id=?", (request_id,),
            ).fetchone()
            if row is None or row[2] in {"settled", "bound_violated"}:
                raise ValueError("Reservation missing or already settled")
            incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
            if type(incoming) is not int or type(outgoing) is not int or min(incoming, outgoing) < 0:
                db.execute("UPDATE calls SET state='unknown' WHERE request_id=?", (request_id,))
                return
            cost = self.cost(incoming, outgoing)
            violated = incoming > row[0] or outgoing > row[1]
            db.execute("INSERT OR REPLACE INTO receipt_usage VALUES (?,?)",
                       (request_id, json.dumps(usage, sort_keys=True)))
            db.execute(
                "UPDATE calls SET charged=?,state=? WHERE request_id=?",
                (cost, "bound_violated" if violated else "settled", request_id),
            )
        if violated:
            raise BudgetExceeded("Provider exceeded reserved token bounds; stop and reconcile billing")

    def reconcile_receipt(self, request_id: str, request_hash: str, usage: dict) -> None:
        """恢复仅接纳同一请求的持久收据；重复结算须与原用量完全一致。"""
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        known = type(incoming) is int and type(outgoing) is int and min(incoming, outgoing) >= 0
        with self._transaction() as db:
            row = db.execute(
                "SELECT request_hash,input_bound,output_bound,state,charged FROM calls WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None or row[0] != request_hash:
                raise ValueError("Recovery receipt does not match reserved request")
            if row[3] in {"settled", "bound_violated"}:
                saved = db.execute("SELECT body FROM receipt_usage WHERE request_id=?", (request_id,)).fetchone()
                # 旧账本只有费用，不能反推token；宁可要求核对也不接受等价金额冒充同一用量。
                if not known or saved is None or json.loads(saved[0]) != usage:
                    raise ValueError("Recovery cannot replace settled usage")
                if row[3] == "bound_violated":
                    raise BudgetExceeded("Recovered provider bound violation")
                return
            if not known:
                db.execute("UPDATE calls SET state='unknown' WHERE request_id=?", (request_id,))
                return
            violated = incoming > row[1] or outgoing > row[2]
            db.execute("INSERT OR REPLACE INTO receipt_usage VALUES (?,?)",
                       (request_id, json.dumps(usage, sort_keys=True)))
            db.execute("UPDATE calls SET charged=?,state=? WHERE request_id=?",
                       (self.cost(incoming, outgoing), "bound_violated" if violated else "settled", request_id))
        if violated:
            raise BudgetExceeded("Recovered provider exceeded reserved token bounds")

    def snapshot(self) -> dict:
        with self._transaction() as db:
            rows = db.execute("SELECT phase,state,count(*),sum(coalesce(charged,reserved)) FROM calls GROUP BY phase,state").fetchall()
            configuration = db.execute("SELECT body FROM configuration WHERE id=1").fetchone()[0]
            amendment_count = db.execute("SELECT count(*) FROM amendments").fetchone()[0]
        current_spec = json.loads(configuration)
        occupied = sum(row[3] for row in rows)
        return {
            "limit_usd": current_spec["limit_usd"], "estimated_or_reserved_usd": occupied / 1_000_000,
            "configuration_sha256": hashlib.sha256(configuration.encode("utf-8")).hexdigest(),
            "holdout_reserve_usd": current_spec.get("holdout_reserve_usd"),
            "mode": current_spec.get("mode", "legacy_budget"),
            "amendments": amendment_count,
            "calls": sum(row[2] for row in rows),
            "checkpoint_reached": (occupied >= micros(current_spec["checkpoint_usd"])) if "checkpoint_usd" in current_spec else False,
            "groups": [{"phase": p, "state": s, "calls": n, "usd": cost / 1_000_000} for p, s, n, cost in rows],
            "actual_billing": "not_claimed",
        }
