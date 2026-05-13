"""Regression tests for CodeQL security-hardening findings."""

from __future__ import annotations

from unittest.mock import patch

import api_server
import coin_prep_worker


def test_coin_prep_cli_rejects_unsafe_args_without_spawning(monkeypatch):
    worker = coin_prep_worker.CoinPrepWorker.__new__(coin_prep_worker.CoinPrepWorker)
    worker.fingerprint = "123;calc"
    spawned = False

    def fake_run(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("unsafe command should not be spawned")

    monkeypatch.setattr(coin_prep_worker.subprocess, "run", fake_run)

    success, output = worker._run_chia_wallet_show()

    assert success is False
    assert "unsafe" in output.lower()
    assert spawned is False


def test_open_data_folder_error_does_not_expose_exception_details():
    api_server.app.testing = True
    client = api_server.app.test_client()
    loopback = {"REMOTE_ADDR": "127.0.0.1"}
    client.get("/", environ_base=loopback)

    with patch("user_paths.data_dir", side_effect=RuntimeError("secret local path leaked")):
        resp = client.post("/api/open-data-folder", environ_base=loopback)

    assert resp.status_code == 500
    body = resp.get_json()
    assert body["error"] == "data_dir_unavailable"
    assert "secret" not in str(body).lower()


def test_api_exception_response_hides_current_exception():
    with api_server.app.test_request_context("/api/example"):
        try:
            raise RuntimeError("secret traceback details")
        except RuntimeError:
            response, status = api_server._api_exception("/api/example")

    assert status == 500
    body = response.get_json()
    assert body == {"error": "Internal server error", "code": "SERVER_ERROR"}
    assert "secret" not in str(body).lower()
