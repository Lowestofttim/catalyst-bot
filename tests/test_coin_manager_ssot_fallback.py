"""Test that the DB-unavailable fallback in _classify_coins_by_designation
uses SSOT coin_classifier bounds (0.98/1.5), not the old legacy ±20% bounds.

Regression test for the 2026-04-17 ladder bug:
    Inner tier = 26,700,000 mojos
    A coin of 23,400,000 mojos = 0.876× inner size
    Old ±20% bounds → classified as inner (BUG — built misfit offer)
    SSOT bounds      → classified as misfit → goes to small (CORRECT)
"""
import sys
import types
import unittest
from unittest.mock import patch

# ---------------------------------------------------------------
# Minimal stubs so coin_manager can import cleanly in a test harness
# ---------------------------------------------------------------
if "dotenv" not in sys.modules:
    _dotenv = types.ModuleType("dotenv")
    _dotenv.load_dotenv = lambda *a, **kw: None
    _dotenv.set_key = lambda *a, **kw: None
    sys.modules["dotenv"] = _dotenv

if "requests" not in sys.modules:
    _requests = types.ModuleType("requests")

    class _Resp:
        status_code = 200
        def json(self): return {}
        def raise_for_status(self): pass

    class _Session:
        headers = {}
        def get(self, *a, **kw): return _Resp()
        def mount(self, *a, **kw): pass

    _requests.get = lambda *a, **kw: _Resp()
    _requests.Session = _Session
    _requests.exceptions = types.SimpleNamespace(Timeout=Exception, ConnectionError=Exception)
    _adapters = types.ModuleType("requests.adapters")
    _adapters.HTTPAdapter = object
    _requests.adapters = _adapters
    sys.modules["requests"] = _requests
    sys.modules["requests.adapters"] = _adapters

if "urllib3" not in sys.modules:
    _urllib3 = types.ModuleType("urllib3")
    _urllib3.Retry = object
    _urllib3.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
    _urllib3.disable_warnings = lambda *a, **kw: None
    sys.modules["urllib3"] = _urllib3

import coin_manager


def _rec(coin_id: str, amount: int) -> dict:
    return {"coin_id": coin_id, "coin": {"amount": amount}}


# Inner tier size used across all tests
_INNER_MOJOS = 26_700_000
_TIER_SIZES = {
    "inner": _INNER_MOJOS,
    "mid": 13_350_000,
    "outer": 6_675_000,
    "extreme": 3_337_500,
}


class SSOTFallbackTests(unittest.TestCase):

    def _make_manager(self):
        with patch.object(coin_manager.CoinManager, "_resolve_fingerprint", return_value="123"):
            return coin_manager.CoinManager()

    def _classify(self, manager, records, *, db_raises=False):
        # The function imports from database inside the body, so we patch
        # the database module directly.
        db_mod = sys.modules.get("database") or types.ModuleType("database")

        if db_raises:
            db_mod.get_free_coins = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down"))
            db_mod.get_locked_coins = lambda *a, **kw: []
            db_mod.set_coin_designation = lambda *a, **kw: None
            db_mod.get_tier_spare_counts = lambda *a, **kw: {}
            db_mod.log_event = lambda *a, **kw: None
        else:
            db_mod.get_free_coins = lambda *a, **kw: []
            db_mod.get_locked_coins = lambda *a, **kw: []
            db_mod.set_coin_designation = lambda *a, **kw: None
            db_mod.get_tier_spare_counts = lambda *a, **kw: {}
            db_mod.log_event = lambda *a, **kw: None
        sys.modules["database"] = db_mod

        with patch.object(coin_manager, "log_event", lambda *a, **kw: None):
            return manager._classify_coins_by_designation(records, "xch", _TIER_SIZES)

    # ------------------------------------------------------------------
    # DB fallback — SSOT bounds must hold
    # ------------------------------------------------------------------

    def test_misfit_coin_goes_to_small_in_ssot_fallback(self):
        """A coin at 87.6% of inner size must land in 'small' (misfit),
        not 'inner'. Old ±20% bounds would pass it as inner — SSOT rejects it."""
        mgr = self._make_manager()
        misfit_mojos = 23_400_000  # 0.876 × inner — below 0.98 SSOT floor
        records = [_rec("0xmisfit", misfit_mojos)]

        result = self._classify(mgr, records, db_raises=True)

        self.assertEqual(result["inner"], [], "misfit coin must NOT land in inner")
        self.assertEqual(len(result["small"]), 1, "misfit coin must land in small")
        self.assertEqual(result["small"][0]["coin_id"], "0xmisfit")

    def test_good_inner_coin_goes_to_inner_in_ssot_fallback(self):
        """A properly-sized inner coin must still land in 'inner'."""
        mgr = self._make_manager()
        good_mojos = _INNER_MOJOS  # exact match
        records = [_rec("0xgood", good_mojos)]

        result = self._classify(mgr, records, db_raises=True)

        self.assertEqual(len(result["inner"]), 1)
        self.assertEqual(result["inner"][0]["coin_id"], "0xgood")
        self.assertEqual(result["small"], [])

    def test_reserve_coin_goes_to_reserve_in_ssot_fallback(self):
        """A large coin (> 1.5× largest tier) must land in 'reserve'."""
        mgr = self._make_manager()
        reserve_mojos = _INNER_MOJOS * 4  # well above 1.5× inner
        records = [_rec("0xreserve", reserve_mojos)]

        result = self._classify(mgr, records, db_raises=True)

        self.assertEqual(len(result["reserve"]), 1)
        self.assertEqual(result["inner"], [])

    def test_old_20pct_bounds_would_have_accepted_misfit(self):
        """Confirm the old bounds would have accepted the misfit coin, so we
        know the test is checking the right thing (not a vacuous pass)."""
        mgr = self._make_manager()
        misfit_mojos = 23_400_000
        tier_sizes = _TIER_SIZES.copy()

        # Manually invoke _classify_coins_tiered (the legacy fallback) and
        # confirm it does classify the misfit as inner — proving the fix matters.
        result_legacy = coin_manager._classify_coins_tiered(
            [_rec("0xmisfit", misfit_mojos)], tier_sizes
        )
        # Legacy ±20% bounds: 26.7M * 0.8 = 21.36M <= 23.4M → accepted as inner
        self.assertEqual(
            len(result_legacy["inner"]), 1,
            "legacy classifier should accept misfit (this confirms the fix matters)"
        )

    # ------------------------------------------------------------------
    # Normal DB path — SSOT still used for new/unknown coins
    # ------------------------------------------------------------------

    def test_misfit_coin_stays_in_small_with_normal_db_path(self):
        """Even with a working DB, a new coin at 87.6% of inner size must
        end up in small after _infer_designation_by_size returns unknown."""
        mgr = self._make_manager()
        misfit_mojos = 23_400_000
        records = [_rec("0xmisfit2", misfit_mojos)]

        # DB has no designation for this coin → new-coin path →
        # _infer_designation_by_size → SSOT → unknown/none → small
        result = self._classify(mgr, records, db_raises=False)

        self.assertEqual(result["inner"], [], "misfit must not land in inner even in DB path")
        self.assertGreaterEqual(
            len(result["small"]), 1,
            "misfit must land in small via new-coin SSOT path"
        )
