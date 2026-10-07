"""Manual read-only proof for restoring this known, untraded sandbox account.

This is deliberately limited to the approved SBER/one-share-lot, 100000 RUB
baseline. It returns state to its authenticated caller; it never writes files,
creates an account, funds it, or submits an order. It must not run on startup.

The sandbox may omit virtual PayIn from its trading operations, and operation
publication can be delayed. An empty response is therefore accepted only with
an explicitly trusted prior cash-only checkpoint plus matching current broker
state. It is not treated as an independently complete trading-history proof.
Official contracts and caveats: RussianInvestments/investAPI, docs/contracts/
sandbox.proto, docs/head-sandbox.md, and docs/head-operations.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from .models import Instrument
from .sandbox import _account_values
from .tbank import _integer, money


class RestoreError(ValueError):
    """An allowlisted restoration failure without broker payloads or secrets."""

    CODES = frozenset({"invalid_restore_request", "sandbox_account_not_found",
                       "invalid_instrument", "active_sandbox_orders",
                       "unsupported_account_holdings", "cash_balance_mismatch",
                       "history_not_proven", "history_truncated",
                       "history_contains_activity", "broker_unavailable"})

    def __init__(self, code: str):
        self.code = code if code in self.CODES else "broker_unavailable"
        super().__init__(self.code)


def _identifier(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 128
            and not any(ch.isspace() or ord(ch) < 32 for ch in value))


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware time required")
    return value.astimezone(timezone.utc)


def _read(client: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    try:
        return getattr(client, method)(*args, **kwargs)
    except Exception:
        raise RestoreError("broker_unavailable") from None


def _objects(data: dict, key: str) -> list[dict]:
    result = data.get(key, [])
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise ValueError("Invalid collection")
    return result


def _rub(value: Any) -> Decimal:
    if not isinstance(value, dict) or value.get("currency", "").lower() != "rub":
        raise ValueError("RUB amount required")
    return money(value)


def _cash_only(portfolio: Any, positions: Any, account_id: str, uid: str,
               initial_cash: Decimal) -> tuple[str, str] | None:
    cash_identity = None
    try:
        if not isinstance(portfolio, dict) or not isinstance(positions, dict):
            raise ValueError("Invalid holdings")
        if any("accountId" in data and data["accountId"] != account_id for data in (portfolio, positions)):
            raise ValueError("Holdings belong to another account")
        if _objects(portfolio, "virtualPositions"):
            raise ValueError("Virtual security positions are unsupported")
        if type(positions.get("limitsLoadingInProgress", False)) is not bool or positions.get("limitsLoadingInProgress", False):
            raise ValueError("Holdings are still loading")
        balances = _objects(positions, "money")
        if len(balances) != 1:
            raise ValueError("One RUB balance required")
        _rub(balances[0])
        for kind in ("securities", "futures", "options"):
            if _objects(positions, kind):
                raise ValueError("No security positions allowed")
        for blocked in _objects(positions, "blocked"):
            if _rub(blocked) != 0:
                raise ValueError("Blocked cash")
        for key in ("totalAmountShares", "totalAmountBonds", "totalAmountEtf", "totalAmountFutures",
                    "totalAmountOptions", "totalAmountSp"):
            if key in portfolio and _rub(portfolio[key]) != 0:
                raise ValueError("Non-cash portfolio value")
        if "totalAmountCurrencies" in portfolio and _rub(portfolio["totalAmountCurrencies"]) != initial_cash:
            raise ValueError("Portfolio currency value differs")
        cash_positions = _objects(portfolio, "positions")
        if len(cash_positions) > 1:
            raise ValueError("One RUB portfolio position allowed")
        for position in cash_positions:
            if (position.get("instrumentType") != "currency" or position.get("figi") != "RUB000UTSTOM"
                    or money(position.get("quantity")) != initial_cash
                    or _rub(position.get("currentPrice")) != 1
                    or money(position.get("blockedLots", {})) != 0):
                raise ValueError("Unexpected portfolio position")
            instrument_uid, position_uid = position.get("instrumentUid", ""), position.get("positionUid", "")
            if any(not isinstance(value, str) or (value and not _identifier(value))
                   for value in (instrument_uid, position_uid)):
                raise ValueError("Invalid RUB position identity")
            if instrument_uid == uid:
                raise ValueError("RUB cash cannot identify the selected share")
            if instrument_uid and position_uid:
                cash_identity = (instrument_uid, position_uid)
        equity, cash, shares = _account_values(portfolio, positions, uid, 1)
    except Exception:
        raise RestoreError("unsupported_account_holdings") from None
    if equity != initial_cash or cash != initial_cash or shares != 0:
        raise RestoreError("cash_balance_mismatch")
    return cash_identity


def _history(operations: Any, initial_cash: Decimal, start: datetime, end: datetime,
             prior_checkpoint_trusted: bool, cash_identity: tuple[str, str] | None) -> None:
    if not isinstance(operations, list) or any(not isinstance(item, dict) for item in operations):
        raise RestoreError("history_not_proven")
    # The legacy endpoint provides no pagination/completeness flag and returns
    # at most the latest 1000 operations. A full response may hide older trades.
    if len(operations) >= 1000:
        raise RestoreError("history_truncated")
    if not operations:
        if not prior_checkpoint_trusted:
            raise RestoreError("history_not_proven")
        return
    if len(operations) != 1:
        raise RestoreError("history_contains_activity")
    operation = operations[0]
    try:
        date = _utc(operation.get("date"))
        if not start <= date <= end or not _identifier(operation.get("id")):
            raise ValueError("Invalid operation identity or time")
        payment_value = operation.get("payment")
        if not isinstance(payment_value, dict) or not isinstance(payment_value.get("currency"), str):
            raise ValueError("Invalid payment currency")
        payment = money(payment_value)
        quantity = _integer(operation.get("quantity", 0))
        remaining = _integer(operation.get("quantityRest", 0))
        trades = _objects(operation, "trades")
        child_operations = _objects(operation, "childOperations")
        price = money(operation.get("price", {}))
        if not isinstance(operation.get("currency"), str):
            raise ValueError("Invalid operation currency")
    except Exception:
        raise RestoreError("history_not_proven") from None
    # Accept only an exact initial virtual funding record if the gateway does
    # supply one. Nothing associated with securities, fees, or withdrawals can
    # be used to reconstruct an untraded baseline, even if balances match.
    if (operation.get("operationType") != "OPERATION_TYPE_INPUT"
            or operation.get("state") != "OPERATION_STATE_EXECUTED"
            or operation["currency"].lower() != "rub" or payment_value["currency"].lower() != "rub"
            or payment != initial_cash
            or quantity != 0 or remaining != 0 or child_operations or price != 0
            or operation.get("instrumentType", "") not in ("", "currency")
            or operation.get("assetUid") or operation.get("parentOperationId")):
        raise RestoreError("history_contains_activity")
    if not trades and not any(operation.get(key) for key in ("figi", "instrumentUid", "positionUid")):
        return
    # The observed SandboxPayIn uses RUB identifiers and one zero-quantity,
    # zero-price ledger marker. Link BOTH identifiers to the independently
    # validated current RUB position before interpreting it as cash funding.
    if (cash_identity is None or operation.get("figi") != "RUB000UTSTOM"
            or operation.get("instrumentUid") != cash_identity[0]
            or operation.get("positionUid") != cash_identity[1] or len(trades) != 1):
        raise RestoreError("history_contains_activity")
    marker = trades[0]
    try:
        root_price_currency = operation.get("price", {}).get("currency", "")
        if not isinstance(root_price_currency, str):
            raise ValueError("Invalid cash funding price currency")
        marker_quantity = _integer(marker.get("quantity", 0))
        marker_price = marker.get("price")
        marker_value = money(marker_price)
        marker_currency = marker_price.get("currency", "")
        if not isinstance(marker_currency, str):
            raise ValueError("Invalid cash marker currency")
        marker_date = _utc(marker.get("dateTime"))
    except Exception:
        raise RestoreError("history_not_proven") from None
    if (root_price_currency.lower() not in ("", "rub")
            or marker_quantity != 0 or marker_value != 0 or marker_currency.lower() not in ("", "rub")
            or marker_date != date):
        raise RestoreError("history_contains_activity")
    if not prior_checkpoint_trusted:
        raise RestoreError("history_not_proven")


def restore_cash_account(client: Any, account_id: str, ticker: str, initial_cash: Decimal,
                         created_at: datetime | str, now: datetime | None = None, *,
                         prior_checkpoint_trusted: bool = False) -> dict:
    """Return the approved bot baseline only after conservative read-only checks.

    ``prior_checkpoint_trusted`` must come from the authenticated operator's
    separately validated historical receipt, never from an unauthenticated
    request or automatically inferred current balance. The caller owns locking,
    one-time persistence, and refusal to overwrite any existing trading state.
    The API supplies current evidence rather than an atomic historical snapshot;
    no other actor may trade or change this dedicated account during recovery.
    """
    try:
        created_at = _utc(created_at)
        now = _utc(now if now is not None else datetime.now(timezone.utc))
        start = created_at - timedelta(minutes=1)
        if (not _identifier(account_id) or not isinstance(ticker, str) or ticker.strip().upper() != "SBER"
                or not isinstance(initial_cash, Decimal) or not initial_cash.is_finite()
                or initial_cash != Decimal("100000") or created_at > now
                or type(prior_checkpoint_trusted) is not bool):
            raise ValueError("Unapproved restoration request")
    except Exception:
        raise RestoreError("invalid_restore_request") from None
    accounts = _read(client, "list_sandbox_accounts")
    if not isinstance(accounts, list) or any(not _identifier(account) for account in accounts):
        raise RestoreError("broker_unavailable")
    if account_id not in accounts:
        raise RestoreError("sandbox_account_not_found")
    instrument = _read(client, "resolve_share", "SBER", class_code="TQBR")
    if (not isinstance(instrument, Instrument) or not _identifier(instrument.uid)
            or instrument.ticker != "SBER" or instrument.class_code != "TQBR"
            or instrument.currency != "rub" or not isinstance(instrument.exchange, str)
            or "MOEX" not in instrument.exchange.upper() or type(instrument.lot) is not int
            or instrument.lot != 1 or instrument.api_trade_available is not True
            or type(instrument.buy_available) is not bool or type(instrument.sell_available) is not bool):
        raise RestoreError("invalid_instrument")
    orders = _read(client, "get_sandbox_orders", account_id)
    if not isinstance(orders, list) or any(not isinstance(order, dict) for order in orders):
        raise RestoreError("broker_unavailable")
    if orders:
        raise RestoreError("active_sandbox_orders")
    portfolio = _read(client, "get_sandbox_portfolio", account_id)
    positions = _read(client, "get_sandbox_positions", account_id)
    cash_identity = _cash_only(portfolio, positions, account_id, instrument.uid, initial_cash)
    operations = _read(client, "get_sandbox_operations", account_id, start, now)
    _history(operations, initial_cash, start, now, prior_checkpoint_trusted, cash_identity)
    return {
        "version": 1,
        "identity": {"account_id": account_id, "uid": instrument.uid, "ticker": "SBER", "lot": 1,
                     "strategy": "sma-entry-hold-v1", "fast": 20, "slow": 60,
                     "max_allocation": "0.2", "max_drawdown": "0.1", "commission": "0.0005"},
        "high_water": "100000", "halted": False, "pending": None,
        "handled_signal": None, "risk_handled_signal": None,
        "failed_signal": None, "risk_failed_signal": None,
    }
