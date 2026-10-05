"""CSV and public MOEX ISS daily candles (unadjusted OHLC)."""
import csv
import json
import random
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from .models import Candle

MOSCOW = ZoneInfo('Europe/Moscow')


def _whole(value, label: str, *, positive=False) -> int:
    try:
        number = Decimal(str(value))
        if isinstance(value, bool) or not number.is_finite() or number != number.to_integral_value() or number < (1 if positive else 0):
            raise ValueError('Invalid integer')
        return int(number)
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise ValueError(f'{label} must be a finite {"positive" if positive else "nonnegative"} integer') from exc


def _moex_time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is not None:
        return stamp.astimezone(MOSCOW)
    return stamp.replace(tzinfo=MOSCOW)


def read_csv(path: str | Path) -> list[Candle]:
    with Path(path).open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        required = {'time', 'open', 'high', 'low', 'close', 'volume'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('CSV requires time,open,high,low,close,volume columns')
        bars = []
        for line, row in enumerate(reader, start=2):
            try:
                bars.append(Candle(datetime.fromisoformat(row['time']),
                    *(Decimal(row[k]) for k in ('open', 'high', 'low', 'close')), int(row['volume'])))
            except (ValueError, ArithmeticError, TypeError) as exc:
                raise ValueError(f'Invalid CSV candle on line {line}') from exc
    return bars


def write_csv(path: str | Path, candles: list[Candle]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['time', 'open', 'high', 'low', 'close', 'volume'])
        for c in candles:
            writer.writerow([c.time.isoformat(), c.open, c.high, c.low, c.close, c.volume])


def _fetch_json(url: str) -> dict:
    try:
        with urlopen(url, timeout=25) as response:
            return json.loads(response.read(), parse_float=Decimal)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError('MOEX ISS request failed; retry later or use a saved CSV') from exc


def download_moex(ticker: str, start: date, end: date, *, now: datetime | None = None,
                  fetch=_fetch_json) -> tuple[list[Candle], int]:
    ticker = ticker.upper()
    if not re.fullmatch(r'[A-Z0-9_-]{1,20}', ticker) or end < start:
        raise ValueError('Invalid ticker or date range')
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError('now must include timezone')
    today = current.astimezone(MOSCOW).date()
    base = f'https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{ticker}'
    details = fetch(base + '.json?' + urlencode({'iss.meta': 'off', 'iss.only': 'securities',
        'securities.columns': 'SECID,LOTSIZE'}))['securities']
    if len(details['data']) != 1:
        raise ValueError('Share not found on MOEX TQBR')
    record = dict(zip(details['columns'], details['data'][0]))
    lot = _whole(record['LOTSIZE'], 'MOEX lot size', positive=True)
    bars: dict[datetime, Candle] = {}
    offset = 0
    for _ in range(1000):
        payload = fetch(base + '/candles.json?' + urlencode({'from': start.isoformat(),
            'till': end.isoformat(), 'interval': 24, 'start': offset, 'iss.meta': 'off'}))['candles']
        rows = payload['data']
        if not rows:
            break
        added = 0
        for row in rows:
            r = dict(zip(payload['columns'], row))
            stamp = _moex_time(r['begin'])
            # Conservative: a current MOEX day is never used as a completed candle.
            if not start <= stamp.date() <= end or stamp.date() >= today:
                continue
            candle = Candle(stamp.astimezone(timezone.utc),
                           *(Decimal(str(r[k])) for k in ('open', 'high', 'low', 'close')),
                           _whole(r['volume'], 'Candle volume'))
            if candle.time in bars and bars[candle.time] != candle:
                raise ValueError('MOEX returned conflicting duplicate candles')
            if candle.time not in bars:
                added += 1
            bars[candle.time] = candle
        offset += len(rows)
        if not added:
            # Usually an incomplete current-day-only page; detect ignored pagination.
            if any(start <= _moex_time(dict(zip(payload['columns'], r))['begin']).date() < today
                   for r in rows):
                raise ValueError('MOEX pagination did not advance')
            break
    else:
        raise ValueError('MOEX pagination exceeded limit')
    if not bars:
        raise ValueError('No completed candles in requested range')
    return sorted(bars.values(), key=lambda c: c.time), lot


def demo_candles(count: int = 400) -> list[Candle]:
    """Synthetic reproducible prices for smoke testing only."""
    rng = random.Random(17)
    bars = []
    day = date(2023, 1, 2)
    price = Decimal('100')
    while len(bars) < count:
        if day.weekday() < 5:
            n = len(bars)
            drift = 0.003 if (n // 80) % 2 == 0 else -0.003
            opening = (price * Decimal(str(1 + rng.uniform(-0.004, 0.004)))).quantize(Decimal('.01'))
            close = (opening * Decimal(str(1 + drift + rng.uniform(-0.012, 0.012)))).quantize(Decimal('.01'))
            high = max(opening, close) + Decimal('.40')
            low = min(opening, close) - Decimal('.40')
            bars.append(Candle(datetime.combine(day, datetime.min.time(), MOSCOW).astimezone(timezone.utc),
                               opening, high, low, close, 100000))
            price = close
        day += timedelta(days=1)
    return bars
