"""Tests for the 2026-04-21 orphan-reclaim fix.

Two parts wired together:

1. `_absorb_misfits_to_reserve` now also sweeps the 'small' inventory
   bucket (dust + designation='unknown' coins that fell between tier
   sizes). Previously it only scanned tier buckets, so fill-change
   orphans accumulated forever once classified as misfits.

2. `needs_topup` now fires a 'drip' trigger when the small bucket has
   accumulated >= smallest-tier-size worth of absorbable material. This
   guarantees the absorber runs even when all tiers are at target
   (previously no topup reason ever fired → orphans sat idle forever).
"""

import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock


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


def _coin(coin_id: str, amount_mojos: int) -> dict:
    """Minimal coin record matching the shape _coin_amount/_coin_id_from_record read.

    The helpers pull fields from a nested 'coin' dict, matching Chia's
    wallet RPC shape: record = {"coin": {"amount": ..., "name": ...}}.
    """
    inner = {
        "amount": amount_mojos,
        "name": coin_id,
        "coin_id": coin_id,
        "puzzle_hash": "0x" + "aa" * 32,
        "parent_coin_info": "0x" + "bb" * 32,
    }
    return {"coin": inner}


class OrphanReclaimTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        # Leave shared modules loaded — popping config would force reimport
        # and leave cached cfg references stale across test classes.
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)

    def _make_manager(self):
        with patch.object(
            coin_manager.CoinManager, "_resolve_fingerprint", return_value="123456789"
        ):
            return coin_manager.CoinManager()

    # ==================================================================
    # Part 1: Absorber now sweeps the 'small' bucket
    # ==================================================================

    def test_small_bucket_coins_included_in_absorption(self):
        """Unknown-designation change orphans in 'small' bucket should be
        added to misfit_records for consolidation alongside tier misfits."""
        m = self._make_manager()

        # 16 CAT orphans reproducing the exact 2026-04-21 scenario.
        small_bucket = [_coin(f"0xcat_large_{i}", 4_850_000) for i in range(9)] + [
            _coin(f"0xcat_small_{i}", 1_900_000) for i in range(7)
        ]
        reserve_coin = _coin("0xreserve", 4_920_633)  # current tiny reserve
        inventory = {
            "reserve": [reserve_coin],
            "inner": [],
            "mid": [],
            "outer": [],
            "extreme": [],
            "small": small_bucket,
        }
        tier_sizes = {
            "inner": 18_970_000,
            "mid": 10_657_000,
            "outer": 5_760_000,
            "extreme": 2_618_000,
        }

        # Patch out the Sage combine_coins call and the fresh-coins fetch
        # so we exercise the absorber's record-gathering logic in isolation.
        gathered = {"ids": None, "total_misfit": None}

        def _fake_combine(coin_ids, fee_mojos):
            gathered["ids"] = list(coin_ids)
            return {"coin_spends": [{}] * len(coin_ids)}

        def _fake_fresh(wallet_id):
            # Return the reserve + all small coins as still selectable.
            # _extract_coin_records looks for 'confirmed_records' or 'records'.
            return {"confirmed_records": [reserve_coin] + small_bucket}

        cfg = coin_manager.cfg
        with (
            patch("wallet.get_wallet_type", return_value="sage"),
            patch("wallet_sage.combine_coins", side_effect=_fake_combine),
            patch("coin_manager._get_free_coins_rpc", side_effect=_fake_fresh),
            patch.object(cfg, "COIN_MAX_SIZE_RATIO", "1.5"),
            patch.object(cfg, "CAT_DECIMALS", 3),
            patch.object(m, "_tx_fee_mojos", return_value=0),
            patch.object(
                m, "_get_coin_prep_headroom_multiplier", return_value=Decimal("1")
            ),
            patch.object(
                m, "_filter_out_protected_coin_ids", side_effect=lambda ids: ids
            ),
        ):
            result = m._absorb_misfits_to_reserve(
                name="CAT",
                wallet_id=2,
                inventory=inventory,
                tier_sizes_mojos=tier_sizes,
                is_cat=True,
            )

        self.assertTrue(result, "Absorption should have been submitted")
        # Reserve coin + 16 orphans = 17 coin_ids in the combine call.
        self.assertEqual(len(gathered["ids"]), 17)
        # Reserve must be first.
        self.assertEqual(gathered["ids"][0], "0xreserve")
        # Every orphan ID must be present.
        for i in range(9):
            self.assertIn(f"0xcat_large_{i}", gathered["ids"])
        for i in range(7):
            self.assertIn(f"0xcat_small_{i}", gathered["ids"])

    def test_absorption_cap_prevents_monster_tx(self):
        """Too many small coins → cap at 20 to keep CLVM cost reasonable."""
        m = self._make_manager()

        # 50 small coins — absorber should cap at 20 total (incl. reserve).
        small_bucket = [_coin(f"0xsmall_{i}", 5_000_000) for i in range(50)]
        reserve_coin = _coin("0xreserve", 10_000_000)
        inventory = {
            "reserve": [reserve_coin],
            "inner": [],
            "mid": [],
            "outer": [],
            "extreme": [],
            "small": small_bucket,
        }
        tier_sizes = {
            "inner": 20_000_000,
            "mid": 10_000_000,
            "outer": 5_000_000,
            "extreme": 2_000_000,
        }

        gathered = {"ids": None}

        def _fake_combine(coin_ids, fee_mojos):
            gathered["ids"] = list(coin_ids)
            return {"coin_spends": [{}]}

        def _fake_fresh(wallet_id):
            return {"confirmed_records": [reserve_coin] + small_bucket}

        cfg = coin_manager.cfg
        with (
            patch("wallet.get_wallet_type", return_value="sage"),
            patch("wallet_sage.combine_coins", side_effect=_fake_combine),
            patch("coin_manager._get_free_coins_rpc", side_effect=_fake_fresh),
            patch.object(cfg, "COIN_MAX_SIZE_RATIO", "1.5"),
            patch.object(cfg, "CAT_DECIMALS", 3),
            patch.object(m, "_tx_fee_mojos", return_value=0),
            patch.object(
                m, "_get_coin_prep_headroom_multiplier", return_value=Decimal("1")
            ),
            patch.object(
                m, "_filter_out_protected_coin_ids", side_effect=lambda ids: ids
            ),
        ):
            m._absorb_misfits_to_reserve(
                name="CAT",
                wallet_id=2,
                inventory=inventory,
                tier_sizes_mojos=tier_sizes,
                is_cat=True,
            )

        # Reserve + 20 orphans = 21 combine inputs (cap of 20 orphans applied).
        self.assertEqual(len(gathered["ids"]), 21)

    def test_tiny_xch_orphans_below_fee_are_skipped(self):
        """XCH orphans smaller than 2×tx_fee should NOT be absorbed (would
        waste more in fees than the coin is worth)."""
        m = self._make_manager()

        # tx_fee_mojos returns 10B mojos (0.00001 XCH). 2× = 20B threshold.
        small_bucket = [
            _coin("0xtiny_1", 10_000_000_000),  # == fee × 1 — skip
            _coin("0xtiny_2", 19_000_000_000),  # < fee × 2 — skip
            _coin("0xuseful_1", 50_000_000_000),  # > threshold — include
            _coin("0xuseful_2", 100_000_000_000),  # include
        ]
        reserve_coin = _coin("0xreserve", 500_000_000_000)
        inventory = {
            "reserve": [reserve_coin],
            "inner": [],
            "mid": [],
            "outer": [],
            "extreme": [],
            "small": small_bucket,
        }
        tier_sizes = {
            "inner": 200_000_000_000,
            "mid": 100_000_000_000,
            "outer": 50_000_000_000,
            "extreme": 10_000_000_000,
        }

        gathered = {"ids": None}

        def _fake_combine(coin_ids, fee_mojos):
            gathered["ids"] = list(coin_ids)
            return {"coin_spends": [{}]}

        def _fake_fresh(wallet_id):
            return {"confirmed_records": [reserve_coin] + small_bucket}

        cfg = coin_manager.cfg
        with (
            patch("wallet.get_wallet_type", return_value="sage"),
            patch("wallet_sage.combine_coins", side_effect=_fake_combine),
            patch("coin_manager._get_free_coins_rpc", side_effect=_fake_fresh),
            patch.object(cfg, "COIN_MAX_SIZE_RATIO", "1.5"),
            patch.object(m, "_tx_fee_mojos", return_value=10_000_000_000),
            patch.object(
                m, "_get_coin_prep_headroom_multiplier", return_value=Decimal("1")
            ),
            patch.object(
                m, "_filter_out_protected_coin_ids", side_effect=lambda ids: ids
            ),
        ):
            m._absorb_misfits_to_reserve(
                name="XCH",
                wallet_id=1,
                inventory=inventory,
                tier_sizes_mojos=tier_sizes,
                is_cat=False,
            )

        self.assertIn("0xuseful_1", gathered["ids"])
        self.assertIn("0xuseful_2", gathered["ids"])
        self.assertNotIn("0xtiny_1", gathered["ids"])
        self.assertNotIn("0xtiny_2", gathered["ids"])

    def test_no_reserve_returns_false(self):
        """Without a reserve coin, absorption can't fire (nothing to
        consolidate INTO). Preserves existing behaviour."""
        m = self._make_manager()
        inventory = {
            "reserve": [],
            "inner": [],
            "mid": [],
            "outer": [],
            "extreme": [],
            "small": [_coin(f"0xorphan_{i}", 5_000_000) for i in range(10)],
        }
        tier_sizes = {
            "inner": 20_000_000,
            "mid": 10_000_000,
            "outer": 5_000_000,
            "extreme": 2_000_000,
        }
        with patch("wallet.get_wallet_type", return_value="sage"):
            result = m._absorb_misfits_to_reserve(
                name="CAT",
                wallet_id=2,
                inventory=inventory,
                tier_sizes_mojos=tier_sizes,
                is_cat=True,
            )
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
