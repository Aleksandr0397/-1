from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from moex_bot.models import Candle, Instrument
from moex_bot.monitor import MonitorError, collect
from moex_bot.tbank import quotation


def rub(amount):
    return {"currency": "rub", **quotation(Decimal(amount))}


class ReadOnlyClient:
    """Broker read boundary; any attempted broker write fails the test."""

    def __init__(self):
        self.account = "private-account-id"
        self.accounts = [self.account, "another-private-account"]
        self.instrument = Instrument("private-instrument-uid", "SBER", "TQBR", "Private name", "rub", 10,
                                     "MOEX", True, True, True)
        self.cash = Decimal("10000")
        self.shares = 0
        self.price = Decimal("100")
        self.valuation_price = Decimal("100")
        self.orders = []
        self.extra_money = []
        self.extra_securities = []
        self.blocked = []
        self.calls = []
        end = datetime(2026, 10, 5, tzinfo=timezone.utc)
        self.candles = []
        for index in range(60):
            time = end - timedelta(days=59-index)
            price = Decimal("80") if index < 40 else Decimal("100")
            self.candles.append(Candle(time, price, price, price, price, 100))

    def list_sandbox_accounts(self):
        self.calls.append(("accounts",))
        return self.accounts

    def resolve_share(self, ticker, class_code="TQBR"):
        self.calls.append(("instrument", ticker, class_code))
        return self.instrument

    def get_daily_candles(self, uid, start, end):
        self.calls.append(("candles", uid, start, end))
        return self.candles

    def get_last_price(self, uid):
        self.calls.append(("price", uid))
        return self.price

    def get_sandbox_orders(self, account):
        self.calls.append(("orders", account))
        return self.orders

    def get_sandbox_portfolio(self, account):
        self.calls.append(("portfolio", account))
        positions = []
        if self.shares:
            positions.append({"instrumentUid": self.instrument.uid, "instrumentType": "share",
                              "quantity": quotation(Decimal(self.shares)), "currentPrice": rub(self.valuation_price)})
        return {"totalAmountPortfolio": rub(self.cash + self.shares * self.valuation_price), "positions": positions}

    def get_sandbox_positions(self, account):
        self.calls.append(("positions", account))
        securities = list(self.extra_securities)
        if self.shares:
            securities.append({"instrumentUid": self.instrument.uid, "balance": str(self.shares), "blocked": "0"})
        return {"money": [rub(self.cash), *self.extra_money], "blocked": self.blocked, "securities": securities}

    def open_sandbox_account(self, *args, **kwargs):
        raise AssertionError("Monitoring must never create accounts")

    def sandbox_pay_in(self, *args, **kwargs):
        raise AssertionError("Monitoring must never fund accounts")

    def post_sandbox_order(self, *args, **kwargs):
        raise AssertionError("Monitoring must never submit orders")

    def cancel_sandbox_order(self, *args, **kwargs):
        raise AssertionError("Monitoring must never cancel orders")

    def get_sandbox_order_state(self, *args, **kwargs):
        raise AssertionError("Monitoring must never claim to reconcile execution history")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.client = ReadOnlyClient()
        self.now = datetime(2026, 10, 6, 10, tzinfo=timezone.utc)

    def collect(self, **kwargs):
        return collect(self.client, self.client.account, "SBER", now=kwargs.pop("now", self.now), **kwargs)

    def test_public_contract_and_buy_proposal_use_only_reads(self):
        with patch("sqlite3.connect", side_effect=AssertionError("Monitoring must not create a risk baseline")):
            report = self.collect()
        self.assertEqual(set(report), {"status", "ticker", "execution_mode", "updated_at", "signal_time",
                                      "equity", "cash", "price", "shares", "last_action", "action_reason",
                                      "planned_lots", "chart", "events", "error"})
        self.assertEqual(report["status"], "connected")
        self.assertEqual(report["execution_mode"], "observe_on_demand")
        self.assertEqual((report["equity"], report["cash"], report["shares"], report["price"]),
                         ("10000", "10000", 0, "100"))
        # 2 lots would cost 2001 RUB including the reserve, above a 2000 RUB budget.
        self.assertEqual((report["last_action"], report["planned_lots"]), ("buy", 1))
        self.assertEqual(report["action_reason"], "sma_buy_proposal")
        self.assertEqual(report["events"], [])
        self.assertIsNone(report["error"])
        self.assertEqual({call[0] for call in self.client.calls},
                         {"accounts", "instrument", "candles", "price", "orders", "portfolio", "positions"})
        public = json.dumps(report)
        for private in (self.client.account, self.client.instrument.uid, self.client.instrument.name,
                        self.client.accounts[1]):
            self.assertNotIn(private, public)

    def test_only_explicit_account_is_read_and_must_be_listed(self):
        self.client.accounts = ["unrelated-account"]
        with self.assertRaises(MonitorError) as error:
            self.collect()
        self.assertEqual(error.exception.code, "sandbox_account_not_found")
        self.assertNotIn(self.client.account, str(error.exception))
        self.assertEqual(self.client.calls, [("accounts",)])

    def test_no_automatic_account_selection(self):
        with self.assertRaises(MonitorError):
            collect(self.client, "", "SBER", now=self.now)
        self.assertEqual(self.client.calls, [])

    def test_current_moscow_day_and_zero_volume_are_not_signal_or_chart(self):
        current_moscow_day = datetime(2026, 10, 5, 21, tzinfo=timezone.utc)
        incomplete_day = Candle(current_moscow_day, *(Decimal("999") for _ in range(4)), 500)
        no_volume_time = datetime(2026, 10, 5, 20, tzinfo=timezone.utc)
        no_volume = Candle(no_volume_time, *(Decimal("888") for _ in range(4)), 0)
        self.client.candles.extend([no_volume, incomplete_day])
        report = self.collect()
        self.assertEqual(report["signal_time"], "2026-10-05T00:00:00+00:00")
        self.assertEqual(len(report["chart"]), 60)
        self.assertEqual(report["chart"][-1], {"time": report["signal_time"], "price": "100"})
        self.assertEqual(report["updated_at"], self.now.isoformat())

    def test_chart_never_invents_timestamp_for_latest_quote(self):
        self.client.price = Decimal("123.45")
        report = self.collect()
        self.assertEqual(report["price"], "123.45")
        self.assertTrue(all(point["price"] != "123.45" for point in report["chart"]))
        self.assertTrue(all(point["time"] != report["updated_at"] for point in report["chart"]))

    def test_future_duplicate_and_unsorted_candles_fail_closed(self):
        original = self.client.candles
        future = Candle(self.now + timedelta(days=1), *(Decimal("100") for _ in range(4)), 100)
        for candles in (original + [future], original + [original[-1]], list(reversed(original))):
            with self.subTest(candles=candles[-1].time):
                self.client.candles = candles
                with self.assertRaises(MonitorError) as error:
                    self.collect()
                self.assertEqual(error.exception.code, "invalid_market_data")

    def test_unsupported_account_holdings_fail_with_static_error(self):
        scenarios = (
            ("extra_money", [{"currency": "usd", **quotation(Decimal(1))}]),
            ("extra_securities", [{"instrumentUid": "foreign-private-uid", "balance": "10", "blocked": "0"}]),
            ("blocked", [rub("1")]),
            ("shares", -10),
            ("shares", 1),
        )
        for field, value in scenarios:
            with self.subTest(field=field, value=value):
                self.client = ReadOnlyClient()
                setattr(self.client, field, value)
                with self.assertRaises(MonitorError) as error:
                    self.collect()
                self.assertEqual(error.exception.code, "unsupported_account_holdings")
                self.assertNotIn("private", str(error.exception))

    def test_active_orders_wait_without_echoing_order_data(self):
        self.client.orders = [{"orderId": "private-order-id", "instrumentUid": "private-order-uid"}]
        report = self.collect()
        self.assertEqual((report["last_action"], report["planned_lots"]), ("wait", 0))
        self.assertEqual(report["action_reason"], "active_sandbox_orders")
        self.assertNotIn("private-order", json.dumps(report))

    def test_sma_entry_holds_existing_position_without_extra_buys(self):
        self.client.shares = 10
        report = self.collect()
        self.assertEqual((report["last_action"], report["planned_lots"]), ("hold", 0))
        self.assertEqual(report["action_reason"], "sma_hold_position")

    def test_negative_sma_proposes_full_lot_sale(self):
        self.client.shares = 30
        self.client.candles = [Candle(c.time, *(Decimal("100") if index < 40 else Decimal("80")
                                            for _ in range(4)), c.volume)
                               for index, c in enumerate(self.client.candles)]
        report = self.collect()
        self.assertEqual((report["last_action"], report["planned_lots"]), ("sell", 3))
        self.assertEqual(report["action_reason"], "sma_sell_proposal")

    def test_insufficient_history_and_stale_history_do_not_propose_orders(self):
        self.client.candles = self.client.candles[-10:]
        report = self.collect()
        self.assertEqual((report["last_action"], report["planned_lots"], report["action_reason"]),
                         ("hold", 0, "insufficient_history"))
        report = self.collect(now=self.now + timedelta(days=6))
        self.assertEqual((report["last_action"], report["planned_lots"], report["action_reason"]),
                         ("hold", 0, "stale_signal_candle"))

    def test_last_price_and_metadata_are_validated(self):
        self.client.price = Decimal("NaN")
        with self.assertRaises(MonitorError) as error:
            self.collect()
        self.assertEqual(error.exception.code, "invalid_market_data")
        self.client = ReadOnlyClient()
        self.client.instrument = replace(self.client.instrument, ticker="BROKER-SECRET")
        with self.assertRaises(MonitorError) as error:
            self.collect()
        self.assertEqual(error.exception.code, "invalid_instrument")

    def test_malformed_instrument_is_rejected_without_echoing_fields(self):
        for change in ({"ticker": None}, {"currency": None}, {"lot": True},
                       {"buy_available": "private-token"}):
            with self.subTest(change=change):
                self.client = ReadOnlyClient()
                self.client.instrument = replace(self.client.instrument, **change)
                with self.assertRaises(MonitorError) as error:
                    self.collect()
                self.assertEqual(error.exception.code, "invalid_instrument")

    def test_monitor_operation_lock_error_remains_a_safe_code(self):
        self.assertEqual(MonitorError("operation_in_progress").code, "operation_in_progress")
        self.assertEqual(MonitorError("private-error-token").code, "broker_unavailable")

    def test_failure_does_not_expose_broker_error_details(self):
        def fail():
            raise RuntimeError("broker-secret-token private-account-id")
        self.client.list_sandbox_accounts = fail
        with self.assertRaises(MonitorError) as error:
            self.collect()
        self.assertEqual(error.exception.code, "broker_unavailable")
        self.assertEqual(str(error.exception), "broker_unavailable")
        self.assertIsNone(error.exception.__cause__)

    def test_timezone_required_before_reading_broker(self):
        with self.assertRaises(MonitorError):
            self.collect(now=datetime(2026, 10, 6))
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
