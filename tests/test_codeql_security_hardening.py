"""Regression tests for CodeQL security-hardening findings."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import api_server
import sage_node
from blueprints import bot as bot_routes
from blueprints import coin_prep as coin_prep_routes
from blueprints import config_bp
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

    with patch(
        "user_paths.data_dir", side_effect=RuntimeError("secret local path leaked")
    ):
        resp = client.post("/api/open-data-folder", environ_base=loopback)

    assert resp.status_code == 500
    body = resp.get_json()
    assert body["error"] == "data_dir_unavailable"
    assert "secret" not in str(body).lower()


def test_api_exception_response_hides_current_exception():
    with patch.object(api_server, "log_event") as log_event:
        with api_server.app.test_request_context("/api/example"):
            try:
                raise RuntimeError("secret traceback details")
            except RuntimeError:
                response, status = api_server._api_exception("/api/example")

    assert status == 500
    body = response.get_json()
    assert body == {"error": "Internal server error", "code": "SERVER_ERROR"}
    assert "secret" not in str(body).lower()
    logged_message = str(log_event.call_args.args[2]).lower()
    assert "secret traceback details" not in logged_message
    assert "traceback" not in logged_message


def test_api_error_event_log_hides_exception_details():
    with patch.object(api_server, "log_event") as log_event:
        with api_server.app.test_request_context("/api/example"):
            response, status = api_server._api_error(
                RuntimeError("secret api error details"),
                "/api/example",
            )

    assert status == 500
    assert response.get_json() == {
        "error": "Internal server error",
        "code": "SERVER_ERROR",
    }
    logged_message = str(log_event.call_args.args[2]).lower()
    assert "secret api error details" not in logged_message


def test_spacescan_context_hides_exception_details():
    with patch(
        "database.get_market_analysis_cache",
        side_effect=RuntimeError("secret spacescan path"),
    ):
        context = api_server._get_spacescan_market_context("a" * 64)

    assert context["message"] == "Spacescan context unavailable right now."
    assert "secret" not in str(context).lower()


def test_sage_cert_pair_accepts_only_wallet_pair_under_same_ssl_dir(tmp_path):
    ssl_dir = tmp_path / "PortableSage" / "ssl"
    ssl_dir.mkdir(parents=True)
    cert = ssl_dir / "wallet.crt"
    key = ssl_dir / "wallet.key"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")

    with patch.dict(
        "os.environ",
        {"SAGE_ALLOWED_CERT_ROOTS": str(tmp_path / "PortableSage")},
        clear=False,
    ):
        ok, reason, cert_real, key_real = sage_node.validate_sage_cert_pair(
            str(cert),
            str(key),
        )

    assert ok is True
    assert reason == ""
    assert cert_real == str(cert.resolve())
    assert key_real == str(key.resolve())


def test_sage_cert_pair_rejects_key_outside_selected_ssl_dir(tmp_path):
    ssl_dir = tmp_path / "PortableSage" / "ssl"
    ssl_dir.mkdir(parents=True)
    cert = ssl_dir / "wallet.crt"
    cert.write_text("cert", encoding="utf-8")

    outside = tmp_path / "other" / "wallet.key"
    outside.parent.mkdir()
    outside.write_text("key", encoding="utf-8")

    with patch.dict(
        "os.environ",
        {"SAGE_ALLOWED_CERT_ROOTS": str(tmp_path / "PortableSage")},
        clear=False,
    ):
        ok, reason, _, _ = sage_node.validate_sage_cert_pair(str(cert), str(outside))

    assert ok is False
    assert "same Sage ssl folder" in reason


def test_sage_cert_pair_rejects_unknown_custom_root(tmp_path):
    ssl_dir = tmp_path / "PortableSage" / "ssl"
    ssl_dir.mkdir(parents=True)
    cert = ssl_dir / "wallet.crt"
    key = ssl_dir / "wallet.key"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")

    with patch.dict("os.environ", {"SAGE_ALLOWED_CERT_ROOTS": ""}, clear=False):
        ok, reason, _, _ = sage_node.validate_sage_cert_pair(str(cert), str(key))

    assert ok is False
    assert "detected Sage data folder" in reason


class _StartableBot:
    def __init__(self):
        self.started = False

    def is_running(self):
        return False

    def start(self):
        self.started = True
        return True


def _api_client():
    api_server.app.testing = True
    return api_server.app.test_client(), {"REMOTE_ADDR": "127.0.0.1"}


def test_bot_start_warnings_do_not_expose_exception_details(monkeypatch):
    client, loopback = _api_client()
    bot = _StartableBot()
    monkeypatch.setattr(api_server, "bot", bot)
    monkeypatch.setattr(api_server.cfg, "CAT_ASSET_ID", "ab" * 32, raising=False)
    monkeypatch.setattr(api_server.cfg, "SPREAD_BPS", 100, raising=False)
    monkeypatch.setattr(api_server.cfg, "ENABLE_COIN_PREP", False, raising=False)
    monkeypatch.setattr(api_server, "_get_sage_signing_block_reason", lambda: None)

    with (
        patch(
            "wallet.get_wallet_sync_status",
            side_effect=RuntimeError("secret wallet traceback"),
        ),
        patch("coin_manager.check_tier_size_drift_standalone", return_value=[]),
    ):
        resp = client.post(
            "/api/bot/start",
            headers={"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN},
            environ_base=loopback,
        )

    assert resp.status_code == 200
    body_text = resp.get_data(as_text=True).lower()
    assert "secret wallet traceback" not in body_text
    assert "traceback" not in body_text


def test_bot_start_coin_prep_gate_hides_worker_exception_details(monkeypatch):
    client, loopback = _api_client()
    bot = _StartableBot()
    monkeypatch.setattr(api_server, "bot", bot)
    monkeypatch.setattr(api_server.cfg, "CAT_ASSET_ID", "ab" * 32, raising=False)
    monkeypatch.setattr(api_server.cfg, "SPREAD_BPS", 100, raising=False)
    monkeypatch.setattr(api_server.cfg, "ENABLE_COIN_PREP", True, raising=False)
    monkeypatch.setattr(api_server, "_get_sage_signing_block_reason", lambda: None)

    failed_state = {
        "running": False,
        "complete": False,
        "phase": "error",
        "error": "secret coin prep traceback",
    }
    with (
        patch(
            "wallet.get_wallet_sync_status",
            return_value={"reachable": True, "sync_state": "synced"},
        ),
        patch("coin_manager.check_tier_size_drift_standalone", return_value=[]),
        patch.dict(api_server._coin_prep_state, failed_state, clear=True),
    ):
        resp = client.post(
            "/api/bot/start",
            headers={"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN},
            environ_base=loopback,
        )

    assert resp.status_code == 400
    body_text = resp.get_data(as_text=True).lower()
    assert "coin prep failed" in body_text
    assert "secret coin prep traceback" not in body_text
    assert "traceback" not in body_text


def test_sage_route_payloads_hide_exception_derived_details(monkeypatch):
    client, loopback = _api_client()
    auth = {"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN}

    with patch(
        "sage_node.start_chia",
        return_value={"success": False, "error": "secret daemon traceback"},
    ):
        resp = client.post(
            "/api/sage/daemon/start",
            json={"services": "all"},
            headers=auth,
            environ_base=loopback,
        )
    assert resp.status_code == 200
    assert "secret daemon traceback" not in resp.get_data(as_text=True).lower()

    with patch(
        "chia_node.get_startup_status",
        return_value={"phase": "error", "error": "secret startup traceback"},
    ):
        resp = client.get("/api/sage/startup-status", environ_base=loopback)
    assert resp.status_code == 200
    assert "secret startup traceback" not in resp.get_data(as_text=True).lower()

    with patch(
        "chia_node.trigger_start",
        return_value={"success": False, "error": "secret trigger traceback"},
    ):
        resp = client.post(
            "/api/sage/start-with-fingerprint",
            json={"fingerprint": "12345678"},
            headers=auth,
            environ_base=loopback,
        )
    assert resp.status_code == 200
    assert "secret trigger traceback" not in resp.get_data(as_text=True).lower()

    fake_cfg = SimpleNamespace(update=lambda *args, **kwargs: True)
    with (
        patch.object(api_server, "bot", None),
        patch.object(api_server, "cfg", fake_cfg),
        patch(
            "chia_node.get_available_fingerprints",
            return_value=[{"fingerprint": "12345678"}],
        ),
        patch(
            "chia_node.trigger_start",
            return_value={"success": False, "error": "secret persist traceback"},
        ),
    ):
        resp = client.post(
            "/api/sage/fingerprint",
            json={"fingerprint": "12345678"},
            headers=auth,
            environ_base=loopback,
        )
    assert resp.status_code == 400
    assert "secret persist traceback" not in resp.get_data(as_text=True).lower()


def test_config_change_address_result_hides_wallet_exception_details(monkeypatch):
    client, loopback = _api_client()
    auth = {"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN}
    fake_cfg = SimpleNamespace(
        SAGE_SET_CHANGE_ADDRESS=True,
        WALLET_ADDRESS="",
        update=lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(api_server, "cfg", fake_cfg)
    monkeypatch.setattr(config_bp, "cfg", fake_cfg)

    with (
        patch("wallet.get_wallet_type", return_value="sage"),
        patch(
            "wallet.get_next_address",
            return_value={"success": True, "address": "xch1safeaddress"},
        ),
        patch(
            "wallet_sage.set_change_address",
            return_value={"success": False, "error": "secret change traceback"},
        ),
    ):
        resp = client.post(
            "/api/config",
            json={"key": "SAGE_SET_CHANGE_ADDRESS", "value": "true"},
            headers=auth,
            environ_base=loopback,
        )

    assert resp.status_code == 200
    assert "secret change traceback" not in resp.get_data(as_text=True).lower()


def test_splash_receive_node_action_hides_exception_details(monkeypatch):
    client, loopback = _api_client()
    auth = {"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN}
    splash_node = SimpleNamespace(
        is_running=lambda: False,
        start=lambda: (_ for _ in ()).throw(RuntimeError("secret splash traceback")),
    )
    bot = SimpleNamespace(
        splash_node=splash_node,
        get_splash_receive_stats=lambda: {"enabled": True},
    )
    fake_cfg = SimpleNamespace(
        SPLASH_ENABLED=True,
        update=lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(api_server, "bot", bot)
    monkeypatch.setattr(api_server, "cfg", fake_cfg)

    resp = client.post(
        "/api/splash/receive",
        json={"enabled": True},
        headers=auth,
        environ_base=loopback,
    )

    assert resp.status_code == 200
    assert "secret splash traceback" not in resp.get_data(as_text=True).lower()


def test_client_safe_payload_removes_traceback_shaped_text():
    payload = {
        "success": True,
        "details": {
            "message": "Traceback (most recent call last): secret local path",
            "count": 3,
        },
    }

    safe = api_server._client_safe_payload(payload)

    assert safe["details"]["message"] == "Details unavailable"
    assert safe["details"]["count"] == 3
    assert "secret local path" not in str(safe).lower()


def test_status_prebot_response_hides_traceback_shaped_cached_values(monkeypatch):
    client, loopback = _api_client()
    asset_id = "ab" * 32
    monkeypatch.setattr(api_server, "bot", None)
    monkeypatch.setitem(api_server._active_cat, "asset_id", asset_id)
    monkeypatch.setitem(api_server._active_cat, "decimals", 3)
    monkeypatch.setattr(api_server.cfg, "CAT_ASSET_ID", asset_id, raising=False)
    monkeypatch.setattr(api_server.cfg, "CAT_DECIMALS", 3, raising=False)
    monkeypatch.setattr(
        bot_routes,
        "_prebot_price_cache",
        {
            "pricing": {
                "mid": "Traceback (most recent call last): secret price traceback",
                "bid": 0,
                "ask": 0,
            },
            "asset_id": asset_id,
            "fetched_at": time.time(),
        },
        raising=False,
    )

    with (
        patch("wallet.get_all_offers", return_value=[]),
        patch("wallet.get_spendable_coin_count", return_value=0),
        patch("chia_node.is_startup_authorised", return_value=False),
    ):
        resp = client.get("/api/status", environ_base=loopback)

    body = resp.get_data(as_text=True).lower()
    assert resp.status_code == 200
    assert "secret price traceback" not in body
    assert "traceback" not in body


def test_coin_prep_verify_response_hides_traceback_shaped_drift_details(monkeypatch):
    client, loopback = _api_client()
    monkeypatch.setitem(api_server._active_cat, "wallet_id", 2)
    monkeypatch.setitem(api_server._active_cat, "decimals", 3)

    drift = [
        {
            "tier": "inner",
            "side": "xch",
            "detail": "Traceback (most recent call last): secret drift traceback",
        }
    ]
    balance = {"wallet_balance": {"confirmed_wallet_balance": 0}}
    with (
        patch("wallet.get_wallet_balance", return_value=balance),
        patch(
            "wallet.get_spendable_coins_rpc",
            return_value={"success": True, "records": []},
        ),
        patch("coin_manager.check_tier_size_drift_standalone", return_value=drift),
    ):
        resp = client.get(
            "/api/coin-prep/verify",
            query_string={
                "tier_enabled": "true",
                "inner_xch": "1",
                "inner_cat": "10",
                "inner_count": "1",
            },
            environ_base=loopback,
        )

    body = resp.get_data(as_text=True).lower()
    assert resp.status_code == 200
    assert "secret drift traceback" not in body
    assert "traceback" not in body


def test_coin_prep_flat_verify_response_hides_traceback_shaped_values(monkeypatch):
    client, loopback = _api_client()
    monkeypatch.setitem(api_server._active_cat, "wallet_id", 2)
    monkeypatch.setitem(api_server._active_cat, "decimals", 3)

    malicious_balance = {
        "wallet_balance": {
            "confirmed_wallet_balance": (
                "Traceback (most recent call last): secret balance traceback"
            )
        }
    }
    malicious_coins = {
        "success": True,
        "records": [
            {
                "coin": {
                    "amount": "Traceback (most recent call last): secret coin traceback"
                }
            }
        ],
    }

    with (
        patch("wallet.get_wallet_balance", side_effect=[malicious_balance] * 2),
        patch("wallet.get_spendable_coins_rpc", side_effect=[malicious_coins] * 2),
        patch("wallet.WALLET_ID_XCH", 1),
        patch.object(coin_prep_routes.cfg, "CAT_DECIMALS", 3),
    ):
        resp = client.get(
            "/api/coin-prep/verify",
            query_string={
                "tier_enabled": "false",
                "liquidity_mode": (
                    "Traceback (most recent call last): secret mode traceback"
                ),
                "trade_size": (
                    "Traceback (most recent call last): secret trade traceback"
                ),
                "prepared_xch_size": (
                    "Traceback (most recent call last): secret xch traceback"
                ),
                "prepared_cat_size": (
                    "Traceback (most recent call last): secret cat traceback"
                ),
                "max_buy": "Traceback (most recent call last): secret buy traceback",
                "max_sell": "Traceback (most recent call last): secret sell traceback",
            },
            environ_base=loopback,
        )

    body = resp.get_data(as_text=True).lower()
    assert resp.status_code == 200
    assert resp.get_json()["liquidity_mode"] == "two_sided"
    assert "secret" not in body
    assert "traceback" not in body
