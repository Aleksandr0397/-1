"""Read-only, stateless public observations of a dedicated sandbox account.

SMA proposals describe the current strategy signal, not submitted or executed
orders. No trading state or drawdown baseline is created or read here. Only
completed candle prices have market timestamps; the adapter's untimed latest
quote is shown separately and is never appended to the chart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import re
from typing import Any, Callable

from .engine import desired_position
from .models import Candle, Instrument
from .sandbox import MOEX_TZ, _account_values


class MonitorError(ValueError):
    """An allowlisted public error without broker messages or account identity."""

    CODES = frozenset({"invalid_monitor_configuration", "sandbox_account_not_found",
                       "invalid_instrument", "invalid_market_data",
                       "unsupported_account_holdings", "broker_unavailable", "operation_in_progress"})

    def __init__(self, code: str):
        if code not in self.CODES:
            code = "broker_unavailable"
        self.code = code
        super().__init__(code)


def _read(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return method(*args, **kwargs)
    except Exception:
        # External errors may include identifiers or credentials. The monitor
        # supplies only a static code, including when logging its exception.
        raise MonitorError("broker_unavailable") from None


def _iso(time: datetime) -> str:
    return time.astimezone(timezone.utc).isoformat()


def collect(client: Any, account_id: str, ticker: str, *, now: datetime | None = None) -> dict:
    """Collect sanitized current values and SMA20/60 proposals using reads only.

    ``account_id`` is explicitly configured server-side. It must be present in
    the sandbox account list and is never included in the public response.
    ``MonitorError.code`` is safe for the caller's cache/failure status contract.
    The caller owns refresh scheduling, observation events and failure caching.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    if (not isinstance(account_id, str) or not 1 <= len(account_id) <= 128
            or any(ch.isspace() or ord(ch) < 32 for ch in account_id)
            or not isinstance(ticker, str)
            or not re.fullmatch(r"[A-Z0-9]{1,16}", ticker.strip().upper(), re.ASCII)
            or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
        raise MonitorError("invalid_monitor_configuration")
    ticker = ticker.strip().upper()
    accounts = _read(client.list_sandbox_accounts)
    if not isinstance(accounts, list) or any(not isinstance(account, str) for account in accounts):
        raise MonitorError("broker_unavailable")
    if account_id not in accounts:
        raise MonitorError("sandbox_account_not_found")
    instrument = _read(client.resolve_share, ticker, class_code="TQBR")
    if (not isinstance(instrument, Instrument)
            or any(not isinstance(getattr(instrument, key), str) for key in
                   ("uid", "ticker", "class_code", "currency", "exchange"))
            or not instrument.uid
            or instrument.ticker.upper() != ticker or instrument.class_code != "TQBR"
            or instrument.currency.lower() != "rub" or "MOEX" not in instrument.exchange.upper()
            or type(instrument.lot) is not int or instrument.lot <= 0
            or any(type(getattr(instrument, key)) is not bool for key in
                   ("api_trade_available", "buy_available", "sell_available"))
            or not instrument.api_trade_available):
        raise MonitorError("invalid_instrument")

    orders = _read(client.get_sandbox_orders, account_id)
    if not isinstance(orders, list) or any(not isinstance(order, dict) for order in orders):
        raise MonitorError("broker_unavailable")
    portfolio = _read(client.get_sandbox_portfolio, account_id)
    positions = _read(client.get_sandbox_positions, account_id)
    try:
        equity, cash, shares = _account_values(portfolio, positions, instrument.uid, instrument.lot)
    except Exception:
        raise MonitorError("unsupported_account_holdings") from None

    candles = _read(client.get_daily_candles, instrument.uid, now - timedelta(days=180), now)
    if (not isinstance(candles, list) or any(not isinstance(candle, Candle) for candle in candles)
            or any(candle.time > now for candle in candles)
            or any(a.time >= b.time for a, b in zip(candles, candles[1:]))):
        raise MonitorError("invalid_market_data")
    today = now.astimezone(MOEX_TZ).date()
    completed = [candle for candle in candles
                 if candle.time.astimezone(MOEX_TZ).date() < today and candle.volume > 0]
    price = _read(client.get_last_price, instrument.uid)
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise MonitorError("invalid_market_data")
    result = {
        "status": "connected", "ticker": ticker, "execution_mode": "observe_on_demand",
        "updated_at": _iso(now), "signal_time": _iso(completed[-1].time) if completed else None,
        "equity": str(equity), "cash": str(cash), "price": str(price), "shares": shares,
        "last_action": "hold", "action_reason": "no_completed_candle", "planned_lots": 0,
        "chart": [{"time": _iso(candle.time), "price": str(candle.close)} for candle in completed[-180:]],
        "events": [], "error": None,
    }
    if orders:
        return {**result, "last_action": "wait", "action_reason": "active_sandbox_orders"}
    if not completed:
        return result
    candle_day = completed[-1].time.astimezone(MOEX_TZ).date()
    if (today - candle_day).days > 5 or candle_day.weekday() >= 5:
        return {**result, "action_reason": "stale_signal_candle"}
    desired = desired_position(completed, 20, 60)
    if desired is None:
        return {**result, "action_reason": "insufficient_history"}
    if today.weekday() >= 5:
        return {**result, "last_action": "wait", "action_reason": "weekend_calendar_guard"}
    held_lots = shares // instrument.lot
    if desired and held_lots:
        return {**result, "action_reason": "sma_hold_position"}
    if not desired:
        if not held_lots:
            return {**result, "action_reason": "sma_hold_cash"}
        if not instrument.sell_available:
            return {**result, "last_action": "wait", "action_reason": "selling_unavailable"}
        return {**result, "last_action": "sell", "action_reason": "sma_sell_proposal",
                "planned_lots": held_lots}
    per_lot = price * instrument.lot
    budget = equity * Decimal("0.2")
    target_lots = int((budget / per_lot).to_integral_value(rounding=ROUND_FLOOR))
    if not target_lots:
        return {**result, "action_reason": "insufficient_allocation_for_lot"}
    affordable = int((min(cash, budget) / (per_lot * Decimal("1.0005")))
                     .to_integral_value(rounding=ROUND_FLOOR))
    lots = min(target_lots, affordable)
    if not lots:
        return {**result, "action_reason": "insufficient_funds_for_lot"}
    if not instrument.buy_available:
        return {**result, "last_action": "wait", "action_reason": "buying_unavailable"}
    return {**result, "last_action": "buy", "action_reason": "sma_buy_proposal", "planned_lots": lots}
