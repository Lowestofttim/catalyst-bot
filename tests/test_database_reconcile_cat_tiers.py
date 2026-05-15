import os
import sqlite3
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path


if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False
    dotenv_stub.set_key = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub


os.environ["TIER_ENABLED"] = "true"
os.environ["INNER_SIZE_XCH"] = "2.4"
os.environ["MID_SIZE_XCH"] = "1.2"
os.environ["OUTER_SIZE_XCH"] = "0.6"
os.environ["EXTREME_SIZE_XCH"] = "0.24"
# Explicitly zero out per-side sell sizes so get_sell_tier_size_xch() falls
# back to the legacy INNER_SIZE_XCH etc. values set above.  A prior test that
# imports api_server may have called load_dotenv(override=True), writing real
# SELL_*_SIZE_XCH values into os.environ; without these zeros cfg.reload()
# would pick those up and change the tier thresholds, breaking the test.
os.environ["SELL_INNER_SIZE_XCH"] = "0"
os.environ["SELL_MID_SIZE_XCH"] = "0"
os.environ["SELL_OUTER_SIZE_XCH"] = "0"
os.environ["SELL_EXTREME_SIZE_XCH"] = "0"
os.environ["BUY_INNER_SIZE_XCH"] = "0"
os.environ["BUY_MID_SIZE_XCH"] = "0"
os.environ["BUY_OUTER_SIZE_XCH"] = "0"
os.environ["BUY_EXTREME_SIZE_XCH"] = "0"
os.environ["BUY_LADDER_REVERSED"] = "false"
os.environ["SNIPER_ENABLED"] = "false"
os.environ["CAT_DECIMALS"] = "3"
os.environ["CAT_COIN_SIZE"] = "4000"
os.environ["COIN_PREP_HEADROOM_PCT"] = "10"

import config
import database


class DatabaseReconcileCatTierTests(unittest.TestCase):
    def setUp(self):
        # Patch config.load_dotenv to a no-op so that cfg.reload() reads from
        # os.environ as set above, rather than re-loading the production .env
        # file (which would overwrite the test values with real values).
        # This is needed when the real dotenv is already in sys.modules (loaded
        # by a prior test that imports api_server), because in that case the
        # module-level `if "dotenv" not in sys.modules` guard above is skipped.
        self._orig_load_dotenv = getattr(config, "load_dotenv", None)
        config.load_dotenv = lambda *args, **kwargs: None

        # Re-apply test env vars AFTER patching load_dotenv and BEFORE reload.
        # This is required because a previous test's Config() init may have
        # already called load_dotenv(override=True), writing the real .env
        # values into os.environ. The patch prevents another overwrite, but we
        # still need to restore our test values first.
        os.environ["TIER_ENABLED"] = "true"
        os.environ["INNER_SIZE_XCH"] = "2.4"
        os.environ["MID_SIZE_XCH"] = "1.2"
        os.environ["OUTER_SIZE_XCH"] = "0.6"
        os.environ["EXTREME_SIZE_XCH"] = "0.24"
        os.environ["SELL_INNER_SIZE_XCH"] = "0"
        os.environ["SELL_MID_SIZE_XCH"] = "0"
        os.environ["SELL_OUTER_SIZE_XCH"] = "0"
        os.environ["SELL_EXTREME_SIZE_XCH"] = "0"
        os.environ["BUY_INNER_SIZE_XCH"] = "0"
        os.environ["BUY_MID_SIZE_XCH"] = "0"
        os.environ["BUY_OUTER_SIZE_XCH"] = "0"
        os.environ["BUY_EXTREME_SIZE_XCH"] = "0"
        os.environ["BUY_LADDER_REVERSED"] = "false"
        os.environ["SNIPER_ENABLED"] = "false"
        os.environ["CAT_DECIMALS"] = "3"
        os.environ["CAT_COIN_SIZE"] = "4000"
        os.environ["COIN_PREP_HEADROOM_PCT"] = "10"

        config.cfg.reload()
        # Re-install the patched real config module into sys.modules so that
        # `from config import cfg` calls inside database functions (e.g.
        # _classify_cat_coin_tier) find our patched version rather than
        # triggering a fresh module import that creates a new Config() with
        # real .env values.  This is needed when a prior test (e.g.
        # test_coin_manager_exact_selectable) popped sys.modules["config"].
        sys.modules["config"] = config

        handle, temp_path = tempfile.mkstemp(
            dir=Path.cwd(), prefix="tmp_db_reconcile_", suffix=".sqlite"
        )
        os.close(handle)
        self.db_path = Path(temp_path)
        self.db_path.unlink(missing_ok=True)
        database.close_connection()
        database.DB_PATH = str(self.db_path)
        database.init_database()

    def tearDown(self):
        # Restore config.load_dotenv so subsequent tests are not affected.
        if self._orig_load_dotenv is not None:
            config.load_dotenv = self._orig_load_dotenv
        database.close_connection()
        self.db_path.unlink(missing_ok=True)

    def test_reconcile_uses_cat_tier_sizes_for_new_coins(self):
        database.record_price(
            cat_asset_id="test-cat",
            combined_price=Decimal("0.0001035294117647058823529411765"),
            dexie_price=None,
            tibet_price=None,
            strategy_used="test",
        )

        coin_id = "0xabc123"
        amount_mojos = 25_500_000
        stats = database.reconcile_coins_with_wallet(
            wallet_selectable={coin_id: amount_mojos},
            wallet_owned={coin_id: amount_mojos},
            wallet_type="cat",
        )

        self.assertEqual(stats["added"], 1)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT designation, assigned_tier FROM coins WHERE coin_id=?",
            (coin_id,),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["designation"], "tier_spare")
        self.assertEqual(row["assigned_tier"], "inner")

    def test_reconcile_flips_reversed_buy_sizes_for_xch_new_coins(self):
        os.environ["BUY_LADDER_REVERSED"] = "true"
        os.environ["BUY_INNER_SIZE_XCH"] = "0.5"
        os.environ["BUY_MID_SIZE_XCH"] = "1.0"
        os.environ["BUY_OUTER_SIZE_XCH"] = "1.5"
        os.environ["BUY_EXTREME_SIZE_XCH"] = "2.0"
        config.cfg.reload()
        sys.modules["config"] = config

        coin_id = "0xreversedmid"
        # Reversed buy: coin-size mid is funded by the outer slot size.
        amount_mojos = 1_650_000_000_000
        stats = database.reconcile_coins_with_wallet(
            wallet_selectable={coin_id: amount_mojos},
            wallet_owned={coin_id: amount_mojos},
            wallet_type="xch",
        )

        self.assertEqual(stats["added"], 1)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT designation, assigned_tier FROM coins WHERE coin_id=?",
            (coin_id,),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["designation"], "tier_spare")
        self.assertEqual(row["assigned_tier"], "mid")


if __name__ == "__main__":
    unittest.main()
