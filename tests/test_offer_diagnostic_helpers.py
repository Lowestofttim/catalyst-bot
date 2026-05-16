import importlib

importlib.import_module("api_server")  # load blueprints through normal API bootstrap
from blueprints import offers


def test_offer_diagnostic_separates_pending_and_terminal_wallet_rows():
    result = offers._classify_offer_diagnostic_sets(
        db_rows=[
            {
                "trade_id": "live-buy",
                "side": "buy",
                "status": "open",
                "lifecycle_state": "open",
            },
            {
                "trade_id": "pending-sell",
                "side": "sell",
                "status": "open",
                "lifecycle_state": "cancel_requested",
            },
            {
                "trade_id": "cancelled-sell",
                "side": "sell",
                "status": "cancelled",
                "lifecycle_state": "cancelled",
            },
        ],
        wallet_ids={"pending-sell", "cancelled-sell", "unknown-wallet"},
    )

    assert result["active_db_ids"] == {"live-buy"}
    assert result["pending_cancel_ids"] == {"pending-sell"}
    assert result["terminal_db_ids"] == {"cancelled-sell"}
    assert result["stale_in_db"] == ["live-buy"]
    assert result["wallet_only"] == ["unknown-wallet"]
    assert result["wallet_cancel_pending"] == ["pending-sell"]
    assert result["wallet_cancelled_still_visible"] == ["cancelled-sell"]
