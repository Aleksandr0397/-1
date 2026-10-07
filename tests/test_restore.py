from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from moex_bot.models import Candle, Instrument
from moex_bot.restore import RestoreError, restore_cash_account
from moex_bot.sandbox import run_step
from moex_bot.tbank import quotation


UTC = timezone.utc
CREATED = datetime(2026, 10, 6, 5, 52, 37, tzinfo=UTC)
NOW = datetime(2026, 10, 6, 8, tzinfo=UTC)


def rub(amount):
    return {"currency": "rub", **quotation(Decimal(amount))}


def pay_in():
    return {"id": "initial-funding", "currency": "rub", "payment": rub("100000"),
            "date": "2026-10-06T05:52:40Z", "state": "OPERATION_STATE_EXECUTED",
            "operationType": "OPERATION_TYPE_INPUT"}


def cash_funding_marker():
    zero_price = {"currency": "", **quotation(Decimal("0"))}
    return {**pay_in(), "date": "2026-10-06T05:52:38.948409Z", "instrumentType": "",
            "figi": "RUB000UTSTOM", "instrumentUid": "rub-cash-uid", "positionUid": "rub-position-uid",
            "quantity": "0", "quantityRest": "0", "price": zero_price,
            "trades": [{"tradeId": "initial-cash-marker", "quantity": "0", "price": zero_price,
                        "dateTime": "2026-10-06T05:52:38.948409Z"}]}


class CashClient:
    def __init__(self):
        self.instrument = Instrument("share-uid", "SBER", "TQBR", "Share", "rub", 1,
                                     "MOEX", True, True, True)
        self.accounts = ["account-one"]
        self.orders = []
        self.portfolio = {"totalAmountPortfolio": rub("100000"), "positions": []}
        self.positions = {"money": [rub("100000")]}
        self.operations = [pay_in()]
        self.reads = []

    def list_sandbox_accounts(self):
        self.reads.append("accounts")
        return deepcopy(self.accounts)

    def resolve_share(self, ticker, class_code="TQBR"):
        self.reads.append(("instrument", ticker, class_code))
        return self.instrument

    def get_sandbox_orders(self, account):
        self.reads.append(("orders", account))
        return deepcopy(self.orders)

    def get_sandbox_portfolio(self, account):
        self.reads.append(("portfolio", account))
        return deepcopy(self.portfolio)

    def get_sandbox_positions(self, account):
        self.reads.append(("positions", account))
        return deepcopy(self.positions)

    def get_sandbox_operations(self, account, start, end):
        self.reads.append(("operations", account, start, end))
        return deepcopy(self.operations)


class RestorationTests(unittest.TestCase):
    def setUp(self):
        self.client = CashClient()

    def restore(self, **kwargs):
        parameters = {"client": self.client, "account_id": "account-one", "ticker": "SBER",
                      "initial_cash": Decimal("100000"), "created_at": CREATED, "now": NOW}
        parameters.update(kwargs)
        return restore_cash_account(**parameters)

    def reject(self, code, **kwargs):
        with self.assertRaises(RestoreError) as caught:
            self.restore(**kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_initial_funding_reconstructs_exact_engine_state_with_reads_only(self):
        self.assertEqual(self.restore(), {
            "version": 1,
            "identity": {"account_id": "account-one", "uid": "share-uid", "ticker": "SBER",
                         "lot": 1, "strategy": "sma-entry-hold-v1", "fast": 20, "slow": 60,
                         "max_allocation": "0.2", "max_drawdown": "0.1", "commission": "0.0005"},
            "high_water": "100000", "halted": False, "pending": None,
            "handled_signal": None, "risk_handled_signal": None,
            "failed_signal": None, "risk_failed_signal": None,
        })
        self.assertEqual(self.client.reads[-1], ("operations", "account-one",
                                                CREATED - timedelta(minutes=1), NOW))

    def test_empty_history_requires_explicit_trusted_prior_checkpoint(self):
        self.client.operations = []
        self.reject("history_not_proven")
        self.assertEqual(self.restore(prior_checkpoint_trusted=True)["high_water"], "100000")

    def test_empty_history_trust_does_not_override_other_proof(self):
        self.client.operations = []
        self.client.orders = [{"orderId": "external-order"}]
        self.reject("active_sandbox_orders", prior_checkpoint_trusted=True)

    def test_trade_payout_cancelled_and_unknown_history_rejected(self):
        for operation_type in ("OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL", "OPERATION_TYPE_OUTPUT",
                               "OPERATION_TYPE_BROKER_FEE", "OPERATION_TYPE_UNSPECIFIED", "new-type"):
            with self.subTest(operation_type=operation_type):
                self.client.operations = [{**pay_in(), "operationType": operation_type}]
                self.reject("history_contains_activity", prior_checkpoint_trusted=True)
        self.client.operations = [{**pay_in(), "state": "OPERATION_STATE_CANCELED"}]
        self.reject("history_contains_activity", prior_checkpoint_trusted=True)

    def test_initial_funding_cannot_hide_security_or_child_trade_fields(self):
        for update in ({"trades": [{"quantity": "1"}]}, {"quantity": "1"}, {"quantityRest": "1"},
                       {"childOperations": [{"instrumentUid": "share-uid", "payment": rub("-1000")}]},
                       {"figi": "security-figi"}, {"instrumentUid": "share-uid"},
                       {"instrumentType": "share"}, {"parentOperationId": "trade-parent"}):
            with self.subTest(update=update):
                self.client.operations = [{**pay_in(), **update}]
                self.reject("history_contains_activity")

    def test_wrong_funding_extra_funding_and_unknown_currency_rejected(self):
        for update in ({"payment": rub("99999")}, {"payment": rub("-100000")},
                       {"currency": "usd"}, {"payment": {**rub("100000"), "currency": "usd"}}):
            with self.subTest(update=update):
                self.client.operations = [{**pay_in(), **update}]
                self.reject("history_contains_activity")
        self.client.operations = [pay_in(), {**pay_in(), "id": "second-funding"}]
        self.reject("history_contains_activity")

    def test_future_out_of_range_and_malformed_operations_rejected(self):
        for update in ({"date": (NOW + timedelta(seconds=1)).isoformat()},
                       {"date": (CREATED - timedelta(minutes=2)).isoformat()},
                       {"date": "2026-10-06T05:52:40"}, {"date": "invalid"},
                       {"id": ""}, {"quantity": True}, {"payment": {"currency": "rub", "units": True}}):
            with self.subTest(update=update):
                self.client.operations = [{**pay_in(), **update}]
                self.reject("history_not_proven")
        for history in (None, {}, ["invalid"]):
            with self.subTest(history=history):
                self.client.operations = history
                self.reject("history_not_proven")

    def test_full_operation_limit_cannot_be_treated_as_complete_history(self):
        self.client.operations = [pay_in() for _ in range(1000)]
        self.reject("history_truncated")

    def test_balances_must_equal_initial_cash_exactly(self):
        for amount in ("99999.999", "100000.001", "110000", "90000"):
            with self.subTest(amount=amount):
                self.client.portfolio = {"totalAmountPortfolio": rub(amount)}
                self.client.positions = {"money": [rub(amount)]}
                self.reject("cash_balance_mismatch")

    def test_open_orders_and_non_cash_holdings_rejected(self):
        self.client.orders = [{}]
        self.reject("active_sandbox_orders")
        self.client.orders = []
        for update in ({"securities": [{"instrumentUid": "share-uid", "balance": "0"}]},
                       {"futures": [{}]}, {"options": [{}]}, {"blocked": [rub("1")]},
                       {"limitsLoadingInProgress": True},
                       {"accountId": "different-account"},
                       {"money": [rub("100000"), {"currency": "usd"}]},
                       {"money": [rub("50000"), rub("50000")]}):
            with self.subTest(update=update):
                self.client.positions = {"money": [rub("100000")], **update}
                self.reject("unsupported_account_holdings")

    def test_portfolio_securities_and_conflicting_cash_values_rejected(self):
        for update in ({"positions": [{"instrumentType": "share", "quantity": {}}]},
                       {"accountId": "different-account"}, {"virtualPositions": [{}]},
                       {"totalAmountShares": rub("1")}, {"totalAmountCurrencies": rub("99999")},
                       {"positions": [{"instrumentType": "currency", "figi": "RUB000UTSTOM",
                                       "quantity": quotation(Decimal("99999.999")), "currentPrice": rub("1")}]}):
            with self.subTest(update=update):
                self.client.portfolio = {"totalAmountPortfolio": rub("100000"), **update}
                self.reject("unsupported_account_holdings")

    def test_exact_rub_portfolio_cash_position_is_supported(self):
        self.client.portfolio["positions"] = [{"instrumentType": "currency", "figi": "RUB000UTSTOM",
                                               "quantity": quotation(Decimal("100000")), "currentPrice": rub("1")}]
        self.assertEqual(self.restore()["high_water"], "100000")
        self.client.portfolio["positions"][0]["blockedLots"] = quotation(Decimal("1"))
        self.reject("unsupported_account_holdings")

    def use_cash_funding_marker(self):
        self.client.portfolio["positions"] = [{"instrumentType": "currency", "figi": "RUB000UTSTOM",
                                               "instrumentUid": "rub-cash-uid", "positionUid": "rub-position-uid",
                                               "quantity": quotation(Decimal("100000")), "currentPrice": rub("1")}]
        self.client.operations = [cash_funding_marker()]

    def test_observed_initial_cash_marker_is_accepted_with_independent_rub_identity(self):
        self.use_cash_funding_marker()
        self.assertEqual(self.restore(prior_checkpoint_trusted=True)["high_water"], "100000")
        for currency in ("", "rub", "RUB"):
            with self.subTest(root_price_currency=currency):
                self.client.operations[0]["price"] = {"currency": currency}
                self.assertEqual(self.restore(prior_checkpoint_trusted=True)["high_water"], "100000")
        self.client.operations[0]["price"] = {}
        self.assertEqual(self.restore(prior_checkpoint_trusted=True)["high_water"], "100000")
        self.reject("history_not_proven")

    def test_cash_marker_requires_both_matching_identifiers_and_rub_figi(self):
        for update in ({"instrumentUid": "share-uid"}, {"positionUid": "another-position"},
                       {"instrumentUid": ""}, {"positionUid": ""}, {"figi": "SBER-figi"},
                       {"assetUid": "another-asset"}, {"parentOperationId": "another-operation"}):
            with self.subTest(update=update):
                self.use_cash_funding_marker()
                self.client.operations[0].update(update)
                self.reject("history_contains_activity", prior_checkpoint_trusted=True)
        for update in ({"instrumentUid": ""}, {"positionUid": ""}):
            with self.subTest(portfolio_update=update):
                self.use_cash_funding_marker()
                self.client.portfolio["positions"][0].update(update)
                self.reject("history_contains_activity", prior_checkpoint_trusted=True)
        self.use_cash_funding_marker()
        self.client.portfolio["positions"] = []
        self.reject("history_contains_activity", prior_checkpoint_trusted=True)
        with self.subTest(rub_cash_uid_matches_selected_share=True):
            self.use_cash_funding_marker()
            self.client.portfolio["positions"][0]["instrumentUid"] = self.client.instrument.uid
            self.client.operations[0]["instrumentUid"] = self.client.instrument.uid
            self.reject("unsupported_account_holdings", prior_checkpoint_trusted=True)

    def test_generic_deposit_cannot_use_the_cash_marker_exception_without_cash_linkage(self):
        self.use_cash_funding_marker()
        marker = self.client.operations[0]["trades"]
        self.client.operations = [{**pay_in(), "trades": marker}]
        self.reject("history_contains_activity", prior_checkpoint_trusted=True)

    def test_cash_marker_nonzero_quantity_price_date_currency_and_multiple_trades_rejected(self):
        for update in ({"quantity": "1"}, {"quantity": "-1"}, {"price": rub("0.000000001")},
                       {"price": rub("-0.000000001")}, {"price": {"currency": "usd"}},
                       {"dateTime": "2026-10-06T05:52:38.948410Z"}):
            with self.subTest(update=update):
                self.use_cash_funding_marker()
                self.client.operations[0]["trades"][0].update(update)
                self.reject("history_contains_activity", prior_checkpoint_trusted=True)
        self.use_cash_funding_marker()
        self.client.operations[0]["trades"] *= 2
        self.reject("history_contains_activity", prior_checkpoint_trusted=True)
        self.use_cash_funding_marker()
        self.client.operations[0]["price"] = {"currency": "usd"}
        self.reject("history_contains_activity", prior_checkpoint_trusted=True)

    def test_cash_marker_malformed_fields_are_not_proof(self):
        for update in ({"quantity": True}, {"quantity": 0.0}, {"price": {"units": True}},
                       {"price": {"currency": False}}, {"dateTime": "bad"},
                       {"dateTime": None}, {"dateTime": "2026-10-06T05:52:38.948409"}):
            with self.subTest(update=update):
                self.use_cash_funding_marker()
                self.client.operations[0]["trades"][0].update(update)
                self.reject("history_not_proven", prior_checkpoint_trusted=True)
        self.use_cash_funding_marker()
        self.client.operations[0]["price"] = {"currency": False}
        self.reject("history_not_proven", prior_checkpoint_trusted=True)

    def test_missing_account_and_changed_instrument_are_rejected(self):
        self.client.accounts = ["different-account"]
        self.reject("sandbox_account_not_found")
        self.client.accounts = ["account-one"]
        for instrument in (Instrument("share-uid", "SBER", "TQBR", "Share", "rub", 10,
                                       "MOEX", True, True, True),
                           Instrument("share-uid", "GAZP", "TQBR", "Share", "rub", 1,
                                      "MOEX", True, True, True)):
            self.client.instrument = instrument
            self.reject("invalid_instrument")

    def test_invalid_and_future_request_rejected_before_broker_reads(self):
        for update in ({"created_at": NOW + timedelta(seconds=1)}, {"now": datetime(2026, 10, 6)},
                       {"created_at": "bad"}, {"ticker": "GAZP"}, {"initial_cash": Decimal("90000")},
                       {"prior_checkpoint_trusted": "true"}, {"account_id": "unsafe account"}):
            with self.subTest(update=update):
                self.reject("invalid_restore_request", **update)
        self.assertEqual(self.client.reads, [])

    def test_iso_creation_time_is_supported(self):
        self.assertEqual(self.restore(created_at=CREATED.isoformat())["high_water"], "100000")

    def test_restored_state_is_accepted_by_real_engine_without_resetting_risk(self):
        state = self.restore()
        candles = []
        for days_ago in range(100, 0, -1):
            time = NOW - timedelta(days=days_ago)
            if time.weekday() < 5:
                price = Decimal(1000 + days_ago)
                candles.append(Candle(time, price, price, price, price, 100))
        self.client.get_daily_candles = lambda *args: candles
        self.client.get_last_price = lambda *args: Decimal("1000")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restored.sqlite"
            with sqlite3.connect(path) as database:
                database.execute("CREATE TABLE bot_state(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
                database.execute("INSERT INTO bot_state(id,data) VALUES(1,?)", (json.dumps(state),))
            result = run_step(self.client, "account-one", "SBER", state_path=path, now=NOW, submit=True)
            with sqlite3.connect(path) as database:
                stored = json.loads(database.execute("SELECT data FROM bot_state WHERE id=1").fetchone()[0])
        self.assertEqual((result["action"], result["reason"]), ("hold", "at_target"))
        self.assertEqual(stored, state)

    def test_broker_failure_and_unknown_error_codes_do_not_expose_credentials(self):
        private = "secret-token-account-one"
        def failed():
            raise ValueError(private)
        self.client.list_sandbox_accounts = failed
        self.reject("broker_unavailable")
        error = RestoreError(private)
        self.assertEqual(str(error), "broker_unavailable")
        self.assertNotIn(private, str(error))


if __name__ == "__main__":
    unittest.main()
