import os
import tempfile
import unittest
import importlib
import sys
from decimal import Decimal


def _load_real_database_module():
    module = sys.modules.get("database")
    if module is not None and hasattr(module, "DB_PATH"):
        return module
    sys.modules.pop("database", None)
    return importlib.import_module("database")


database = _load_real_database_module()


class DatabaseVerifiedFillsTests(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        database.close_connection()
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_database()

    def tearDown(self):
        database.close_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.unlink(self._tmp.name)
        except FileNotFoundError:
            pass

    def test_stats_and_position_ignore_legacy_fills(self):
        conn = database.get_connection()
        asset_id = "asset-test"

        conn.execute(
            """INSERT INTO fills (
                   trade_id, side, price_xch, size_xch, size_cat,
                   filled_at, cat_asset_id, tier, verification_status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "legacy-fill",
                "buy",
                "0.1",
                "1.0",
                "1000",
                "2026-03-20T00:00:00+00:00",
                asset_id,
                "mid",
                "legacy",
            ),
        )
        conn.commit()

        database.record_fill(
            trade_id="verified-fill",
            side="sell",
            price_xch=Decimal("0.2"),
            size_xch=Decimal("2.0"),
            size_cat=Decimal("2000"),
            cat_asset_id=asset_id,
            tier="outer",
        )

        stats = database.get_stats(asset_id)
        fills = database.get_fills(cat_asset_id=asset_id, limit=10)
        position = database.get_net_position(asset_id)

        self.assertEqual(stats["total_fills"], 1)
        self.assertEqual(stats["fill_rate_per_hour"], 1.0)
        self.assertEqual(stats["buy_fills"], 0)
        self.assertEqual(stats["sell_fills"], 1)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["trade_id"], "verified-fill")
        # get_net_position() intentionally includes legacy fills — they represent
        # real accumulated inventory from before the verification system was added.
        # Stats (total_fills, buy_fills, sell_fills) exclude legacy; position does not.
        # Net = legacy buy +1000 + verified sell -2000 = -1000.
        self.assertEqual(position, Decimal("-1000"))

    def test_stats_report_fee_adjusted_net_xch_flow(self):
        asset_id = "asset-fee-flow"

        database.record_fill(
            trade_id="fee-buy",
            side="buy",
            price_xch=Decimal("0.001"),
            size_xch=Decimal("0.100"),
            size_cat=Decimal("100"),
            cat_asset_id=asset_id,
            tier="inner",
            fee_mojos_xch=1_000_000_000,
        )
        database.record_fill(
            trade_id="fee-sell",
            side="sell",
            price_xch=Decimal("0.0011"),
            size_xch=Decimal("0.110"),
            size_cat=Decimal("100"),
            cat_asset_id=asset_id,
            tier="inner",
            fee_mojos_xch=1_000_000_000,
        )

        stats = database.get_stats(asset_id)

        self.assertEqual(stats["net_xch_flow"], "0.010")
        self.assertEqual(stats["fee_xch"], "0.002")
        self.assertEqual(stats["net_xch_flow_after_fees"], "0.008")

    def test_offer_coin_usage_summary_counts_requoted_source_coin(self):
        asset_id = "asset-requote"
        coin_id = "0xcoin-reused"

        database.add_offer(
            trade_id="trade-old",
            side="sell",
            price_xch=Decimal("0.001"),
            size_xch=Decimal("0.1"),
            size_cat=Decimal("100"),
            cat_asset_id=asset_id,
            tier="inner",
            coin_id=coin_id,
        )
        database.add_offer(
            trade_id="trade-new",
            side="sell",
            price_xch=Decimal("0.001"),
            size_xch=Decimal("0.1"),
            size_cat=Decimal("100"),
            cat_asset_id=asset_id,
            tier="inner",
            coin_id=coin_id,
        )
        database.record_fill(
            trade_id="trade-new",
            side="sell",
            price_xch=Decimal("0.001"),
            size_xch=Decimal("0.1"),
            size_cat=Decimal("100"),
            cat_asset_id=asset_id,
            tier="inner",
        )

        summary = database.get_offer_coin_usage_summary(coin_id, asset_id)

        self.assertEqual(summary["offer_count"], 2)
        self.assertEqual(summary["verified_fill_count"], 1)
        self.assertEqual(set(summary["trade_ids"]), {"trade-old", "trade-new"})
        self.assertEqual(summary["verified_trade_ids"], ["trade-new"])

    def test_stats_deduplicate_verified_fills_for_reused_source_coin(self):
        asset_id = "asset-requote-stats"
        coin_id = "0xcoin-reused-stats"

        for trade_id in ("trade-old", "trade-new"):
            database.add_offer(
                trade_id=trade_id,
                side="sell",
                price_xch=Decimal("0.001"),
                size_xch=Decimal("0.1"),
                size_cat=Decimal("100"),
                cat_asset_id=asset_id,
                tier="inner",
                coin_id=coin_id,
            )
            database.record_fill(
                trade_id=trade_id,
                side="sell",
                price_xch=Decimal("0.001"),
                size_xch=Decimal("0.1"),
                size_cat=Decimal("100"),
                cat_asset_id=asset_id,
                tier="inner",
            )

        stats = database.get_stats(asset_id)

        self.assertEqual(stats["raw_total_fills"], 2)
        self.assertEqual(stats["duplicate_fill_rows"], 1)
        self.assertEqual(stats["total_fills"], 1)
        self.assertEqual(stats["sell_fills"], 1)
        self.assertEqual(stats["volume_xch"], "0.1")

    def test_unmatched_fills_deduplicate_verified_fills_for_reused_source_coin(self):
        asset_id = "asset-requote-unmatched"
        coin_id = "0xcoin-reused-unmatched"

        for trade_id in ("trade-old", "trade-new"):
            database.add_offer(
                trade_id=trade_id,
                side="sell",
                price_xch=Decimal("0.001"),
                size_xch=Decimal("0.1"),
                size_cat=Decimal("100"),
                cat_asset_id=asset_id,
                tier="inner",
                coin_id=coin_id,
            )
            database.record_fill(
                trade_id=trade_id,
                side="sell",
                price_xch=Decimal("0.001"),
                size_xch=Decimal("0.1"),
                size_cat=Decimal("100"),
                cat_asset_id=asset_id,
                tier="inner",
            )

        unmatched = database.get_unmatched_fills(asset_id, "sell")

        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]["trade_id"], "trade-new")

    def test_fill_and_expiry_update_all_locked_coins_for_trade(self):
        conn = database.get_connection()
        asset_id = "asset-test"

        database.upsert_coin("0xcoin-a", "xch", 2200000000000)
        database.upsert_coin("0xcoin-b", "xch", 220000000000)
        database.add_offer(
            trade_id="trade-multi",
            side="buy",
            price_xch=Decimal("0.1"),
            size_xch=Decimal("2.2"),
            size_cat=Decimal("22000"),
            cat_asset_id=asset_id,
            tier="inner",
            coin_id="0xcoin-a",
        )
        database.lock_coin("0xcoin-a", "trade-multi")
        database.lock_coin("0xcoin-b", "trade-multi")

        database.record_fill(
            trade_id="trade-multi",
            side="buy",
            price_xch=Decimal("0.1"),
            size_xch=Decimal("2.2"),
            size_cat=Decimal("22000"),
            cat_asset_id=asset_id,
            tier="inner",
        )

        rows = conn.execute(
            "SELECT coin_id, status, designation, assigned_tier FROM coins WHERE trade_id=? ORDER BY coin_id",
            ("trade-multi",),
        ).fetchall()
        self.assertEqual(
            [
                (
                    row["coin_id"],
                    row["status"],
                    row["designation"],
                    row["assigned_tier"],
                )
                for row in rows
            ],
            [
                ("0xcoin-a", "spent", "unknown", "none"),
                ("0xcoin-b", "spent", "unknown", "none"),
            ],
        )

        database.upsert_coin("0xcoin-c", "cat", 10473000)
        database.upsert_coin("0xcoin-d", "cat", 2095000)
        database.add_offer(
            trade_id="trade-expire",
            side="sell",
            price_xch=Decimal("0.1"),
            size_xch=Decimal("1.1"),
            size_cat=Decimal("8446"),
            cat_asset_id=asset_id,
            tier="mid",
            coin_id="0xcoin-c",
        )
        database.lock_coin("0xcoin-c", "trade-expire")
        database.lock_coin("0xcoin-d", "trade-expire")
        database.update_offer_status("trade-expire", "expired")

        rows = conn.execute(
            "SELECT coin_id, status, trade_id FROM coins WHERE coin_id IN ('0xcoin-c', '0xcoin-d') ORDER BY coin_id"
        ).fetchall()
        self.assertEqual(
            [(row["coin_id"], row["status"], row["trade_id"]) for row in rows],
            [
                ("0xcoin-c", "free", None),
                ("0xcoin-d", "free", None),
            ],
        )

    def test_stats_net_position_honors_fresh_run_cutoff(self):
        asset_id = "asset-test"

        database.record_fill(
            trade_id="old-run-buy",
            side="buy",
            price_xch=Decimal("0.1"),
            size_xch=Decimal("1.0"),
            size_cat=Decimal("1000"),
            cat_asset_id=asset_id,
            tier="mid",
            filled_at="2026-03-27T20:00:00+00:00",
        )
        database.record_fill(
            trade_id="fresh-run-sell",
            side="sell",
            price_xch=Decimal("0.1"),
            size_xch=Decimal("0.2"),
            size_cat=Decimal("200"),
            cat_asset_id=asset_id,
            tier="mid",
            filled_at="2026-03-28T22:10:00+00:00",
        )

        stats = database.get_stats(asset_id, since="2026-03-28T22:07:28+00:00")

        self.assertEqual(stats["total_fills"], 1)
        self.assertEqual(stats["net_position"], "-200")
        self.assertEqual(stats["net_cat_flow"], "-200")

    def test_fill_upgrade_clears_cancelled_timestamp(self):
        conn = database.get_connection()
        asset_id = "asset-test"

        database.upsert_coin("0xcoin-upgrade", "xch", 240000000000)
        database.add_offer(
            trade_id="trade-upgrade",
            side="sell",
            price_xch=Decimal("0.1"),
            size_xch=Decimal("0.24"),
            size_cat=Decimal("1900"),
            cat_asset_id=asset_id,
            tier="extreme",
            coin_id="0xcoin-upgrade",
        )
        database.lock_coin("0xcoin-upgrade", "trade-upgrade")

        self.assertTrue(database.update_offer_status("trade-upgrade", "cancelled"))
        self.assertTrue(database.update_offer_status("trade-upgrade", "filled"))

        row = conn.execute(
            "SELECT status, filled_at, cancelled_at FROM offers WHERE trade_id=?",
            ("trade-upgrade",),
        ).fetchone()
        self.assertEqual(row["status"], "filled")
        self.assertIsNotNone(row["filled_at"])
        self.assertIsNone(row["cancelled_at"])

    def test_backfill_verified_fills_from_filled_offers_repairs_stats(self):
        asset_id = "asset-test"

        database.add_offer(
            trade_id="trade-backfill",
            side="sell",
            price_xch=Decimal("0.125"),
            size_xch=Decimal("0.6"),
            size_cat=Decimal("4800"),
            cat_asset_id=asset_id,
            tier="outer",
            coin_id="0xcoin-backfill",
        )
        self.assertTrue(database.update_offer_status("trade-backfill", "filled"))

        stats_before = database.get_stats(asset_id)
        self.assertEqual(stats_before["total_fills"], 0)

        repaired = database.backfill_verified_fills_from_offers(limit=10)
        self.assertEqual(len(repaired), 1)
        self.assertTrue(repaired[0]["created"])
        self.assertEqual(repaired[0]["trade_id"], "trade-backfill")
        self.assertEqual(repaired[0]["tier"], "outer")

        fills = database.get_fills(cat_asset_id=asset_id, limit=10)
        stats_after = database.get_stats(asset_id)

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["trade_id"], "trade-backfill")
        self.assertEqual(stats_after["total_fills"], 1)
        self.assertEqual(stats_after["sell_fills"], 1)

    def test_backfill_promotes_legacy_fill_to_verified(self):
        conn = database.get_connection()
        asset_id = "asset-test"

        database.add_offer(
            trade_id="trade-upgrade-verified",
            side="buy",
            price_xch=Decimal("0.11"),
            size_xch=Decimal("1.2"),
            size_cat=Decimal("10000"),
            cat_asset_id=asset_id,
            tier="mid",
            coin_id="0xcoin-upgrade-verified",
        )
        self.assertTrue(
            database.update_offer_status("trade-upgrade-verified", "filled")
        )

        conn.execute(
            """INSERT INTO fills (
                   trade_id, side, price_xch, size_xch, size_cat,
                   filled_at, cat_asset_id, tier, verification_status
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "trade-upgrade-verified",
                "buy",
                "0.11",
                "1.2",
                "10000",
                "2026-03-27T20:00:00+00:00",
                asset_id,
                "mid",
                "legacy",
            ),
        )
        conn.commit()

        repaired = database.backfill_verified_fills_from_offers(limit=10)
        self.assertEqual(len(repaired), 1)
        self.assertTrue(repaired[0]["upgraded"])
        self.assertFalse(repaired[0]["created"])

        row = conn.execute(
            "SELECT verification_status FROM fills WHERE trade_id=?",
            ("trade-upgrade-verified",),
        ).fetchone()
        self.assertEqual(row["verification_status"], "verified")

    def test_backfill_and_stats_honor_fresh_run_cutoff(self):
        conn = database.get_connection()
        asset_id = "asset-test"

        database.add_offer(
            trade_id="trade-old-filled",
            side="sell",
            price_xch=Decimal("0.12"),
            size_xch=Decimal("0.6"),
            size_cat=Decimal("5000"),
            cat_asset_id=asset_id,
            tier="outer",
            coin_id="0xcoin-old-filled",
        )
        conn.execute(
            "UPDATE offers SET status='filled', filled_at=?, created_at=? WHERE trade_id=?",
            (
                "2026-03-27T22:00:00+00:00",
                "2026-03-27T21:59:00+00:00",
                "trade-old-filled",
            ),
        )

        database.add_offer(
            trade_id="trade-new-filled",
            side="buy",
            price_xch=Decimal("0.11"),
            size_xch=Decimal("0.6"),
            size_cat=Decimal("5000"),
            cat_asset_id=asset_id,
            tier="outer",
            coin_id="0xcoin-new-filled",
        )
        conn.execute(
            "UPDATE offers SET status='filled', filled_at=?, created_at=? WHERE trade_id=?",
            (
                "2026-03-28T22:10:00+00:00",
                "2026-03-28T22:09:00+00:00",
                "trade-new-filled",
            ),
        )
        conn.commit()

        repaired = database.backfill_verified_fills_from_offers(
            limit=10,
            since="2026-03-28T22:07:28+00:00",
        )
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0]["trade_id"], "trade-new-filled")

        fills = database.get_fills(cat_asset_id=asset_id, limit=10)
        self.assertEqual([f["trade_id"] for f in fills], ["trade-new-filled"])

        stats = database.get_stats(asset_id, since="2026-03-28T22:07:28+00:00")
        self.assertEqual(stats["total_fills"], 1)
        self.assertEqual(stats["buy_fills"], 1)
        self.assertEqual(stats["sell_fills"], 0)


if __name__ == "__main__":
    unittest.main()
