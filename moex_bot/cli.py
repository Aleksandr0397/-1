import argparse
import csv
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .data import MOSCOW, demo_candles, download_moex, read_csv, write_csv
from .engine import BacktestConfig, backtest


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _client(token_file: Path | None = None):
    from .tbank import TInvestClient
    token = os.environ.get('TINVEST_SANDBOX_TOKEN', '').strip()
    if not token and token_file is not None:
        try:
            token = token_file.read_text(encoding='utf-8').strip()
        except OSError:
            raise ValueError('Sandbox token file cannot be read') from None
    if not token:
        raise ValueError('Set TINVEST_SANDBOX_TOKEN to a sandbox token locally')
    return TInvestClient(token)


def _report(bars, config, out: Path) -> dict:
    result = backtest(bars, config)
    report = {
        'strategy': f'SMA({config.fast}/{config.slow}), long only',
        'period_start': bars[0].time, 'period_end': bars[-1].time, 'candles': len(bars),
        'config': asdict(config), 'initial_cash_rub': config.initial_cash,
        'final_equity_rub': result.final_equity, 'cash_rub': result.final_cash,
        'open_quantity': result.final_quantity,
        'return_percent': result.total_return * 100,
        'max_drawdown_percent': result.max_drawdown * 100,
        'fees_rub': result.total_fees, 'trade_count': len(result.trades),
        'risk_halted': result.halted,
        'buy_hold_same_initial_allocation_return_percent': result.benchmark_return * 100,
        'assumptions': ['Unadjusted prices: no dividends, taxes or corporate actions',
            'Constant lot size for entire period',
            'Fill at next nonzero-volume bar open with configured slippage; no order book model',
            'Final holdings marked at last close; closing costs are not deducted',
            'Allocation limits entries; subsequent price changes can raise portfolio weight',
            'Drawdown triggers exit at a later open and may exceed the configured threshold'],
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / 'summary.json').write_text(_json(report) + '\n', encoding='utf-8')
    for filename, records in (('trades.csv', result.trades), ('equity.csv', result.equity)):
        rows = [asdict(record) for record in records]
        names = list(rows[0]) if rows else ['time', 'signal_time', 'side', 'quantity', 'price', 'fee', 'cash_after']
        with (out / filename).open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=names)
            writer.writeheader()
            writer.writerows(rows)
    return report


def _risk_options(parser):
    parser.add_argument('--fast', type=int, default=20)
    parser.add_argument('--slow', type=int, default=60)
    parser.add_argument('--allocation', type=Decimal, default=Decimal('0.2'), help='Fraction, default 0.2')
    parser.add_argument('--drawdown', type=Decimal, default=Decimal('0.1'), help='Fraction, default 0.1')
    parser.add_argument('--commission', type=Decimal, default=Decimal('0.0005'), help='Per-side fraction')


def _config(args, lot=1):
    return BacktestConfig(initial_cash=getattr(args, 'capital', Decimal('100000')),
        fast=args.fast, slow=args.slow, lot=lot, max_allocation=args.allocation,
        max_drawdown=args.drawdown, commission=args.commission,
        slippage=getattr(args, 'slippage', Decimal('0.001')))


def build_parser():
    parser = argparse.ArgumentParser(description='MOEX backtests and T-Invest sandbox bot; virtual funds only')
    commands = parser.add_subparsers(dest='command', required=True)
    demo = commands.add_parser('demo', help='Reproducible smoke test on synthetic prices')
    demo.add_argument('--out', type=Path, default=Path('reports/demo'))
    _risk_options(demo)
    bt = commands.add_parser('backtest', help='Backtest local daily candle CSV')
    bt.add_argument('--csv', type=Path, required=True)
    bt.add_argument('--lot', type=int, required=True, help='Historical lot size, constant during this test')
    bt.add_argument('--capital', type=Decimal, default=Decimal('100000'))
    bt.add_argument('--slippage', type=Decimal, default=Decimal('0.001'))
    bt.add_argument('--out', type=Path, default=Path('reports/backtest'))
    _risk_options(bt)
    download = commands.add_parser('download', help='Download completed daily candles')
    download.add_argument('--ticker', default='SBER')
    download.add_argument('--from', dest='start', type=date.fromisoformat, required=True)
    download.add_argument('--to', dest='end', type=date.fromisoformat, default=date.today())
    download.add_argument('--source', choices=('moex', 'tbank'), default='moex')
    download.add_argument('--out', type=Path, required=True)
    download.add_argument('--token-file', type=Path, help='Private sandbox token file; environment takes precedence')
    init = commands.add_parser('sandbox-init', help='Create and fund a virtual sandbox account')
    init.add_argument('--cash', type=Decimal, default=Decimal('100000'))
    init.add_argument('--token-file', type=Path)
    accounts = commands.add_parser('sandbox-accounts', help='List sandbox accounts')
    accounts.add_argument('--token-file', type=Path)
    check = commands.add_parser('sandbox-check', help='Check authentication and market data without creating accounts or orders')
    check.add_argument('--token-file', type=Path, default=Path('state/.sandbox-token'))
    check.add_argument('--ticker', default='SBER')
    for command in ('sandbox-step', 'sandbox-run'):
        step = commands.add_parser(command, help='Plan or submit sandbox orders' if command.endswith('step')
                                   else 'Poll and run sandbox steps until stopped')
        step.add_argument('--account', required=True)
        step.add_argument('--ticker', default='SBER')
        step.add_argument('--state', type=Path, default=Path('state/sandbox.sqlite'))
        step.add_argument('--submit', action='store_true', help='Send virtual sandbox orders; default dry run')
        step.add_argument('--token-file', type=Path)
        _risk_options(step)
        if command.endswith('run'):
            step.add_argument('--interval', type=int, default=300, help='Seconds between polls, minimum 30')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'demo':
            bars = demo_candles()
            write_csv(args.out / 'synthetic.csv', bars)
            report = _report(bars, _config(args), args.out)
            report['data_kind'] = 'SYNTHETIC: checks software, not profitability'
            (args.out / 'summary.json').write_text(_json(report) + '\n', encoding='utf-8')
            print(_json(report))
        elif args.command == 'backtest':
            print(_json(_report(read_csv(args.csv), _config(args, args.lot), args.out)))
        elif args.command == 'download':
            if args.end < args.start:
                raise ValueError('End date must not precede start date')
            if args.source == 'moex':
                bars, lot = download_moex(args.ticker, args.start, args.end)
            else:
                client = _client(args.token_file)
                instrument = client.resolve_share(args.ticker.upper())
                start = datetime.combine(args.start, datetime.min.time(), MOSCOW).astimezone(timezone.utc)
                end = datetime.combine(args.end + timedelta(days=1), datetime.min.time(), MOSCOW).astimezone(timezone.utc)
                bars = client.get_daily_candles(instrument.uid, start, min(end, datetime.now(timezone.utc)))
                lot = instrument.lot
                if not bars:
                    raise ValueError('No completed candles in requested range')
            write_csv(args.out, bars)
            metadata = {'ticker': args.ticker.upper(), 'source': args.source, 'board': 'TQBR',
                'current_lot': lot, 'candles': len(bars), 'csv': str(args.out),
                'warning': 'Current lot may differ from historical lot; unadjusted prices'}
            args.out.with_suffix('.meta.json').write_text(_json(metadata) + '\n', encoding='utf-8')
            print(_json(metadata))
        elif args.command == 'sandbox-init':
            if not args.cash.is_finite() or args.cash <= 0:
                raise ValueError('Virtual cash must be finite and positive')
            from .tbank import quotation
            quotation(args.cash)  # Validate representability before creating an account.
            client = _client(args.token_file)
            account = client.open_sandbox_account()
            try:
                client.sandbox_pay_in(account, args.cash)
            except Exception:
                print(_json({'created_account': account, 'funding': 'failed; inspect account before retry'}))
                raise
            print(_json({'sandbox_account': account, 'virtual_cash_rub': args.cash}))
        elif args.command == 'sandbox-accounts':
            print(_json({'sandbox_accounts': _client(args.token_file).list_sandbox_accounts()}))
        elif args.command == 'sandbox-check':
            client = _client(args.token_file)
            accounts = client.list_sandbox_accounts()
            instrument = client.resolve_share(args.ticker.upper())
            current = datetime.now(timezone.utc)
            candles = client.get_daily_candles(instrument.uid, current - timedelta(days=180), current)
            print(_json({'connected': True, 'sandbox_account_count': len(accounts),
                'ticker': instrument.ticker, 'lot': instrument.lot,
                'last_price': client.get_last_price(instrument.uid), 'completed_candles': len(candles)}))
        else:
            from .sandbox import run_step
            _config(args)  # Validate before any request.
            if args.command == 'sandbox-run' and args.interval < 30:
                raise ValueError('Polling interval must be at least 30 seconds')
            client = _client(args.token_file)
            while True:
                report = run_step(client, args.account, args.ticker.upper(), state_path=args.state,
                    fast=args.fast, slow=args.slow, max_allocation=args.allocation,
                    max_drawdown=args.drawdown, commission=args.commission, submit=args.submit)
                print(_json(report), flush=True)
                if args.command == 'sandbox-step':
                    break
                time.sleep(args.interval)
        return 0
    except KeyboardInterrupt:
        print('Stopped', file=sys.stderr)
        return 130
    except Exception as exc:
        # Adapter error messages deliberately exclude tokens and response bodies.
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
