from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Candle:
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0

    def __post_init__(self) -> None:
        if self.time.tzinfo is None or self.time.utcoffset() is None:
            raise ValueError("Candle time must include a timezone")
        prices = (self.open, self.high, self.low, self.close)
        if any(not p.is_finite() or p <= 0 for p in prices):
            raise ValueError("Candle prices must be finite and positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Invalid candle OHLC range")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ValueError("Candle volume must be a nonnegative integer")


@dataclass(frozen=True)
class Instrument:
    uid: str
    ticker: str
    class_code: str
    name: str
    currency: str
    lot: int
    exchange: str
    api_trade_available: bool
    buy_available: bool
    sell_available: bool
