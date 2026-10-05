"""Deterministic long-only backtest: close signal, next bar open execution."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Sequence

from .models import Candle

ZERO = Decimal('0')
ONE = Decimal('1')


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: Decimal = Decimal('100000')
    fast: int = 20
    slow: int = 60
    lot: int = 1
    max_allocation: Decimal = Decimal('0.2')
    max_drawdown: Decimal = Decimal('0.1')
    commission: Decimal = Decimal('0.0005')
    slippage: Decimal = Decimal('0.001')

    def __post_init__(self) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (self.fast, self.slow, self.lot)) or not 0 < self.fast < self.slow or self.lot <= 0:
            raise ValueError('Require 0 < fast < slow and lot > 0')
        for name in ('initial_cash', 'max_allocation', 'max_drawdown', 'commission', 'slippage'):
            if not getattr(self, name).is_finite():
                raise ValueError(f'{name} must be finite')
        if self.initial_cash <= 0 or not ZERO < self.max_allocation <= ONE:
            raise ValueError('Require cash > 0 and allocation in (0, 1]')
        if not ZERO < self.max_drawdown < ONE:
            raise ValueError('Drawdown must be in (0, 1)')
        if not ZERO <= self.commission < ONE or not ZERO <= self.slippage < ONE:
            raise ValueError('Costs must be in [0, 1)')


def desired_position(candles: Sequence[Candle], fast: int = 20, slow: int = 60) -> bool | None:
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (fast, slow)) or not 0 < fast < slow:
        raise ValueError('Require 0 < fast < slow')
    if len(candles) < slow:
        return None
    usable = [c for c in candles if c.volume > 0]
    if len(usable) < slow:
        return None
    fast_mean = sum((c.close for c in usable[-fast:]), ZERO) / fast
    slow_mean = sum((c.close for c in usable[-slow:]), ZERO) / slow
    return fast_mean > slow_mean


@dataclass(frozen=True)
class Trade:
    time: datetime
    signal_time: datetime
    side: str
    quantity: int
    price: Decimal
    fee: Decimal
    cash_after: Decimal


@dataclass(frozen=True)
class EquityPoint:
    time: datetime
    equity: Decimal
    cash: Decimal
    quantity: int
    drawdown: Decimal


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity: list[EquityPoint]
    final_cash: Decimal
    final_quantity: int
    final_equity: Decimal
    total_fees: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    halted: bool
    benchmark_return: Decimal = ZERO


def _simulate(candles: Sequence[Candle], cfg: BacktestConfig, benchmark: bool = False) -> BacktestResult:
    cash, quantity, peak = cfg.initial_cash, 0, cfg.initial_cash
    pending: bool | None = None
    signal_time = candles[0].time
    trades: list[Trade] = []
    equity: list[EquityPoint] = []
    halted = False
    max_dd = ZERO
    fees = ZERO
    history: list[Candle] = []
    for candle in candles:
        if candle.volume > 0:
            if pending is False and quantity:
                price = candle.open * (ONE - cfg.slippage)
                fee = quantity * price * cfg.commission
                cash += quantity * price - fee
                trades.append(Trade(candle.time, signal_time, 'SELL', quantity, price, fee, cash))
                quantity = 0
                fees += fee
            elif pending is True and quantity == 0 and not halted:
                price = candle.open * (ONE + cfg.slippage)
                budget = min(cash, (cash + quantity * candle.open) * cfg.max_allocation)
                lot_cost = price * cfg.lot * (ONE + cfg.commission)
                lots = int((budget / lot_cost).to_integral_value(rounding=ROUND_FLOOR))
                if lots > 0:
                    quantity = lots * cfg.lot
                    fee = quantity * price * cfg.commission
                    cash -= quantity * price + fee
                    trades.append(Trade(candle.time, signal_time, 'BUY', quantity, price, fee, cash))
                    fees += fee
        value = cash + quantity * candle.close
        peak = max(peak, value)
        drawdown = (peak - value) / peak
        max_dd = max(max_dd, drawdown)
        equity.append(EquityPoint(candle.time, value, cash, quantity, drawdown))
        if not benchmark and drawdown >= cfg.max_drawdown:
            halted = True
        if halted:
            pending = False
            signal_time = candle.time
        elif candle.volume > 0:
            history.append(candle)
            signal_time = candle.time
            pending = (True if len(history) >= cfg.slow else None) if benchmark else desired_position(history, cfg.fast, cfg.slow)
    final = equity[-1].equity
    return BacktestResult(trades, equity, cash, quantity, final, fees,
                          final / cfg.initial_cash - ONE, max_dd, halted)


def backtest(candles: Sequence[Candle], config: BacktestConfig | None = None) -> BacktestResult:
    cfg = config or BacktestConfig()
    if len(candles) <= cfg.slow:
        raise ValueError('Need more than slow-window candles to execute at the next open')
    if any(a.time >= b.time for a, b in zip(candles, candles[1:])):
        raise ValueError('Candles must be strictly chronological, without duplicates')
    result = _simulate(candles, cfg)
    result.benchmark_return = _simulate(candles, cfg, benchmark=True).total_return
    return result
