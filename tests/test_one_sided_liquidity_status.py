from unittest.mock import patch
from pathlib import Path

import api_server


def test_buy_only_status_is_not_parked_when_buy_offers_are_live():
    raw_status = {
        "balances": {"xch": {"spendable": 76.538979}, "cat": {"spendable": 1463005}},
        "coin_tracking": {"xch_free": 71, "cat_free": 1},
        "offers": {"buy": [{"trade_id": "buy-live"}], "sell": []},
    }

    with (
        patch.object(api_server.cfg, "LIQUIDITY_MODE", "buy_only"),
        patch.object(api_server.cfg, "active_side", return_value="buy"),
        patch.object(api_server.cfg, "XCH_RESERVE", 76.538979),
    ):
        block = api_server._build_liquidity_status_block(raw_status)

    assert block["mode"] == "buy_only"
    assert block["parked"] is False
    assert block["parked_reason"] is None


def test_sell_only_status_is_not_parked_when_cat_tier_spares_exist():
    raw_status = {
        "balances": {
            "xch": {"spendable": 76.5},
            "cat": {"spendable": 731502.767},
        },
        "coin_tracking": {"xch_free": 1, "cat_free": 44},
        "offers": {"buy": [], "sell": []},
        "pricing": {"mid": 0.000115},
    }

    with (
        patch.object(api_server.cfg, "LIQUIDITY_MODE", "sell_only"),
        patch.object(api_server.cfg, "active_side", return_value="sell"),
        patch.object(api_server.cfg, "CAT_RESERVE", 731502.767),
    ):
        block = api_server._build_liquidity_status_block(raw_status)

    assert block["mode"] == "sell_only"
    assert block["parked"] is False
    assert block["parked_reason"] is None


def test_status_endpoint_feeds_enriched_coin_tracking_into_liquidity_block():
    source = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "catalyst"
        / "blueprints"
        / "bot.py"
    ).read_text(encoding="utf-8")

    assert '_liquidity_raw["coin_tracking"] = coin_tracking' in source
    assert '_liquidity_raw["offers"] = offers_out' in source
