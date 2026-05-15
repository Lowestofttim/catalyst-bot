"""Slice 03-10 — sniper arb cycle integration test.

Tests the full flow: arb gap detected → Sniper.try_snipe() fires both-sided
probe offers → offer recorded in DB (add_offer + lock_coin) → prune_active_snipes
removes closed offers.

Uses real SQLite temp DB for add_offer/lock_coin. offer_manager and
risk_manager are mocked (no actual wallet calls).
"""

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import database as _db
    from database import get_open_offers, init_database

    _SKIP_DB = None
except ModuleNotFoundError as exc:
    _db = None
    _SKIP_DB = str(exc)

try:
    import sniper as _sniper_mod
    from sniper import Sniper

    _SKIP_SN = None
except ModuleNotFoundError as exc:
    Sniper = None
    _SKIP_SN = str(exc)


# ---------------------------------------------------------------------------
# Fake config factory
# ---------------------------------------------------------------------------


def _fake_cfg(**overrides):
    defaults = dict(
        LIQUIDITY_MODE="two_sided",
        SNIPER_COOLDOWN_SECS=0,  # no cooldown in tests
        ENABLE_BUY=True,
        ENABLE_SELL=True,
        DRY_RUN=False,
        SNIPER_EXPIRY_SECS=1800,
        COIN_IDS_ENABLED=False,
        CAT_DECIMALS=3,
        WALLET_ID_XCH=1,
        CAT_WALLET_ID=2,
        CAT_ASSET_ID="aabbcc",
        SNIPER_ENABLED=True,
        SNIPER_SIZE_XCH=Decimal("0.01"),
        SNIPER_SCALE_FACTOR=Decimal("0"),  # fixed size
        SNIPER_MIN_SIZE_XCH=Decimal("0.001"),
        SNIPER_MAX_SIZE_XCH=Decimal("1.0"),
        MAX_TRADE_XCH=Decimal("10.0"),
        DEFAULT_TRADE_XCH=Decimal("0.01"),
        DEXIE_AUTO_POST=False,
        INVENTORY_ENABLED=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_trade_counter = [0]


def _fake_offer_manager():
    """Return a minimal mock offer_manager that simulates a successful create."""
    om = MagicMock()

    # Each call gets a unique trade_id to avoid add_offer duplicate rejection
    def _create(*a, **kw):
        _trade_counter[0] += 1
        return {
            "success": True,
            "trade_id": f"snipe-{_trade_counter[0]:04d}",
            "offer": "offer1abc...",
            "locked_coin_id": None,
            "trade_record": {},
        }

    om.create_offer_with_retry.side_effect = _create
    om._cycle_used_coin_ids = set()
    om._offer_details_cache = {}
    return om


def _fake_risk_manager(full_halt=False, blocked_side=None):
    rm = MagicMock()
    rm.is_full_halt.return_value = full_halt
    rm.get_circuit_breaker_blocked_side.return_value = blocked_side
    return rm


# ---------------------------------------------------------------------------
# Temp-DB base class
# ---------------------------------------------------------------------------


class _TempDB(unittest.TestCase):
    def setUp(self):
        sys.modules["database"] = _db

        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._tmp_path = self._tmp.name

        self._orig_db_path = _db.DB_PATH
        _db.DB_PATH = self._tmp_path
        self._orig_init_path = _db._db_initialized_path
        _db._db_initialized_path = ""

        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.init_database()

    def tearDown(self):
        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.DB_PATH = self._orig_db_path
        _db._db_initialized_path = self._orig_init_path
        sys.modules["database"] = _db
        try:
            os.unlink(self._tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 1. Full arb cycle — try_snipe + DB recording
# ---------------------------------------------------------------------------


@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_SN is not None,
    f"dependencies unavailable: db={_SKIP_DB} sn={_SKIP_SN}",
)
class TestSniperArbCycle(_TempDB):
    def _make_sniper(self, **cfg_overrides):
        fake_cfg = _fake_cfg(**cfg_overrides)
        om = _fake_offer_manager()
        rm = _fake_risk_manager()
        sniper = Sniper(offer_manager=om, risk_manager=rm)
        self._cfg_patch = patch.object(_sniper_mod, "cfg", fake_cfg)
        self._cfg_patch.start()
        self.addCleanup(self._cfg_patch.stop)
        return sniper, om

    def test_try_snipe_both_sides_on_arb_gap(self):
        sniper, om = self._make_sniper()
        results = sniper.try_snipe(
            bid_price=Decimal("0.001"),
            ask_price=Decimal("0.0011"),
            arb_gap_bps=Decimal("100"),
        )
        # Both buy and sell created
        self.assertEqual(len(results), 2)
        sides = {r["side"] for r in results}
        self.assertIn("buy", sides)
        self.assertIn("sell", sides)

    def test_snipe_offer_recorded_in_db(self):
        sniper, om = self._make_sniper(ENABLE_SELL=False)  # one side only
        om.create_offer_with_retry.side_effect = None
        om.create_offer_with_retry.return_value = {
            "success": True,
            "trade_id": "snipe-buy-001",
            "offer": "offer_buy...",
            "locked_coin_id": None,
            "trade_record": {},
        }
        sniper.try_snipe(
            bid_price=Decimal("0.001"),
            ask_price=Decimal("0.0011"),
        )
        open_offers = get_open_offers(cat_asset_id="aabbcc")
        trade_ids = [o["trade_id"] for o in open_offers]
        self.assertIn("snipe-buy-001", trade_ids)

    def test_dry_run_does_not_record_to_db(self):
        sniper, om = self._make_sniper(DRY_RUN=True)
        results = sniper.try_snipe(
            bid_price=Decimal("0.001"),
            ask_price=Decimal("0.0011"),
        )
        self.assertEqual(results, [])
        open_offers = get_open_offers(cat_asset_id="aabbcc")
        self.assertEqual(len(open_offers), 0)

    def test_full_halt_blocks_all_snipes(self):
        fake_cfg = _fake_cfg()
        rm = _fake_risk_manager(full_halt=True)
        om = _fake_offer_manager()
        sniper = Sniper(offer_manager=om, risk_manager=rm)
        with patch.object(_sniper_mod, "cfg", fake_cfg):
            results = sniper.try_snipe(
                bid_price=Decimal("0.001"),
                ask_price=Decimal("0.0011"),
            )
        self.assertEqual(results, [])
        om.create_offer_with_retry.assert_not_called()

    def test_cooldown_blocks_immediate_retry(self):
        sniper, om = self._make_sniper(SNIPER_COOLDOWN_SECS=3600)
        # Force last_snipe_time to now — immediate cooldown
        import time

        sniper._last_snipe_time = time.time()
        results = sniper.try_snipe(
            bid_price=Decimal("0.001"),
            ask_price=Decimal("0.0011"),
        )
        self.assertEqual(results, [])

    def test_single_side_mode_buy_only_skips_sell(self):
        sniper, om = self._make_sniper(
            LIQUIDITY_MODE="buy_only",
            ENABLE_SELL=False,
        )
        results = sniper.try_snipe(
            bid_price=Decimal("0.001"),
            ask_price=Decimal("0.0011"),
        )
        # Liquidity mode gate returns [] immediately for non-two_sided
        self.assertEqual(results, [])

    def test_circuit_breaker_blocks_sell_side(self):
        fake_cfg = _fake_cfg()
        rm = _fake_risk_manager(blocked_side="sell")
        om = _fake_offer_manager()
        # unique trade id per call
        call_count = [0]

        def _side_result(*a, **kw):
            call_count[0] += 1
            return {
                "success": True,
                "trade_id": f"snipe-{call_count[0]}",
                "offer": "",
                "locked_coin_id": None,
                "trade_record": {},
            }

        om.create_offer_with_retry.side_effect = _side_result
        sniper = Sniper(offer_manager=om, risk_manager=rm)
        with patch.object(_sniper_mod, "cfg", fake_cfg):
            results = sniper.try_snipe(
                bid_price=Decimal("0.001"),
                ask_price=Decimal("0.0011"),
            )
        # Only buy should be created
        sides = {r["side"] for r in results}
        self.assertNotIn("sell", sides)
        self.assertIn("buy", sides)

    def test_offer_manager_called_with_correct_side_amounts(self):
        sniper, om = self._make_sniper(
            ENABLE_SELL=False,  # buy only to simplify assertion
            SNIPER_SIZE_XCH=Decimal("0.1"),
        )
        sniper.try_snipe(
            bid_price=Decimal("0.001"),
            ask_price=Decimal("0.0011"),
        )
        om.create_offer_with_retry.assert_called_once()
        call_kwargs = om.create_offer_with_retry.call_args
        # First positional arg should be the offer_dict {wallet_id: mojos}
        offer_dict = call_kwargs[0][0]
        # For a buy: XCH is negative (spent), CAT is positive (received)
        self.assertIn("1", offer_dict)  # WALLET_ID_XCH = 1
        xch_mojos = offer_dict["1"]
        self.assertLess(xch_mojos, 0)


# ---------------------------------------------------------------------------
# 2. prune_active_snipes cleanup
# ---------------------------------------------------------------------------


@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_SN is not None,
    f"dependencies unavailable: db={_SKIP_DB} sn={_SKIP_SN}",
)
class TestSniperPruneCycle(_TempDB):
    def _make_sniper_with_active(self, trade_ids):
        fake_cfg = _fake_cfg()
        sniper = Sniper(offer_manager=MagicMock(), risk_manager=_fake_risk_manager())
        with patch.object(_sniper_mod, "cfg", fake_cfg):
            for i, tid in enumerate(trade_ids):
                side = "buy" if i % 2 == 0 else "sell"
                with sniper._snipe_lock:
                    sniper._active_snipe_ids.append(tid)
                    sniper._active_snipe_sides[tid] = side
        return sniper

    def test_prune_removes_closed_offer(self):
        sniper = self._make_sniper_with_active(["tid-a", "tid-b"])
        # tid-a closed (not in open set), tid-b still open
        sniper.prune_active_snipes({"tid-b"})
        self.assertNotIn("tid-a", sniper._active_snipe_ids)
        self.assertIn("tid-b", sniper._active_snipe_ids)

    def test_prune_clears_side_mapping(self):
        sniper = self._make_sniper_with_active(["tid-fill"])
        sniper.prune_active_snipes(set())
        self.assertNotIn("tid-fill", sniper._active_snipe_sides)

    def test_prune_empty_open_set_removes_all(self):
        sniper = self._make_sniper_with_active(["t1", "t2", "t3"])
        sniper.prune_active_snipes(set())
        self.assertEqual(sniper._active_snipe_ids, [])
        self.assertEqual(sniper._active_snipe_sides, {})

    def test_prune_noop_when_all_still_open(self):
        sniper = self._make_sniper_with_active(["t1", "t2"])
        sniper.prune_active_snipes({"t1", "t2", "t3"})
        self.assertEqual(len(sniper._active_snipe_ids), 2)

    def test_after_prune_new_snipe_can_be_created(self):
        fake_cfg = _fake_cfg()
        om = _fake_offer_manager()
        om.create_offer_with_retry.return_value = {
            "success": True,
            "trade_id": "new-snipe",
            "offer": "",
            "locked_coin_id": None,
            "trade_record": {},
        }
        sniper = Sniper(offer_manager=om, risk_manager=_fake_risk_manager())
        # Fill both slots
        with sniper._snipe_lock:
            sniper._active_snipe_ids.extend(["old-buy", "old-sell"])
            sniper._active_snipe_sides["old-buy"] = "buy"
            sniper._active_snipe_sides["old-sell"] = "sell"
        # Prune both → cap freed
        sniper.prune_active_snipes(set())
        # Now try_snipe should be able to create new snipes
        with patch.object(_sniper_mod, "cfg", fake_cfg):
            results = sniper.try_snipe(
                bid_price=Decimal("0.001"),
                ask_price=Decimal("0.0011"),
            )
        self.assertGreater(len(results), 0)


# ---------------------------------------------------------------------------
# 3. Stats tracking across the cycle
# ---------------------------------------------------------------------------


@unittest.skipIf(
    _SKIP_DB is not None or _SKIP_SN is not None,
    f"dependencies unavailable: db={_SKIP_DB} sn={_SKIP_SN}",
)
class TestSniperStats(_TempDB):
    def test_stats_increment_on_successful_snipe(self):
        fake_cfg = _fake_cfg()
        om = _fake_offer_manager()
        sniper = Sniper(offer_manager=om, risk_manager=_fake_risk_manager())
        with patch.object(_sniper_mod, "cfg", fake_cfg):
            sniper.try_snipe(Decimal("0.001"), Decimal("0.0011"))
        stats = sniper.get_stats()
        self.assertEqual(stats["total_snipes"], 1)

    def test_stats_skip_increments_when_circuit_breaker_active(self):
        fake_cfg = _fake_cfg()
        rm = _fake_risk_manager(full_halt=True)
        sniper = Sniper(offer_manager=MagicMock(), risk_manager=rm)
        with patch.object(_sniper_mod, "cfg", fake_cfg):
            sniper.try_snipe(Decimal("0.001"), Decimal("0.0011"))
        stats = sniper.get_stats()
        self.assertGreater(stats["total_skipped"], 0)


if __name__ == "__main__":
    unittest.main()
