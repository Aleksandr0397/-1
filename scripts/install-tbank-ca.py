#!/usr/bin/env python3
"""Validate and copy the official additional T-Bank CA for a service runtime.

This creates an additional-CA file, not a replacement system trust bundle.
Provenance and independent fingerprints are in docs/tbank-ca.md.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import ssl
import tempfile
from datetime import datetime, timezone


ROOT_SOURCE = Path(__file__).resolve().parents[1] / "certs" / "russian-trusted-root-ca.pem"
ROOT_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"
ROOT_NOT_BEFORE = datetime(2022, 3, 1, 21, 4, 15, tzinfo=timezone.utc)
ROOT_NOT_AFTER = datetime(2032, 2, 27, 21, 4, 15, tzinfo=timezone.utc)
_SINGLE_CERT = re.compile(
    rb"\s*-----BEGIN CERTIFICATE-----\s+([A-Za-z0-9+/=\s]+)-----END CERTIFICATE-----\s*"
)


def verified_root(source: Path = ROOT_SOURCE) -> str:
    """Return one canonical PEM certificate after checking the pinned DER hash."""
    raw = source.read_bytes()
    if len(raw) > 16_384 or not _SINGLE_CERT.fullmatch(raw):
        raise ValueError("Expected exactly one PEM certificate")
    der = ssl.PEM_cert_to_DER_cert(raw.decode("ascii"))
    if hashlib.sha256(der).hexdigest() != ROOT_SHA256:
        raise ValueError("Russian Trusted Root CA fingerprint does not match the pin")
    now = datetime.now(timezone.utc)
    if not ROOT_NOT_BEFORE <= now < ROOT_NOT_AFTER:
        raise ValueError("Russian Trusted Root CA is outside its validity period")
    pem = ssl.DER_cert_to_PEM_cert(der)
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=pem)
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("TLS verification must remain enabled")
    return pem


def install(output: Path) -> None:
    pem = verified_root()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", newline="\n", dir=output.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(pem)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Additional-CA PEM file to create")
    args = parser.parse_args()
    try:
        install(args.out)
    except (OSError, ValueError, UnicodeError) as exc:
        parser.exit(1, f"Certificate installation failed: {exc}\n")
    print(f"Installed additional CA: {args.out} (DER SHA-256 {ROOT_SHA256})")


if __name__ == "__main__":
    main()
