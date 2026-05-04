#!/usr/bin/env python3
"""Smoke-test the packaged Catalyst app API against an isolated runtime.

This catches release regressions where the source checkout behaves correctly,
but the PyInstaller bundle cannot read the same environment, route requests
through the same Flask API, or authenticate to Sage RPC with bundled code.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NamedTuple


_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from packaged_sage_rpc_smoke import (  # noqa: E402
    _create_ca,
    _create_signed_cert,
    _print_safe,
)


class SmokeFailure(RuntimeError):
    """Raised when the packaged API smoke contract is not met."""


class EndpointCheck(NamedTuple):
    method: str
    path: str
    required_keys: tuple[str, ...]
    body: dict[str, Any] | None = None
    requires_token: bool = False
    timeout_s: float = 10.0
    allow_statuses: tuple[int, ...] = (200,)


class MockSageHandler(BaseHTTPRequestHandler):
    server: "MockSageServer"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or "0")
        if length:
            self.rfile.read(length)

        self.server.request_paths.append(self.path)
        if self.connection.getpeercert():
            self.server.saw_client_cert = True

        payload = _mock_sage_payload(self.path)
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


def _mock_sage_payload(path: str) -> dict[str, Any]:
    if path == "/initialize":
        return {"success": True}
    if path == "/get_version":
        return {"success": True, "version": "0.12.0"}
    if path == "/get_key":
        return {
            "success": True,
            "key": {
                "fingerprint": 123456789,
                "name": "Packaged Smoke Sage",
                "has_secrets": True,
                "network_id": "mainnet",
            },
        }
    if path == "/get_keys":
        return {
            "success": True,
            "keys": [
                {
                    "fingerprint": 123456789,
                    "name": "Packaged Smoke Sage",
                    "has_secrets": True,
                    "network_id": "mainnet",
                }
            ],
        }
    if path == "/get_sync_status":
        return {
            "success": True,
            "synced": True,
            "synced_coins": 1,
            "total_coins": 1,
            "selectable_balance": 10_000_000_000,
            "receive_address": "xch1packagedsmokeaddress0000000000000000000000000000000000000000",
        }
    if path == "/get_cats":
        return {"success": True, "cats": []}
    return {"success": True}


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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build_env(
    *,
    base_env: dict[str, str],
    temp_dir: Path,
    sage_rpc_url: str,
    client_cert: Path,
    client_key: Path,
    flask_port: int,
    local_token: str,
) -> dict[str, str]:
    env = dict(base_env)
    env.update(
        {
            "WALLET_TYPE": "sage",
            "SAGE_RPC_URL": sage_rpc_url,
            "SAGE_CERT_PATH": str(client_cert),
            "SAGE_KEY_PATH": str(client_key),
            "SAGE_DATA_DIR": str(temp_dir / "sage-data"),
            "CMM_DATA_DIR": str(temp_dir / "catalyst-data"),
            "CATALYST_FLASK_PORT": str(flask_port),
            "BOT_LOCAL_WRITE_TOKEN": local_token,
            "_CATALYST_PRESERVE_PROCESS_ENV": "1",
            "PYTHONIOENCODING": "utf-8",
            # Keep the smoke deterministic when no user .env exists.
            "CAT_ASSET_ID": "0" * 64,
            "CAT_NAME": "Packaged Smoke Token",
            "CAT_TICKER": "SMOKE",
        }
    )
    return env


def _endpoint_checks() -> list[EndpointCheck]:
    return [
        EndpointCheck(
            "GET",
            "/api/health",
            ("status", "version", "wallet_type", "bot_running", "chia_health"),
        ),
        EndpointCheck(
            "GET",
            "/api/wallet/sage-running",
            ("running", "rpc_authenticated", "rpc_port_listening"),
        ),
        EndpointCheck(
            "POST",
            "/api/wallet/begin-startup",
            ("started",),
            body={"auto_launch": False},
            requires_token=True,
        ),
        EndpointCheck(
            "GET",
            "/api/sage/startup-status",
            ("phase", "message", "wallet_type", "preload_running"),
        ),
        EndpointCheck(
            "GET",
            "/api/config/validate",
            ("is_valid", "errors", "warnings", "error_count", "warning_count"),
        ),
        EndpointCheck(
            "GET",
            "/api/diagnostics/api-stats",
            ("spacescan.available", "coinset.available", "dexie.available"),
        ),
        EndpointCheck(
            "GET",
            "/api/self-test",
            ("all_ok", "results"),
        ),
        EndpointCheck(
            "GET",
            "/api/doctor?force=true",
            ("can_start", "summary", "checks"),
            timeout_s=25.0,
        ),
    ]


def _has_key(payload: Any, dotted_key: str) -> bool:
    current = payload
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def _validate_payload(check: EndpointCheck, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise SmokeFailure(f"{check.path} returned {type(payload).__name__}, expected JSON object")
    missing = [key for key in check.required_keys if not _has_key(payload, key)]
    if missing:
        raise SmokeFailure(f"{check.path} missing required key(s): {', '.join(missing)}")


def _request_json(
    *,
    base_url: str,
    check: EndpointCheck,
    local_token: str,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {"Accept": "application/json"}
    if check.body is not None:
        data = json.dumps(check.body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if check.requires_token:
        headers["X-Bot-Local-Token"] = local_token

    request = urllib.request.Request(
        f"{base_url}{check.path}",
        data=data,
        headers=headers,
        method=check.method,
    )
    try:
        with urllib.request.urlopen(request, timeout=check.timeout_s) as response:
            raw = response.read().decode("utf-8", "replace")
            payload = json.loads(raw or "{}")
            return int(response.status), payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return int(exc.code), payload


def _wait_for_health(base_url: str, deadline: float) -> dict[str, Any]:
    check = EndpointCheck("GET", "/api/health", ("status", "version"))
    last_error = ""
    while time.time() < deadline:
        try:
            status, payload = _request_json(base_url=base_url, check=check, local_token="")
            if status == 200:
                _validate_payload(check, payload)
                return payload
            last_error = f"HTTP {status}: {payload}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise SmokeFailure(f"Packaged app did not become healthy: {last_error}")


def _terminate_process(proc: subprocess.Popen, base_url: str, local_token: str) -> None:
    try:
        check = EndpointCheck(
            "POST",
            "/api/shutdown",
            ("success",),
            body={"cancel_offers": False},
            requires_token=True,
            timeout_s=3.0,
        )
        _request_json(base_url=base_url, check=check, local_token=local_token)
    except Exception:
        pass

    try:
        proc.wait(timeout=8)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        proc.terminate()
        proc.wait(timeout=5)
        return
    except Exception:
        pass

    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def _read_tail(path: Path, limit: int = 12000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def run_smoke(exe_path: Path, timeout_s: int) -> int:
    if not exe_path.is_file():
        print(f"ERROR: executable not found: {exe_path}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="catalyst-api-smoke-") as temp:
        temp_dir = Path(temp)
        server, thread, client_cert, client_key = _start_mock_sage(temp_dir)
        host, sage_port = server.server_address
        flask_port = _free_port()
        base_url = f"http://127.0.0.1:{flask_port}"
        local_token = "packaged-api-smoke-token"
        stdout_path = temp_dir / "catalyst-stdout.log"
        stderr_path = temp_dir / "catalyst-stderr.log"

        env = _build_env(
            base_env=os.environ.copy(),
            temp_dir=temp_dir,
            sage_rpc_url=f"https://{host}:{sage_port}",
            client_cert=client_cert,
            client_key=client_key,
            flask_port=flask_port,
            local_token=local_token,
        )
        cmd = [str(exe_path), "--flask"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        stdout_handle = stdout_path.open("w", encoding="utf-8", errors="replace")
        stderr_handle = stderr_path.open("w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            cmd,
            cwd=str(exe_path.parent),
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )

        try:
            deadline = time.time() + timeout_s
            health = _wait_for_health(base_url, deadline)
            print(f"Packaged app responded on /api/health: v{health.get('version', 'unknown')}")

            for check in _endpoint_checks():
                status, payload = _request_json(
                    base_url=base_url,
                    check=check,
                    local_token=local_token,
                )
                if status not in check.allow_statuses:
                    raise SmokeFailure(f"{check.path} returned HTTP {status}: {payload}")
                _validate_payload(check, payload)
                print(f"OK {check.method} {check.path}")

            if not server.saw_client_cert:
                raise SmokeFailure("mock Sage server did not receive a client certificate")
            if "/get_version" not in server.request_paths:
                raise SmokeFailure("mock Sage server did not receive the /get_version RPC probe")

            print("Packaged Catalyst API smoke PASSED")
            return 0
        except Exception as exc:
            print(f"ERROR: packaged API smoke failed: {exc}", file=sys.stderr)
            print("\n--- Catalyst stdout tail ---", file=sys.stderr)
            _print_safe(_read_tail(stdout_path), stream=sys.stderr)
            print("\n--- Catalyst stderr tail ---", file=sys.stderr)
            _print_safe(_read_tail(stderr_path), stream=sys.stderr)
            return 1
        finally:
            _terminate_process(proc, base_url, local_token)
            stdout_handle.close()
            stderr_handle.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test packaged Catalyst API")
    parser.add_argument("--exe", required=True, help="Path to packaged Catalyst executable")
    parser.add_argument("--timeout", type=int, default=45, help="Startup timeout in seconds")
    args = parser.parse_args()
    return run_smoke(Path(args.exe).resolve(), args.timeout)


if __name__ == "__main__":
    sys.exit(main())
