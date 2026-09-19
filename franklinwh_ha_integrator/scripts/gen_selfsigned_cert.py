#!/usr/bin/env python3
"""Generate a self-signed TLS certificate for FHAI.

    python scripts/gen_selfsigned_cert.py [--host 192.168.0.247] [--days 820]

Writes server.crt / server.key into <data_dir>/ssl, which is where
`tls_cert_path` / `tls_key_path` default to. Run it INSIDE the container on a
Docker install — `get_data_dir()` resolves to /data there and ./data on the
host, and the container's /data is a named volume (see docs §7.1).

Uses `cryptography` (already a dependency) rather than shelling out to
openssl, which is not installed in the slim image.

Every name you might reach the app by must be in the SAN list — browsers
ignore CN entirely. Pass --host for the LAN IP; localhost, 127.0.0.1 and the
container name are always included.

Default validity is 820 days to stay inside Apple's 825-day cap for TLS server
certificates; see docs/security_guide.md §4a.1. If you only ever use Chrome or
Firefox a longer --days works, but Safari/iOS will refuse it.
"""
from __future__ import annotations

import argparse
import datetime
import ipaddress
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.absolute()))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from src.config.environment import get_data_dir


def _san_entries(hosts: list[str]) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = []
    for h in hosts:
        h = h.strip()
        if not h:
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            names.append(x509.DNSName(h))
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append", default=[],
                    help="extra hostname or IP to include in the SAN (repeatable)")
    # 820 days, not 10 years. Apple platforms (Safari on macOS, iOS, iPadOS)
    # reject TLS server certificates issued after 2019-07-01 whose validity
    # exceeds 825 days — the connection fails outright rather than offering the
    # usual "proceed anyway". Apple relaxes some of these rules for
    # user-installed roots, but staying inside the limit costs nothing and
    # removes the question. Renew with this same script.
    ap.add_argument("--days", type=int, default=820)
    ap.add_argument("--force", action="store_true", help="overwrite an existing certificate")
    args = ap.parse_args()

    ssl_dir = get_data_dir() / "ssl"
    crt, key = ssl_dir / "server.crt", ssl_dir / "server.key"

    if crt.exists() and not args.force:
        print(f"✋ {crt} already exists — refusing to overwrite.")
        print("   Re-run with --force to replace it (this invalidates any trust")
        print("   you have already granted the old certificate).")
        return 2

    ssl_dir.mkdir(parents=True, exist_ok=True)

    hosts = ["localhost", "127.0.0.1", "fwhhai-app", *args.host]
    print(f"🔐 Generating self-signed certificate")
    print(f"   dir  : {ssl_dir}")
    print(f"   names: {', '.join(hosts)}")
    print(f"   valid: {args.days} days")

    key_obj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "FranklinWH HA Integrator"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "FHAI"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key_obj.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))   # tolerate clock skew
        .not_valid_after(now + datetime.timedelta(days=args.days))
        .add_extension(x509.SubjectAlternativeName(_san_entries(hosts)), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key_obj, hashes.SHA256())
    )

    key.write_bytes(key_obj.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    key.chmod(0o600)
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    print(f"✅ Wrote {crt}")
    print(f"✅ Wrote {key} (0600)")
    print()
    print("   Restart the app to pick it up. scripts/start.sh enables TLS")
    print("   automatically when both files are present.")
    print("   Self-signed means your browser will warn once — trust it, or")
    print("   import server.crt into your OS keychain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
