"""Unit tests for dexie_incentives + dexie_claims.

Pure-Python tests — no network, no wallet RPC. The HTTP layer is patched
to return fixtures captured from Dexie's live `/v1/incentives` payload.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "catalyst"))

import dexie_incentives  # noqa: E402
from dexie_claims import _b58encode, compute_offer_hash, puzzle_hash_to_address  # noqa: E402


SBX_ASSET_ID = "a628c1c2c6fcb74d53746157e438e108eab5c0bb3e5c80ff9b1910b3e4832913"
WUSDC_ASSET_ID = "fa4a180ac326e67ea289b869e3448256f6af05721f7cf934cb9901baa6b7a99d"

# Trimmed fixture matching the real /v1/incentives payload shape.
_FIXTURE = {
    "success": True,
    "incentives": [
        {
            "offered": {"id": "xch", "code": "XCH"},
            "requested": {"id": SBX_ASSET_ID, "code": "SBX"},
            "range": {"id": "xch", "code": "XCH", "min": 0.1, "max": 20},
            "rewardRate": {"id": "dbxid", "code": "DBX", "amount": 100},
            "maxSpread": 0.05,
            "withinSpread": 356.39,
            "estimatedAPR": 0.667,
            "marketPrice": 11000.0,
        },
        {
            "offered": {"id": SBX_ASSET_ID, "code": "SBX"},
            "requested": {"id": "xch", "code": "XCH"},
            "range": {"id": SBX_ASSET_ID, "code": "SBX", "min": 10000, "max": 1000000},
            "rewardRate": {"id": "dbxid", "code": "DBX", "amount": 100},
            "maxSpread": 0.05,
            "withinSpread": 4300748.36,
            "estimatedAPR": 0.606,
            "marketPrice": 9.15e-05,
        },
        {
            "offered": {"id": "xch", "code": "XCH"},
            "requested": {"id": WUSDC_ASSET_ID, "code": "wUSDC"},
            "range": {"id": "xch", "code": "XCH", "min": 1, "max": 100},
            "rewardRate": {"id": "dbxid", "code": "DBX", "amount": 100},
            "maxSpread": 0.05,
            "withinSpread": 920.58,
            "estimatedAPR": 0.258,
            "marketPrice": 2.41,
        },
    ],
}


class _IncentivesBase(unittest.TestCase):
    def setUp(self):
        dexie_incentives.clear_cache()
        self._patcher = patch("dexie_incentives.requests.get")
        self._mock = self._patcher.start()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _FIXTURE
        self._mock.return_value = resp

    def tearDown(self):
        self._patcher.stop()
        dexie_incentives.clear_cache()


class TestFetch(_IncentivesBase):
    def test_fetch_returns_payload(self):
        out = dexie_incentives.fetch_incentives(force=True)
        self.assertTrue(out["success"])
        self.assertEqual(len(out["incentives"]), 3)

    def test_cache_avoids_second_http_call(self):
        dexie_incentives.fetch_incentives(force=True)
        dexie_incentives.fetch_incentives()
        self.assertEqual(self._mock.call_count, 1)


class TestPairLookup(_IncentivesBase):
    def test_sbx_has_both_sides(self):
        pair = dexie_incentives.get_pair_incentives(SBX_ASSET_ID)
        self.assertTrue(pair["incentivized"])
        self.assertIsNotNone(pair["buy"])
        self.assertIsNotNone(pair["sell"])
        self.assertEqual(pair["buy"]["range_unit"], "XCH")
        self.assertEqual(pair["sell"]["range_unit"], "SBX")
        self.assertEqual(pair["buy"]["max_spread_bps"], 500)
        self.assertEqual(pair["buy"]["reward_token"], "DBX")

    def test_wusdc_is_buy_only_in_fixture(self):
        # Fixture only includes one direction for wUSDC.
        pair = dexie_incentives.get_pair_incentives(WUSDC_ASSET_ID)
        self.assertTrue(pair["incentivized"])
        self.assertIsNotNone(pair["buy"])
        self.assertIsNone(pair["sell"])

    def test_unknown_pair_not_incentivized(self):
        pair = dexie_incentives.get_pair_incentives("00" * 32)
        self.assertFalse(pair["incentivized"])
        self.assertIsNone(pair["buy"])
        self.assertIsNone(pair["sell"])

    def test_asset_id_normalisation(self):
        upper = SBX_ASSET_ID.upper()
        prefixed = "0x" + SBX_ASSET_ID
        self.assertTrue(dexie_incentives.get_pair_incentives(upper)["incentivized"])
        self.assertTrue(dexie_incentives.get_pair_incentives(prefixed)["incentivized"])


class TestBase58(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(_b58encode(b""), "")
        self.assertEqual(compute_offer_hash(""), "")

    def test_leading_zeros_preserved(self):
        # Base58 convention: each leading zero byte → leading '1'
        self.assertEqual(_b58encode(b"\x00\x00\x01"), "112")

    def test_offer_hash_length_matches_dexie_format(self):
        # Dexie offer ids are 44 chars (base58 of a 32-byte sha256)
        h = compute_offer_hash("offer1qqqqqqqqqqqqqqqqqqqq")
        self.assertEqual(len(h), 44)
        # Should only contain bitcoin alphabet chars
        for ch in h:
            self.assertIn(
                ch, "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
            )


try:
    import chia.util.bech32m as _bech32m_mod  # noqa: F401

    _CHIA_AVAILABLE = True
except ImportError:
    _CHIA_AVAILABLE = False


@unittest.skipUnless(_CHIA_AVAILABLE, "chia-blockchain not installed")
class TestPuzzleHashToAddress(unittest.TestCase):
    def test_round_trip(self):
        from chia.util.bech32m import decode_puzzle_hash

        ph = "8b9b8c0e7f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c"
        # Force the mainnet prefix without poking the wallet.
        with patch("dexie_claims._network_prefix", return_value="xch"):
            addr = puzzle_hash_to_address(ph)
        self.assertTrue(addr.startswith("xch1"))
        self.assertEqual(decode_puzzle_hash(addr).hex(), ph)

    def test_returns_empty_when_chia_missing(self):
        # The production helper falls back gracefully when chia isn't
        # importable (CI environments). Confirm the contract by patching
        # encode_puzzle_hash to None — same as a missing-import code path.
        with patch("dexie_claims.encode_puzzle_hash", None):
            self.assertEqual(puzzle_hash_to_address("ab" * 32), "")


if __name__ == "__main__":
    unittest.main()
