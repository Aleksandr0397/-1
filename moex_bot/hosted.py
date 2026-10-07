"""Authenticated, on-demand control of one dedicated T-Invest sandbox account.

There is no startup account creation, background trader, token-file reader, or
real-money API. Lost/uncertain initialization state requires manual inspection.
Free hosting can lose its local disk. An explicitly configured public monitor
can still read its virtual account, but never resets the lost trading session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import threading
import time
from typing import Callable
from uuid import UUID

from .autorun import SandboxAutoRunner
from .dashboard import DASHBOARD_HTML
from .monitor import MonitorError, collect
from .restore import RestoreError, restore_cash_account
from .sandbox import MOEX_TZ, run_step
from .tbank import ApiError, TInvestClient, money, quotation


_MAX_INPUT = 4096
_CONTROL_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z", re.ASCII)
_TICKER_PATTERN = re.compile(r"[A-Z0-9]{1,12}\Z", re.ASCII)
_BROKER_TIMEOUT = 10
_PUBLIC_HEALTH = {"ok": True, "service": "moex-sandbox-control"}
_MONITOR_INTERVAL = 30
_SIGNAL_FIELDS = {"handled_signal", "risk_handled_signal", "failed_signal", "risk_failed_signal"}


@dataclass(frozen=True)
class HostedConfig:
    control_token: str = field(repr=False)
    sandbox_token: str = field(repr=False)
    ticker: str = "SBER"
    initial_cash: Decimal = Decimal("100000")
    state_dir: Path = Path("state/hosted")
    # Explicit publication of this dedicated virtual account only. This does
    # not restore a lost trading session or authorize any broker mutations.
    public_monitor_account_id: str | None = field(default=None, repr=False)
    auto_trade: bool = False
    auto_interval: int = 300

    def __post_init__(self) -> None:
        if (not isinstance(self.control_token, str)
                or not _CONTROL_PATTERN.fullmatch(self.control_token)
                or len(set(self.control_token)) < 8):
            raise ValueError("A distinct random control secret is required")
        if (not isinstance(self.sandbox_token, str) or not self.sandbox_token
                or not self.sandbox_token.isascii()
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in self.sandbox_token)):
            raise ValueError("A sandbox token is required")
        if hmac.compare_digest(self.control_token, self.sandbox_token):
            raise ValueError("Control and broker secrets must be distinct")
        if not isinstance(self.ticker, str) or not _TICKER_PATTERN.fullmatch(self.ticker):
            raise ValueError("Invalid configured ticker")
        if (not isinstance(self.initial_cash, Decimal) or not self.initial_cash.is_finite()
                or self.initial_cash <= 0):
            raise ValueError("Invalid configured virtual capital")
        quotation(self.initial_cash)
        if not isinstance(self.state_dir, Path) or str(self.state_dir) == ":memory:":
            raise ValueError("Private local state directory is required")
        if type(self.auto_trade) is not bool or type(self.auto_interval) is not int or not 30 <= self.auto_interval <= 86400:
            raise ValueError("Invalid automatic sandbox configuration")
        if self.auto_trade and self.public_monitor_account_id is None:
            raise ValueError("An explicit sandbox account is required for automatic execution")
        if self.public_monitor_account_id is not None:
            try:
                if str(UUID(self.public_monitor_account_id)) != self.public_monitor_account_id:
                    raise ValueError("Noncanonical account identifier")
            except (ValueError, AttributeError, TypeError):
                raise ValueError("Invalid configured monitor account") from None

    @classmethod
    def from_environment(cls) -> HostedConfig:
        # Hosted operation intentionally never falls back to a local token file.
        auto_text = os.environ.get("BOT_AUTOTRADE_ENABLED", "false")
        interval_text = os.environ.get("BOT_AUTOTRADE_INTERVAL", "300")
        if auto_text not in {"true", "false"} or not re.fullmatch(r"[0-9]{1,5}", interval_text, re.ASCII):
            raise ValueError("Invalid automatic sandbox environment")
        return cls(control_token=os.environ.get("BOT_CONTROL_TOKEN", ""),
                   sandbox_token=os.environ.get("TINVEST_SANDBOX_TOKEN", ""),
                   ticker=os.environ.get("BOT_TICKER", "SBER").upper(),
                   initial_cash=Decimal(os.environ.get("BOT_INITIAL_CASH", "100000")),
                   state_dir=Path(os.environ.get("BOT_STATE_DIR", "state/hosted")),
                   public_monitor_account_id=os.environ.get("BOT_PUBLIC_MONITOR_ACCOUNT_ID") or None,
                   auto_trade=auto_text == "true", auto_interval=int(interval_text))


class _ControlError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code


def _broker_diagnostics(error: Exception) -> dict:
    if not isinstance(error, ApiError):
        return {}
    status = error.status_code
    reason = error.reason
    code = error.broker_code
    return {"broker_status_code": status if type(status) is int and 100 <= status <= 599 else None,
            "broker_reason": reason if reason in {"proxy_tls_certificate", "broker_tls_certificate"} else None,
            "broker_error_code": code if type(code) is int and 0 <= code <= 999_999_999 else None}


def _private_database(path: Path) -> sqlite3.Connection:
    if path.is_symlink():
        raise ValueError("Invalid private state")
    # Create without a permissive interval, before SQLite opens the file.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    path.chmod(0o600)
    connection = sqlite3.connect(path, timeout=0, check_same_thread=False)
    connection.execute("PRAGMA synchronous=FULL")
    return connection


@contextmanager
def _database(path: Path):
    connection = _private_database(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class HostedControl:
    """One lifetime process lock, serialized requests, and durable init stages."""

    def __init__(self, config: HostedConfig,
                 client_factory: Callable[[], TInvestClient] | None = None) -> None:
        self.config = config
        self.state_dir = config.state_dir.expanduser().resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.connection_path = self.state_dir / "connection.sqlite3"
        self.trading_path = self.state_dir / "trading.sqlite3"
        self._requests = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._monitor_lock = threading.Lock()
        self._monitor_snapshot: dict | None = None
        self._monitor_checked = float("-inf")
        self._monitor_running = False
        self._closed = False
        self._close_done = threading.Event()
        self._client_factory = client_factory or (lambda: TInvestClient(config.sandbox_token, timeout=_BROKER_TIMEOUT))
        self._lifetime_lock = _private_database(self.state_dir / "service.lock.sqlite3")
        try:
            self._lifetime_lock.execute("CREATE TABLE IF NOT EXISTS service_lock(id INTEGER PRIMARY KEY)")
            self._lifetime_lock.commit()
            self._lifetime_lock.execute("BEGIN IMMEDIATE")
            with _database(self.connection_path) as database:
                database.execute("CREATE TABLE IF NOT EXISTS connection_state(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        except Exception:
            self._lifetime_lock.close()
            raise
        self.automatic = SandboxAutoRunner(self._automatic_step, enabled=config.auto_trade,
                                          interval=config.auto_interval)

    def close(self) -> None:
        with self._lifecycle_lock:
            already_closing = self._closed
            with self._monitor_lock:
                self._closed = True
        if already_closing:
            self._close_done.wait()
            return
        # Closing rejects new work before draining both background and manual
        # broker requests. Retain lifetime ownership through their completion.
        try:
            self.automatic.stop()
            with self._requests:
                self._lifetime_lock.close()
        finally:
            self._close_done.set()

    def start_auto(self) -> bool:
        with self._lifecycle_lock:
            if self._closed:
                return False
            return self.automatic.start()

    def _automatic_state(self) -> dict:
        state = self._load()
        if state is None or state["stage"] != "ready" or state.get("trading_state_started") is not True:
            raise _ControlError(409, "automatic_state_not_ready")
        if self._account(state) != self.config.public_monitor_account_id:
            raise _ControlError(409, "monitor_account_mismatch")
        self._trading_state(state)
        return state

    def _automatic_step(self) -> tuple[int, dict]:
        if not self._requests.acquire(blocking=False):
            raise _ControlError(409, "operation_in_progress")
        try:
            if self._closed:
                raise _ControlError(503, "service_stopping")
            self._automatic_state()  # Never initialize or reset state in a tick.
            return self._step(True)
        finally:
            self._requests.release()

    def _with_automation(self, report: dict) -> dict:
        status = self.automatic.public_status()
        return {**report, "execution_mode": "sandbox_auto" if self.config.auto_trade else "observe_on_demand",
                "automation": {"enabled": status["enabled"], "status": status["status"],
                               "interval_seconds": status["interval_seconds"],
                               "last_checked_at": status["last_completed_at"],
                               "next_check_at": status["next_run_at"],
                               "last_result": status["last_result"], "error": status["error"]}}

    def _empty_monitor(self, status: str, error: str | None = None) -> dict:
        return {"status": status, "ticker": self.config.ticker,
                "execution_mode": "observe_on_demand", "updated_at": None,
                "signal_time": None, "equity": None, "cash": None, "price": None,
                "shares": None, "last_action": None, "action_reason": None,
                "planned_lots": None, "chart": [], "events": [], "error": error}

    def public_status(self) -> dict:
        """Schedule a bounded read-only refresh; HTTP requests never wait on API."""
        if self.config.public_monitor_account_id is None:
            return self._with_automation(self._empty_monitor("error", "monitor_not_configured"))
        with self._monitor_lock:
            if self._closed:
                return self._with_automation(self._empty_monitor("error", "monitor_unavailable"))
            if not self._monitor_running and time.monotonic() - self._monitor_checked >= _MONITOR_INTERVAL:
                self._monitor_running = True
                worker = threading.Thread(target=self._refresh_monitor, daemon=True)
                worker.start()
            return self._with_automation(self._monitor_snapshot or self._empty_monitor("loading"))

    def _refresh_monitor(self) -> None:
        acquired = self._requests.acquire(blocking=False)
        try:
            if not acquired:
                raise MonitorError("operation_in_progress")
            if self._closed:
                raise MonitorError("broker_unavailable")
            report = collect(self._client_factory(), self.config.public_monitor_account_id,
                             self.config.ticker)
            label = "Данные виртуального счёта обновлены"
        except Exception as error:
            # Keep the last verified values and their original timestamp when
            # a refresh fails. Broker text, IDs and secrets never enter JSON.
            code = error.code if isinstance(error, MonitorError) else "monitor_refresh_failed"
            with self._monitor_lock:
                report = dict(self._monitor_snapshot or self._empty_monitor("error"))
            report.update(status="error", error=code)
            label = "Не удалось обновить данные"
        finally:
            if acquired:
                self._requests.release()
        with self._monitor_lock:
            events = list((self._monitor_snapshot or {}).get("events", []))
            events.append({"time": datetime.now(timezone.utc).isoformat(), "label": label})
            report["events"] = events[-12:]
            if not self._closed:
                self._monitor_snapshot = report
            self._monitor_checked = time.monotonic()
            self._monitor_running = False

    def authorized(self, authorization: str | None) -> bool:
        if not isinstance(authorization, str) or not authorization.isascii():
            return False
        return hmac.compare_digest(authorization, "Bearer " + self.config.control_token)

    def redact(self, value: object) -> object:
        if isinstance(value, str):
            for secret in (self.config.control_token, self.config.sandbox_token):
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def _identity(self) -> dict:
        return {"version": 1, "ticker": self.config.ticker, "cash": str(self.config.initial_cash)}

    def _load(self) -> dict | None:
        with _database(self.connection_path) as database:
            row = database.execute("SELECT data FROM connection_state WHERE id=1").fetchone()
        if row is None:
            return None
        state = json.loads(row[0])
        if (not isinstance(state, dict) or state.get("identity") != self._identity()
                or state.get("stage") not in {"opening", "funding", "ready"}):
            raise _ControlError(409, "state_requires_manual_review")
        return state

    def _save(self, state: dict) -> None:
        with _database(self.connection_path) as database:
            database.execute("INSERT INTO connection_state(id,data) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                             (json.dumps(state, separators=(",", ":"), sort_keys=True),))

    def _account(self, state: dict) -> str:
        account = state.get("account_id")
        if (not isinstance(account, str) or not account or len(account) > 128
                or any(ch.isspace() or ord(ch) < 32 for ch in account)
                or any(secret in account for secret in (self.config.control_token, self.config.sandbox_token))):
            raise _ControlError(409, "state_requires_manual_review")
        return account

    def _connection_report(self, state: dict) -> dict:
        return {"connected": True, "sandbox_account": self._account(state),
                "virtual_cash_rub": str(self.config.initial_cash), "ticker": self.config.ticker,
                "background_trading": False}

    @staticmethod
    def _signal_time(value: object) -> bool:
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            return False
        try:
            time = datetime.fromisoformat(value)
            return (time.tzinfo is not None and time.utcoffset() is not None
                    and time <= datetime.now(timezone.utc))
        except ValueError:
            return False

    def _trading_state(self, connection_state: dict) -> dict:
        """Read existing state without allowing SQLite to recreate/reset it."""
        try:
            if (self.trading_path.is_symlink() or not self.trading_path.is_file()
                    or self.trading_path.stat().st_size == 0):
                raise ValueError("Missing trading state")
            database = sqlite3.connect(self.trading_path.as_uri() + "?mode=ro", uri=True, timeout=0)
            try:
                rows = database.execute("SELECT id,data FROM bot_state LIMIT 2").fetchall()
            finally:
                database.close()
            if (len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], str)
                    or len(rows[0][1]) > 65536):
                raise ValueError("Invalid singleton state")
            state = json.loads(rows[0][1], object_pairs_hook=_json_object, parse_constant=_reject_constant)
            required = {"version", "identity", "high_water", "halted", "pending"} | _SIGNAL_FIELDS
            if not isinstance(state, dict) or set(state) - required - {"last_order"} or not required <= set(state):
                raise ValueError("Incomplete trading state")
            if type(state["version"]) is not int or state["version"] != 1 or not isinstance(state["halted"], bool):
                raise ValueError("Invalid trading state version or risk flag")
            if not isinstance(state["high_water"], str) or len(state["high_water"]) > 100:
                raise ValueError("Invalid high water")
            high_water = Decimal(state["high_water"])
            if not high_water.is_finite() or high_water < 0:
                raise ValueError("Invalid high water")
            for key in _SIGNAL_FIELDS:
                if state[key] is not None and not self._signal_time(state[key]):
                    raise ValueError("Invalid signal time")
            identity = state["identity"]
            fixed = {"account_id": self._account(connection_state), "ticker": self.config.ticker,
                     "strategy": "sma-entry-hold-v1", "fast": 20, "slow": 60,
                     "max_allocation": "0.2", "max_drawdown": "0.1", "commission": "0.0005"}
            if (not isinstance(identity, dict) or set(identity) != set(fixed) | {"uid", "lot"}
                    or any(identity[key] != value or type(identity[key]) is not type(value) for key, value in fixed.items())
                    or not isinstance(identity["uid"], str) or not 1 <= len(identity["uid"]) <= 128
                    or any(ch.isspace() or ord(ch) < 32 for ch in identity["uid"])
                    or type(identity["lot"]) is not int or not 1 <= identity["lot"] <= 2**31 - 1):
                raise ValueError("Invalid trading identity")
            if connection_state["trading_state_started"]:
                if connection_state.get("trading_identity") != identity:
                    raise ValueError("Trading identity was lost or changed")
            elif (state["pending"] is not None or "last_order" in state
                  or any(state[key] is not None for key in _SIGNAL_FIELDS)):
                # An unregistered preflight can observe risk, but cannot have
                # submitted or handled an order. Such history needs inspection.
                raise ValueError("Unexpected prior execution history")
            for key in ("pending", "last_order"):
                order = state.get(key)
                if order is None:
                    if key == "last_order" and key in state:
                        raise ValueError("Invalid last order")
                    continue
                fields = {"order_id", "signal_time", "direction", "lots", "risk_exit", "uid"}
                if key == "last_order":
                    fields |= {"status", "lots_executed"}
                if (not isinstance(order, dict) or set(order) != fields
                        or not isinstance(order["order_id"], str) or str(UUID(order["order_id"])) != order["order_id"]
                        or not self._signal_time(order["signal_time"])
                        or order["direction"] not in {"BUY", "SELL"}
                        or type(order["lots"]) is not int or not 0 < order["lots"] <= 2**63 - 1
                        or not isinstance(order["risk_exit"], bool) or order["uid"] != identity["uid"]):
                    raise ValueError("Invalid persisted order")
                if key == "last_order" and (order["status"] not in {
                        "EXECUTION_REPORT_STATUS_FILL", "EXECUTION_REPORT_STATUS_REJECTED", "EXECUTION_REPORT_STATUS_CANCELLED"}
                        or type(order["lots_executed"]) is not int or not 0 <= order["lots_executed"] <= order["lots"]
                        or (order["status"] == "EXECUTION_REPORT_STATUS_FILL" and order["lots_executed"] != order["lots"])):
                    raise ValueError("Invalid persisted execution")
            return state
        except Exception:
            raise _ControlError(409, "trading_state_invalid_manual_review_required") from None

    def _check(self, market_data: bool) -> tuple[int, dict]:
        client = self._client_factory()
        try:
            accounts = client.list_sandbox_accounts()
        except Exception as error:
            return 502, {"connected": False, "sandbox_authenticated": False,
                         "error": "sandbox_check_failed", **_broker_diagnostics(error)}
        report = {"connected": True, "sandbox_authenticated": True,
                  "sandbox_account_count": len(accounts), "market_data_checked": market_data}
        if market_data:
            stage = 'resolve_share'
            try:
                instrument = client.resolve_share(self.config.ticker)
                now = datetime.now(timezone.utc)
                stage = 'get_daily_candles'
                candles = client.get_daily_candles(instrument.uid, now - timedelta(days=180), now)
                today = now.astimezone(MOEX_TZ).date()
                completed = [candle for candle in candles if candle.time < now
                             and candle.time.astimezone(MOEX_TZ).date() < today]
                stage = 'get_last_price'
                price = client.get_last_price(instrument.uid)
                report.update({"market_data_authorized": True, "ticker": instrument.ticker,
                               "lot": instrument.lot, "completed_candles": len(completed),
                               "last_price": str(price)})
            except Exception as error:
                return 502, {**report, "connected": False, "market_data_authorized": False,
                             "error": "market_data_check_failed", "failed_market_data_stage": stage,
                             **_broker_diagnostics(error)}
        return 200, report

    def _connect(self) -> tuple[int, dict]:
        state = self._load()
        if self.config.public_monitor_account_id is not None:
            if state is None:
                raise _ControlError(409, "configured_monitor_account_readonly")
            if self._account(state) != self.config.public_monitor_account_id:
                raise _ControlError(409, "monitor_account_mismatch")
        if state is not None:
            if state["stage"] != "ready":
                raise _ControlError(409, "initialization_outcome_uncertain_manual_review_required")
            return 200, self._connection_report(state)
        client = self._client_factory()
        state = {"identity": self._identity(), "stage": "opening", "trading_state_started": False}
        self._save(state)  # Durable before the non-idempotent account creation.
        try:
            state["account_id"] = client.open_sandbox_account()
            self._account(state)
            state["stage"] = "funding"
            self._save(state)  # Durable before the non-idempotent cash deposit.
            result = client.sandbox_pay_in(state["account_id"], self.config.initial_cash)
            balance = result["balance"]
            if str(balance.get("currency", "")).lower() != "rub" or money(balance) != self.config.initial_cash:
                raise ValueError("Funding balance was not confirmed")
            state["stage"] = "ready"
            self._save(state)
        except Exception:
            # Opening/funding remain persisted. Never infer that a timed-out
            # mutation failed, and never retry either mutation automatically.
            return 502, {"connected": False,
                         "error": "initialization_outcome_uncertain_manual_review_required"}
        return 200, self._connection_report(state)

    def _step(self, submit: bool) -> tuple[int, dict]:
        state = self._load()
        if state is None or state["stage"] != "ready":
            raise _ControlError(409, "explicit_sandbox_connection_required")
        account = self._account(state)
        if not isinstance(state.get("trading_state_started"), bool):
            raise _ControlError(409, "state_requires_manual_review")
        if state["trading_state_started"] or self.trading_path.exists() or self.trading_path.is_symlink():
            self._trading_state(state)  # Strictly before constructing a broker client.
        client = self._client_factory()
        if not state["trading_state_started"]:
            # Establish a durable baseline without permission to submit. Early
            # transient failures cannot latch the session before state exists.
            result = run_step(client, account, self.config.ticker,
                              state_path=self.trading_path, submit=False)
            baseline = self._trading_state(state)
            state["trading_identity"] = baseline["identity"]
            state["trading_state_started"] = True
            self._save(state)
            if not submit:
                self.trading_path.chmod(0o600)
                return 200, {"sandbox_only": True, "result": result}
        self._trading_state(state)
        result = run_step(client, account, self.config.ticker,
                          state_path=self.trading_path, submit=submit)
        self.trading_path.chmod(0o600)
        return 200, {"sandbox_only": True, "result": result}

    def _restore(self, parameters: dict) -> tuple[int, dict]:
        """Explicit operator recovery of the documented, never-traded account."""
        account = self.config.public_monitor_account_id
        if account is None or self._load() is not None or self.trading_path.exists() or self.trading_path.is_symlink():
            raise _ControlError(409, "restore_requires_empty_local_state")
        checkpoint = parameters["checkpoint"]
        expected = {"action": "hold", "reason": "at_target", "lots": 0,
                    "submit": True, "shares": 0, "halted": False}
        allowed = set(expected) | {"equity", "cash", "high_water", "drawdown", "signal_time", "price"}
        try:
            if (not isinstance(checkpoint, dict) or set(checkpoint) != allowed
                    or any(type(checkpoint[key]) is not type(value) or checkpoint[key] != value
                           for key, value in expected.items())
                    or not self._signal_time(checkpoint["signal_time"])):
                raise ValueError("Invalid checkpoint")
            for field in ("equity", "cash", "high_water"):
                value = checkpoint[field]
                if not isinstance(value, str) or len(value) > 100 or Decimal(value) != self.config.initial_cash:
                    raise ValueError("Noninitial checkpoint")
            if (not isinstance(checkpoint["drawdown"], str) or len(checkpoint["drawdown"]) > 100
                    or Decimal(checkpoint["drawdown"]) != 0):
                raise ValueError("Invalid risk checkpoint")
            created = datetime.fromisoformat(parameters["created_at"])
            signal = datetime.fromisoformat(checkpoint["signal_time"])
            now = datetime.now(timezone.utc)
            if created.tzinfo is None or created > now or signal > created:
                raise ValueError("Invalid checkpoint times")
        except (ValueError, TypeError, ArithmeticError):
            raise _ControlError(400, "invalid_restore_checkpoint") from None
        try:
            restored = restore_cash_account(self._client_factory(), account, self.config.ticker,
                                            self.config.initial_cash, created, now,
                                            prior_checkpoint_trusted=True)
        except RestoreError as error:
            raise _ControlError(409, error.code) from None
        state = {"identity": self._identity(), "stage": "restoring", "account_id": account,
                 "trading_state_started": False}
        self._save(state)  # A crash during the local restore must not reset risk.
        with _database(self.trading_path) as database:
            database.execute("CREATE TABLE bot_state(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
            database.execute("INSERT INTO bot_state(id,data) VALUES(1,?)",
                             (json.dumps(restored, sort_keys=True),))
        baseline = self._trading_state(state)
        state.update(stage="ready", trading_state_started=True, trading_identity=baseline["identity"])
        self._save(state)
        return 200, {"sandbox_only": True, "restored": True, "automatic_started": False}

    def _resume_auto(self) -> tuple[int, dict]:
        with self._lifecycle_lock:
            if self._closed:
                raise _ControlError(503, "service_stopping")
            if not self.config.auto_trade:
                raise _ControlError(409, "automatic_mode_not_enabled")
            if not self._requests.acquire(blocking=False):
                raise _ControlError(409, "operation_in_progress")
            try:
                self._automatic_state()
            finally:
                self._requests.release()
            started = self.automatic.resume()
            return 200, {"sandbox_only": True, "automatic_started": started}

    def _restore_history(self, created_at: str) -> tuple[int, dict]:
        """Authenticated diagnostic reads for the configured sandbox only."""
        account = self.config.public_monitor_account_id
        if account is None:
            raise _ControlError(409, "monitor_not_configured")
        try:
            created = datetime.fromisoformat(created_at)
            now = datetime.now(timezone.utc)
            if created.tzinfo is None or created > now:
                raise ValueError("Invalid time")
        except (ValueError, TypeError):
            raise _ControlError(400, "invalid_restore_checkpoint") from None
        client = self._client_factory()
        rows = client.get_sandbox_operations(account, created - timedelta(minutes=1), now)
        portfolio = client.get_sandbox_portfolio(account)
        cash_positions = [position for position in portfolio.get("positions", [])
                          if isinstance(position, dict) and position.get("instrumentType") == "currency"
                          and position.get("figi") == "RUB000UTSTOM"]
        cash_position = cash_positions[0] if len(cash_positions) == 1 else {}
        def amount(value):
            if not isinstance(value, dict):
                return None
            try:
                currency = value.get("currency", "")
                code = currency.lower() if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{0,3}", currency) else "other"
                return {"value": str(money(value)), "currency": code}
            except Exception:
                return None
        def date(value):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo is not None else None
            except (ValueError, TypeError, AttributeError):
                return None
        def quantity(value):
            return str(value) if isinstance(value, (str, int)) and re.fullmatch(r"[0-9]{1,19}", str(value)) else None
        result = []
        for row in rows[:50]:
            kind, state = row.get("operationType"), row.get("state")
            result.append({"operation_type": kind if isinstance(kind, str) and re.fullmatch(r"OPERATION_TYPE_[A-Z_]{1,64}", kind) else "unknown",
                           "state": state if isinstance(state, str) and re.fullmatch(r"OPERATION_STATE_[A-Z_]{1,64}", state) else "unknown",
                           "date": date(row.get("date")),
                           "payment": amount(row.get("payment")), "price": amount(row.get("price")),
                           "quantity": row.get("quantity") if isinstance(row.get("quantity"), (str, int)) and re.fullmatch(r"[0-9]{1,19}", str(row.get("quantity"))) else None,
                           "quantity_rest": row.get("quantityRest") if isinstance(row.get("quantityRest"), (str, int)) and re.fullmatch(r"[0-9]{1,19}", str(row.get("quantityRest"))) else None,
                           "instrument_type": row.get("instrumentType") if row.get("instrumentType") in {"currency", "share", "bond", "etf", "futures", "option", ""} else "unknown",
                           "rub_figi": row.get("figi") == "RUB000UTSTOM",
                           "has_figi": bool(row.get("figi")), "has_instrument_uid": bool(row.get("instrumentUid")),
                           "has_parent": bool(row.get("parentOperationId")),
                           "has_position_uid": bool(row.get("positionUid")), "has_asset_uid": bool(row.get("assetUid")),
                           "instrument_uid_matches_rub_cash": bool(row.get("instrumentUid")) and row.get("instrumentUid") == cash_position.get("instrumentUid"),
                           "position_uid_matches_rub_cash": bool(row.get("positionUid")) and row.get("positionUid") == cash_position.get("positionUid"),
                           "trades_count": len(row.get("trades", [])) if isinstance(row.get("trades", []), list) else None,
                           "trades": [{"quantity": quantity(trade.get("quantity")), "price": amount(trade.get("price")),
                                       "date": date(trade.get("dateTime"))}
                                      for trade in row.get("trades", [])[:5] if isinstance(trade, dict)] if isinstance(row.get("trades", []), list) else None,
                           "child_operations_count": len(row.get("childOperations", [])) if isinstance(row.get("childOperations", []), list) else None})
        return 200, {"sandbox_only": True, "operation_count": len(rows), "operations": result}

    def execute(self, path: str, parameters: dict) -> tuple[int, dict]:
        if self._closed:
            raise _ControlError(503, "service_stopping")
        if path == "/api/check":
            if set(parameters) - {"market_data"} or not isinstance(parameters.get("market_data", False), bool):
                raise _ControlError(400, "invalid_parameters")
        elif path == "/api/connect":
            if parameters:
                raise _ControlError(400, "invalid_parameters")
        elif path == "/api/step":
            if set(parameters) != {"submit"} or not isinstance(parameters["submit"], bool):
                raise _ControlError(400, "explicit_boolean_submit_required")
        elif path == "/api/restore":
            if set(parameters) != {"created_at", "checkpoint"} or not isinstance(parameters["created_at"], str):
                raise _ControlError(400, "explicit_restore_checkpoint_required")
        elif path == "/api/restore-history":
            if set(parameters) != {"created_at"} or not isinstance(parameters["created_at"], str):
                raise _ControlError(400, "invalid_parameters")
        elif path in {"/api/auto-resume", "/api/auto-stop"}:
            if parameters:
                raise _ControlError(400, "invalid_parameters")
            if path == "/api/auto-resume":
                return self._resume_auto()
            return 200, {"sandbox_only": True, "automatic_stopped": self.automatic.stop(timeout=0)}
        else:
            raise _ControlError(404, "not_found")
        if not self._requests.acquire(blocking=False):
            raise _ControlError(409, "operation_in_progress")
        try:
            if self._closed:
                raise _ControlError(503, "service_stopping")
            if path == "/api/check":
                return self._check(parameters.get("market_data", False))
            if path == "/api/connect":
                return self._connect()
            if path == "/api/restore":
                return self._restore(parameters)
            if path == "/api/restore-history":
                return self._restore_history(parameters["created_at"])
            return self._step(parameters["submit"])
        finally:
            self._requests.release()


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Invalid JSON constant")


class HostedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], control: HostedControl) -> None:
        self.control = control
        super().__init__(address, HostedHandler)

    def handle_error(self, request, client_address) -> None:
        # BaseServer normally prints tracebacks; broker/config secrets must not
        # appear in host logs, including failures outside the normal handler.
        pass


class HostedHandler(BaseHTTPRequestHandler):
    server: HostedServer
    server_version = "SandboxControl"
    sys_version = ""

    def setup(self) -> None:
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format: str, *args) -> None:
        # Avoid access logs containing URLs, headers or client-provided text.
        pass

    def _response(self, status: int, body: object, content_type: str = "application/json") -> None:
        if isinstance(body, bytes):
            encoded = body
        else:
            encoded = json.dumps(self.server.control.redact(body), separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(encoded)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._response(code, {"error": "invalid_request"})

    def do_GET(self) -> None:
        if self.path == "/health":
            self._response(200, _PUBLIC_HEALTH)
        elif self.path == "/":
            self._response(200, DASHBOARD_HTML, "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._response(200, self.server.control.public_status())
        else:
            self._response(405 if self.path.startswith("/api/") else 404, {"error": "post_required" if self.path.startswith("/api/") else "not_found"})

    def do_POST(self) -> None:
        # Authenticate before parsing bodies or constructing any broker client.
        authorization = self.headers.get_all("Authorization", [])
        if len(authorization) != 1 or not self.server.control.authorized(authorization[0]):
            self._response(401, {"error": "unauthorized"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]{1,8}", lengths[0], re.ASCII)):
                raise _ControlError(400, "invalid_body_length")
            length = int(lengths[0])
            if length > _MAX_INPUT:
                raise _ControlError(413, "request_too_large")
            if length == 0 or self.headers.get_content_type() != "application/json":
                raise _ControlError(400, "json_object_required")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise _ControlError(400, "invalid_body_length")
            try:
                parameters = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise _ControlError(400, "invalid_json") from None
            if not isinstance(parameters, dict):
                raise _ControlError(400, "json_object_required")
            status, report = self.server.control.execute(self.path, parameters)
            self._response(status, report)
        except _ControlError as error:
            self._response(error.status, {"error": error.code})
        except (TimeoutError, socket.timeout):
            self._response(408, {"error": "request_timeout"})
        except Exception:
            self._response(502, {"error": "operation_failed"})


def main() -> int:
    control = None
    server = None
    try:
        config = HostedConfig.from_environment()
        port_text = os.environ.get("PORT", "10000")
        if not re.fullmatch(r"[0-9]{1,5}", port_text, re.ASCII) or not 1 <= int(port_text) <= 65535:
            raise ValueError("Invalid port")
        control = HostedControl(config)
        server = HostedServer(("0.0.0.0", int(port_text)), control)
        control.start_auto()
        print("Hosted sandbox control service is running.", flush=True)
        server.serve_forever(poll_interval=0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Hosted service configuration or local state is invalid.", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
        if control is not None:
            control.close()


if __name__ == "__main__":
    raise SystemExit(main())
