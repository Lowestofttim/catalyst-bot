"""Unit tests for the packaged Sage RPC smoke-test helper."""

from __future__ import annotations

import importlib.util
import ssl
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "packaged_sage_rpc_smoke.py"
_SPEC = importlib.util.spec_from_file_location("packaged_sage_rpc_smoke", _SCRIPT)
packaged_sage_rpc_smoke = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(packaged_sage_rpc_smoke)


def test_mock_sage_server_context_requires_tls_1_2_or_newer(tmp_path):
    ca_path, ca_key, ca_cert = packaged_sage_rpc_smoke._create_ca(tmp_path)
    server_cert, server_key = packaged_sage_rpc_smoke._create_signed_cert(
        tmp_path, "server", "mock-sage-server", ca_key, ca_cert, is_server=True
    )

    context = packaged_sage_rpc_smoke._mock_sage_server_context(
        server_cert, server_key, ca_path
    )

    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
