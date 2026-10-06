from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import http.client
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from moex_bot.hosted import HostedConfig, HostedControl, HostedServer
from moex_bot.models import Candle, Instrument
from moex_bot.sandbox import run_step
from moex_bot.tbank import ApiError, quotation


CONTROL_SECRET = "0123456789abcdef" * 4
BROKER_SECRET = "sandbox-test-private-broker-token"
MONITOR_ACCOUNT = "11111111-2222-4333-8444-555555555555"


def rub(amount):
    return {"currency": "rub", **quotation(Decimal(amount))}


class SandboxClient:
    def __init__(self):
        self.calls = []
        self.cash = Decimal("0")
        self.instrument = Instrument("share-uid", "SBER", "TQBR", "Share", "rub", 10,
                                     "MOEX", True, True, True)
        self.fail_open = False
        self.fail_fund = False
        self.fail_market_data = False
        self.funding_balance = None
        self.open_hook = None
        self.funding_hook = None
        self.check_hook = None
        self.orders = {}
        self.posts = []

    def list_sandbox_accounts(self):
        self.calls.append(("accounts",))
        if self.check_hook:
            self.check_hook()
        return ["unrelated-virtual-account"]

    def open_sandbox_account(self):
        self.calls.append(("open",))
        if self.open_hook:
            self.open_hook()
        if self.fail_open:
            raise ApiError(CONTROL_SECRET + BROKER_SECRET)
        return "service-owned-virtual-account"

    def sandbox_pay_in(self, account, amount):
        self.calls.append(("fund", account, amount))
        if self.funding_hook:
            self.funding_hook()
        self.cash += amount
        if self.fail_fund:
            raise ApiError(CONTROL_SECRET + BROKER_SECRET)
        return {"balance": rub(self.cash if self.funding_balance is None else self.funding_balance)}

    def resolve_share(self, ticker, class_code="TQBR"):
        self.calls.append(("resolve", ticker))
        if self.fail_market_data:
            raise ApiError(BROKER_SECRET, status_code=403)
        return self.instrument

    def get_daily_candles(self, uid, start, end):
        self.calls.append(("candles", uid))
        # Completed before the Monday used by the integrated step tests.
        last = datetime(2026, 10, 2, 7, tzinfo=timezone.utc)
        return [Candle(last - timedelta(days=74 - index), Decimal(25 + index),
                       Decimal(25 + index), Decimal(25 + index), Decimal(25 + index), 100)
                for index in range(75)]

    def get_last_price(self, uid):
        self.calls.append(("price", uid))
        return Decimal("100")

    def get_sandbox_orders(self, account):
        self.calls.append(("orders", account))
        return []

    def get_sandbox_portfolio(self, account):
        self.calls.append(("portfolio", account))
        return {"totalAmountPortfolio": rub(self.cash), "positions": []}

    def get_sandbox_positions(self, account):
        self.calls.append(("positions", account))
        return {"money": [rub(self.cash)], "securities": [], "blocked": []}

    def post_sandbox_order(self, account, uid, lots, direction, order_id):
        self.calls.append(("post", account, uid, lots, direction, order_id))
        self.posts.append(self.calls[-1])
        return {"executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
                "lotsRequested": str(lots), "lotsExecuted": str(lots),
                "direction": "ORDER_DIRECTION_" + direction, "instrumentUid": uid}


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = HostedConfig(CONTROL_SECRET, BROKER_SECRET, state_dir=Path(self.temp.name) / "private")
        self.client = SandboxClient()
        self.factory = Mock(return_value=self.client)
        self.resources = []
        self.addCleanup(self.stop_all)
        self.start()

    def start(self):
        self.control = HostedControl(self.config, self.factory)
        self.server = HostedServer(("127.0.0.1", 0), self.control)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.resources.append((self.server, self.control, self.thread))

    def stop_all(self):
        while self.resources:
            server, control, thread = self.resources.pop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            control.close()

    def request(self, path, parameters=None, *, method="POST", authenticated=True, raw=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        request_headers = {"Content-Type": "application/json"}
        if authenticated:
            request_headers["Authorization"] = "Bearer " + CONTROL_SECRET
        if headers:
            request_headers.update(headers)
        body = raw if raw is not None else json.dumps(parameters if parameters is not None else {})
        try:
            connection.request(method, path, body=body if method == "POST" else None, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def api(self, path, parameters=None, **kwargs):
        status, body = self.request(path, parameters, **kwargs)
        return status, json.loads(body)

    def stored(self):
        with sqlite3.connect(self.control.connection_path) as database:
            row = database.execute("SELECT data FROM connection_state WHERE id=1").fetchone()
            return json.loads(row[0]) if row else None

    def test_start_and_public_routes_never_call_broker_or_expose_private_data(self):
        for path in ("/", "/health"):
            with self.subTest(path=path):
                status, body = self.request(path, method="GET", authenticated=False)
                self.assertEqual(status, 200)
                for private in (CONTROL_SECRET, BROKER_SECRET, MONITOR_ACCOUNT, "100000", str(self.control.state_dir)):
                    self.assertNotIn(private, body)
        self.assertIsNone(self.stored())
        self.factory.assert_not_called()

    def enable_monitor(self):
        self.stop_all()
        self.config = replace(self.config, public_monitor_account_id=MONITOR_ACCOUNT)
        self.start()

    def await_monitor(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.control._monitor_lock:
                if not self.control._monitor_running:
                    return self.control._monitor_snapshot
            threading.Event().wait(0.005)
        self.fail("Monitor refresh did not finish")

    def monitor_report(self):
        return {"status": "connected", "ticker": "SBER", "execution_mode": "observe_on_demand",
                "updated_at": "2026-10-06T06:00:00+00:00", "signal_time": "2026-10-05T00:00:00+00:00",
                "equity": "100000", "cash": "100000", "price": "283.10", "shares": 0,
                "last_action": "hold", "action_reason": "at_target", "planned_lots": 0,
                "chart": [{"time": "2026-10-05T00:00:00+00:00", "price": "283"}],
                "events": [], "error": None}

    def test_monitor_is_explicit_opt_in_and_default_status_has_no_broker_calls(self):
        status, report = self.api("/api/status", method="GET", authenticated=False)
        self.assertEqual(status, 200)
        self.assertEqual(report["error"], "monitor_not_configured")
        self.assertIsNone(report["equity"])
        self.factory.assert_not_called()

    def test_monitor_public_get_refreshes_once_without_recreating_trading_state(self):
        self.enable_monitor()
        with patch("moex_bot.hosted.collect", return_value=self.monitor_report()) as collector:
            status, first = self.api("/api/status", method="GET", authenticated=False)
            self.assertEqual(status, 200)
            self.assertIn(first["status"], {"loading", "connected"})
            self.await_monitor()
            for _ in range(3):
                status, report = self.api("/api/status", method="GET", authenticated=False)
                self.assertEqual((status, report["status"], report["equity"]), (200, "connected", "100000"))
                for private in (MONITOR_ACCOUNT, CONTROL_SECRET, BROKER_SECRET, str(self.control.state_dir)):
                    self.assertNotIn(private, json.dumps(report))
            collector.assert_called_once_with(self.client, MONITOR_ACCOUNT, "SBER")
        self.assertIsNone(self.stored())
        self.assertFalse(self.control.trading_path.exists())
        self.assertEqual(len(report["events"]), 1)
        self.assertFalse(self.client.calls)

    def test_monitor_failure_preserves_previous_verified_timestamp_and_redacts_exception(self):
        self.enable_monitor()
        with patch("moex_bot.hosted.collect", return_value=self.monitor_report()):
            self.control.public_status()
            previous = self.await_monitor()
        with self.control._monitor_lock:
            self.control._monitor_checked = float("-inf")
        with patch("moex_bot.hosted.collect", side_effect=ApiError(CONTROL_SECRET + BROKER_SECRET)):
            self.control.public_status()
            report = self.await_monitor()
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["error"], "monitor_refresh_failed")
        self.assertEqual(report["updated_at"], previous["updated_at"])
        self.assertEqual(report["equity"], previous["equity"])
        self.assertNotIn(BROKER_SECRET, json.dumps(report))
        self.assertEqual(len(report["events"]), 2)

    def test_monitor_does_not_overlap_sensitive_broker_operations(self):
        self.enable_monitor()
        self.control._requests.acquire()
        try:
            self.control.public_status()
            report = self.await_monitor()
        finally:
            self.control._requests.release()
        self.assertEqual(report["error"], "operation_in_progress")
        self.factory.assert_not_called()

    def test_existing_monitor_account_prevents_new_account_after_state_loss(self):
        self.enable_monitor()
        status, report = self.api("/api/connect")
        self.assertEqual((status, report["error"]), (409, "configured_monitor_account_readonly"))
        self.assertIsNone(self.stored())
        self.factory.assert_not_called()

    def test_monitor_account_configuration_requires_canonical_uuid(self):
        for account in ("other-account", "../secret", 42, "ABCDEFAB-2222-4333-8444-555555555555"):
            with self.subTest(account=account), self.assertRaises(ValueError):
                replace(self.config, public_monitor_account_id=account)

    def initial_checkpoint(self):
        return {"action": "hold", "reason": "at_target", "lots": 0, "submit": True,
                "shares": 0, "halted": False, "equity": "100000", "cash": "100000",
                "high_water": "100000", "drawdown": "0", "price": "100",
                "signal_time": "2026-10-05T00:00:00+00:00"}

    def restored_state(self):
        return {"version": 1, "identity": {"account_id": MONITOR_ACCOUNT, "ticker": "SBER",
                "uid": "share-uid", "lot": 10, "strategy": "sma-entry-hold-v1", "fast": 20,
                "slow": 60, "max_allocation": "0.2", "max_drawdown": "0.1", "commission": "0.0005"},
                "high_water": "100000", "halted": False, "pending": None,
                "handled_signal": None, "risk_handled_signal": None,
                "failed_signal": None, "risk_failed_signal": None}

    def restore_parameters(self):
        return {"created_at": "2026-10-06T05:52:37+00:00", "checkpoint": self.initial_checkpoint()}

    def test_restore_is_authorized_explicit_and_only_accepts_initial_untraded_checkpoint(self):
        self.enable_monitor()
        status, _ = self.api("/api/restore", self.restore_parameters(), authenticated=False)
        self.assertEqual(status, 401)
        self.factory.assert_not_called()
        for field, value in (("shares", 10), ("halted", True), ("cash", "99999"),
                             ("high_water", "100001"), ("order_id", "old-order")):
            parameters = self.restore_parameters()
            parameters["checkpoint"][field] = value
            status, report = self.api("/api/restore", parameters)
            self.assertEqual((status, report["error"]), (400, "invalid_restore_checkpoint"))
        self.factory.assert_not_called()

    def test_restore_writes_complete_state_once_and_never_resets_existing_history(self):
        self.enable_monitor()
        with patch("moex_bot.hosted.restore_cash_account", return_value=self.restored_state()) as restore:
            status, report = self.api("/api/restore", self.restore_parameters())
            self.assertEqual((status, report["restored"]), (200, True))
            self.assertTrue(restore.call_args.kwargs["prior_checkpoint_trusted"])
            state = self.stored()
            self.assertTrue(state["trading_state_started"])
            self.assertEqual(self.control._trading_state(state), self.restored_state())
            status, report = self.api("/api/restore", self.restore_parameters())
            self.assertEqual((status, report["error"]), (409, "restore_requires_empty_local_state"))
            restore.assert_called_once()
        self.assertFalse(self.client.calls)

    def test_interrupted_restore_blocks_trade_and_future_restore(self):
        self.enable_monitor()
        with patch("moex_bot.hosted.restore_cash_account", return_value={"bad": "state"}):
            status, _ = self.api("/api/restore", self.restore_parameters())
            self.assertEqual(status, 409)
        self.assertEqual(self.stored()["stage"], "restoring")
        self.factory.reset_mock()
        for path, parameters in (("/api/step", {"submit": True}), ("/api/restore", self.restore_parameters())):
            status, _ = self.api(path, parameters)
            self.assertEqual(status, 409)
        self.factory.assert_not_called()

    def test_automatic_start_halts_missing_state_before_any_broker_request(self):
        self.stop_all()
        self.config = replace(self.config, public_monitor_account_id=MONITOR_ACCOUNT, auto_trade=True)
        self.start()
        self.assertTrue(self.control.start_auto())
        deadline = time.monotonic() + 2
        while self.control.automatic.public_status()["status"] != "error" and time.monotonic() < deadline:
            threading.Event().wait(.005)
        report = self.control.automatic.public_status()
        self.assertEqual((report["status"], report["error"]), ("error", "automatic_state_not_ready"))
        self.factory.assert_not_called()
        self.assertFalse(self.control.trading_path.exists())

    def test_automatic_tick_uses_restored_state_and_requires_submission_explicitly(self):
        self.stop_all()
        self.config = replace(self.config, public_monitor_account_id=MONITOR_ACCOUNT, auto_trade=True)
        self.start()
        with patch("moex_bot.hosted.restore_cash_account", return_value=self.restored_state()):
            self.assertEqual(self.api("/api/restore", self.restore_parameters())[0], 200)
        result = {"action": "hold", "reason": "at_target", "lots": 0}
        with patch("moex_bot.hosted.run_step", return_value=result) as step:
            self.control.start_auto()
            deadline = time.monotonic() + 2
            while self.control.automatic.public_status()["last_result"] is None and time.monotonic() < deadline:
                threading.Event().wait(.005)
            self.assertEqual(self.control.automatic.public_status()["last_result"]["action"], "hold")
            step.assert_called_once_with(self.client, MONITOR_ACCOUNT, "SBER", state_path=self.control.trading_path, submit=True)
        self.control.automatic.stop()
        status = self.control.public_status()
        self.assertEqual(status["execution_mode"], "sandbox_auto")
        self.assertTrue(status["automation"]["enabled"])
        self.assertNotIn(MONITOR_ACCOUNT, json.dumps(status))

    def test_automatic_private_resume_revalidates_state_and_stop_remains_authorized(self):
        self.enable_monitor()
        for path in ("/api/auto-resume", "/api/auto-stop"):
            self.assertEqual(self.api(path, authenticated=False)[0], 401)
            self.assertEqual(self.api(path, {"account_id": MONITOR_ACCOUNT})[0], 400)
        self.assertEqual(self.api("/api/auto-resume")[0], 409)
        self.factory.assert_not_called()

    def test_automatic_rejects_future_saved_signal_before_broker_access(self):
        self.enable_monitor()
        with patch("moex_bot.hosted.restore_cash_account", return_value=self.restored_state()):
            self.assertEqual(self.api("/api/restore", self.restore_parameters())[0], 200)
        state = self.restored_state()
        state["handled_signal"] = "2050-01-01T00:00:00+00:00"
        with sqlite3.connect(self.control.trading_path) as database:
            database.execute("UPDATE bot_state SET data=? WHERE id=1", (json.dumps(state),))
        self.factory.reset_mock()
        with self.assertRaises(Exception) as error:
            self.control._automatic_step()
        self.assertEqual(error.exception.code, "trading_state_invalid_manual_review_required")
        self.factory.assert_not_called()

    def test_shutdown_drains_manual_request_and_rejects_new_work_before_releasing_ownership(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_check():
            entered.set()
            release.wait(2)
        self.client.check_hook = blocked_check
        caller = threading.Thread(target=lambda: self.api("/api/check"))
        caller.start()
        self.assertTrue(entered.wait(1))
        closing = threading.Thread(target=self.control.close)
        closing.start()
        deadline = time.monotonic() + 1
        while not self.control._closed and time.monotonic() < deadline:
            threading.Event().wait(.005)
        try:
            self.assertTrue(closing.is_alive())
            self.assertFalse(self.control.start_auto())
            for path in ("/api/check", "/api/auto-resume"):
                self.assertEqual(self.api(path)[0], 503)
            with self.assertRaises(sqlite3.OperationalError):
                HostedControl(self.config, self.factory)
        finally:
            release.set()
            caller.join(2)
            closing.join(2)
        self.assertFalse(closing.is_alive())
        self.control.close()  # Idempotent after the drain is complete.

    def test_restore_history_diagnostics_are_private_readonly_and_omit_identifiers(self):
        self.enable_monitor()
        self.client.get_sandbox_operations = Mock(return_value=[{
            "operationType": "OPERATION_TYPE_INPUT", "state": "OPERATION_STATE_EXECUTED",
            "payment": rub("100000"), "price": rub("0"), "quantity": "0", "quantityRest": "0",
            "instrumentType": "currency", "figi": "RUB000UTSTOM", "instrumentUid": BROKER_SECRET,
            "id": MONITOR_ACCOUNT, "description": CONTROL_SECRET, "trades": [], "childOperations": []}])
        parameters = {"created_at": "2026-10-06T05:52:37+00:00"}
        self.assertEqual(self.api("/api/restore-history", parameters, authenticated=False)[0], 401)
        self.client.get_sandbox_operations.assert_not_called()
        status, report = self.api("/api/restore-history", parameters)
        self.assertEqual((status, report["operation_count"]), (200, 1))
        self.assertEqual(report["operations"][0]["payment"]["value"], "100000")
        for private in (MONITOR_ACCOUNT, CONTROL_SECRET, BROKER_SECRET):
            self.assertNotIn(private, json.dumps(report))
        self.assertEqual(self.client.calls, [])
        self.assertIsNone(self.stored())

    def test_unauthenticated_sensitive_operations_fail_before_network(self):
        for path in ("/api/check", "/api/connect", "/api/step"):
            with self.subTest(path=path):
                status, report = self.api(path, {"submit": True}, authenticated=False)
                self.assertEqual((status, report), (401, {"error": "unauthorized"}))
                status, _ = self.request(path, method="GET", authenticated=False)
                self.assertEqual(status, 405)
        self.assertIsNone(self.stored())
        self.factory.assert_not_called()

    def test_wrong_and_non_ascii_secrets_are_rejected(self):
        for authorization in ("Bearer " + BROKER_SECRET, "Bearer " + CONTROL_SECRET + "x", "Basic " + CONTROL_SECRET):
            status, _ = self.api("/api/connect", headers={"Authorization": authorization})
            self.assertEqual(status, 401)
        self.assertFalse(self.control.authorized("Bearer é"))
        self.factory.assert_not_called()

    def test_invalid_parameters_and_nonboolean_submission_fail_before_network(self):
        cases = [("/api/check", {"ticker": "GAZP"}), ("/api/check", {"market_data": 1}),
                 ("/api/connect", {"cash": "200000"}), ("/api/connect", {"account": "other"}),
                 ("/api/step", {}), ("/api/step", {"submit": "false"}),
                 ("/api/step", {"submit": 1}), ("/api/step", {"submit": None}),
                 ("/api/step", {"submit": True, "account": "other"}),
                 ("/api/step", {"submit": True, "state": "elsewhere"}),
                 ("/api/step", {"submit": True, "url": "https://example.com"})]
        for path, parameters in cases:
            with self.subTest(path=path, parameters=parameters):
                status, _ = self.api(path, parameters)
                self.assertEqual(status, 400)
        self.factory.assert_not_called()

    def test_json_input_is_bounded_and_rejects_duplicates(self):
        for raw, expected in ((" " * 5000, 413), ('{"submit":false,"submit":true}', 400),
                              ('{"submit":NaN}', 400), ("[]", 400), ("bad json", 400)):
            with self.subTest(raw=raw[:40]):
                status, _ = self.api("/api/step", raw=raw)
                self.assertEqual(status, expected)
        self.factory.assert_not_called()

    def test_readonly_check_calls_only_accounts_by_default(self):
        status, report = self.api("/api/check")
        self.assertEqual(status, 200)
        self.assertTrue(report["connected"])
        self.assertTrue(report["sandbox_authenticated"])
        self.assertFalse(report["market_data_checked"])
        self.assertEqual(report["sandbox_account_count"], 1)
        self.assertEqual(self.client.calls, [("accounts",)])
        self.assertNotIn("unrelated-virtual-account", json.dumps(report))
        self.assertIsNone(self.stored())

    def test_optional_market_data_check_reports_sandbox_auth_separately(self):
        self.client.fail_market_data = True
        status, report = self.api("/api/check", {"market_data": True})
        self.assertEqual(status, 502)
        self.assertTrue(report["sandbox_authenticated"])
        self.assertFalse(report["connected"])
        self.assertFalse(report["market_data_authorized"])
        self.assertEqual(report["failed_market_data_stage"], "resolve_share")
        self.assertEqual(report["broker_status_code"], 403)
        self.assertIsNone(report["broker_reason"])
        self.assertNotIn(BROKER_SECRET, json.dumps(report))
        self.assertEqual(self.client.calls, [("accounts",), ("resolve", "SBER")])

    def test_check_exposes_only_known_safe_broker_diagnostics(self):
        for reason, expected in (("proxy_tls_certificate", "proxy_tls_certificate"),
                                 ("broker_tls_certificate", "broker_tls_certificate"),
                                 (BROKER_SECRET, None)):
            with self.subTest(reason=reason):
                def failed_check():
                    raise ApiError(CONTROL_SECRET + BROKER_SECRET, status_code=503, reason=reason)

                self.client.check_hook = failed_check
                status, report = self.api("/api/check")
                self.assertEqual(status, 502)
                self.assertFalse(report["connected"])
                self.assertFalse(report["sandbox_authenticated"])
                self.assertEqual(report["broker_status_code"], 503)
                self.assertEqual(report["broker_reason"], expected)
                self.assertNotIn(CONTROL_SECRET, json.dumps(report))
                self.assertNotIn(BROKER_SECRET, json.dumps(report))

    def test_optional_market_data_check_is_readonly(self):
        status, report = self.api("/api/check", {"market_data": True})
        self.assertEqual(status, 200)
        self.assertTrue(report["market_data_authorized"])
        self.assertEqual(report["last_price"], "100")
        self.assertEqual([call[0] for call in self.client.calls], ["accounts", "resolve", "candles", "price"])
        self.assertIsNone(self.stored())

    def test_explicit_connect_creates_dedicated_account_and_funds_exact_capital_once(self):
        stages = []
        self.client.open_hook = lambda: stages.append(self.stored()["stage"])
        self.client.funding_hook = lambda: stages.append(self.stored()["stage"])
        status, report = self.api("/api/connect")
        self.assertEqual(status, 200)
        self.assertEqual(report["sandbox_account"], "service-owned-virtual-account")
        self.assertEqual(report["virtual_cash_rub"], "100000")
        self.assertFalse(report["background_trading"])
        self.assertEqual(stages, ["opening", "funding"])
        self.assertEqual(self.client.calls, [("open",), ("fund", "service-owned-virtual-account", Decimal("100000"))])
        status, again = self.api("/api/connect")
        self.assertEqual((status, again), (200, report))
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.control.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.control.connection_path.stat().st_mode & 0o777, 0o600)
        data = self.control.connection_path.read_bytes()
        self.assertNotIn(CONTROL_SECRET.encode(), data)
        self.assertNotIn(BROKER_SECRET.encode(), data)

    def test_uncertain_creation_survives_restart_and_never_retries(self):
        self.client.fail_open = True
        stderr = StringIO()
        with redirect_stderr(stderr):
            status, body = self.request("/api/connect")
        self.assertEqual(status, 502)
        for private in (CONTROL_SECRET, BROKER_SECRET):
            self.assertNotIn(private, body + stderr.getvalue())
        self.assertEqual(self.stored()["stage"], "opening")
        self.stop_all()
        self.start()
        self.factory.reset_mock()
        self.client.fail_open = False
        status, report = self.api("/api/connect")
        self.assertEqual(status, 409)
        self.assertIn("manual_review", report["error"])
        self.factory.assert_not_called()
        self.assertEqual(self.client.calls, [("open",)])

    def test_uncertain_funding_never_duplicates_deposit(self):
        self.client.fail_fund = True
        self.assertEqual(self.api("/api/connect")[0], 502)
        self.assertEqual(self.stored()["stage"], "funding")
        self.assertEqual(self.client.cash, Decimal("100000"))
        self.client.fail_fund = False
        self.assertEqual(self.api("/api/connect")[0], 409)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.client.cash, Decimal("100000"))

    def test_unconfirmed_funding_balance_does_not_claim_connection(self):
        self.client.funding_balance = Decimal("200000")
        status, report = self.api("/api/connect")
        self.assertEqual(status, 502)
        self.assertFalse(report["connected"])
        self.assertEqual(self.stored()["stage"], "funding")
        self.assertEqual(self.api("/api/connect")[0], 409)

    def test_step_requires_explicit_owned_connection(self):
        status, _ = self.api("/api/step", {"submit": True})
        self.assertEqual(status, 409)
        self.factory.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_step_uses_real_sandbox_engine_fixed_state_and_explicit_submission(self):
        self.assertEqual(self.api("/api/connect")[0], 200)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

        def timed_step(*args, **kwargs):
            return run_step(*args, **kwargs, now=now)

        with patch("moex_bot.hosted.run_step", side_effect=timed_step) as runner:
            status, report = self.api("/api/step", {"submit": False})
            self.assertEqual(status, 200)
            self.assertTrue(report["result"]["dry_run"])
            self.assertEqual(self.client.posts, [])
            status, report = self.api("/api/step", {"submit": True})
            self.assertEqual(status, 200)
            self.assertEqual(report["result"]["order_status"], "EXECUTION_REPORT_STATUS_FILL")
            self.assertEqual(len(self.client.posts), 1)
            self.assertEqual(self.client.posts[0][1], "service-owned-virtual-account")
            self.assertEqual(self.client.posts[0][2], "share-uid")
            self.assertEqual(runner.call_args.kwargs["state_path"], self.control.trading_path)
        self.assertTrue(self.control.trading_path.is_file())
        self.assertEqual(self.control.trading_path.stat().st_mode & 0o777, 0o600)

    def test_lost_started_trading_state_refuses_even_dry_run_after_restart(self):
        self.assertEqual(self.api("/api/connect")[0], 200)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        with patch("moex_bot.hosted.run_step", side_effect=lambda *args, **kwargs: run_step(*args, **kwargs, now=now)):
            self.assertEqual(self.api("/api/step", {"submit": False})[0], 200)
        self.control.trading_path.unlink()
        self.stop_all()
        self.start()
        self.factory.reset_mock()
        status, report = self.api("/api/step", {"submit": False})
        self.assertEqual(status, 409)
        self.assertIn("manual_review", report["error"])
        self.factory.assert_not_called()

    def test_deleted_recreated_or_corrupt_started_state_fails_before_network(self):
        self.assertEqual(self.api("/api/connect")[0], 200)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        with patch("moex_bot.hosted.run_step", side_effect=lambda *args, **kwargs: run_step(*args, **kwargs, now=now)):
            self.assertEqual(self.api("/api/step", {"submit": False})[0], 200)
        original = self.control.trading_path.read_bytes()

        def sql_mutation(query, values=()):
            with sqlite3.connect(self.control.trading_path) as database:
                database.execute(query, values)

        def state_mutation(change):
            with sqlite3.connect(self.control.trading_path) as database:
                row = database.execute("SELECT data FROM bot_state WHERE id=1").fetchone()
                data = json.loads(row[0])
                change(data)
                database.execute("UPDATE bot_state SET data=? WHERE id=1", (json.dumps(data),))

        def recreate_database():
            self.control.trading_path.unlink()
            sql_mutation("CREATE TABLE bot_state(id INTEGER PRIMARY KEY, data TEXT NOT NULL)")

        changes = {
            "zero_byte": lambda: self.control.trading_path.write_bytes(b""),
            "unreadable_database": lambda: self.control.trading_path.write_bytes(b"not a database"),
            "deleted_row": lambda: sql_mutation("DELETE FROM bot_state"),
            "recreated_database": recreate_database,
            "truncated_row": lambda: sql_mutation("UPDATE bot_state SET data=?", ('{"version":1,',)),
            "missing_risk_state": lambda: state_mutation(lambda data: data.pop("halted")),
            "invalid_high_water": lambda: state_mutation(lambda data: data.update(high_water="NaN")),
            "wrong_account": lambda: state_mutation(lambda data: data["identity"].update(account_id="another-account")),
            "changed_instrument": lambda: state_mutation(lambda data: data["identity"].update(uid="another-share")),
            "truncated_pending_order": lambda: state_mutation(lambda data: data.update(pending={"order_id": "missing-order-details"})),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.control.trading_path.write_bytes(original)
                change()
                self.factory.reset_mock()
                self.client.calls.clear()
                status, report = self.api("/api/step", {"submit": True})
                self.assertEqual(status, 409)
                self.assertIn("manual_review", report["error"])
                self.factory.assert_not_called()
                self.assertEqual(self.client.calls, [])
                self.assertEqual(self.client.posts, [])

    def test_first_transient_lookup_failure_can_retry_without_submission_or_duplicate(self):
        self.assertEqual(self.api("/api/connect")[0], 200)
        self.client.fail_market_data = True
        status, _ = self.api("/api/step", {"submit": True})
        self.assertEqual(status, 502)
        self.assertFalse(self.stored()["trading_state_started"])
        self.assertFalse(self.control.trading_path.exists())
        self.assertEqual(self.client.posts, [])
        self.client.fail_market_data = False
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        submits = []

        def timed_step(*args, **kwargs):
            submits.append(kwargs["submit"])
            return run_step(*args, **kwargs, now=now)

        with patch("moex_bot.hosted.run_step", side_effect=timed_step):
            status, report = self.api("/api/step", {"submit": True})
            self.assertEqual(status, 200)
            self.assertEqual(report["result"]["order_status"], "EXECUTION_REPORT_STATUS_FILL")
            self.assertEqual(submits, [False, True])
            self.assertEqual(len(self.client.posts), 1)
            self.assertTrue(self.stored()["trading_state_started"])
            self.assertEqual(self.stored()["trading_identity"]["account_id"], "service-owned-virtual-account")
            status, report = self.api("/api/step", {"submit": True})
            self.assertEqual(status, 200)
            self.assertEqual(report["result"]["reason"], "signal_already_handled")
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(sum(call[0] == "fund" for call in self.client.calls), 1)

    def test_existing_empty_unstarted_database_is_suspicious_before_network(self):
        self.assertEqual(self.api("/api/connect")[0], 200)
        self.control.trading_path.touch(mode=0o600)
        self.factory.reset_mock()
        status, report = self.api("/api/step", {"submit": True})
        self.assertEqual(status, 409)
        self.assertIn("manual_review", report["error"])
        self.assertFalse(self.stored()["trading_state_started"])
        self.factory.assert_not_called()

    def test_lifetime_lock_blocks_second_service_for_same_state(self):
        with self.assertRaises(sqlite3.OperationalError):
            HostedControl(self.config, self.factory)
        self.factory.assert_not_called()

    def test_concurrent_requests_fail_fast_before_second_broker_call(self):
        entered = threading.Event()
        released = threading.Event()

        def wait_check():
            entered.set()
            if not released.wait(timeout=2):
                raise RuntimeError("Test wait timed out")

        self.client.check_hook = wait_check
        first = []
        worker = threading.Thread(target=lambda: first.append(self.api("/api/check")))
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            status, report = self.api("/api/connect")
            self.assertEqual((status, report), (409, {"error": "operation_in_progress"}))
            self.assertEqual(self.client.calls, [("accounts",)])
        finally:
            released.set()
            worker.join(timeout=2)
        self.assertEqual(first[0][0], 200)

    def test_configuration_changes_refuse_existing_account_before_network(self):
        self.assertEqual(self.api("/api/connect")[0], 200)
        self.stop_all()
        self.config = HostedConfig(CONTROL_SECRET, BROKER_SECRET, initial_cash=Decimal("200000"), state_dir=self.config.state_dir)
        self.start()
        self.factory.reset_mock()
        self.assertEqual(self.api("/api/connect")[0], 409)
        self.assertEqual(self.api("/api/step", {"submit": False})[0], 409)
        self.factory.assert_not_called()

    def test_response_redacts_secrets_even_if_downstream_report_contains_them(self):
        self.assertEqual(self.api("/api/connect")[0], 200)
        now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        with patch("moex_bot.hosted.run_step", side_effect=lambda *args, **kwargs: run_step(*args, **kwargs, now=now)):
            self.assertEqual(self.api("/api/step", {"submit": False})[0], 200)
        with patch("moex_bot.hosted.run_step", return_value={"reason": CONTROL_SECRET + ":" + BROKER_SECRET}):
            status, body = self.request("/api/step", {"submit": False})
        self.assertEqual(status, 200)
        self.assertNotIn(CONTROL_SECRET, body)
        self.assertNotIn(BROKER_SECRET, body)
        self.assertIn("[redacted]", body)


class HostedConfigTests(unittest.TestCase):
    def test_invalid_secret_and_capital_are_rejected(self):
        for changes in ({"control_token": "weak"}, {"control_token": "a" * 64},
                        {"sandbox_token": CONTROL_SECRET}, {"sandbox_token": ""},
                        {"initial_cash": Decimal("0")}, {"initial_cash": Decimal("NaN")},
                        {"initial_cash": Decimal("0.0000000001")}, {"ticker": "SBER?x=secret"}):
            with self.subTest(changes=changes):
                values = {"control_token": CONTROL_SECRET, "sandbox_token": BROKER_SECRET, **changes}
                with self.assertRaises(ValueError):
                    HostedConfig(**values)

    def test_environment_is_only_token_source_and_repr_is_private(self):
        with patch.dict(os.environ, {"BOT_CONTROL_TOKEN": CONTROL_SECRET, "TINVEST_SANDBOX_TOKEN": BROKER_SECRET}, clear=True), \
             patch.object(Path, "read_text", side_effect=AssertionError("Token file must not be accessed")):
            config = HostedConfig.from_environment()
        self.assertEqual(config.initial_cash, Decimal("100000"))
        self.assertNotIn(CONTROL_SECRET, repr(config))
        self.assertNotIn(BROKER_SECRET, repr(config))

    def test_module_startup_error_has_no_traceback_or_secret(self):
        environment = {**os.environ, "BOT_CONTROL_TOKEN": CONTROL_SECRET, "TINVEST_SANDBOX_TOKEN": BROKER_SECRET, "PORT": BROKER_SECRET}
        result = subprocess.run([sys.executable, "-m", "moex_bot.hosted"], env=environment,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(CONTROL_SECRET, result.stdout + result.stderr)
        self.assertNotIn(BROKER_SECRET, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
