from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "install-tbank-ca.py"
ROOT = PROJECT / "certs" / "russian-trusted-root-ca.pem"
EXPECTED_ROOT_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"


class TBankCaInstallerTests(unittest.TestCase):
    def run_installer(self, script: Path, output: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(script), "--out", str(output)],
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_installed_root_preserves_default_trust_and_tls_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "additional-ca.pem"
            result = self.run_installer(SCRIPT, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            der = ssl.PEM_cert_to_DER_cert(output.read_text(encoding="ascii"))
            self.assertEqual(hashlib.sha256(der).hexdigest(), EXPECTED_ROOT_SHA256)
            context = ssl.create_default_context()
            defaults = set(context.get_ca_certs(binary_form=True))
            self.assertTrue(defaults)
            context.load_verify_locations(cafile=str(output))
            after = set(context.get_ca_certs(binary_form=True))
            self.assertTrue(defaults <= after)
            self.assertIn(der, after)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)

    def assert_rejects_source(self, content: bytes, message: str):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "scripts").mkdir()
            (project / "certs").mkdir()
            script = project / "scripts" / SCRIPT.name
            shutil.copyfile(SCRIPT, script)
            (project / "certs" / ROOT.name).write_bytes(content)
            output = project / "existing.pem"
            output.write_bytes(b"existing output must survive\n")
            result = self.run_installer(script, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)
            self.assertEqual(output.read_bytes(), b"existing output must survive\n")

    def test_rejects_certificate_tampering_without_replacing_output(self):
        root = ROOT.read_bytes()
        corrupted = root.replace(b"MIIF", b"NIIF", 1)
        self.assertNotEqual(corrupted, root)
        self.assert_rejects_source(corrupted, "fingerprint does not match")

    def test_rejects_extra_certificates_without_replacing_output(self):
        root = ROOT.read_bytes()
        self.assert_rejects_source(root + root, "exactly one PEM certificate")


if __name__ == "__main__":
    unittest.main()
