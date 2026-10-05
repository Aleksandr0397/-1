import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from moex_bot.cli import main


class CliTests(unittest.TestCase):
    def test_connection_check_uses_private_token_file_and_readonly_methods(self):
        # This client deliberately has no account creation or trading methods.
        client = SimpleNamespace(
            list_sandbox_accounts=lambda: ['virtual-account'],
            resolve_share=lambda ticker: SimpleNamespace(uid='share-id', ticker=ticker, lot=1),
            get_daily_candles=lambda uid, start, end: [],
            get_last_price=lambda uid: Decimal('123.45'))
        with tempfile.TemporaryDirectory() as temp:
            token_file = Path(temp) / 'token'
            token_file.write_text('private-connection-test-token')
            output = StringIO()
            with patch.dict(os.environ, {'TINVEST_SANDBOX_TOKEN': ''}), \
                 patch('moex_bot.tbank.TInvestClient', return_value=client) as factory, \
                 redirect_stdout(output):
                code = main(['sandbox-check', '--token-file', str(token_file)])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertTrue(report['connected'])
            self.assertEqual(report['sandbox_account_count'], 1)
            self.assertEqual(report['last_price'], '123.45')
            self.assertNotIn('private-connection-test-token', output.getvalue())
            factory.assert_called_once_with('private-connection-test-token')

    def test_demo_runs_offline_and_writes_consistent_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / 'report'
            result = subprocess.run([sys.executable, '-m', 'moex_bot', 'demo', '--out', str(out)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            printed = json.loads(result.stdout)
            saved = json.loads((out / 'summary.json').read_text())
            self.assertEqual(printed, saved)
            self.assertEqual(saved['candles'], 400)
            self.assertIn('SYNTHETIC', saved['data_kind'])
            self.assertTrue((out / 'equity.csv').is_file())
            self.assertTrue((out / 'trades.csv').is_file())

    def test_sandbox_without_token_fails_with_actionable_error(self):
        env = os.environ.copy()
        env.pop('TINVEST_SANDBOX_TOKEN', None)
        result = subprocess.run([sys.executable, '-m', 'moex_bot', 'sandbox-step', '--account', 'example'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('TINVEST_SANDBOX_TOKEN', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_invalid_virtual_cash_is_rejected_before_creating_account(self):
        result = subprocess.run([sys.executable, '-m', 'moex_bot', 'sandbox-init', '--cash', '0.0000000001'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('nine decimal places', result.stderr)


if __name__ == '__main__':
    unittest.main()
