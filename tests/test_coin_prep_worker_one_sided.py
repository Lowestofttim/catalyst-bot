import sys
import types
from decimal import Decimal
from unittest.mock import MagicMock, patch

import coin_prep_worker


def test_sage_tiered_prep_handles_buy_only_without_cat_tiers():
    fake_wallet_sage = types.ModuleType("wallet_sage")
    fake_wallet_sage.get_next_address = lambda wallet_id, new_address=False: {
        "address": "xch1testaddress",
    }
    fake_wallet_sage.send_transaction = MagicMock(return_value={"success": True})
    fake_wallet_sage.send_transaction_multi = MagicMock(
        return_value={"success": True, "transaction_id": "11" * 32}
    )
    fake_wallet_sage.send_cat_multi = MagicMock(
        side_effect=AssertionError("buy-only prep should not submit CAT multi_send")
    )
    fake_wallet_sage.split_coins_rpc = MagicMock(
        return_value={"success": True, "transaction_id": "22" * 32}
    )
    fake_wallet_sage.sage_topup_split = MagicMock(
        side_effect=AssertionError("buy-only prep should not split CAT pools")
    )
    fake_wallet_sage.get_pending_transactions = lambda *args, **kwargs: []
    fake_wallet_sage.get_peer_connections = lambda: [{"peer_id": "peer"}]

    worker = coin_prep_worker.CoinPrepWorker.__new__(coin_prep_worker.CoinPrepWorker)
    worker.is_sage = True
    worker.xch_wallet_id = 1
    worker.cat_wallet_id = 2
    worker.cat_decimals = 3
    worker.tier_xch_sizes = {"inner": Decimal("1")}
    worker.tier_cat_sizes = {"inner": Decimal("0")}
    worker.xch_tier_counts = {"inner": 2}
    worker.cat_tier_counts = {}
    worker.xch_target_coins = 2
    worker.cat_target_coins = 0
    worker.xch_expected_total_coins = 3
    worker.cat_expected_total_coins = 1
    worker.log = MagicMock()
    worker.update_status = MagicMock()
    worker._tx_fee_mojos = lambda: 0
    worker._split_tx_fee_mojos = lambda: 0
    worker._wait_for_preselected_pool_coin = (
        lambda wallet_id, pool_coin, side_label, tier_name, timeout_s=300, poll_interval_s=5: (
            pool_coin
        )
    )
    worker._get_transaction_confirmation_state = lambda tx_ids: {
        "known": True,
        "confirmed": True,
        "confirmed_count": len(tx_ids or []),
        "total": len(tx_ids or []),
        "height": 1,
    }
    worker._set_status_coin_counts = lambda *args, **kwargs: None
    worker.get_confirmed_coin_count = lambda wallet_id: 3 if wallet_id == 1 else 1

    pool_id = "aa" * 32
    output_ids = ["bb" * 32, "cc" * 32]

    def owned_map(wallet_id, name):
        if "split-poll-cycle" in name:
            return {output_ids[0]: 1_000_000_000_000, output_ids[1]: 1_000_000_000_000}
        if wallet_id == 1:
            return {pool_id: 2_000_000_000_000}
        return {}

    worker._get_owned_coin_amount_map = owned_map
    worker._get_strict_selectable_coin_id_set = lambda wallet_id, name: (
        set(output_ids)
        if "split-poll-cycle" in name
        else ({pool_id} if wallet_id == 1 else set())
    )
    worker._are_coin_ids_selectable = lambda wallet_id, coin_ids, name: True

    with (
        patch.dict(sys.modules, {"wallet_sage": fake_wallet_sage}),
        patch("coin_prep_worker.time.sleep", return_value=None),
    ):
        assert (
            worker.create_and_split_tier_pools_sage(
                Decimal("2"),
                Decimal("0"),
            )
            is True
        )

    fake_wallet_sage.send_transaction_multi.assert_called_once()
    fake_wallet_sage.send_cat_multi.assert_not_called()
    fake_wallet_sage.sage_topup_split.assert_not_called()
