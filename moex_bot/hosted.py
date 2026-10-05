"""Authenticated, on-demand control of one dedicated T-Invest sandbox account.

There is no startup account creation, background trader, token-file reader, or
real-money API. Lost/uncertain initialization state requires manual inspection.
Free hosting can lose its local disk; a new explicit connection then creates a
new virtual account instead of guessing ownership of an existing one.
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
from typing import Callable
from uuid import UUID

from .sandbox import MOEX_TZ, run_step
from .tbank import ApiError, TInvestClient, money, quotation


_MAX_INPUT = 4096
_CONTROL_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z", re.ASCII)
_TICKER_PATTERN = re.compile(r"[A-Z0-9]{1,12}\Z", re.ASCII)
_BROKER_TIMEOUT = 10
_PUBLIC_HEALTH = {"ok": True, "service": "moex-sandbox-control"}
_PUBLIC_PAGE = b"<!doctype html><html lang='en'><title>Sandbox control</title><body><h1>Sandbox control service</h1><p>Service is running. Virtual funds only.</p></body></html>"
_SIGNAL_FIELDS = {"handled_signal", "risk_handled_signal", "failed_signal", "risk_failed_signal"}


@dataclass(frozen=True)
class HostedConfig:
    control_token: str = field(repr=False)
    sandbox_token: str = field(repr=False)
    ticker: str = "SBER"
    initial_cash: Decimal = Decimal("100000")
    state_dir: Path = Path("state/hosted")

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

    @classmethod
    def from_environment(cls) -> HostedConfig:
        # Hosted operation intentionally never falls back to a local token file.
        return cls(control_token=os.environ.get("BOT_CONTROL_TOKEN", ""),
                   sandbox_token=os.environ.get("TINVEST_SANDBOX_TOKEN", ""),
                   ticker=os.environ.get("BOT_TICKER", "SBER").upper(),
                   initial_cash=Decimal(os.environ.get("BOT_INITIAL_CASH", "100000")),
                   state_dir=Path(os.environ.get("BOT_STATE_DIR", "state/hosted")))


class _ControlError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code


def _broker_diagnostics(error: Exception) -> dict:
    if not isinstance(error, ApiError):
        return {}
    status = error.status_code
    reason = error.reason
    return {"broker_status_code": status if type(status) is int and 100 <= status <= 599 else None,
            "broker_reason": reason if reason in {"proxy_tls_certificate", "broker_tls_certificate"} else None}


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

    def close(self) -> None:
        self._lifetime_lock.close()

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
            return time.tzinfo is not None and time.utcoffset() is not None
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
            try:
                instrument = client.resolve_share(self.config.ticker)
                now = datetime.now(timezone.utc)
                candles = client.get_daily_candles(instrument.uid, now - timedelta(days=180), now)
                today = now.astimezone(MOEX_TZ).date()
                completed = [candle for candle in candles if candle.time < now
                             and candle.time.astimezone(MOEX_TZ).date() < today]
                price = client.get_last_price(instrument.uid)
                report.update({"market_data_authorized": True, "ticker": instrument.ticker,
                               "lot": instrument.lot, "completed_candles": len(completed),
                               "last_price": str(price)})
            except Exception as error:
                return 502, {**report, "connected": False, "market_data_authorized": False,
                             "error": "market_data_check_failed", **_broker_diagnostics(error)}
        return 200, report

    def _connect(self) -> tuple[int, dict]:
        state = self._load()
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

    def execute(self, path: str, parameters: dict) -> tuple[int, dict]:
        if path == "/api/check":
            if set(parameters) - {"market_data"} or not isinstance(parameters.get("market_data", False), bool):
                raise _ControlError(400, "invalid_parameters")
        elif path == "/api/connect":
            if parameters:
                raise _ControlError(400, "invalid_parameters")
        elif path == "/api/step":
            if set(parameters) != {"submit"} or not isinstance(parameters["submit"], bool):
                raise _ControlError(400, "explicit_boolean_submit_required")
        else:
            raise _ControlError(404, "not_found")
        if not self._requests.acquire(blocking=False):
            raise _ControlError(409, "operation_in_progress")
        try:
            if path == "/api/check":
                return self._check(parameters.get("market_data", False))
            if path == "/api/connect":
                return self._connect()
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
            self._response(200, _PUBLIC_PAGE, "text/html; charset=utf-8")
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
