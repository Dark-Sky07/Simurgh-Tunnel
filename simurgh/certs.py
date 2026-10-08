"""Certificate helpers.

For a production setup the right answer is a real certificate (Let's Encrypt /
``acme.sh``) -- Simurgh happily uses the same files the panel's own web server
uses.  When there is no domain yet, ``ensure_certificate`` creates a
self-signed one; the relay pins its fingerprint, so nothing relies on a CA.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .util import get_logger

log = get_logger("simurgh.cert")


def have_cryptography() -> bool:
    """True when PyNaCl-free, pure-`cryptography` certificate work is possible."""
    import importlib.util

    return importlib.util.find_spec("cryptography") is not None


def generate_self_signed(domain: str, cert_path: str | Path, key_path: str | Path,
                         days: int = 3650) -> tuple[str, str]:
    """Return (cert_path, key_path). Uses `cryptography` or the openssl CLI."""
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    if have_cryptography():
        _generate_with_cryptography(domain, cert_path, key_path, days)
    else:
        _generate_with_openssl(domain, cert_path, key_path, days)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return str(cert_path), str(key_path)


def _generate_with_cryptography(domain: str, cert_path: Path, key_path: Path,
                                days: int) -> None:
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
    )
    try:
        import ipaddress

        san = x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address(domain))]
            if _is_ip(domain) else [x509.DNSName(domain)]
        )
    except Exception:
        san = x509.SubjectAlternativeName([x509.DNSName(domain)])
    cert = builder.add_extension(san, critical=False).sign(key, hashes.SHA256())
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _generate_with_openssl(domain: str, cert_path: Path, key_path: Path,
                           days: int) -> None:
    cmd = [
        "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
        "ec_paramgen_curve:prime256v1", "-nodes",
        "-keyout", str(key_path), "-out", str(cert_path),
        "-days", str(days), "-subj", f"/CN={domain}",
        "-addext", f"subjectAltName=DNS:{domain},IP:{domain}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not cert_path.exists():
        # one more try: older openssl without -addext
        cmd = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key_path), "-out", str(cert_path),
            "-days", str(days), "-subj", f"/CN={domain}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"openssl failed: {proc.stderr.strip()[:200]}")


def _is_ip(value: str) -> bool:
    import socket

    try:
        socket.inet_pton(socket.AF_INET, value)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, value)
        return True
    except OSError:
        return False


def fingerprint_of(cert_path: str | Path) -> str:
    """SHA-256 of the DER certificate (what the relay pins)."""
    import base64
    import hashlib

    data = Path(cert_path).read_bytes()
    if b"-----BEGIN" in data:
        try:
            from cryptography import x509

            return x509.load_pem_x509_certificate(data).fingerprint(
                __import__("cryptography.hazmat.primitives.hashes",
                           fromlist=["SHA256"]).SHA256()
            ).hex()
        except Exception:
            body = b"".join(
                line.strip() for line in data.splitlines()
                if line and not line.startswith(b"-----")
            )
            data = base64.b64decode(body)
    return hashlib.sha256(data).hexdigest()


def ensure_certificate(home, domain: str = "simurgh.local",
                       cert_file: str | None = None,
                       key_file: str | None = None) -> tuple[str, str]:
    """Use an existing certificate or create one under *home*."""
    if cert_file and key_file and Path(cert_file).exists() and Path(key_file).exists():
        return cert_file, key_file
    cert = Path(home.path) / "cert" / "cert.pem"
    key = Path(home.path) / "cert" / "key.pem"
    if not (cert.exists() and key.exists()):
        log.info("generating a self-signed certificate for %s", domain)
        generate_self_signed(domain, cert, key)
    return str(cert), str(key)
