#!/usr/bin/env python3
"""Smoke-test a packaged Catalyst worker against a mock Sage mTLS RPC.

This catches release packaging regressions where the GUI starts but the
PyInstaller coin-prep worker cannot import its helpers, load its Sage client
certificate, or complete the authenticated Sage initialize/get_key path.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _print_safe(text: str, *, stream=None) -> None:
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(text.encode(encoding, "replace").decode(encoding))
    if not text.endswith("\n"):
        stream.write("\n")


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _serial() -> int:
    return x509.random_serial_number()


def _validity(builder: x509.CertificateBuilder) -> x509.CertificateBuilder:
    now = datetime.now(timezone.utc)
    return builder.not_valid_before(now - timedelta(minutes=5)).not_valid_after(
        now + timedelta(days=2)
    )


def _create_ca(temp_dir: Path) -> tuple[Path, object, x509.Certificate]:
    key = _new_key()
    cert = (
        _validity(
            x509.CertificateBuilder()
            .subject_name(_name("catalyst-smoke-ca"))
            .issuer_name(_name("catalyst-smoke-ca"))
            .public_key(key.public_key())
            .serial_number(_serial())
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = temp_dir / "ca.crt"
    _write_bytes(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key, cert


def _create_signed_cert(
    temp_dir: Path,
    filename: str,
    common_name: str,
    ca_key,
    ca_cert: x509.Certificate,
    *,
    is_server: bool,
) -> tuple[Path, Path]:
    key = _new_key()
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(_serial())
    )
    if is_server:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
    cert = _validity(builder).sign(ca_key, hashes.SHA256())
    cert_path = temp_dir / f"{filename}.crt"
    key_path = temp_dir / f"{filename}.key"
    _write_bytes(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    _write_bytes(
        key_path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    return cert_path, key_path


class MockSageHandler(BaseHTTPRequestHandler):
    server: "MockSageServer"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or "0")
        if length:
            self.rfile.read(length)

        self.server.request_paths.append(self.path)
        if self.connection.getpeercert():
            self.server.saw_client_cert = True

        if self.path == "/initialize":
            payload = {"success": True}
        elif self.path == "/get_key":
            payload = {
                "success": True,
                "key": {
                    "fingerprint": 123456789,
                    "name": "Mock Sage",
                    "has_secrets": True,
                    "network_id": "mainnet",
                },
            }
        else:
            payload = {"success": False, "error": f"unexpected path {self.path}"}

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class MockSageServer(ThreadingHTTPServer):
    request_paths: list[str]
    saw_client_cert: bool


def _start_mock_sage(temp_dir: Path) -> tuple[MockSageServer, threading.Thread, Path, Path]:
    ca_path, ca_key, ca_cert = _create_ca(temp_dir)
    server_cert, server_key = _create_signed_cert(
        temp_dir, "server", "mock-sage-server", ca_key, ca_cert, is_server=True
    )
    client_cert, client_key = _create_signed_cert(
        temp_dir, "client", "mock-sage-client", ca_key, ca_cert, is_server=False
    )

    httpd = MockSageServer(("127.0.0.1", 0), MockSageHandler)
    httpd.request_paths = []
    httpd.saw_client_cert = False

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(server_cert), str(server_key))
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(ca_path))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)

    thread = threading.Thread(target=httpd.serve_forever, name="mock-sage-rpc", daemon=True)
    thread.start()
    return httpd, thread, client_cert, client_key


def run_smoke(exe_path: Path, timeout_s: int) -> int:
    if not exe_path.is_file():
        print(f"ERROR: executable not found: {exe_path}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="catalyst-sage-smoke-") as temp:
        temp_dir = Path(temp)
        server, thread, client_cert, client_key = _start_mock_sage(temp_dir)
        host, port = server.server_address
        env = os.environ.copy()
        env.update(
            {
                "WALLET_TYPE": "sage",
                "SAGE_RPC_URL": f"https://{host}:{port}",
                "SAGE_CERT_PATH": str(client_cert),
                "SAGE_KEY_PATH": str(client_key),
                "SAGE_DATA_DIR": str(temp_dir / "sage-data"),
                "CMM_DATA_DIR": str(temp_dir / "catalyst-data"),
                "_CATALYST_PRESERVE_PROCESS_ENV": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        cmd = [str(exe_path), "--coin-prep-worker", "--sage-rpc-smoke"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            result = subprocess.run(
                cmd,
                cwd=str(exe_path.parent),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                creationflags=creationflags,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    _print_safe(result.stdout)
    if result.stderr:
        _print_safe(result.stderr, stream=sys.stderr)

    if result.returncode != 0:
        print(f"ERROR: smoke worker exited with {result.returncode}", file=sys.stderr)
        return result.returncode
    if not server.saw_client_cert:
        print("ERROR: mock Sage server did not receive a client certificate", file=sys.stderr)
        return 1
    required_paths = {"/initialize", "/get_key"}
    missing = required_paths.difference(server.request_paths)
    if missing:
        print(f"ERROR: mock Sage server did not receive required calls: {sorted(missing)}", file=sys.stderr)
        return 1

    print("Packaged Sage RPC worker smoke PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test packaged Sage RPC worker")
    parser.add_argument("--exe", required=True, help="Path to packaged Catalyst executable")
    parser.add_argument("--timeout", type=int, default=30, help="Worker timeout in seconds")
    args = parser.parse_args()
    return run_smoke(Path(args.exe).resolve(), args.timeout)


if __name__ == "__main__":
    sys.exit(main())
