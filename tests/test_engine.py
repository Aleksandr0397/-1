import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from moex_bot.engine import BacktestConfig, backtest, desired_position
from moex_bot.models import Candle


def candles(prices):
    first = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [Candle(first + timedelta(days=i), D(str(p)), D(str(p)), D(str(p)), D(str(p)), 1000) for i, p in enumerate(prices)]


class EngineTests(unittest.TestCase):
    def test_signal_requires_warmup(self):
        self.assertIsNone(desired_position(candles([100, 110]), 2, 3))
        self.assertTrue(desired_position(candles([100, 110, 120]), 2, 3))
        self.assertFalse(desired_position(candles([120, 110, 100]), 2, 3))

    def test_next_open_and_both_side_costs(self):
        bars = candles([100, 110, 120, 100, 80])
        config = BacktestConfig(initial_cash=D('1000'), fast=1, slow=2, lot=1,
                                max_allocation=D('1'), max_drawdown=D('0.99'),
                                commission=D('0.01'), slippage=D('0.01'))
        result = backtest(bars, config)
        buy, sell = result.trades
        self.assertEqual(buy.time, bars[2].time)
        self.assertEqual(buy.signal_time, bars[1].time)
        self.assertEqual(buy.price, D('121.20'))
        self.assertEqual(buy.quantity, 8)
        # 1000 - 8*121.2*1.01 + 8*79.2*0.99 = 647.968
        self.assertEqual(sell.time, bars[4].time)
        self.assertEqual(result.final_equity, D('647.968'))
        self.assertEqual(result.final_quantity, 0)
        self.assertEqual(result.total_fees, D('16.032'))

    def test_allocation_and_lot_rounding(self):
        result = backtest(candles([100, 110, 120, 130]), BacktestConfig(
            initial_cash=D('10000'), fast=1, slow=2, lot=10, max_allocation=D('0.2'),
            commission=D('0'), slippage=D('0')))
        self.assertEqual(result.trades[0].quantity, 10)
        self.assertGreaterEqual(result.final_cash, 0)

    def test_large_open_gap_does_not_overdraw(self):
        result = backtest(candles([100, 110, 10000]), BacktestConfig(
            initial_cash=D('1000'), fast=1, slow=2, max_allocation=D('1')))
        self.assertEqual(result.trades, [])
        self.assertEqual(result.final_cash, D('1000'))

    def test_risk_halt_sells_next_open_and_never_reenters(self):
        bars = candles([100, 110, 120, 50, 60, 130, 140])
        result = backtest(bars, BacktestConfig(initial_cash=D('1000'), fast=1, slow=2,
            max_allocation=D('1'), max_drawdown=D('0.1'), commission=D('0'), slippage=D('0')))
        self.assertTrue(result.halted)
        self.assertEqual([t.side for t in result.trades], ['BUY', 'SELL'])
        self.assertEqual(result.trades[-1].time, bars[4].time)
        self.assertEqual(result.final_quantity, 0)

    def test_future_prices_cannot_change_earlier_trades(self):
        cfg = BacktestConfig(fast=1, slow=2, max_drawdown=D('0.99'))
        prefix = candles([100, 110, 120, 130])
        longer = candles([100, 110, 120, 130, 1, 10000])
        a = backtest(prefix, cfg)
        b = backtest(longer, cfg)
        self.assertEqual(a.trades, [t for t in b.trades if t.time <= prefix[-1].time])
        self.assertEqual(a.equity, b.equity[:len(a.equity)])

    def test_zero_volume_bar_cannot_fill(self):
        bars = candles([100, 110, 120, 130])
        c = bars[2]
        bars[2] = Candle(c.time, c.open, c.high, c.low, c.close, 0)
        result = backtest(bars, BacktestConfig(fast=1, slow=2))
        self.assertEqual(result.trades[0].time, bars[3].time)

    def test_zero_volume_bar_cannot_cancel_pending_signal(self):
        bars = candles([100, 110, 110, 120])
        c = bars[2]
        bars[2] = Candle(c.time, c.open, c.high, c.low, c.close, 0)
        result = backtest(bars, BacktestConfig(fast=1, slow=2))
        self.assertEqual(result.trades[0].time, bars[3].time)
        self.assertEqual(result.trades[0].signal_time, bars[1].time)

    def test_unordered_duplicate_and_bad_parameters_rejected(self):
        bars = candles([100, 110, 120])
        for invalid in ([bars[1], bars[0]], [bars[0], bars[0]], []):
            with self.assertRaises(ValueError):
                backtest(invalid, BacktestConfig(fast=1, slow=2))
        for settings in ({'lot': 0}, {'lot': 1.5}, {'fast': True}, {'fast': 60, 'slow': 20}, {'commission': D('-1')},
                         {'max_allocation': D('NaN')}, {'max_drawdown': D('0')}):
            with self.assertRaises(ValueError):
                BacktestConfig(**settings)


if __name__ == '__main__':
    unittest.main()
