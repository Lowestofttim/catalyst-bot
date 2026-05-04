"""Unit tests for the packaged Catalyst API smoke-test helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "packaged_api_smoke.py"
_SPEC = importlib.util.spec_from_file_location("packaged_api_smoke", _SCRIPT)
packaged_api_smoke = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(packaged_api_smoke)


def test_build_env_uses_isolated_packaged_runtime_paths(tmp_path):
    env = packaged_api_smoke._build_env(
        base_env={"PATH": "keep-me"},
        temp_dir=tmp_path,
        sage_rpc_url="https://127.0.0.1:9257",
        client_cert=tmp_path / "wallet.crt",
        client_key=tmp_path / "wallet.key",
        flask_port=51234,
        local_token="test-token",
    )

    assert env["PATH"] == "keep-me"
    assert env["WALLET_TYPE"] == "sage"
    assert env["SAGE_RPC_URL"] == "https://127.0.0.1:9257"
    assert env["SAGE_CERT_PATH"] == str(tmp_path / "wallet.crt")
    assert env["SAGE_KEY_PATH"] == str(tmp_path / "wallet.key")
    assert env["SAGE_DATA_DIR"] == str(tmp_path / "sage-data")
    assert env["CMM_DATA_DIR"] == str(tmp_path / "catalyst-data")
    assert env["CATALYST_FLASK_PORT"] == "51234"
    assert env["BOT_LOCAL_WRITE_TOKEN"] == "test-token"
    assert env["_CATALYST_PRESERVE_PROCESS_ENV"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_endpoint_contract_includes_core_release_surfaces():
    checks = packaged_api_smoke._endpoint_checks()
    paths = [check.path for check in checks]

    assert "/api/health" in paths
    assert "/api/config/validate" in paths
    assert "/api/diagnostics/api-stats" in paths
    assert "/api/self-test" in paths
    assert "/api/sage/startup-status" in paths
    assert "/api/wallet/begin-startup" in paths

    begin_startup = next(c for c in checks if c.path == "/api/wallet/begin-startup")
    assert begin_startup.method == "POST"
    assert begin_startup.requires_token is True
    assert begin_startup.body == {"auto_launch": False}


def test_validate_payload_rejects_missing_required_keys():
    check = packaged_api_smoke.EndpointCheck(
        method="GET",
        path="/api/health",
        required_keys=("status", "version"),
    )

    packaged_api_smoke._validate_payload(check, {"status": "ok", "version": "1.2.3"})

    try:
        packaged_api_smoke._validate_payload(check, {"version": "1.2.3"})
    except packaged_api_smoke.SmokeFailure as exc:
        assert "/api/health" in str(exc)
        assert "status" in str(exc)
    else:
        raise AssertionError("missing required key should fail the smoke contract")


def test_validate_payload_rejects_nested_required_keys():
    check = packaged_api_smoke.EndpointCheck(
        method="GET",
        path="/api/diagnostics/api-stats",
        required_keys=("spacescan.available", "coinset.available", "dexie.available"),
    )

    packaged_api_smoke._validate_payload(
        check,
        {
            "spacescan": {"available": True},
            "coinset": {"available": False},
            "dexie": {"available": False},
        },
    )

    try:
        packaged_api_smoke._validate_payload(
            check,
            {"spacescan": {"available": True}, "coinset": {}, "dexie": {}},
        )
    except packaged_api_smoke.SmokeFailure as exc:
        assert "coinset.available" in str(exc)
        assert "dexie.available" in str(exc)
    else:
        raise AssertionError("missing nested keys should fail the smoke contract")
