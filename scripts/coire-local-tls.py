#!/usr/bin/env python3
"""Prepare a private local HTTPS bundle from Keychain; never open listeners or alter trust."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "coire-local-browser-tls"


def prepare(directory: Path, hostname: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", hostname):
        raise ValueError("a DNS hostname is required")
    if not directory.is_absolute() or directory.resolve().is_relative_to(ROOT):
        raise ValueError("TLS bundle must be outside the repository")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise ValueError("TLS bundle must be a private operator-owned directory")
    directory.chmod(0o700)
    account = hostname
    found = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    if found.returncode == 0:
        material = json.loads(base64.b64decode(found.stdout.strip()))
    elif found.returncode == 44:
        with tempfile.TemporaryDirectory(prefix="coire-local-cert-") as temp:
            key, cert = Path(temp) / "key.pem", Path(temp) / "cert.pem"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:3072",
                    "-sha256",
                    "-nodes",
                    "-days",
                    "365",
                    "-subj",
                    f"/CN={hostname}",
                    "-addext",
                    f"subjectAltName=DNS:{hostname}",
                    "-addext",
                    "basicConstraints=critical,CA:FALSE",
                    "-addext",
                    "extendedKeyUsage=serverAuth",
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                ],
                check=True,
                capture_output=True,
            )
            material = {"key": key.read_text(), "cert": cert.read_text()}
        encoded = base64.b64encode(json.dumps(material).encode()).decode()
        # Use Security.framework directly: private key material never enters argv,
        # shell history or security(1)'s bounded interactive command buffer.
        framework = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        add = framework.SecKeychainAddGenericPassword
        add.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_void_p,
        ]
        add.restype = ctypes.c_int32
        status = add(
            None,
            len(SERVICE.encode()),
            SERVICE.encode(),
            len(account.encode()),
            account.encode(),
            len(encoded.encode()),
            encoded.encode(),
            None,
        )
        if status != 0:
            raise ValueError(f"Keychain credential creation failed (OSStatus {status})")
        verified = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
        if verified.stdout.strip() != encoded:
            raise ValueError("Keychain did not retain the local TLS credential")
    else:
        raise ValueError("Keychain read failed; existing credentials were not replaced")
    generation = directory / f"generation-{secrets.token_hex(8)}"
    generation.mkdir(mode=0o700)
    for name, content in (
        ("certificate.pem", material["cert"]),
        ("private-key.pem", material["key"]),
    ):
        path = generation / name
        path.write_text(content)
        # Non-root container must read Compose file secrets. The enclosing host directory
        # remains mode0700; no other host account can traverse it.
        path.chmod(0o444)
    source = (ROOT / "apps/coire-web/nginx/nginx.conf").read_text()
    start = source.index("    server {")
    plain = source[start : source.rfind("}")]
    tls = plain.replace(
        "listen 8080;",
        "listen 8081 ssl;\n        ssl_certificate /run/secrets/local_tls_certificate;\n        ssl_certificate_key /run/secrets/local_tls_private_key;\n        ssl_protocols TLSv1.2 TLSv1.3;",
    )
    config = generation / "nginx.conf"
    config.write_text(source[: source.rfind("}")] + tls + "}\n")
    config.chmod(0o444)
    return {
        "COIRE_LOCAL_TLS_CONFIG": str(config),
        "COIRE_LOCAL_TLS_CERTIFICATE": str(generation / "certificate.pem"),
        "COIRE_LOCAL_TLS_PRIVATE_KEY": str(generation / "private-key.pem"),
        "COIRE_LOCAL_AUTH_ORIGIN": f"https://{hostname}:8443",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path.home() / ".coire/local-tls")
    parser.add_argument("--hostname", default="coire-core.lab")
    args = parser.parse_args()
    print(json.dumps(prepare(args.directory, args.hostname), indent=2))


if __name__ == "__main__":
    main()
