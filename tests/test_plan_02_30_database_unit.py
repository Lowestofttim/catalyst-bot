"""Slice 02-30 — database.py unit tests: every public function.

Uses a per-test temp SQLite database (not the real bot.db) to ensure
isolation. Tests covers norm_coin_id, add_offer/get_offer/update_offer_status,
get_open_offers, record_fill/get_fills/get_net_position, upsert_coin/
get_free_coins/lock_coin/free_coin/get_coin_summary, record_price/
get_recent_prices, log_event/get_recent_events, get_setting/set_setting.
"""
import os
import tempfile
import unittest
from decimal import Decimal

try:
    import database as _db
    _SKIP = None
except ModuleNotFoundError as exc:
    _db = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Per-test temp DB helper
# ---------------------------------------------------------------------------

class _TempDB(unittest.TestCase):
    """Base class: creates an isolated temp DB for each test method."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._tmp_path = self._tmp.name

        # Redirect database to the temp file
        self._orig_db_path = _db.DB_PATH
        _db.DB_PATH = self._tmp_path

        # Reset init guard so init_database runs fresh
        self._orig_init_path = _db._db_initialized_path
        _db._db_initialized_path = ""

        # Discard any cached thread-local connection
        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None

        _db.init_database()

    def tearDown(self):
        # Close thread-local connection
        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None

        # Restore original DB path
        _db.DB_PATH = self._orig_db_path
        _db._db_initialized_path = self._orig_init_path

        # Clean up temp file
        try:
            os.unlink(self._tmp_path)
        except OSError:
            pass


# ===========================================================================
# Pure function tests (no DB needed)
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestNormCoinId(unittest.TestCase):
    """norm_coin_id — normalize coin IDs to lowercase 0x-prefixed form."""

    def test_adds_0x_prefix(self):
        self.assertEqual(_db.norm_coin_id("abc123"), "0xabc123")

    def test_preserves_existing_0x_prefix(self):
        self.assertEqual(_db.norm_coin_id("0xABC123"), "0xabc123")

    def test_lowercases_hex(self):
        self.assertEqual(_db.norm_coin_id("0xABCDEF"), "0xabcdef")

    def test_empty_string_returns_empty(self):
        self.assertEqual(_db.norm_coin_id(""), "")

    def test_none_returns_empty(self):
        self.assertEqual(_db.norm_coin_id(None), "")

    def test_whitespace_stripped(self):
        self.assertEqual(_db.norm_coin_id("  abc123  "), "0xabc123")


# ===========================================================================
# DB tests (isolated per-test temp database)
# ===========================================================================

@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestAddOffer(_TempDB):
    """add_offer and get_offer — insert and read."""

    def test_add_offer_returns_true(self):
        result = _db.add_offer("t1", "buy", Decimal("1.00"), Decimal("0.5"),
                               Decimal("500"), "assetid1")
        self.assertTrue(result)

    def test_get_offer_returns_inserted(self):
        _db.add_offer("t2", "sell", Decimal("1.10"), Decimal("0.3"),
                      Decimal("300"), "assetid1")
        offer = _db.get_offer("t2")
        self.assertIsNotNone(offer)
        self.assertEqual(offer["trade_id"], "t2")
        self.assertEqual(offer["side"], "sell")
        self.assertEqual(offer["status"], "open")

    def test_duplicate_trade_id_returns_false(self):
        _db.add_offer("t3", "buy", Decimal("1.00"), Decimal("0.5"),
                      Decimal("500"), "assetid1")
        result = _db.add_offer("t3", "buy", Decimal("1.00"), Decimal("0.5"),
                               Decimal("500"), "assetid1")
        self.assertFalse(result)

    def test_get_offer_missing_returns_none(self):
        self.assertIsNone(_db.get_offer("nonexistent"))

    def test_tier_is_stored(self):
        _db.add_offer("t4", "buy", Decimal("1.00"), Decimal("0.5"),
                      Decimal("500"), "assetid1", tier="inner")
        offer = _db.get_offer("t4")
        self.assertEqual(offer["tier"], "inner")


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestUpdateOfferStatus(_TempDB):
    """update_offer_status — state transitions."""

    def test_returns_true_on_success(self):
        _db.add_offer("t1", "buy", Decimal("1.00"), Decimal("0.5"),
                      Decimal("500"), "assetid1")
        result = _db.update_offer_status("t1", "cancelled")
        self.assertTrue(result)

    def test_status_updated(self):
        _db.add_offer("t2", "buy", Decimal("1.00"), Decimal("0.5"),
                      Decimal("500"), "assetid1")
        _db.update_offer_status("t2", "filled")
        offer = _db.get_offer("t2")
        self.assertEqual(offer["status"], "filled")

    def test_returns_true_on_unknown_trade_id(self):
        # SQLite UPDATE of 0 rows is not an error; function returns True (no-op success)
        result = _db.update_offer_status("nonexistent", "cancelled")
        self.assertTrue(result)


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestGetOpenOffers(_TempDB):
    """get_open_offers — filtered query."""

    def setUp(self):
        super().setUp()
        _db.add_offer("b1", "buy", Decimal("1.00"), Decimal("0.5"),
                      Decimal("500"), "assetA")
        _db.add_offer("s1", "sell", Decimal("1.10"), Decimal("0.3"),
                      Decimal("300"), "assetA")
        _db.add_offer("b2", "buy", Decimal("0.99"), Decimal("0.5"),
                      Decimal("500"), "assetB")

    def test_returns_all_open_when_no_filter(self):
        offers = _db.get_open_offers()
        self.assertGreaterEqual(len(offers), 3)

    def test_filters_by_side(self):
        buys = _db.get_open_offers(side="buy")
        self.assertTrue(all(o["side"] == "buy" for o in buys))

    def test_filters_by_cat_asset_id(self):
        offers = _db.get_open_offers(cat_asset_id="assetA")
        trade_ids = {o["trade_id"] for o in offers}
        self.assertIn("b1", trade_ids)
        self.assertIn("s1", trade_ids)
        self.assertNotIn("b2", trade_ids)

    def test_excludes_cancelled_offers(self):
        _db.update_offer_status("b1", "cancelled")
        buys = _db.get_open_offers(side="buy", cat_asset_id="assetA")
        self.assertNotIn("b1", {o["trade_id"] for o in buys})

    def test_excludes_pending_fill_verification_by_default(self):
        _db.update_offer_lifecycle_state("b1", "mempool_observed")
        offers = _db.get_open_offers(cat_asset_id="assetA")
        self.assertNotIn("b1", {o["trade_id"] for o in offers})

        with_pending = _db.get_open_offers(
            cat_asset_id="assetA",
            include_mempool_observed=True,
        )
        self.assertIn("b1", {o["trade_id"] for o in with_pending})

    def test_get_stats_excludes_non_actionable_open_rows(self):
        _db.update_offer_lifecycle_state("b1", "mempool_observed")
        _db.update_offer_lifecycle_state("s1", "cancel_requested")

        stats = _db.get_stats(cat_asset_id="assetA")

        self.assertEqual(stats["open_offers"], 0)
        self.assertEqual(stats["open_buys"], 0)
        self.assertEqual(stats["open_sells"], 0)

    def test_get_offers_for_repost_excludes_non_actionable_open_rows(self):
        conn = _db.get_connection()
        conn.execute(
            "UPDATE offers SET offer_bech32='offer1fake' WHERE trade_id='b1'"
        )
        conn.execute(
            "UPDATE offers SET offer_bech32='offer1fake' WHERE trade_id='s1'"
        )
        conn.commit()
        _db.update_offer_lifecycle_state("b1", "mempool_observed")

        repostable = _db.get_offers_for_repost(cat_asset_id="assetA")
        trade_ids = {o["trade_id"] for o in repostable}

        self.assertNotIn("b1", trade_ids)
        self.assertIn("s1", trade_ids)


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestRecordFill(_TempDB):
    """record_fill and get_fills — fill recording round-trip."""

    def test_record_fill_returns_fill_id(self):
        fill_id = _db.record_fill("t1", "buy", Decimal("1.00"), Decimal("0.5"),
                                  Decimal("500"), "assetA")
        self.assertIsNotNone(fill_id)
        self.assertGreater(fill_id, 0)

    def test_get_fills_returns_recorded_fill(self):
        _db.record_fill("t2", "sell", Decimal("1.10"), Decimal("0.3"),
                        Decimal("300"), "assetA")
        fills = _db.get_fills(cat_asset_id="assetA")
        trade_ids = [f["trade_id"] for f in fills]
        self.assertIn("t2", trade_ids)

    def test_get_fills_filters_by_side(self):
        _db.record_fill("tb", "buy", Decimal("1.00"), Decimal("0.5"),
                        Decimal("500"), "assetA")
        _db.record_fill("ts", "sell", Decimal("1.10"), Decimal("0.3"),
                        Decimal("300"), "assetA")
        buys = _db.get_fills(cat_asset_id="assetA", side="buy")
        self.assertTrue(all(f["side"] == "buy" for f in buys))


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestGetNetPosition(_TempDB):
    """get_net_position — computed from fills."""

    def test_no_fills_returns_zero(self):
        pos = _db.get_net_position("assetA")
        self.assertEqual(pos, Decimal("0"))

    def test_buy_fill_adds_to_position(self):
        _db.record_fill("t1", "buy", Decimal("1.00"), Decimal("0.5"),
                        Decimal("500"), "assetA")
        pos = _db.get_net_position("assetA")
        self.assertGreater(pos, Decimal("0"))

    def test_sell_fill_subtracts_from_position(self):
        _db.record_fill("t1", "sell", Decimal("1.00"), Decimal("0.5"),
                        Decimal("500"), "assetA")
        pos = _db.get_net_position("assetA")
        self.assertLess(pos, Decimal("0"))

    def test_balanced_buys_and_sells_near_zero(self):
        _db.record_fill("b1", "buy", Decimal("1.00"), Decimal("0.5"),
                        Decimal("500"), "assetA")
        _db.record_fill("s1", "sell", Decimal("1.05"), Decimal("0.5"),
                        Decimal("500"), "assetA")
        pos = _db.get_net_position("assetA")
        self.assertAlmostEqual(float(pos), 0.0, delta=1.0)


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestUpsertCoin(_TempDB):
    """upsert_coin, get_free_coins, lock_coin, free_coin."""

    def test_upsert_coin_can_be_retrieved(self):
        _db.upsert_coin("0xabc1", "xch", 250_000_000_000,
                        designation="mid", tier="mid")
        coins = _db.get_free_coins("xch")
        ids = [c["coin_id"] for c in coins]
        self.assertIn("0xabc1", ids)

    def test_lock_coin_removes_from_free_list(self):
        _db.upsert_coin("0xabc2", "xch", 250_000_000_000,
                        designation="mid", tier="mid")
        _db.lock_coin("0xabc2", "offer-t1")
        coins = _db.get_free_coins("xch")
        ids = [c["coin_id"] for c in coins]
        self.assertNotIn("0xabc2", ids)

    def test_free_coin_restores_to_free_list(self):
        _db.upsert_coin("0xabc3", "xch", 250_000_000_000,
                        designation="mid", tier="mid")
        _db.lock_coin("0xabc3", "offer-t2")
        _db.free_coin("0xabc3")
        coins = _db.get_free_coins("xch")
        ids = [c["coin_id"] for c in coins]
        self.assertIn("0xabc3", ids)

    def test_upsert_updates_amount_on_conflict(self):
        _db.upsert_coin("0xabc4", "xch", 100_000_000_000,
                        designation="mid", tier="mid")
        _db.upsert_coin("0xabc4", "xch", 200_000_000_000,
                        designation="mid", tier="mid")
        coins = _db.get_free_coins("xch")
        match = next((c for c in coins if c["coin_id"] == "0xabc4"), None)
        self.assertIsNotNone(match)
        self.assertEqual(int(match["amount_mojos"]), 200_000_000_000)


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestGetCoinSummary(_TempDB):
    """get_coin_summary — aggregate counts."""

    def test_empty_db_returns_zeroes(self):
        summary = _db.get_coin_summary()
        self.assertEqual(summary.get("xch_free_count", 0), 0)
        self.assertEqual(summary.get("cat_free_count", 0), 0)

    def test_counts_free_coins(self):
        _db.upsert_coin("0xcoin1", "xch", 250_000_000_000,
                        designation="mid", tier="mid")
        _db.upsert_coin("0xcoin2", "xch", 250_000_000_000,
                        designation="mid", tier="mid")
        summary = _db.get_coin_summary()
        self.assertGreaterEqual(summary.get("xch_free_count", 0), 2)


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestLogEventAndGetRecentEvents(_TempDB):
    """log_event and get_recent_events — event log."""

    def test_log_event_stored(self):
        _db.log_event("info", "test_event", "test message")
        events = _db.get_recent_events(limit=10)
        types = [e["event_type"] for e in events]
        self.assertIn("test_event", types)

    def test_severity_filter(self):
        _db.log_event("info", "info_ev", "msg")
        _db.log_event("error", "err_ev", "msg")
        errors = _db.get_recent_events(limit=10, severity="error")
        self.assertTrue(all(e["severity"] == "error" for e in errors))

    def test_limit_respected(self):
        for i in range(20):
            _db.log_event("info", f"ev_{i}", "msg")
        events = _db.get_recent_events(limit=5)
        self.assertLessEqual(len(events), 5)


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestGetSetSetting(_TempDB):
    """get_setting and set_setting — key-value store."""

    def test_get_missing_returns_default(self):
        val = _db.get_setting("nonexistent_key", "default_value")
        self.assertEqual(val, "default_value")

    def test_set_and_get_round_trip(self):
        _db.set_setting("test_key", "test_value")
        val = _db.get_setting("test_key")
        self.assertEqual(val, "test_value")

    def test_update_existing_setting(self):
        _db.set_setting("my_key", "v1")
        _db.set_setting("my_key", "v2")
        val = _db.get_setting("my_key")
        self.assertEqual(val, "v2")

    def test_different_keys_independent(self):
        _db.set_setting("k1", "val1")
        _db.set_setting("k2", "val2")
        self.assertEqual(_db.get_setting("k1"), "val1")
        self.assertEqual(_db.get_setting("k2"), "val2")


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestSplashIncomingMissingTable(_TempDB):
    """Splash helpers tolerate older or narrow test DBs without the table."""

    def setUp(self):
        super().setUp()
        conn = _db.get_connection()
        conn.execute("DROP TABLE splash_incoming_offers")
        conn.commit()

    def test_clear_missing_table_is_quiet_noop(self):
        self.assertEqual(_db.clear_splash_incoming(), 0)

    def test_prune_missing_table_is_quiet_noop(self):
        self.assertEqual(_db.prune_splash_incoming(), 0)

    def test_list_missing_table_returns_empty_list(self):
        self.assertEqual(_db.get_splash_incoming_offers(), [])

    def test_stats_missing_table_returns_zeroes(self):
        stats = _db.get_splash_incoming_stats("assetA")
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["new"], 0)
        self.assertEqual(stats["relevant"], 0)


@unittest.skipIf(_SKIP is not None, f"database unavailable: {_SKIP}")
class TestRecordPrice(_TempDB):
    """record_price and get_recent_prices — price history."""

    def test_record_and_retrieve_price(self):
        _db.record_price("assetA", Decimal("1.05"), Decimal("1.00"), Decimal("1.10"))
        prices = _db.get_recent_prices("assetA", hours=1.0)
        self.assertGreater(len(prices), 0)

    def test_prices_for_different_asset_separated(self):
        _db.record_price("assetA", Decimal("1.05"), Decimal("1.00"), Decimal("1.10"))
        _db.record_price("assetB", Decimal("2.00"), Decimal("1.90"), Decimal("2.10"))
        prices_a = _db.get_recent_prices("assetA", hours=1.0)
        prices_b = _db.get_recent_prices("assetB", hours=1.0)
        combined_prices = [float(p["combined_price"]) for p in prices_a]
        self.assertAlmostEqual(combined_prices[0], 1.05, places=4)
        self.assertNotIn(2.0, [float(p["combined_price"]) for p in prices_a])


if __name__ == "__main__":
    unittest.main()
