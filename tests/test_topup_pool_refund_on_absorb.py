"""Tests for the topup-pool-budget refund path on misfit absorption.

The topup worker tracks a cumulative session spend counter
(`topup_pool_xch_spent_mojos` / `topup_pool_cat_spent_mojos`). Every
successful split increments it; once it hits `TOPUP_POOL_XCH` /
`TOPUP_POOL_CAT` the budget guard refuses further splits.

Before the 2026-04-21 fix, the counter was only ever incremented. When
`_absorb_misfits_to_reserve` folded stranded tier coins back into the
reserve (a reverse operation — coins physically returning to the pool),
nothing credited them back. Over a long session the counter drifted
permanently higher than the real net carve-out, and legitimate tier
refills got refused even while the reserve held plenty of coins.

These tests pin the symmetric refund behaviour that fixes the drift.
"""

import sys
import types
import unittest
from unittest.mock import patch


_INSTALLED_STUBS: list = []

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    dotenv_stub.set_key = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub
    _INSTALLED_STUBS.append("dotenv")

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")

    class _DummyResponse:
        status_code = 200

        def json(self):
            return {"status": "success"}

        def raise_for_status(self):
            return None

    class _StubSession:
        def __init__(self):
            self.headers = {}

        def get(self, *args, **kwargs):
            return _DummyResponse()

        def mount(self, *args, **kwargs):
            pass

    requests_stub.get = lambda *args, **kwargs: _DummyResponse()
    requests_stub.Session = _StubSession
    requests_stub.exceptions = types.SimpleNamespace(
        Timeout=Exception,
        ConnectionError=Exception,
    )
    requests_adapters_stub = types.ModuleType("requests.adapters")
    requests_adapters_stub.HTTPAdapter = object
    requests_stub.adapters = requests_adapters_stub
    sys.modules["requests"] = requests_stub
    sys.modules["requests.adapters"] = requests_adapters_stub
    _INSTALLED_STUBS.extend(["requests", "requests.adapters"])

if "urllib3" not in sys.modules:
    urllib3_stub = types.ModuleType("urllib3")
    urllib3_stub.Retry = object
    urllib3_stub.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
    urllib3_stub.disable_warnings = lambda *args, **kwargs: None
    sys.modules["urllib3"] = urllib3_stub
    _INSTALLED_STUBS.append("urllib3")


import coin_manager


class TopupPoolRefundTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        # Leave coin_manager / config / database loaded: popping them
        # here would force other test classes to re-import and cfg would
        # get re-initialised from .env while bot_health.cfg still points
        # to the original instance — leading to TOPUP_POOL_* values that
        # don't match between modules and spurious drift check failures.
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)

    def _make_manager(self):
        with patch.object(coin_manager.CoinManager, "_resolve_fingerprint",
                          return_value="123456789"):
            return coin_manager.CoinManager()

    # ------------------------------------------------------------------
    # Refund path
    # ------------------------------------------------------------------

    def test_xch_refund_decrements_spent_counter(self):
        """A refund of 15 XCH reduces the XCH spent counter by that amount."""
        manager = self._make_manager()
        storage = {"topup_pool_xch_spent_mojos": str(62_544_900_000_000)}

        def _fake_get(key, default="0"):
            return storage.get(key, default)

        def _fake_set(key, value):
            storage[key] = value

        with patch("database.get_setting", side_effect=_fake_get), \
             patch("database.set_setting", side_effect=_fake_set):
            manager._record_topup_pool_refund(
                is_cat=False,
                amount_mojos=15_069_600_000_000,  # 15.0696 XCH
            )

        self.assertEqual(
            storage["topup_pool_xch_spent_mojos"],
            str(62_544_900_000_000 - 15_069_600_000_000),
        )

    def test_cat_refund_decrements_spent_counter(self):
        """A refund on the CAT side touches the CAT key, not the XCH one."""
        manager = self._make_manager()
        storage = {
            "topup_pool_cat_spent_mojos": str(140_000_000),
            "topup_pool_xch_spent_mojos": str(5_000_000_000_000),
        }

        def _fake_get(key, default="0"):
            return storage.get(key, default)

        def _fake_set(key, value):
            storage[key] = value

        with patch("database.get_setting", side_effect=_fake_get), \
             patch("database.set_setting", side_effect=_fake_set):
            manager._record_topup_pool_refund(
                is_cat=True,
                amount_mojos=30_000_000,
            )

        self.assertEqual(storage["topup_pool_cat_spent_mojos"],
                         str(140_000_000 - 30_000_000))
        # XCH counter must be untouched.
        self.assertEqual(storage["topup_pool_xch_spent_mojos"],
                         str(5_000_000_000_000))

    def test_refund_clamped_at_zero(self):
        """An over-refund must not push the counter negative."""
        manager = self._make_manager()
        storage = {"topup_pool_xch_spent_mojos": str(10_000_000_000_000)}

        def _fake_get(key, default="0"):
            return storage.get(key, default)

        def _fake_set(key, value):
            storage[key] = value

        with patch("database.get_setting", side_effect=_fake_get), \
             patch("database.set_setting", side_effect=_fake_set):
            manager._record_topup_pool_refund(
                is_cat=False,
                amount_mojos=999_999_999_999_999,  # way more than the counter
            )

        self.assertEqual(storage["topup_pool_xch_spent_mojos"], "0")

    def test_refund_of_zero_is_noop(self):
        """A zero or negative refund must not touch storage."""
        manager = self._make_manager()
        touched = {"flag": False}

        def _fake_set(key, value):
            touched["flag"] = True

        with patch("database.get_setting", return_value="0"), \
             patch("database.set_setting", side_effect=_fake_set):
            manager._record_topup_pool_refund(is_cat=False, amount_mojos=0)
            manager._record_topup_pool_refund(is_cat=False, amount_mojos=-100)

        self.assertFalse(touched["flag"])

    # ------------------------------------------------------------------
    # Round-trip invariant: spend then refund cancels out
    # ------------------------------------------------------------------

    def test_spend_then_refund_round_trip(self):
        """spend(N) then refund(N) leaves the counter where it started."""
        manager = self._make_manager()
        storage = {"topup_pool_xch_spent_mojos": "0"}

        def _fake_get(key, default="0"):
            return storage.get(key, default)

        def _fake_set(key, value):
            storage[key] = value

        with patch("database.get_setting", side_effect=_fake_get), \
             patch("database.set_setting", side_effect=_fake_set):
            manager._record_topup_pool_spend(
                is_cat=False, amount_mojos=2_650_230_000_000)  # 2.6502 XCH
            self.assertEqual(storage["topup_pool_xch_spent_mojos"],
                             str(2_650_230_000_000))

            manager._record_topup_pool_refund(
                is_cat=False, amount_mojos=2_650_230_000_000)
            self.assertEqual(storage["topup_pool_xch_spent_mojos"], "0")

    def test_pool_rebuild_refunds_spent_counter(self):
        """Excess tier coins consolidated back into reserve credit the pool."""
        manager = self._make_manager()
        storage = {"topup_pool_xch_spent_mojos": "500"}
        inventory = {
            "reserve": [],
            "small": [],
            "inner": [
                {"coin_id": "0xa", "coin": {"amount": 100}},
                {"coin_id": "0xb", "coin": {"amount": 100}},
                {"coin_id": "0xc", "coin": {"amount": 100}},
                {"coin_id": "0xd", "coin": {"amount": 100}},
            ],
            "mid": [],
            "outer": [],
            "extreme": [],
        }

        def _fake_get(key, default="0"):
            return storage.get(key, default)

        def _fake_set(key, value):
            storage[key] = value

        with patch("database.get_setting", side_effect=_fake_get), \
             patch("database.set_setting", side_effect=_fake_set), \
             patch.object(manager, "_configured_tier_sizes_xch",
                          return_value={"inner": 1}), \
             patch.object(coin_manager, "get_weighted_tier_prep_counts",
                          return_value={"inner": 1}), \
             patch.object(manager, "_consolidate_coins", return_value=True), \
             patch.object(coin_manager, "log_event"):
            result = manager._smart_topup_wallet(
                "XCH-inner",
                wallet_id=1,
                inventory=inventory,
                trading_size_mojos=100,
                needed=1,
                is_cat=False,
            )

        self.assertEqual(result, "rebuild")
        # Four inner coins, target/floor=1, so three excess coins (300 mojos)
        # were consolidated back into the topup pool.
        self.assertEqual(storage["topup_pool_xch_spent_mojos"], "200")


if __name__ == "__main__":
    unittest.main()
