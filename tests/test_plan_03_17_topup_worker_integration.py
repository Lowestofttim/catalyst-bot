"""Slice 03-17 — topup worker integration test.

Tests CoinManager.needs_topup() trigger conditions and
_record_topup_pool_spend() DB persistence — no wallet calls required.

Key isolation technique:
  - _TempDB base for SQLite isolation (database module pinned via sys.modules)
  - patch.object(coin_manager, "cfg", fake_cfg) to supply predictable settings
  - cfg.WALLET_FINGERPRINT is a digit string so _resolve_fingerprint() returns
    early without an RPC call
  - TIER_ENABLED=False to exercise the simpler non-tiered path in needs_topup()
"""

import os
import sys
import tempfile
import time
import types
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import database as _db
    import coin_manager as _cm_mod
    from coin_manager import CoinManager

    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    CoinManager = None
    _SKIP = str(exc)


# ---------------------------------------------------------------------------
# Fake cfg — just enough for non-tiered needs_topup() path
# ---------------------------------------------------------------------------


def _fake_cfg():
    return types.SimpleNamespace(
        # Lets _resolve_fingerprint() return early without RPC
        WALLET_FINGERPRINT="12345678",
        WALLET_TYPE="sage",
        # needs_topup() non-tiered path fields
        TIER_ENABLED=False,
        ENABLE_BUY=True,
        ENABLE_SELL=True,
        MAX_ACTIVE_BUY_OFFERS=10,
        MAX_ACTIVE_SELL_OFFERS=10,
        COIN_PREP_MULTIPLIER=Decimal("1.0"),
        # Pace scaling off → stable threshold in tests
        TIER_TRIGGER_PACE_SCALE=False,
        TIER_TRIGGER_PCT_INNER=50,  # spare_keep_pct = 0.5 for non-tiered
        # Sniper / fee pool disabled (not relevant for non-tiered path)
        SNIPER_ENABLED=False,
        SNIPER_PREP_COUNT=0,
        FEE_PREP_COUNT=0,
        FEE_COIN_SIZE_XCH=Decimal("0.0"),
        TRANSACTION_FEE_MODE="none",
    )


# ---------------------------------------------------------------------------
# Base — temp SQLite DB + patched cfg
# ---------------------------------------------------------------------------


class _TempDB(unittest.TestCase):
    def setUp(self):
        sys.modules["database"] = _db
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        _db.DB_PATH = self._db_path
        if hasattr(_db._local, "conn"):
            _db._local.conn = None
        _db.init_database()

    def tearDown(self):
        sys.modules["database"] = _db
        if hasattr(_db._local, "conn"):
            _db._local.conn = None
        try:
            os.unlink(self._db_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 1. needs_topup() gate conditions — flag-based
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestNeedsTopupGates(_TempDB):
    def _make_cm(self):
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            return CoinManager()

    def test_returns_false_when_topup_running(self):
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            cm._topup_running = True
            self.assertFalse(cm.needs_topup())

    def test_returns_false_when_prep_running(self):
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            cm._prep_running = True
            self.assertFalse(cm.needs_topup())

    def test_returns_false_when_both_cooldowns_active(self):
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            now = time.time()
            cm._last_topup_time = now  # emergency not ready
            cm._last_drip_time = now  # drip not ready
            self.assertFalse(cm.needs_topup())

    def test_drip_timer_overrides_emergency_cooldown(self):
        """If emergency cooldown is active but drip is ready, still evaluates."""
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            cm._last_topup_time = time.time()  # emergency blocked
            cm._last_drip_time = 0  # drip ready (long ago)
            cm._xch_coins = 0  # would trigger low-coins
            # drip path passes cooldown gate — function proceeds to threshold check
            # result can be True or False depending on thresholds, but must not raise
            result = cm.needs_topup()
            self.assertIsInstance(result, bool)

    def test_emergency_ready_when_last_topup_is_zero(self):
        """_last_topup_time=0 means emergency cooldown expired (since epoch)."""
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            cm._last_topup_time = 0
            cm._last_drip_time = 0
            # Just ensure gate passes (no exception); result is threshold-dependent
            result = cm.needs_topup()
            self.assertIsInstance(result, bool)


# ---------------------------------------------------------------------------
# 2. needs_topup() threshold logic — non-tiered path
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestNeedsTopupThreshold(_TempDB):
    def test_returns_true_when_xch_coins_zero(self):
        """0 free XCH is below threshold (max(3, 10*0.5)=5) → needs topup."""
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            cm._xch_coins = 0
            cm._cat_coins = 99  # cat side healthy
            cm._last_topup_time = 0
            cm._last_drip_time = 0
            self.assertTrue(cm.needs_topup())

    def test_returns_true_when_cat_coins_zero(self):
        """0 free CAT is below threshold → needs topup."""
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            cm._xch_coins = 99  # xch side healthy
            cm._cat_coins = 0
            cm._last_topup_time = 0
            cm._last_drip_time = 0
            self.assertTrue(cm.needs_topup())

    def test_returns_false_when_coins_above_threshold(self):
        """Ample free coins → no topup needed."""
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            # threshold = max(3, int(10 * 1.0 * 0.5)) = max(3,5) = 5
            # Set coins well above threshold
            cm._xch_coins = 20
            cm._cat_coins = 20
            cm._last_topup_time = 0
            cm._last_drip_time = 0
            self.assertFalse(cm.needs_topup())

    def test_buy_only_mode_ignores_cat_shortage(self):
        """With ENABLE_SELL=False, low CAT coins alone don't trigger topup."""
        cfg = _fake_cfg()
        cfg.ENABLE_SELL = False
        cfg.ENABLE_BUY = True
        with patch.object(_cm_mod, "cfg", cfg):
            cm = CoinManager()
            cm._xch_coins = 20  # xch healthy
            cm._cat_coins = 0  # cat low but sell disabled
            cm._last_topup_time = 0
            cm._last_drip_time = 0
            self.assertFalse(cm.needs_topup())

    def test_sell_only_mode_ignores_xch_shortage(self):
        """With ENABLE_BUY=False, low XCH coins alone don't trigger topup."""
        cfg = _fake_cfg()
        cfg.ENABLE_BUY = False
        cfg.ENABLE_SELL = True
        with patch.object(_cm_mod, "cfg", cfg):
            cm = CoinManager()
            cm._xch_coins = 0  # xch low but buy disabled
            cm._cat_coins = 20  # cat healthy
            cm._last_topup_time = 0
            cm._last_drip_time = 0
            self.assertFalse(cm.needs_topup())

    def test_threshold_scales_with_max_offers(self):
        """Higher MAX_ACTIVE_BUY_OFFERS raises the trigger threshold."""
        cfg_low = _fake_cfg()
        cfg_low.MAX_ACTIVE_BUY_OFFERS = 6  # threshold = max(3, int(6*0.5)) = 3
        cfg_high = _fake_cfg()
        cfg_high.MAX_ACTIVE_BUY_OFFERS = 20  # threshold = max(3, int(20*0.5)) = 10

        with patch.object(_cm_mod, "cfg", cfg_low):
            cm_low = CoinManager()
            cm_low._xch_coins = 5
            cm_low._cat_coins = 20
            cm_low._last_topup_time = 0
            cm_low._last_drip_time = 0
            low_result = cm_low.needs_topup()

        with patch.object(_cm_mod, "cfg", cfg_high):
            cm_high = CoinManager()
            cm_high._xch_coins = 5
            cm_high._cat_coins = 20
            cm_high._last_topup_time = 0
            cm_high._last_drip_time = 0
            high_result = cm_high.needs_topup()

        # 5 coins: above threshold-3 (low config) but below threshold-10 (high config)
        self.assertFalse(low_result)  # 5 >= 3 → no topup needed
        self.assertTrue(high_result)  # 5 < 10 → topup needed


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestTieredDripTopupSources(_TempDB):
    def test_optional_source_ignores_reserve_smaller_than_requested_tier(self):
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            cm._cat_inventory = {
                "reserve": [{"amount": 23_950}],
                "small": [],
            }

            self.assertFalse(cm._optional_topup_source_available("cat", 24_129_000))

    def test_trading_tier_drip_waits_when_low_tier_has_no_split_source(self):
        cfg = _fake_cfg()
        cfg.TIER_ENABLED = True
        cfg.TIER_DRIP_PCT = 100

        dist = {"inner": 0, "mid": 5, "outer": 0, "extreme": 0}
        prepared = {"inner": 0, "mid": 10, "outer": 0, "extreme": 0}

        with (
            patch.object(_cm_mod, "cfg", cfg),
            patch.object(_cm_mod, "get_tier_distribution", return_value=dist),
            patch.object(
                _cm_mod, "get_weighted_tier_prep_counts", return_value=prepared
            ),
            patch.object(_cm_mod, "log_event") as log_event_mock,
        ):
            cm = CoinManager()
            cm._last_topup_time = time.time()
            cm._last_drip_time = 0
            cm._tier_spares = {
                "xch": {"inner": 0, "mid": 10, "outer": 0, "extreme": 0},
                "cat": {"inner": 0, "mid": 1, "outer": 0, "extreme": 0},
            }
            with (
                patch.object(
                    cm, "_get_tier_sizes_mojos", return_value={"mid": 24_129_000}
                ),
                patch.object(
                    cm, "_optional_topup_source_available", return_value=False
                ) as source_mock,
            ):
                result = cm.needs_topup(active_buy_count=4, active_sell_count=4)

        self.assertFalse(result)
        source_mock.assert_called_once_with("cat", 24_129_000)
        self.assertTrue(
            any(
                call.args[1] == "drip_source_unavailable"
                for call in log_event_mock.call_args_list
            )
        )


# ---------------------------------------------------------------------------
# 3. _record_topup_pool_spend() — DB persistence
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestTopupPoolSpendPersistence(_TempDB):
    def _make_cm(self):
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            return CoinManager()

    def test_noop_for_zero_amount(self):
        """Zero or negative amounts are silently ignored."""
        cm = self._make_cm()
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm._record_topup_pool_spend(is_cat=False, amount_mojos=0)
        # DB key should not exist (or be "0")
        val = _db.get_setting("topup_pool_xch_spent_mojos", "0")
        self.assertEqual(str(val or "0"), "0")

    def test_records_xch_spend(self):
        cm = self._make_cm()
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm._record_topup_pool_spend(is_cat=False, amount_mojos=1_000_000_000)
        val = int(_db.get_setting("topup_pool_xch_spent_mojos", "0") or "0")
        self.assertEqual(val, 1_000_000_000)

    def test_records_cat_spend(self):
        cm = self._make_cm()
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm._record_topup_pool_spend(is_cat=True, amount_mojos=500_000)
        val = int(_db.get_setting("topup_pool_cat_spent_mojos", "0") or "0")
        self.assertEqual(val, 500_000)

    def test_accumulates_across_calls(self):
        """Each call adds to the previous value — idempotent across restarts."""
        cm = self._make_cm()
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm._record_topup_pool_spend(is_cat=False, amount_mojos=1_000)
            cm._record_topup_pool_spend(is_cat=False, amount_mojos=2_000)
            cm._record_topup_pool_spend(is_cat=False, amount_mojos=3_000)
        val = int(_db.get_setting("topup_pool_xch_spent_mojos", "0") or "0")
        self.assertEqual(val, 6_000)

    def test_xch_and_cat_counters_are_independent(self):
        cm = self._make_cm()
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm._record_topup_pool_spend(is_cat=False, amount_mojos=100)
            cm._record_topup_pool_spend(is_cat=True, amount_mojos=200)
        xch_val = int(_db.get_setting("topup_pool_xch_spent_mojos", "0") or "0")
        cat_val = int(_db.get_setting("topup_pool_cat_spent_mojos", "0") or "0")
        self.assertEqual(xch_val, 100)
        self.assertEqual(cat_val, 200)


# ---------------------------------------------------------------------------
# 4. Cooldown backoff state
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"coin_manager unavailable: {_SKIP}")
class TestTopupCooldownState(_TempDB):
    def test_no_coins_backoff_flag_not_set_initially(self):
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
        self.assertFalse(cm._no_coins_backoff)
        self.assertEqual(cm._no_coins_backoff_count, 0)

    def test_last_topup_time_initialized_to_zero(self):
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
        self.assertEqual(cm._last_topup_time, 0)
        self.assertEqual(cm._last_drip_time, 0)

    def test_topup_running_false_initially(self):
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
        self.assertFalse(cm._topup_running)
        self.assertFalse(cm._prep_running)

    def test_budget_backoff_blocks_low_coins_even_when_drip_ready(self):
        """A spent topup budget should not retry every bot cycle via drip readiness."""
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            cm._xch_coins = 0
            cm._cat_coins = 20
            cm._last_topup_time = 0
            cm._last_drip_time = 0
            cm._topup_budget_backoff_until = time.time() + 600
            cm._topup_budget_backoff_probe = {
                "name": "XCH-extreme",
                "is_cat": False,
                "trading_size_mojos": 1_000,
            }

            with patch.object(cm, "_max_coins_within_topup_budget", return_value=0):
                self.assertFalse(cm.needs_topup())

    def test_budget_backoff_clears_when_headroom_returns(self):
        """Re-running Smart Settings resets spend; the next status check should notice."""
        with patch.object(_cm_mod, "cfg", _fake_cfg()):
            cm = CoinManager()
            cm._xch_coins = 0
            cm._cat_coins = 20
            cm._last_topup_time = 0
            cm._last_drip_time = 0
            cm._topup_budget_backoff_until = time.time() + 600
            cm._topup_budget_backoff_count = 1
            cm._topup_budget_backoff_probe = {
                "name": "XCH-extreme",
                "is_cat": False,
                "trading_size_mojos": 1_000,
            }

            with patch.object(cm, "_max_coins_within_topup_budget", return_value=1):
                self.assertTrue(cm.needs_topup())
            self.assertEqual(cm._topup_budget_backoff_until, 0)
            self.assertEqual(cm._topup_budget_backoff_count, 0)


if __name__ == "__main__":
    unittest.main()
