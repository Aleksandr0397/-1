"""Small REST client for T-Invest's sandbox, using only the standard library.

Contracts: https://github.com/RussianInvestments/investAPI/tree/main/src/docs
The endpoint is intentionally fixed. No real-money order service is supported.
"""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import UUID

from .models import Candle, Instrument


SANDBOX_REST_URL = "https://sandbox-invest-public-api.tbank.ru/rest"
_PACKAGE = "tinkoff.public.invest.api.contract.v1"
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_DAY_WINDOW = timedelta(days=365)
_INTEGER = re.compile(r"-?\d+\Z", re.ASCII)
Transport = Callable[[urllib.request.Request, float], bytes]


class ApiError(RuntimeError):
    """A safe error without response payloads, credentials, or remote messages."""

    def __init__(self, message: str, *, status_code: int | None = None, reason: str | None = None,
                 broker_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.broker_code = broker_code


def _integer(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("Expected an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and len(value) <= 20 and _INTEGER.fullmatch(value):
        return int(value)
    raise ValueError("Expected an integer")


def money(value: dict) -> Decimal:
    """Decode protobuf MoneyValue/Quotation without floating-point conversion.

    Omitted scalar fields mean zero in protobuf JSON. Invalid nano ranges and
    inconsistent signs are rejected rather than interpreted as a price.
    """
    if not isinstance(value, dict):
        raise ValueError("Expected a MoneyValue or Quotation object")
    units = _integer(value.get("units", 0))
    nano = _integer(value.get("nano", 0))
    if not _INT64_MIN <= units <= _INT64_MAX or not -999_999_999 <= nano <= 999_999_999:
        raise ValueError("MoneyValue is outside the protobuf range")
    if (units > 0 and nano < 0) or (units < 0 and nano > 0):
        raise ValueError("MoneyValue has inconsistent signs")
    with localcontext() as ctx:
        ctx.prec = 40
        return Decimal(units) + Decimal(nano) / Decimal(1_000_000_000)


def quotation(value: Decimal) -> dict:
    """Encode an exact Decimal as protobuf JSON, rejecting lost precision."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Quotation must be a finite Decimal")
    # Bound values before as_integer_ratio to avoid expanding huge exponents.
    if value < Decimal("-9223372036854775808.999999999") or value > Decimal("9223372036854775807.999999999"):
        raise ValueError("Quotation is outside the protobuf range")
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    trailing_zeros = 0
    for digit in reversed(digits):
        if digit:
            break
        trailing_zeros += 1
    if value and exponent + trailing_zeros < -9:
        raise ValueError("Quotation supports at most nine decimal places")
    numerator, denominator = value.as_integer_ratio()
    nanounits, remainder = divmod(abs(numerator) * 1_000_000_000, denominator)
    if remainder:
        raise ValueError("Quotation supports at most nine decimal places")
    units, nano = divmod(nanounits, 1_000_000_000)
    if numerator < 0:
        units, nano = -units, -nano
    return {"units": str(units), "nano": nano}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must never forward the Authorization header to another host.
        return None


def _http_transport(request: urllib.request.Request, timeout: float) -> bytes:
    # Additional official roots supplement system trust; hostname verification
    # and the inherited HTTP proxy remain enabled. This cannot change trust in
    # an external proxy, but permits a separately configured runtime to connect.
    context = ssl.create_default_context()
    if extra_ca := os.environ.get("TINVEST_CA_FILE"):
        context.load_verify_locations(cafile=extra_ca)
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))
    with opener.open(request, timeout=timeout) as response:
        return response.read(_MAX_RESPONSE_BYTES + 1)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("Expected a nonempty identifier without whitespace")
    return value


def _text(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("Missing instrument field")
    return value


def _flag(data: dict, key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise ValueError("Invalid boolean field")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Time must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid candle timestamp")
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _json_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _objects(data: dict, key: str) -> list[dict]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ApiError("Invalid broker response structure")
    return value


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str):
    raise ValueError("Invalid JSON numeric constant")


class TInvestClient:
    def __init__(self, token: str, timeout: float = 20, transport: Transport | None = None) -> None:
        if not isinstance(token, str) or not token or any(ch.isspace() or ord(ch) < 32 for ch in token):
            raise ValueError("A nonempty API token without whitespace is required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be a finite positive number")
        self._token = token
        self._timeout = float(timeout)
        self._transport = transport if transport is not None else _http_transport

    def _call(self, service: str, method: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"{SANDBOX_REST_URL}/{_PACKAGE}.{service}/{method}",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            payload = self._transport(request, self._timeout)
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read(4096)
            except (OSError, ValueError, AttributeError):
                error_body = b''
            if exc.code == 503:
                if isinstance(error_body, bytes) and b'upstream connect error' in error_body and b'CERTIFICATE_VERIFY_FAILED' in error_body:
                    raise ApiError('Cloud proxy cannot verify the broker certificate; authentication was not reached',
                                   status_code=503, reason='proxy_tls_certificate') from None
            broker_code = None
            try:
                error_data = json.loads(error_body)
                candidate = _integer(error_data.get('code'))
                if 0 <= candidate <= 999_999_999:
                    broker_code = candidate
            except (ValueError, TypeError, AttributeError, UnicodeError):
                pass
            raise ApiError(f"Broker HTTP error ({exc.code})", status_code=exc.code, broker_code=broker_code) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise ApiError("Broker TLS certificate verification failed; authentication was not reached",
                               reason="broker_tls_certificate") from None
            raise ApiError("Broker connection failed; request outcome may be unknown") from None
        except (OSError, TimeoutError):
            raise ApiError("Broker connection failed; request outcome may be unknown") from None
        if not isinstance(payload, bytes) or len(payload) > _MAX_RESPONSE_BYTES:
            raise ApiError("Invalid or oversized broker response")
        try:
            data = json.loads(payload, object_pairs_hook=_json_object, parse_constant=_reject_json_constant)
        except (ValueError, UnicodeError):
            raise ApiError("Invalid JSON response from broker") from None
        if not isinstance(data, dict):
            raise ApiError("Invalid broker response structure")
        if "code" in data and ("message" in data or "description" in data):
            raise ApiError("Broker rejected the request")
        return data

    def resolve_share(self, ticker: str, class_code: str = "TQBR") -> Instrument:
        ticker, class_code = _identifier(ticker).upper(), _identifier(class_code).upper()
        result = self._call("InstrumentsService", "ShareBy", {"idType": "INSTRUMENT_ID_TYPE_TICKER", "classCode": class_code, "id": ticker})
        try:
            data = result["instrument"]
            if not isinstance(data, dict) or _text(data, "ticker").upper() != ticker or _text(data, "classCode").upper() != class_code:
                raise ValueError("Instrument does not match the request")
            lot = _integer(data["lot"])
            if lot <= 0 or lot > 2**31 - 1:
                raise ValueError("Invalid lot size")
            return Instrument(
                uid=_identifier(data["uid"]), ticker=_text(data, "ticker"), class_code=_text(data, "classCode"),
                name=_text(data, "name"), currency=_text(data, "currency").lower(), lot=lot,
                exchange=_text(data, "exchange"), api_trade_available=_flag(data, "apiTradeAvailableFlag"),
                buy_available=_flag(data, "buyAvailableFlag"), sell_available=_flag(data, "sellAvailableFlag"),
            )
        except (KeyError, ValueError, TypeError):
            raise ApiError("Invalid share metadata from broker") from None

    def get_daily_candles(self, instrument_id: str, start: datetime, end: datetime) -> list[Candle]:
        """Fetch completed exchange bars for [start, end), sorted in UTC.

        Windows are at most 365 days, well below the daily REST limit (6 years,
        2400 bars). Adjacent boundary duplicates are checked and deduplicated.
        """
        instrument_id = _identifier(instrument_id)
        start, end = _utc(start), _utc(end)
        if start >= end:
            raise ValueError("Candle start must precede end")
        candles: dict[datetime, Candle] = {}
        cursor = start
        while cursor < end:
            page_end = min(cursor + _DAY_WINDOW, end)
            result = self._call("MarketDataService", "GetCandles", {
                "instrumentId": instrument_id, "from": _json_time(cursor), "to": _json_time(page_end),
                # A one-year daily window fits the service's default response
                # size. Omit the optional limit for older REST gateways.
                "interval": "CANDLE_INTERVAL_DAY", "candleSourceType": "CANDLE_SOURCE_EXCHANGE",
            })
            for item in _objects(result, "candles"):
                try:
                    if not _flag(item, "isComplete"):
                        continue
                    time = _timestamp(item["time"])
                    if not cursor <= time <= page_end or not start <= time < end:
                        continue
                    volume = _integer(item.get("volume", 0))
                    if not 0 <= volume <= _INT64_MAX:
                        raise ValueError("Invalid candle volume")
                    candle = Candle(time=time, open=money(item["open"]), high=money(item["high"]),
                                    low=money(item["low"]), close=money(item["close"]), volume=volume)
                    if time in candles and candles[time] != candle:
                        raise ValueError("Conflicting completed bars")
                    candles[time] = candle
                except (KeyError, ValueError, TypeError):
                    raise ApiError("Invalid completed candle data from broker") from None
            cursor = page_end
        return sorted(candles.values(), key=lambda candle: candle.time)

    def get_last_price(self, instrument_id: str) -> Decimal:
        instrument_id = _identifier(instrument_id)
        result = self._call("MarketDataService", "GetLastPrices", {"instrumentId": [instrument_id]})
        prices = _objects(result, "lastPrices")
        try:
            if len(prices) != 1:
                raise ValueError("No unique last price")
            item = prices[0]
            identifiers = (item.get("instrumentUid"), item.get("figi"), f"{item.get('ticker', '')}_{item.get('classCode', '')}")
            if instrument_id not in identifiers:
                raise ValueError("Last price belongs to a different instrument")
            price = money(item["price"])
            if price <= 0:
                raise ValueError("Invalid last price")
            return price
        except (KeyError, ValueError, TypeError):
            raise ApiError("Invalid last price from broker") from None

    def list_sandbox_accounts(self) -> list[str]:
        result = self._call("SandboxService", "GetSandboxAccounts", {})
        try:
            return [_identifier(item["id"]) for item in _objects(result, "accounts")]
        except (KeyError, ValueError, TypeError):
            raise ApiError("Invalid sandbox accounts from broker") from None

    def open_sandbox_account(self) -> str:
        result = self._call("SandboxService", "OpenSandboxAccount", {})
        try:
            return _identifier(result["accountId"])
        except (KeyError, ValueError, TypeError):
            raise ApiError("Invalid sandbox account response") from None

    def sandbox_pay_in(self, account_id: str, amount: Decimal) -> dict:
        account_id = _identifier(account_id)
        encoded = quotation(amount)
        if amount <= 0:
            raise ValueError("Sandbox pay-in must be positive")
        return self._call("SandboxService", "SandboxPayIn", {"accountId": account_id, "amount": {"currency": "rub", **encoded}})

    def get_sandbox_portfolio(self, account_id: str) -> dict:
        return self._call("SandboxService", "GetSandboxPortfolio", {"accountId": _identifier(account_id), "currency": "RUB"})

    def get_sandbox_positions(self, account_id: str) -> dict:
        return self._call("SandboxService", "GetSandboxPositions", {"accountId": _identifier(account_id)})

    def get_sandbox_orders(self, account_id: str) -> list[dict]:
        return _objects(self._call("SandboxService", "GetSandboxOrders", {"accountId": _identifier(account_id)}), "orders")

    def get_sandbox_operations(self, account_id: str, start: datetime, end: datetime) -> list[dict]:
        """Read unfiltered account operations for an explicit UTC interval.

        The legacy endpoint returns at most the latest 1000 operations and has
        no completeness indicator; callers must reject a full 1000-row response
        when older operations matter. Publication may lag actual execution.
        Neither an instrument filter nor a state filter is applied, so security
        activity and any returned cancellations are visible to recovery checks.
        """
        account_id = _identifier(account_id)
        start, end = _utc(start), _utc(end)
        if start >= end:
            raise ValueError("Operation start must precede end")
        result = self._call("SandboxService", "GetSandboxOperations", {
            "accountId": account_id, "from": _json_time(start), "to": _json_time(end),
        })
        operations = _objects(result, "operations")
        if len(operations) > 1000:
            raise ApiError("Broker operation history exceeds the supported limit")
        return operations

    def get_sandbox_order_state(self, account_id: str, order_id: str) -> dict:
        """Look up the persisted client request UUID, including after a timeout."""
        return self._call("SandboxService", "GetSandboxOrderState", {"accountId": _identifier(account_id),
                         "orderId": _identifier(order_id), "orderIdType": "ORDER_ID_TYPE_REQUEST"})

    def post_sandbox_order(self, account_id: str, instrument_id: str, lots: int, direction: str, order_id: str) -> dict:
        account_id, instrument_id, order_id = _identifier(account_id), _identifier(instrument_id), _identifier(order_id)
        if not isinstance(lots, int) or isinstance(lots, bool) or not 0 < lots <= _INT64_MAX:
            raise ValueError("Order lots must be a positive int64 integer")
        if not isinstance(direction, str):
            raise ValueError("Order direction must be BUY or SELL")
        direction = direction.upper().removeprefix("ORDER_DIRECTION_")
        if direction not in ("BUY", "SELL"):
            raise ValueError("Order direction must be BUY or SELL")
        try:
            parsed = UUID(order_id)
        except ValueError:
            raise ValueError("Order request ID must be a UUID") from None
        if str(parsed) != order_id.lower():
            raise ValueError("Order request ID must be a hyphenated UUID")
        # Never retry mutations automatically: the caller persists this UUID
        # first and reconciles an uncertain result through GetSandboxOrderState.
        return self._call("SandboxService", "PostSandboxOrder", {
            "accountId": account_id, "instrumentId": instrument_id, "quantity": str(lots),
            "direction": f"ORDER_DIRECTION_{direction}", "orderType": "ORDER_TYPE_MARKET",
            "orderId": order_id, "confirmMarginTrade": False,
        })
