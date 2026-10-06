from datetime import datetime
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from moex_bot.autorun import SandboxAutoRunner


PRIVATE = "private-token-account-order-id"


def step(*, action="hold", reason="at_target", lots=0, **extra):
    return 200, {"sandbox_only": True,
                 "result": {"action": action, "reason": reason, "lots": lots, **extra}}


class ControlFailure(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(PRIVATE)


class AutoRunnerTests(unittest.TestCase):
    def runner(self, callback, **config):
        runner = SandboxAutoRunner(callback, **config)
        self.addCleanup(runner.stop, 1)
        return runner

    def await_status(self, runner, status, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            report = runner.public_status()
            if report["status"] == status:
                return report
            threading.Event().wait(0.001)
        self.fail(f"Runner did not enter {status}: {runner.public_status()}")

    def test_default_disabled_and_constructor_never_starts(self):
        callback = Mock(return_value=step())
        disabled = self.runner(callback)
        self.assertFalse(disabled.start())
        self.assertFalse(disabled.resume())
        self.assertTrue(disabled.stop())
        self.assertEqual(disabled.public_status()["status"], "stopped")
        self.assertFalse(disabled.public_status()["enabled"])
        self.assertEqual(disabled.public_status()["interval_seconds"], 300)
        enabled = self.runner(callback, enabled=True)
        self.assertIsNone(enabled.public_status()["started_at"])
        callback.assert_not_called()

    def test_thread_start_failure_halts_safely_and_can_be_stopped(self):
        callback = Mock(return_value=step())
        runner = self.runner(callback, enabled=True)
        with patch("moex_bot.autorun.threading.Thread.start", side_effect=RuntimeError(PRIVATE)):
            self.assertFalse(runner.start())
        self.assertTrue(runner.stop(1))
        self.assertFalse(runner.start())
        snapshot = runner.public_status()
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["error"], "automatic_execution_failed")
        self.assertIsNone(snapshot["next_run_at"])
        self.assertNotIn(PRIVATE, json.dumps(snapshot))
        callback.assert_not_called()

    def test_runs_immediately_then_again_without_any_http_requests(self):
        first, second = threading.Event(), threading.Event()
        calls = []

        def callback():
            calls.append(time.monotonic())
            (first if len(calls) == 1 else second).set()
            return step()

        runner = self.runner(callback, enabled=True, interval=0.02)
        self.assertTrue(runner.start())
        self.assertTrue(first.wait(1))
        self.assertTrue(second.wait(1))
        self.assertTrue(runner.stop(1))
        self.assertGreaterEqual(calls[1] - calls[0], 0.02)
        snapshot = runner.public_status()
        self.assertEqual(snapshot["status"], "stopped")
        self.assertIsNone(snapshot["next_run_at"])
        for key in ("started_at", "last_started_at", "last_completed_at"):
            self.assertIsNotNone(datetime.fromisoformat(snapshot[key]).utcoffset())

    def test_concurrent_start_is_single_flight_and_stop_drains(self):
        entered, release = threading.Event(), threading.Event()
        calls, starts = [], []

        def callback():
            calls.append(1)
            entered.set()
            release.wait(1)
            return step()

        runner = self.runner(callback, enabled=True, interval=0.01)
        self.addCleanup(release.set)
        starters = [threading.Thread(target=lambda: starts.append(runner.start()))
                    for _ in range(12)]
        for starter in starters:
            starter.start()
        for starter in starters:
            starter.join(1)
            self.assertFalse(starter.is_alive())
        self.assertTrue(entered.wait(1))
        self.assertEqual(sum(starts), 1)
        self.assertEqual(runner.public_status()["status"], "running")
        self.assertFalse(runner.stop(0.04))  # Longer than several tick intervals.
        self.assertFalse(runner.start())
        self.assertEqual(calls, [1])
        release.set()
        self.assertTrue(runner.stop(1))
        self.assertEqual(calls, [1])
        self.assertEqual(runner.public_status()["status"], "stopped")

    def test_stop_interrupts_the_long_interval(self):
        callback = Mock(return_value=step())
        runner = self.runner(callback, enabled=True)
        runner.start()
        self.await_status(runner, "waiting")
        # Initial startup also has a brief waiting status; require completion.
        deadline = time.monotonic() + 1
        while runner.public_status()["last_completed_at"] is None and time.monotonic() < deadline:
            threading.Event().wait(0.001)
        self.assertIsNotNone(runner.public_status()["last_completed_at"])
        before = time.monotonic()
        self.assertTrue(runner.stop(0.5))
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertEqual(callback.call_count, 1)

    def test_interval_begins_after_a_slow_callback_completes(self):
        first, release, second = threading.Event(), threading.Event(), threading.Event()
        calls, completions = [], []

        def callback():
            calls.append(time.monotonic())
            if len(calls) == 1:
                first.set()
                release.wait(1)
                completions.append(time.monotonic())
            else:
                second.set()
            return step()

        runner = self.runner(callback, enabled=True, interval=0.02)
        self.addCleanup(release.set)
        runner.start()
        self.assertTrue(first.wait(1))
        self.assertFalse(second.wait(0.04))
        release.set()
        self.assertTrue(second.wait(1))
        runner.stop(1)
        self.assertGreaterEqual(calls[1] - completions[0], 0.02)

    def test_old_stop_does_not_overwrite_a_concurrently_restarted_worker(self):
        first, first_release = threading.Event(), threading.Event()
        second, second_release = threading.Event(), threading.Event()
        joined, finalize_stop = threading.Event(), threading.Event()
        calls, stop_results = [], []

        def callback():
            calls.append(1)
            if len(calls) == 1:
                first.set()
                first_release.wait(1)
            else:
                second.set()
                second_release.wait(1)
            return step()

        runner = self.runner(callback, enabled=True, interval=0.01)
        self.addCleanup(first_release.set)
        self.addCleanup(second_release.set)
        self.addCleanup(finalize_stop.set)
        runner.start()
        self.assertTrue(first.wait(1))
        old_worker = runner._thread
        real_join = old_worker.join

        def delayed_join(timeout=None):
            # Expose the lifecycle gap between draining the old worker and
            # publishing the stop result, while using real worker threads.
            real_join(timeout)
            joined.set()
            finalize_stop.wait(1)

        with patch.object(old_worker, "join", delayed_join):
            stopper = threading.Thread(target=lambda: stop_results.append(runner.stop(1)))
            stopper.start()
            first_release.set()
            self.assertTrue(joined.wait(1))
            self.assertTrue(runner.start())
            self.assertTrue(second.wait(1))
            finalize_stop.set()
            stopper.join(1)
            self.assertFalse(stopper.is_alive())
        self.assertEqual(stop_results, [False])
        self.assertEqual(runner.public_status()["status"], "running")
        second_release.set()
        self.assertTrue(runner.stop(1))

    def test_status_is_bounded_whitelisted_and_independent(self):
        callback = Mock(return_value=step(
            action="buy", reason="rebalance_buy", lots=7,
            order_status="EXECUTION_REPORT_STATUS_FILL", order_id=PRIVATE,
            account_id=PRIVATE, token=PRIVATE, diagnostics={"secret": PRIVATE},
            error=PRIVATE, message=PRIVATE * 10000))
        runner = self.runner(callback, enabled=True)
        runner.start()
        deadline = time.monotonic() + 1
        while runner.public_status()["last_result"] is None and time.monotonic() < deadline:
            threading.Event().wait(0.001)
        self.assertTrue(runner.stop(1))
        snapshot = runner.public_status()
        self.assertEqual(snapshot["last_result"], {
            "action": "buy", "reason": "rebalance_buy", "lots": 7,
            "order_status": "EXECUTION_REPORT_STATUS_FILL"})
        self.assertNotIn(PRIVATE, json.dumps(snapshot))
        self.assertLess(len(json.dumps(snapshot)), 1200)
        snapshot["last_result"]["lots"] = 99
        self.assertEqual(runner.public_status()["last_result"]["lots"], 7)

    def test_busy_response_and_exception_skip_one_interval(self):
        for first_outcome in ((409, {"error": "operation_in_progress", "private": PRIVATE}),
                              ControlFailure("operation_in_progress")):
            with self.subTest(first_outcome=type(first_outcome).__name__):
                first, second, release = threading.Event(), threading.Event(), threading.Event()
                calls = []

                def callback():
                    calls.append(1)
                    if len(calls) == 1:
                        first.set()
                        if isinstance(first_outcome, Exception):
                            raise first_outcome
                        return first_outcome
                    second.set()
                    release.wait(1)
                    return step()

                runner = self.runner(callback, enabled=True, interval=0.05)
                self.addCleanup(release.set)
                runner.start()
                self.assertTrue(first.wait(1))
                snapshot = self.await_status(runner, "waiting")
                self.assertEqual(snapshot["error"], "operation_in_progress")
                self.assertIsNone(snapshot["last_result"])
                self.assertIsNotNone(snapshot["next_run_at"])
                self.assertTrue(second.wait(1))
                self.assertFalse(runner.stop(0))
                release.set()
                runner.stop(1)
                self.assertEqual(len(calls), 2)
                self.assertEqual(runner.public_status()["last_result"]["reason"], "at_target")

    def test_errors_latch_and_start_does_not_resume_them(self):
        failures = [
            (RuntimeError(PRIVATE), "automatic_execution_failed"),
            (SystemExit(PRIVATE), "automatic_execution_failed"),
            (ControlFailure("trading_state_invalid_manual_review_required"),
             "trading_state_invalid_manual_review_required"),
            ((502, {}), "automatic_execution_failed"),
            ((502, {"error": None}), "automatic_execution_failed"),
            ((409, {"error": PRIVATE}), "automatic_execution_failed"),
            ((200, {"result": {"action": "hold", "reason": "at_target"}}),
             "invalid_execution_result"),
            (step(action=PRIVATE), "invalid_execution_result"),
            (step(reason=PRIVATE), "invalid_execution_result"),
            (step(lots=True), "invalid_execution_result"),
            (step(lots=1_000_000_001), "invalid_execution_result"),
            (step(order_status=PRIVATE), "invalid_execution_result"),
            ([200, {}], "invalid_execution_result"),
            ((200, []), "invalid_execution_result"),
        ]
        for failure, expected in failures:
            with self.subTest(expected=expected, failure_type=type(failure).__name__):
                calls = []

                def callback():
                    calls.append(1)
                    if isinstance(failure, BaseException):
                        raise failure
                    return failure

                runner = self.runner(callback, enabled=True, interval=0.01)
                runner.start()
                snapshot = self.await_status(runner, "error")
                self.assertTrue(runner.stop(1))
                self.assertFalse(runner.start())
                self.assertEqual(calls, [1])
                self.assertEqual(snapshot["error"], expected)
                self.assertIsNone(snapshot["next_run_at"])
                self.assertIsNotNone(snapshot["last_completed_at"])
                self.assertNotIn(PRIVATE, json.dumps(snapshot))

    def test_only_explicit_resume_clears_the_error_latch(self):
        calls, recovered = [], threading.Event()

        def callback():
            calls.append(1)
            if len(calls) == 1:
                raise ControlFailure("state_requires_manual_review")
            recovered.set()
            return step()

        runner = self.runner(callback, enabled=True)
        runner.start()
        self.await_status(runner, "error")
        runner.stop(1)
        self.assertFalse(runner.start())
        self.assertEqual(runner.public_status()["error"], "state_requires_manual_review")
        self.assertTrue(runner.resume())
        self.assertTrue(recovered.wait(1))
        runner.stop(1)
        self.assertEqual(calls, [1, 1])
        self.assertIsNone(runner.public_status()["error"])

    def test_known_strategy_guards_continue_persisted_reconciliation(self):
        for reason in ("submission_uncertain", "pending_order_uncertain", "pending_order_open",
                       "pending_order_failed", "signal_order_failed", "risk_halt", "order_cancelled"):
            with self.subTest(reason=reason):
                calls, reconciled, release = [], threading.Event(), threading.Event()

                def callback():
                    calls.append(1)
                    if len(calls) == 1:
                        return step(action="wait", reason=reason, lots=1, order_id=PRIVATE)
                    reconciled.set()
                    release.wait(1)
                    return step(reason="pending_order_executed", lots=1,
                                order_status="EXECUTION_REPORT_STATUS_FILL")

                runner = self.runner(callback, enabled=True, interval=0.005)
                self.addCleanup(release.set)
                runner.start()
                self.assertTrue(reconciled.wait(1))
                self.assertFalse(runner.stop(0))
                release.set()
                runner.stop(1)
                self.assertEqual(len(calls), 2)
                self.assertIsNone(runner.public_status()["error"])
                self.assertNotIn(PRIVATE, json.dumps(runner.public_status()))

    def test_callback_can_request_stop_without_joining_itself(self):
        callback_stops, completed = [], threading.Event()
        runner = None

        def callback():
            callback_stops.append(runner.stop(0.1))
            completed.set()
            return step()

        runner = self.runner(callback, enabled=True, interval=0.01)
        runner.start()
        self.assertTrue(completed.wait(1))
        self.assertTrue(runner.stop(1))
        self.assertEqual(callback_stops, [False])
        self.assertEqual(runner.public_status()["status"], "stopped")
        self.assertEqual(runner.public_status()["last_result"]["reason"], "at_target")

    def test_invalid_scheduler_configuration_is_rejected(self):
        for interval in (True, 0, -1, 86_401, float("inf"), float("nan"), "300", 10 ** 400):
            with self.subTest(interval=repr(interval)):
                with self.assertRaises(ValueError):
                    SandboxAutoRunner(lambda: step(), interval=interval)
        with self.assertRaises(ValueError):
            SandboxAutoRunner(None)
        with self.assertRaises(ValueError):
            SandboxAutoRunner(lambda: step(), enabled="true")


if __name__ == "__main__":
    unittest.main()
