import tempfile
import unittest
from decimal import Decimal
from datetime import date, datetime, timezone
from pathlib import Path

from moex_bot.data import _moex_time, _whole, demo_candles, download_moex, read_csv, write_csv


class DataTests(unittest.TestCase):
    def test_fractional_or_nonfinite_integer_fields_rejected(self):
        for value in (Decimal('10.9'), Decimal('NaN'), Decimal('Infinity'), -1, True):
            with self.assertRaises(ValueError):
                _whole(value, 'volume')

    def test_aware_iss_timestamp_preserves_instant(self):
        self.assertEqual(_moex_time('2024-01-03T00:00:00+00:00').astimezone(timezone.utc),
                         datetime(2024, 1, 3, tzinfo=timezone.utc))

    def test_csv_roundtrip(self):
        bars = demo_candles(10)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bars.csv'
            write_csv(path, bars)
            self.assertEqual(read_csv(path), bars)

    def test_moex_pages_and_excludes_current_day(self):
        rows = [[100, 101, 102, 99, 1000, '2024-01-03 00:00:00'],
                [101, 102, 103, 100, 1000, '2024-01-04 00:00:00']]
        requests = []
        def fetch(url):
            requests.append(url)
            if '/candles' not in url:
                return {'securities': {'columns': ['SECID', 'LOTSIZE'], 'data': [['SBER', 10]]}}
            return {'candles': {'columns': ['open', 'close', 'high', 'low', 'volume', 'begin'],
                                'data': rows if 'start=0' in url else []}}
        bars, lot = download_moex('sber', date(2024, 1, 1), date(2024, 1, 4),
            now=datetime(2024, 1, 4, 12, tzinfo=timezone.utc), fetch=fetch)
        self.assertEqual(lot, 10)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].time.isoformat(), '2024-01-02T21:00:00+00:00')
        self.assertIn('start=2', requests[-1])

    def test_broken_pagination_detected(self):
        def fetch(url):
            if '/candles' not in url:
                return {'securities': {'columns': ['SECID', 'LOTSIZE'], 'data': [['SBER', 10]]}}
            return {'candles': {'columns': ['open', 'close', 'high', 'low', 'volume', 'begin'],
                'data': [[100, 101, 102, 99, 1000, '2024-01-03 00:00:00']]}}
        with self.assertRaisesRegex(ValueError, 'pagination'):
            download_moex('SBER', date(2024, 1, 1), date(2024, 1, 4),
                          now=datetime(2024, 1, 5, tzinfo=timezone.utc), fetch=fetch)


if __name__ == '__main__':
    unittest.main()
