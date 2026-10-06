"""Opt-in scheduling of serialized, state-validated sandbox execution.

The callback owns every broker operation and persistent-state check. This
module creates neither accounts nor trading state and performs no HTTP calls.
Its single worker starts immediately, then waits after each completed step.
Unexpected failures latch until an operator explicitly calls ``resume``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import threading
from typing import Callable


_ACTIONS = frozenset({"buy", "sell", "hold", "wait"})
_REASONS = frozenset({
    "pending_order_uncertain", "pending_order_open", "pending_order_executed",
    "pending_order_failed", "no_completed_candle", "stale_signal_candle",
    "risk_halt", "signal_already_handled", "signal_order_failed",
    "insufficient_history", "insufficient_funds_for_lot", "rebalance_buy",
    "drawdown_exit", "rebalance_sell", "insufficient_allocation_for_lot",
    "at_target", "weekend_calendar_guard", "submission_uncertain",
    "order_rejected", "order_cancelled",
})
_ORDER_STATUSES = frozenset({
    "EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_REJECTED",
    "EXECUTION_REPORT_STATUS_CANCELLED", "EXECUTION_REPORT_STATUS_NEW",
    "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
})
_ERRORS = frozenset({
    "automatic_execution_failed", "invalid_execution_result",
    "state_requires_manual_review", "trading_state_invalid_manual_review_required",
    "initialization_outcome_uncertain_manual_review_required",
    "explicit_sandbox_connection_required", "monitor_account_mismatch",
    "persistent_state_required", "automatic_state_not_ready",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _summary(report: dict) -> dict | None:
    """Whitelist successful step fields instead of redacting arbitrary text."""
    if report.get("sandbox_only") is not True:
        return None
    result = report.get("result")
    if not isinstance(result, dict):
        return None
    action, reason = result.get("action"), result.get("reason")
    lots, order_status = result.get("lots", 0), result.get("order_status")
    if (not isinstance(action, str) or action not in _ACTIONS
            or not isinstance(reason, str) or reason not in _REASONS
            or type(lots) is not int or not 0 <= lots <= 1_000_000_000
            or (order_status is not None
                and (not isinstance(order_status, str) or order_status not in _ORDER_STATUSES))):
        return None
    return {"action": action, "reason": reason, "lots": lots,
            "order_status": order_status}


class SandboxAutoRunner:
    """A disabled-by-default, single-worker sandbox scheduler.

    ``callback`` must return the hosted step contract
    ``(200, {"sandbox_only": True, "result": {...}})``. It must serialize with
    manual actions and reject missing/invalid persistent state before broker
    activity. A 409 ``operation_in_progress`` response or exception skips one
    interval; other failures halt. Known successful strategy waits retain the
    callback's persisted order-reconciliation and risk-control semantics.

    No work starts in the constructor. ``start`` is idempotent and never clears
    an error latch. ``resume`` is a separate, explicit operator action; callers
    must revalidate their persistent state first. ``stop`` cannot interrupt an
    in-flight broker request: it joins that step before reporting completion.
    """

    def __init__(self, callback: Callable[[], tuple[int, dict]], *,
                 enabled: bool = False, interval: float = 300.0) -> None:
        if not callable(callback) or type(enabled) is not bool:
            raise ValueError("Invalid automatic sandbox configuration")
        if (type(interval) not in {int, float} or not 0 < interval <= 86_400
                or not math.isfinite(interval)):
            raise ValueError("Invalid automatic sandbox interval")
        self._callback = callback
        self._enabled = enabled
        self._interval = float(interval)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "stopped"
        self._started_at: str | None = None
        self._last_started_at: str | None = None
        self._last_completed_at: str | None = None
        self._next_run_at: str | None = None
        self._last_result: dict | None = None
        self._error: str | None = None

    def start(self) -> bool:
        """Start once if enabled and not halted; return whether it started."""
        with self._lock:
            if (not self._enabled or self._status == "error"
                    or (self._thread is not None and self._thread.is_alive())):
                return False
            self._stop_event.clear()
            self._status = "waiting"
            self._error = None
            self._started_at = _now().isoformat()
            self._next_run_at = self._started_at
            try:
                self._thread = threading.Thread(target=self._run,
                                                name="sandbox-auto-runner", daemon=True)
                self._thread.start()
            except Exception:
                # An unstarted Thread cannot be joined. Fail closed without
                # retaining its exception text or breaking service teardown.
                self._thread = None
                self._status = "error"
                self._error = "automatic_execution_failed"
                self._next_run_at = None
                self._stop_event.set()
                return False
            return True

    def resume(self) -> bool:
        """Explicitly clear a stopped error latch and start a fresh worker."""
        with self._lock:
            if not self._enabled or (self._thread is not None and self._thread.is_alive()):
                return False
            self._status = "stopped"
            self._error = None
        return self.start()

    def stop(self, timeout: float | None = None) -> bool:
        """Request stop and join; False means a step is still in progress."""
        with self._lock:
            self._stop_event.set()
            self._next_run_at = None
            worker = self._thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        with self._lock:
            # A concurrent explicit restart may replace a worker after its
            # join. Never overwrite that new worker's status or report it idle.
            current = self._thread
            finished = current is None or not current.is_alive()
            if finished and self._status != "error":
                self._status = "stopped"
            return finished

    def public_status(self) -> dict:
        """Return a bounded snapshot with no callback text or identifiers."""
        with self._lock:
            return {"enabled": self._enabled, "status": self._status,
                    "interval_seconds": self._interval, "started_at": self._started_at,
                    "last_started_at": self._last_started_at,
                    "last_completed_at": self._last_completed_at,
                    "next_run_at": self._next_run_at,
                    "last_result": dict(self._last_result) if self._last_result else None,
                    "error": self._error}

    def _halt(self, code: object) -> None:
        with self._lock:
            self._status = "error"
            self._error = code if isinstance(code, str) and code in _ERRORS else "automatic_execution_failed"
            self._next_run_at = None
            self._stop_event.set()

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    if self._stop_event.is_set():
                        break
                    self._status = "running"
                    self._last_started_at = _now().isoformat()
                    self._next_run_at = None
                busy = False
                summary = None
                error = None
                try:
                    response = self._callback()
                    if (not isinstance(response, tuple) or len(response) != 2
                            or type(response[0]) is not int or not isinstance(response[1], dict)):
                        error = "invalid_execution_result"
                    else:
                        status, report = response
                        busy = status == 409 and report.get("error") == "operation_in_progress"
                        if not busy:
                            if status != 200:
                                error = report.get("error", "automatic_execution_failed")
                                if error is None:
                                    error = "automatic_execution_failed"
                            else:
                                summary = _summary(report)
                                if summary is None:
                                    error = "invalid_execution_result"
                except BaseException as failure:
                    # Broker/control exceptions may contain private text. Never
                    # log them or copy their arbitrary messages into snapshots.
                    code = getattr(failure, "code", None)
                    busy = getattr(failure, "status", None) == 409 and code == "operation_in_progress"
                    if not busy:
                        error = code if isinstance(code, str) else "automatic_execution_failed"
                completed = _now()
                with self._lock:
                    self._last_completed_at = completed.isoformat()
                if error is not None:
                    self._halt(error)
                    break
                with self._lock:
                    if summary is not None:
                        self._last_result = summary
                    self._error = "operation_in_progress" if busy else None
                    if self._stop_event.is_set():
                        break
                    self._status = "waiting"
                    self._next_run_at = (completed + timedelta(seconds=self._interval)).isoformat()
                if self._stop_event.wait(self._interval):
                    break
        finally:
            with self._lock:
                self._next_run_at = None
                if self._status != "error":
                    self._status = "stopped"
