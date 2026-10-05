from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from moex_bot.models import Candle, Instrument
from moex_bot.sandbox import run_step
from moex_bot.tbank import ApiError, quotation


def rub(amount):
    return {"currency": "rub", **quotation(Decimal(amount))}


class FakeClient:
    def __init__(self, *, cash="10000", shares=0, lot=10):
        self.instrument = Instrument("share-uid", "SBER", "TQBR", "Share", "rub", lot,
                                     "MOEX", True, True, True)
        self.cash = Decimal(cash)
        self.shares = shares
        self.price = Decimal("100")
        self.candles = [Candle(datetime(2026, 9, day, tzinfo=timezone.utc), Decimal(value),
                               Decimal(value), Decimal(value), Decimal(value), 100)
                        for day, value in ((28, "90"), (29, "95"), (30, "100"))]
        self.posts = []
        self.reconciliations = []
        self.orders = {}
        self.timeout_after_submit = False
        self.state_unavailable = False
        self.response_status = "EXECUTION_REPORT_STATUS_FILL"
        self.external_orders = []
        self.extra_securities = []
        self.extra_money = []
        self.blocked_cash = []
        self.portfolio_cash_entry = False

    def resolve_share(self, ticker, class_code="TQBR"):
        return self.instrument

    def get_daily_candles(self, uid, start, end):
        return self.candles

    def get_last_price(self, uid):
        return self.price

    def get_sandbox_portfolio(self, account):
        positions = []
        if self.portfolio_cash_entry:
            positions.append({"figi": "RUB000UTSTOM", "instrumentType": "currency",
                              "quantity": quotation(self.cash), "currentPrice": rub("1")})
        if self.shares:
            positions.append({"instrumentUid": "share-uid", "instrumentType": "share",
                              "quantity": quotation(Decimal(self.shares)),
                              "currentPrice": rub(self.price)})
        return {"totalAmountPortfolio": rub(self.cash + self.shares * self.price),
                "positions": positions}

    def get_sandbox_positions(self, account):
        securities = list(self.extra_securities)
        if self.shares:
            securities.append({"instrumentUid": "share-uid", "balance": str(self.shares),
                               "blocked": "0"})
        return {"money": [rub(self.cash), *self.extra_money], "blocked": self.blocked_cash,
                "securities": securities}

    def get_sandbox_orders(self, account):
        return self.external_orders + [value for value in self.orders.values()
                                      if value["executionReportStatus"] in
                                      ("EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_PARTIALLYFILL")]

    def get_sandbox_order_state(self, account, order_id):
        self.reconciliations.append(order_id)
        if self.state_unavailable:
            raise ApiError("State unavailable")
        return self.orders[order_id]

    def post_sandbox_order(self, account, uid, lots, direction, order_id):
        self.posts.append((account, uid, lots, direction, order_id))
        executed = lots if self.response_status == "EXECUTION_REPORT_STATUS_FILL" else 0
        report = {"orderId": order_id, "lotsRequested": str(lots),
                  "orderRequestId": order_id, "instrumentUid": uid,
                  "lotsExecuted": str(executed), "direction": "ORDER_DIRECTION_" + direction,
                  "executionReportStatus": self.response_status}
        self.orders[order_id] = report
        if executed:
            quantity = lots * self.instrument.lot
            cost = self.price * quantity
            if direction == "BUY":
                self.shares += quantity
                self.cash -= cost * Decimal("1.0005")
            else:
                self.shares -= quantity
                self.cash += cost * Decimal("0.9995")
        if self.timeout_after_submit:
            self.timeout_after_submit = False
            raise ApiError("Timeout")
        return report


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.now = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
        self.client = FakeClient()

    def step(self, *, client=None, **kwargs):
        return run_step(client or self.client, "account-one", "SBER", state_path=self.path,
                        fast=1, slow=2, now=self.now, **kwargs)

    def stored(self):
        with sqlite3.connect(self.path) as connection:
            return json.loads(connection.execute("SELECT data FROM bot_state WHERE id=1").fetchone()[0])

    def test_dry_run_does_not_consume_signal_or_trade(self):
        plan = self.step()
        self.assertEqual((plan["action"], plan["lots"]), ("buy", 1))
        self.assertTrue(plan["dry_run"])
        self.assertEqual(self.client.posts, [])
        self.assertIsNone(self.stored()["handled_signal"])
        result = self.step(submit=True)
        self.assertEqual(result["lots_executed"], 1)
        self.assertEqual(len(self.client.posts), 1)
        self.assertIsNotNone(self.stored()["handled_signal"])

    def test_commission_reserve_prevents_spending_all_cash(self):
        client = FakeClient(cash="100", lot=1)
        result = self.step(client=client, max_allocation=Decimal(1), submit=True)
        self.assertEqual(result["reason"], "insufficient_funds_for_lot")
        self.assertEqual(client.posts, [])

    def test_zero_cash_protobuf_omits_money_but_can_exit_shares(self):
        client = FakeClient(cash='0', shares=100)
        client.candles = [Candle(datetime(2026, 9, day, tzinfo=timezone.utc), Decimal(value),
                                Decimal(value), Decimal(value), Decimal(value), 100)
                          for day, value in ((28, '100'), (29, '95'), (30, '90'))]
        original_positions = client.get_sandbox_positions
        def positions_without_empty_money(account):
            result = original_positions(account)
            result.pop('money')
            return result
        client.get_sandbox_positions = positions_without_empty_money
        result = self.step(client=client, submit=True)
        self.assertEqual((result['action'], result['lots_executed']), ('sell', 10))
        self.assertEqual(client.shares, 0)

    def test_allocation_too_small_for_one_lot(self):
        client = FakeClient(cash="100", lot=10)
        result = self.step(client=client, submit=True)
        self.assertEqual(result["reason"], "insufficient_allocation_for_lot")
        self.assertEqual(client.posts, [])

    def test_duplicate_run_never_repeats_executed_candle(self):
        self.step(submit=True)
        result = self.step(submit=True)
        self.assertEqual(result["reason"], "signal_already_handled")
        self.assertEqual(len(self.client.posts), 1)

    def test_pending_request_is_durable_before_network_submission(self):
        original = self.client.post_sandbox_order

        def inspect_durable_state(account, uid, lots, direction, order_id):
            state = self.stored()
            self.assertEqual(state["pending"]["order_id"], order_id)
            self.assertIsNone(state["handled_signal"])
            return original(account, uid, lots, direction, order_id)

        with patch.object(self.client, "post_sandbox_order", side_effect=inspect_durable_state):
            self.step(submit=True)
        self.assertEqual(len(self.client.posts), 1)

    def test_concurrent_runs_share_one_transaction_lock(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.step(submit=True), range(2)))
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual({result["reason"] for result in results},
                         {"rebalance_buy", "signal_already_handled"})

    def test_existing_position_is_held_without_daily_topup_or_trim(self):
        self.client.shares = 10
        self.assertEqual(self.step(submit=True)["reason"], "at_target")
        self.client.price = Decimal("1000")
        self.assertEqual(self.step(submit=True)["reason"], "at_target")
        self.assertEqual(self.client.posts, [])

    def test_timeout_reconciles_before_another_order(self):
        self.client.timeout_after_submit = True
        first = self.step(submit=True)
        self.assertEqual(first["reason"], "submission_uncertain")
        self.assertEqual(self.stored()["pending"]["order_id"], first["order_id"])
        second = self.step(submit=True)
        self.assertEqual(second["reason"], "pending_order_executed")
        self.assertEqual(self.client.reconciliations, [first["order_id"]])
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.step(submit=True)["reason"], "signal_already_handled")

    def test_unresolved_request_never_retries(self):
        self.client.timeout_after_submit = True
        self.step(submit=True)
        self.client.state_unavailable = True
        self.assertEqual(self.step(submit=True)["reason"], "pending_order_uncertain")
        self.assertEqual(self.step(submit=True)["reason"], "pending_order_uncertain")
        self.assertEqual(len(self.client.posts), 1)

    def test_active_orders_reconcile_by_request_uuid_when_lookup_unavailable(self):
        self.client.response_status = "EXECUTION_REPORT_STATUS_NEW"
        first = self.step(submit=True)
        self.client.orders[first["order_id"]]["orderId"] = "broker-order-id"
        self.client.state_unavailable = True
        result = self.step(submit=True)
        self.assertEqual(result["reason"], "pending_order_open")
        self.assertEqual(result["order_id"], first["order_id"])
        self.assertEqual(len(self.client.posts), 1)

    def test_dry_run_reconciles_pending_without_submitting(self):
        self.client.timeout_after_submit = True
        self.step(submit=True)
        result = self.step()
        self.assertEqual(result["reason"], "pending_order_executed")
        self.assertEqual(len(self.client.posts), 1)

    def test_rejected_order_is_classified_and_not_repeated_same_candle(self):
        self.client.response_status = "EXECUTION_REPORT_STATUS_REJECTED"
        result = self.step(submit=True)
        self.assertEqual(result["reason"], "order_rejected")
        self.assertIsNone(self.stored()["handled_signal"])
        self.assertEqual(self.step(submit=True)["reason"], "signal_order_failed")
        self.assertEqual(len(self.client.posts), 1)

    def test_cancelled_unexecuted_order_is_classified_without_claiming_trade(self):
        self.client.response_status = "EXECUTION_REPORT_STATUS_CANCELLED"
        result = self.step(submit=True)
        self.assertEqual((result["action"], result["reason"]), ("hold", "order_cancelled"))
        self.assertEqual(result["planned_action"], "buy")
        self.assertIsNone(self.stored()["handled_signal"])
        self.assertEqual(self.step(submit=True)["reason"], "signal_order_failed")
        self.assertEqual(len(self.client.posts), 1)

    def test_partial_open_then_cancelled_execution_does_not_duplicate(self):
        self.client.response_status = "EXECUTION_REPORT_STATUS_PARTIALLYFILL"
        first = self.step(submit=True)
        self.assertEqual(self.step(submit=True)["reason"], "pending_order_open")
        report = self.client.orders[first["order_id"]]
        report["lotsExecuted"] = "1"
        report["executionReportStatus"] = "EXECUTION_REPORT_STATUS_CANCELLED"
        self.assertEqual(self.step(submit=True)["reason"], "pending_order_executed")
        self.assertEqual(self.step(submit=True)["reason"], "signal_already_handled")
        self.assertEqual(len(self.client.posts), 1)

    def test_drawdown_exits_even_when_signal_already_executed(self):
        self.step(submit=True, max_drawdown=Decimal("0.01"))
        self.client.price = Decimal("50")
        exit_order = self.step(submit=True, max_drawdown=Decimal("0.01"))
        self.assertEqual((exit_order["action"], exit_order["reason"]), ("sell", "drawdown_exit"))
        self.assertTrue(exit_order["halted"])
        self.assertEqual(self.client.shares, 0)
        self.client.cash = Decimal("20000")
        result = self.step(submit=True, max_drawdown=Decimal("0.01"))
        self.assertEqual(result["reason"], "risk_halt")
        self.assertEqual(len(self.client.posts), 2)

    def test_dry_run_observes_risk_but_does_not_consume_exit(self):
        self.client.shares = 10
        self.step(max_drawdown=Decimal("0.01"))
        self.client.price = Decimal("50")
        plan = self.step(max_drawdown=Decimal("0.01"))
        self.assertEqual(plan["reason"], "drawdown_exit")
        self.assertEqual(self.client.posts, [])
        self.assertIsNone(self.stored()["risk_handled_signal"])
        executed = self.step(submit=True, max_drawdown=Decimal("0.01"))
        self.assertEqual(executed["lots_executed"], 1)

    def test_state_cannot_be_reused_for_other_account_or_config(self):
        self.step()
        with self.assertRaisesRegex(ValueError, "different account"):
            run_step(self.client, "account-two", "SBER", state_path=self.path,
                     fast=1, slow=2, now=self.now)
        with self.assertRaisesRegex(ValueError, "different account"):
            self.step(max_allocation=Decimal("0.3"))
        self.assertEqual(self.client.posts, [])

    def test_foreign_currency_other_holdings_and_blocked_cash_fail_closed(self):
        bad_balances = [("extra_money", [{"currency": "usd", "units": "1", "nano": 0}]),
                        ("extra_securities", [{"instrumentUid": "other", "balance": "1", "blocked": "0"}]),
                        ("blocked_cash", [rub("1")])]
        for name, values in bad_balances:
            with self.subTest(name=name):
                client = FakeClient()
                setattr(client, name, values)
                with self.assertRaises(ValueError):
                    self.step(client=client, submit=True)
                self.assertEqual(client.posts, [])

    def test_standard_rub_cash_portfolio_position_is_allowed(self):
        self.client.portfolio_cash_entry = True
        result = self.step(submit=True)
        self.assertEqual(result["lots_executed"], 1)

    def test_currency_valued_in_rub_is_not_assumed_to_be_rub_cash(self):
        original = self.client.get_sandbox_portfolio

        def foreign_portfolio(account):
            result = original(account)
            result["positions"].append({"figi": "USD000UTSTOM", "instrumentType": "currency",
                                        "quantity": quotation(Decimal(1)), "currentPrice": rub("100")})
            return result

        with patch.object(self.client, "get_sandbox_portfolio", side_effect=foreign_portfolio):
            with self.assertRaisesRegex(ValueError, "foreign"):
                self.step(submit=True)
        self.assertEqual(self.client.posts, [])

    def test_inconsistent_snapshots_do_not_trade(self):
        original = self.client.get_sandbox_portfolio

        def inconsistent_portfolio(account):
            result = original(account)
            result["totalAmountPortfolio"] = rub("20000")
            return result

        with patch.object(self.client, "get_sandbox_portfolio", side_effect=inconsistent_portfolio):
            with self.assertRaisesRegex(ValueError, "inconsistent"):
                self.step(submit=True)
        self.assertEqual(self.client.posts, [])

    def test_open_external_order_fails_closed(self):
        self.client.external_orders = [{"orderId": "manual-order"}]
        with self.assertRaisesRegex(ValueError, "open orders"):
            self.step(submit=True)
        self.assertEqual(self.client.posts, [])

    def test_current_day_is_excluded_and_stale_candles_do_not_trade(self):
        future = Candle(datetime(2026, 10, 1, tzinfo=timezone.utc), Decimal("1"),
                        Decimal("1"), Decimal("1"), Decimal("1"), 100)
        self.client.candles.append(future)
        result = self.step()
        self.assertEqual(result["action"], "buy")
        self.assertTrue(result["signal_time"].startswith("2026-09-30"))
        self.now = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
        self.assertEqual(self.step(submit=True)["reason"], "stale_signal_candle")
        self.assertEqual(self.client.posts, [])

    def test_zero_volume_candle_does_not_replace_latest_trade_signal(self):
        self.now = datetime(2026, 10, 2, 10, tzinfo=timezone.utc)
        self.client.candles.append(Candle(datetime(2026, 10, 1, tzinfo=timezone.utc),
                                          Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), 0))
        result = self.step()
        self.assertEqual(result["action"], "buy")
        self.assertTrue(result["signal_time"].startswith("2026-09-30"))

    def test_weekend_guard_blocks_virtual_submission(self):
        self.now = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
        result = self.step(submit=True)
        self.assertEqual(result["reason"], "weekend_calendar_guard")
        self.assertEqual(result["planned_action"], "buy")
        self.assertEqual(self.client.posts, [])

    def test_now_requires_timezone(self):
        self.now = datetime(2026, 10, 1)
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.step()


if __name__ == "__main__":
    unittest.main()
