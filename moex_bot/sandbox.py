"""One fail-closed rebalance of a dedicated virtual RUB share account.

This module has no production order interface.  Its calendar check is deliberately
conservative: previous Moscow weekdays, at most five calendar days old.  It is a
heuristic, not a MOEX holiday calendar; long closures require human inspection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from .engine import desired_position
from .tbank import money


MOEX_TZ = ZoneInfo("Europe/Moscow")
_TERMINAL = {
    "EXECUTION_REPORT_STATUS_FILL",
    "EXECUTION_REPORT_STATUS_REJECTED",
    "EXECUTION_REPORT_STATUS_CANCELLED",
}
_OPEN = {
    "EXECUTION_REPORT_STATUS_NEW",
    "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
}


def _decimal(value: Any, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Invalid {label}") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {label}")
    number = _decimal(value, label)
    if number != number.to_integral_value():
        raise ValueError(f"{label} must be an integer")
    return int(number)


def _quotation(value: Any, label: str) -> Decimal:
    if not isinstance(value, dict):
        raise ValueError(f"Missing {label}")
    return _decimal(money(value), label)


def _rub_amount(value: Any, label: str) -> Decimal:
    if not isinstance(value, dict) or str(value.get("currency", "")).lower() != "rub":
        raise ValueError(f"{label} must be denominated in RUB")
    return _quotation(value, label)


def _save(connection: sqlite3.Connection, state: dict) -> None:
    # A separate lock database remains locked while this durable commit completes.
    with connection:
        connection.execute(
            "INSERT INTO bot_state(id, data) VALUES(1, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (json.dumps(state, sort_keys=True, separators=(",", ":")),),
        )


def _result(action: str, reason: str, *, signal_time: str | None = None,
            lots: int = 0, **extra: Any) -> dict:
    return {"action": action, "reason": reason, "signal_time": signal_time,
            "lots": lots, **extra}


def _order_status(report: dict, pending: dict) -> tuple[str, int]:
    if not isinstance(report, dict):
        raise ValueError("Invalid order state response")
    status = report.get("executionReportStatus", "")
    if status not in _TERMINAL | _OPEN:
        raise ValueError("Order execution status is unknown")
    requested = _integer(report.get("lotsRequested", pending["lots"]), "requested lots")
    executed = _integer(report.get("lotsExecuted", 0), "executed lots")
    if requested != pending["lots"] or not 0 <= executed <= requested:
        raise ValueError("Order quantities do not match the pending request")
    if status == "EXECUTION_REPORT_STATUS_FILL" and executed != requested:
        raise ValueError("Filled order lacks confirmation of all requested lots")
    direction = report.get("direction")
    if direction and direction not in (pending["direction"], "ORDER_DIRECTION_" + pending["direction"]):
        raise ValueError("Order direction does not match the pending request")
    if report.get("instrumentUid") and report["instrumentUid"] != pending["uid"]:
        raise ValueError("Order instrument does not match the pending request")
    return status, executed


def _finish_order(state: dict, pending: dict, status: str, executed: int) -> None:
    state["pending"] = None
    state["last_order"] = {**pending, "status": status, "lots_executed": executed}
    if executed:
        # A cancelled partial fill is still an execution; never repeat its candle.
        key = "risk_handled_signal" if pending["risk_exit"] else "handled_signal"
        state[key] = pending["signal_time"]
        failure_key = "risk_failed_signal" if pending["risk_exit"] else "failed_signal"
        state[failure_key] = None
    else:
        failure_key = "risk_failed_signal" if pending["risk_exit"] else "failed_signal"
        state[failure_key] = pending["signal_time"]


def _account_values(portfolio: dict, positions: dict, uid: str,
                    lot: int) -> tuple[Decimal, Decimal, int]:
    """Validate actual portfolio value and available cash/share balances."""
    if not isinstance(portfolio, dict) or not isinstance(positions, dict):
        raise ValueError("Invalid account response")
    equity = _rub_amount(portfolio.get("totalAmountPortfolio"), "Portfolio value")
    if equity < 0:
        raise ValueError("Negative portfolio value is not supported")
    # Protobuf JSON omits empty repeated fields: no money means zero cash.
    # Equity reconciliation below still rejects inconsistent snapshots.
    cash = Decimal(0)
    for balance in positions.get("money", []):
        amount = _rub_amount(balance, "Cash balance")
        if amount < 0:
            raise ValueError("Borrowed cash is not supported")
        cash += amount
    for balance in positions.get("blocked", []):
        if _rub_amount(balance, "Blocked cash") != 0:
            raise ValueError("Account has blocked cash")
    shares = 0
    for security in positions.get("securities", []):
        balance = _integer(security.get("balance", 0), "Security balance")
        blocked = _integer(security.get("blocked", 0), "Blocked securities")
        if balance < 0:
            raise ValueError("Short positions are not supported")
        if blocked or security.get("exchangeBlocked", False):
            raise ValueError("Account has blocked securities")
        instrument_uid = security.get("instrumentUid")
        if balance and instrument_uid != uid:
            raise ValueError("Account must contain only the selected share")
        if instrument_uid == uid:
            shares += balance
    for kind in ("futures", "options"):
        if positions.get(kind):
            raise ValueError("Account must contain only RUB cash and the selected share")
    portfolio_shares = Decimal(0)
    portfolio_cash = Decimal(0)
    has_cash_position = False
    share_value = Decimal(0)
    for position in portfolio.get("positions", []):
        quantity = _quotation(position.get("quantity"), "Portfolio position quantity")
        if quantity < 0:
            raise ValueError("Short positions are not supported")
        if position.get("blocked", False):
            raise ValueError("Portfolio contains a blocked position")
        if position.get("quantityLots") is not None:
            quantity_lots = _quotation(position["quantityLots"], "Portfolio lot quantity")
            if quantity_lots < 0:
                raise ValueError("Short positions are not supported")
        if quantity:
            if position.get("instrumentType") == "currency":
                # RUB valuation currency alone cannot identify a RUB balance:
                # a USD position can also be valued in RUB. Require its RUB FIGI.
                if position.get("figi") != "RUB000UTSTOM":
                    raise ValueError("Portfolio contains a foreign or unidentified currency")
                _rub_amount(position.get("currentPrice"), "RUB cash valuation")
                if _quotation(position["currentPrice"], "RUB cash valuation") != 1:
                    raise ValueError("RUB cash valuation must be one")
                has_cash_position = True
                portfolio_cash += quantity
                continue
            if position.get("instrumentUid") != uid or position.get("instrumentType") != "share":
                raise ValueError("Account must contain only the selected share")
            current_price = position.get("currentPrice")
            valuation = _rub_amount(current_price, "Share valuation")
            if valuation <= 0:
                raise ValueError("Share valuation must be positive")
            share_value += quantity * valuation
            portfolio_shares += quantity
    if portfolio_shares != Decimal(shares):
        raise ValueError("Portfolio and security balances disagree; retry after settlement")
    if shares % lot:
        raise ValueError("Share balance is not a whole number of current lots")
    tolerance = Decimal("0.01")
    if has_cash_position and abs(portfolio_cash - cash) > tolerance:
        raise ValueError("Portfolio RUB cash and available cash balances disagree")
    if abs(equity - cash - share_value) > tolerance:
        raise ValueError("Portfolio value is inconsistent with account holdings")
    return equity, cash, shares


def run_step(client: Any, account_id: str, ticker: str, *, state_path: str | Path,
             fast: int = 20, slow: int = 60,
             max_allocation: Decimal = Decimal("0.2"),
             max_drawdown: Decimal = Decimal("0.1"),
             commission: Decimal = Decimal("0.0005"), submit: bool = False,
             now: datetime | None = None) -> dict:
    """Plan one rebalance; submit=True permits only sandbox virtual orders.

    The state file belongs to one account, instrument and configuration.  Dry runs
    update observed equity/risk but never submit or mark a signal as executed.
    A timed-out request is retained until the broker confirms its terminal state.
    """
    if not isinstance(submit, bool):
        raise ValueError("submit must be boolean")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("A dedicated sandbox account ID is required")
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError("A share ticker is required")
    if isinstance(fast, bool) or isinstance(slow, bool) or not isinstance(fast, int) or not isinstance(slow, int) or not 0 < fast < slow:
        raise ValueError("Moving-average windows must satisfy 0 < fast < slow")
    max_allocation = _decimal(max_allocation, "maximum allocation")
    max_drawdown = _decimal(max_drawdown, "maximum drawdown")
    commission = _decimal(commission, "commission")
    if not 0 < max_allocation <= 1 or not 0 < max_drawdown < 1 or not 0 <= commission < 1:
        raise ValueError("Invalid allocation, drawdown or commission")
    now = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    instrument = client.resolve_share(ticker.strip().upper(), class_code="TQBR")
    if (not instrument.uid or instrument.class_code != "TQBR"
            or instrument.currency.lower() != "rub" or "MOEX" not in instrument.exchange.upper()
            or isinstance(instrument.lot, bool) or not isinstance(instrument.lot, int) or instrument.lot <= 0
            or not instrument.api_trade_available):
        raise ValueError("Instrument must be a tradable MOEX TQBR RUB share with a valid lot")
    identity = {"account_id": account_id, "uid": instrument.uid,
                "ticker": instrument.ticker, "lot": instrument.lot,
                "strategy": "sma-entry-hold-v1",
                "fast": fast, "slow": slow, "max_allocation": str(max_allocation.normalize()),
                "max_drawdown": str(max_drawdown.normalize()), "commission": str(commission.normalize())}
    if str(state_path) == ":memory:":
        raise ValueError("Persistent state is required")
    path = Path(state_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The separate SQLite write transaction guards the entire operation, while
    # state updates are committed durably before any network order submission.
    lock = sqlite3.connect(str(path) + ".lock.sqlite3", timeout=30)
    connection: sqlite3.Connection | None = None
    try:
        lock.execute("CREATE TABLE IF NOT EXISTS step_lock(id INTEGER PRIMARY KEY)")
        lock.commit()
        lock.execute("BEGIN IMMEDIATE")
        connection = sqlite3.connect(path, timeout=30)
        connection.execute("CREATE TABLE IF NOT EXISTS bot_state(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        connection.commit()
        row = connection.execute("SELECT data FROM bot_state WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else {"version": 1, "identity": identity,
                    "high_water": "0", "halted": False, "handled_signal": None,
                    "risk_handled_signal": None, "failed_signal": None,
                    "risk_failed_signal": None, "pending": None}
        if not isinstance(state, dict) or state.get("version") != 1 or state.get("identity") != identity:
            raise ValueError("State belongs to a different account, instrument or configuration; use another state file")
        if not isinstance(state.get("halted"), bool) or _decimal(state.get("high_water"), "stored high water") < 0:
            raise ValueError("Invalid stored risk state")
        if not row:
            _save(connection, state)
        pending = state.get("pending")
        if pending:
            try:
                try:
                    report = client.get_sandbox_order_state(account_id, pending["order_id"])
                except Exception:
                    # Some temporary lookup failures still permit confirmation
                    # from the active orders list by the original request UUID.
                    active = client.get_sandbox_orders(account_id)
                    matches = [order for order in active if
                               order.get("orderRequestId") == pending["order_id"]
                               or order.get("orderId") == pending["order_id"]]
                    if len(matches) != 1:
                        raise ValueError("Cannot uniquely reconcile the pending request")
                    report = matches[0]
                status, executed = _order_status(report, pending)
            except Exception:
                return _result("wait", "pending_order_uncertain", signal_time=pending["signal_time"],
                               lots=pending["lots"], order_id=pending["order_id"], submit=submit)
            if status not in _TERMINAL:
                return _result("wait", "pending_order_open", signal_time=pending["signal_time"],
                               lots=pending["lots"], lots_executed=executed, order_id=pending["order_id"], submit=submit)
            _finish_order(state, pending, status, executed)
            _save(connection, state)
            return _result("hold", "pending_order_executed" if executed else "pending_order_failed",
                           signal_time=pending["signal_time"], lots=pending["lots"], lots_executed=executed,
                           order_status=status, order_id=pending["order_id"], submit=submit)
        orders = client.get_sandbox_orders(account_id)
        if not isinstance(orders, list):
            raise ValueError("Invalid open-orders response")
        if orders:
            raise ValueError("Dedicated account has open orders outside this bot's pending state")
        portfolio = client.get_sandbox_portfolio(account_id)
        positions = client.get_sandbox_positions(account_id)
        equity, cash, shares = _account_values(portfolio, positions, instrument.uid, instrument.lot)
        high_water = max(_decimal(state.get("high_water"), "stored high water"), equity)
        drawdown = (high_water - equity) / high_water if high_water else Decimal(0)
        if drawdown >= max_drawdown:
            state["halted"] = True
        state["high_water"] = str(high_water)
        _save(connection, state)
        diagnostics = {"submit": submit, "equity": str(equity), "cash": str(cash),
                       "shares": shares, "high_water": str(high_water),
                       "drawdown": str(drawdown), "halted": state["halted"]}
        today = now.astimezone(MOEX_TZ).date()
        candles = client.get_daily_candles(instrument.uid, now - timedelta(days=max(slow * 3, slow + 30)), now)
        if any(candle.time > now for candle in candles):
            raise ValueError("Received a future candle")
        if any(a.time >= b.time for a, b in zip(candles, candles[1:])):
            raise ValueError("Candles must have unique increasing timestamps")
        completed = [candle for candle in candles
                     if candle.time.astimezone(MOEX_TZ).date() < today and candle.volume > 0]
        if not completed:
            return _result("hold", "no_completed_candle", **diagnostics)
        last = completed[-1]
        signal_time = last.time.astimezone(timezone.utc).isoformat()
        candle_day = last.time.astimezone(MOEX_TZ).date()
        if (today - candle_day).days > 5 or candle_day.weekday() >= 5:
            return _result("hold", "stale_signal_candle", signal_time=signal_time, **diagnostics)
        risk_exit = bool(state["halted"] and shares)
        if state["halted"] and not shares:
            return _result("hold", "risk_halt", signal_time=signal_time, **diagnostics)
        handled_key = "risk_handled_signal" if risk_exit else "handled_signal"
        if state.get(handled_key) == signal_time:
            return _result("hold", "signal_already_handled", signal_time=signal_time, **diagnostics)
        failure_key = "risk_failed_signal" if risk_exit else "failed_signal"
        if state.get(failure_key) == signal_time:
            return _result("hold", "signal_order_failed", signal_time=signal_time, **diagnostics)
        desired = False if risk_exit else desired_position(completed, fast, slow)
        if desired is None:
            return _result("hold", "insufficient_history", signal_time=signal_time, **diagnostics)
        price = _decimal(client.get_last_price(instrument.uid), "last price")
        if price <= 0:
            raise ValueError("Last price must be positive")
        held_lots = shares // instrument.lot
        per_lot = price * instrument.lot
        reserve_per_lot = per_lot * (1 + commission)
        # Match the backtest: cap the initial entry, hold it while SMA is true,
        # and exit all shares when false. No daily purchases or position trims.
        target_lots = (held_lots or int((equity * max_allocation / per_lot).to_integral_value(rounding=ROUND_FLOOR))) if desired else 0
        delta = target_lots - held_lots
        if delta > 0:
            allocation_available = max(Decimal(0), equity * max_allocation - per_lot * held_lots)
            affordable = int((min(cash, allocation_available) / reserve_per_lot).to_integral_value(rounding=ROUND_FLOOR))
            lots = min(delta, affordable)
            if not lots:
                return _result("hold", "insufficient_funds_for_lot", signal_time=signal_time,
                               price=str(price), **diagnostics)
            direction = "BUY"
            reason = "rebalance_buy"
            if not instrument.buy_available:
                raise ValueError("Buying this instrument is unavailable")
        elif delta < 0:
            lots = -delta
            direction = "SELL"
            reason = "drawdown_exit" if risk_exit else "rebalance_sell"
            if not instrument.sell_available:
                raise ValueError("Selling this instrument is unavailable")
        else:
            reason = "insufficient_allocation_for_lot" if desired and not held_lots else "at_target"
            return _result("hold", reason, signal_time=signal_time, price=str(price), **diagnostics)
        plan = _result(direction.lower(), reason, signal_time=signal_time, lots=lots,
                       price=str(price), estimated_notional=str(per_lot * lots),
                       commission_reserve=str(per_lot * lots * commission), **diagnostics)
        if today.weekday() >= 5:
            return {**plan, "action": "hold", "planned_action": direction.lower(), "reason": "weekend_calendar_guard"}
        if not submit:
            return {**plan, "dry_run": True}
        request_identity = json.dumps({**identity, "signal_time": signal_time,
                "direction": direction, "lots": lots, "risk_exit": risk_exit}, sort_keys=True)
        order_id = str(uuid5(NAMESPACE_URL, "moex-sandbox:" + request_identity))
        pending = {"order_id": order_id, "signal_time": signal_time, "direction": direction,
                   "lots": lots, "risk_exit": risk_exit, "uid": instrument.uid}
        state["pending"] = pending
        _save(connection, state)
        try:
            report = client.post_sandbox_order(account_id, instrument.uid, lots, direction, order_id)
            status, executed = _order_status(report, pending)
        except Exception:
            return {**plan, "action": "wait", "planned_action": direction.lower(),
                    "reason": "submission_uncertain", "order_id": order_id}
        if status in _TERMINAL:
            _finish_order(state, pending, status, executed)
            _save(connection, state)
        if status in {"EXECUTION_REPORT_STATUS_REJECTED", "EXECUTION_REPORT_STATUS_CANCELLED"} and not executed:
            failure_reason = "order_rejected" if status == "EXECUTION_REPORT_STATUS_REJECTED" else "order_cancelled"
            return {**plan, "action": "hold", "planned_action": direction.lower(),
                    "order_id": order_id, "order_status": status,
                    "lots_executed": executed, "reason": failure_reason}
        return {**plan, "order_id": order_id, "order_status": status,
                "lots_executed": executed, "reason": reason if status != "EXECUTION_REPORT_STATUS_REJECTED" else "order_rejected"}
    finally:
        if connection is not None:
            connection.close()
        if lock.in_transaction:
            lock.rollback()
        lock.close()
