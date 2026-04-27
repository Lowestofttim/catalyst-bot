import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import bot_loop


class OversizedLockedOffersTests(unittest.TestCase):
    def setUp(self):
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        database.DB_PATH = self._db_path
        if hasattr(database._local, "conn"):
            database._local.conn = None
        database.init_database()

    def tearDown(self):
        if hasattr(database._local, "conn"):
            conn = getattr(database._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            database._local.conn = None
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def _add_locked_buy(self, trade_id, coin_id, coin_mojos, size_xch,
                        designation="tier_spare"):
        self.assertTrue(database.upsert_coin(
            coin_id=coin_id,
            wallet_type="xch",
            amount_mojos=coin_mojos,
            designation=designation,
            assigned_tier="mid" if designation != "reserve" else "none",
        ))
        self.assertTrue(database.add_offer(
            trade_id=trade_id,
            side="buy",
            price_xch=Decimal("0.001"),
            size_xch=Decimal(str(size_xch)),
            size_cat=Decimal("1000"),
            cat_asset_id="cat",
            tier="mid",
            coin_id=coin_id,
        ))
        self.assertTrue(database.lock_coin(coin_id, trade_id))

    def test_flags_reserve_coin_even_when_amount_would_fit(self):
        self._add_locked_buy(
            "trade-reserve",
            "0x" + "a" * 64,
            1_000_000_000_000,
            "1.0",
            designation="reserve",
        )

        rows = database.get_oversized_locked_offers(max_ratio=1.5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_id"], "trade-reserve")
        self.assertEqual(rows[0]["reason"], "reserve_coin_locked")

    def test_flags_oversized_trade_coin(self):
        self._add_locked_buy(
            "trade-big",
            "0x" + "b" * 64,
            48_000_000_000_000,
            "1.4",
            designation="tier_spare",
        )

        rows = database.get_oversized_locked_offers(max_ratio=1.5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_id"], "trade-big")
        self.assertEqual(rows[0]["reason"], "oversized_coin_locked")

    def test_ignores_correct_sized_tier_coin(self):
        self._add_locked_buy(
            "trade-ok",
            "0x" + "c" * 64,
            1_450_000_000_000,
            "1.4",
            designation="tier_spare",
        )

        self.assertEqual(database.get_oversized_locked_offers(max_ratio=1.5), [])

    def test_reclaim_bypasses_cancel_storm_guard(self):
        loop = bot_loop.BotLoop.__new__(bot_loop.BotLoop)
        loop.offer_manager = MagicMock()
        flagged = [
            {
                "trade_id": f"trade-{i}",
                "side": "buy",
                "tier": "mid",
                "wallet_type": "xch",
                "amount_mojos": 5_000_000_000_000,
                "expected_mojos": 1_000_000_000_000,
                "ratio": "5.0000",
                "reason": "oversized_coin_locked",
            }
            for i in range(6)
        ]

        with (
            patch.object(bot_loop.cfg, "TIER_ENABLED", True),
            patch.object(bot_loop.cfg, "COIN_MAX_SIZE_RATIO", 1.5),
            patch.object(bot_loop.cfg, "CAT_DECIMALS", 3),
            patch("database.get_oversized_locked_offers", return_value=flagged),
        ):
            self.assertTrue(loop._reclaim_oversized_locked_offers())

        loop.offer_manager.cancel_offers.assert_called_once_with(
            [f"trade-{i}" for i in range(6)],
            reason="oversized_coin_reclaim",
            force_storm=True,
            skip_confirmation=True,
        )


if __name__ == "__main__":
    unittest.main()
