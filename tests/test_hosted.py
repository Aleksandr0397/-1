from contextlib import redirect_stderr
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
import unittest
from unittest.mock import Mock, patch

from moex_bot.hosted import HostedConfig, HostedControl, HostedServer
from moex_bot.models import Candle, Instrument
from moex_bot.sandbox import run_step
from moex_bot.tbank import ApiError, quotation


CONTROL_SECRET = "0123456789abcdef" * 4
BROKER_SECRET = "sandbox-test-private-broker-token"


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
                for private in (CONTROL_SECRET, BROKER_SECRET, "SBER", "100000", "account", str(self.control.state_dir)):
                    self.assertNotIn(private, body)
        self.assertIsNone(self.stored())
        self.factory.assert_not_called()

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
