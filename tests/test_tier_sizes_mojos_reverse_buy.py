"""Regression test for ``get_tier_sizes_mojos_from_cfg`` under BUY_LADDER_REVERSED.

The returned dict must be COIN-SIZE-BUCKET indexed so the F70 SSOT misfit
rejection path (in ``offer_manager._select_coin_for_offer``) can compare
``preferred_tier`` (from ``coin_size_tier_for_slot_position`` — which IS
bucket-indexed under reverse-buy) against the classifier's output.

Bug history
-----------
Buy storage is POSITION-indexed: Smart Defaults writes ``BUY_INNER_SIZE_XCH``
as the size for the inner POSITION (small near mid under reverse-buy). But
coin-size buckets follow ``_BUY_REVERSED_POSITION_TO_COIN_SIZE`` which maps
position-inner → bucket-extreme (smallest bucket). So the tier_sizes_mojos
dict needs a flip to re-key from position to bucket.

Pre-fix history on 2026-04-18:
  * F79 added a flip in ``get_buy_tier_size_xch`` that made it size-indexed.
  * F80 worked around F79 by reading env fields directly — but that reverted
    to position-indexed output, which was still wrong for F70.

After the 2026-04-19 fix: storage stays position-indexed (matches Smart
Defaults' write), ``get_buy_tier_size_xch`` returns position directly, and
``get_tier_sizes_mojos_from_cfg`` applies the position→bucket flip itself.
"""
import os
import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import patch


class _StubCfg:
    def __init__(self, *, reversed_buy: bool):
        self.TIER_ENABLED = True
        self.BUY_LADDER_REVERSED = reversed_buy
        # POSITION-indexed storage. Under reverse-buy: inner position offers
        # the smallest amount (close to mid), extreme position offers the
        # largest (far from mid). Smart Defaults + handleReverseLadderToggle
        # both write storage this way.
        if reversed_buy:
            self.BUY_INNER_SIZE_XCH = Decimal("0.29")     # inner pos = small
            self.BUY_MID_SIZE_XCH = Decimal("0.6379")
            self.BUY_OUTER_SIZE_XCH = Decimal("1.1598")
            self.BUY_EXTREME_SIZE_XCH = Decimal("2.0876") # extreme pos = large
        else:
            self.BUY_INNER_SIZE_XCH = Decimal("2.0876")   # inner pos = large (normal)
            self.BUY_MID_SIZE_XCH = Decimal("1.1598")
            self.BUY_OUTER_SIZE_XCH = Decimal("0.6379")
            self.BUY_EXTREME_SIZE_XCH = Decimal("0.29")
        self.SELL_INNER_SIZE_XCH = Decimal("2.0111")
        self.SELL_MID_SIZE_XCH = Decimal("1.1173")
        self.SELL_OUTER_SIZE_XCH = Decimal("0.6145")
        self.SELL_EXTREME_SIZE_XCH = Decimal("0.2793")
        self.INNER_SIZE_XCH = Decimal("0")
        self.MID_SIZE_XCH = Decimal("0")
        self.OUTER_SIZE_XCH = Decimal("0")
        self.EXTREME_SIZE_XCH = Decimal("0")
        self.CAT_DECIMALS = 3
        # PCT=0 keeps the tier sizes raw so these assertions compare
        # position→bucket remapping without also needing to account for
        # the prep-headroom multiplier (which is exercised by other tests).
        self.COIN_PREP_HEADROOM_PCT = Decimal("0")
        self.WALLET_FINGERPRINT = ""


# Stubs needed for coin_manager import
def _ensure_stubs():
    if "dotenv" not in sys.modules:
        d = types.ModuleType("dotenv")
        d.load_dotenv = lambda *a, **kw: None
        d.set_key = lambda *a, **kw: None
        sys.modules["dotenv"] = d
    if "requests" not in sys.modules:
        r = types.ModuleType("requests")
        class _Resp:
            status_code = 200
            def json(self): return {}
            def raise_for_status(self): pass
        class _Session:
            headers = {}
            def get(self, *a, **kw): return _Resp()
            def mount(self, *a, **kw): pass
        r.get = lambda *a, **kw: _Resp()
        r.Session = _Session
        r.exceptions = types.SimpleNamespace(Timeout=Exception, ConnectionError=Exception)
        a = types.ModuleType("requests.adapters")
        a.HTTPAdapter = object
        r.adapters = a
        sys.modules["requests"] = r
        sys.modules["requests.adapters"] = a
    if "urllib3" not in sys.modules:
        u = types.ModuleType("urllib3")
        u.Retry = object
        u.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
        u.disable_warnings = lambda *a, **kw: None
        sys.modules["urllib3"] = u

_ensure_stubs()


class TierSizesReverseBuyTests(unittest.TestCase):
    """The dict returned for the BUY side must be COIN-SIZE-BUCKET indexed
    (inner bucket = biggest coin, extreme bucket = smallest) regardless of
    reverse-buy mode, because that's the naming convention the selector,
    coin_prep_worker, and coin_classifier all agree on."""

    @classmethod
    def setUpClass(cls):
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())

    def setUp(self):
        import config
        import coin_manager
        self._real_cfg = config.cfg
        self._real_cm_cfg = getattr(coin_manager, 'cfg', None)

    def tearDown(self):
        import config
        import coin_manager
        config.cfg = self._real_cfg
        if self._real_cm_cfg is not None:
            coin_manager.cfg = self._real_cm_cfg

    def _patch_cfg(self, stub):
        import config
        import coin_manager
        config.cfg = stub
        coin_manager.cfg = stub

    def test_non_reverse_returns_position_equals_size_indexed(self):
        """Non-reverse: position and size are the same thing (inner=biggest)."""
        from coin_manager import get_tier_sizes_mojos_from_cfg
        self._patch_cfg(_StubCfg(reversed_buy=False))
        sizes = get_tier_sizes_mojos_from_cfg(is_cat=False)
        # Inner key holds the largest size (BUY_INNER_SIZE_XCH = 2.0876)
        self.assertEqual(sizes["inner"], int(Decimal("2.0876") * 10**12))
        self.assertEqual(sizes["mid"], int(Decimal("1.1598") * 10**12))
        self.assertEqual(sizes["outer"], int(Decimal("0.6379") * 10**12))
        self.assertEqual(sizes["extreme"], int(Decimal("0.29") * 10**12))

    def test_reverse_buy_remaps_position_to_coin_size_bucket(self):
        """Under reverse-buy, position-indexed storage gets re-keyed so the
        dict is coin-size-bucket indexed (inner bucket = biggest).

        Storage (position-indexed): inner=0.29 (small inner-position offer),
        extreme=2.0876 (big extreme-position offer).
        Output (bucket-indexed): inner=2.0876 (biggest bucket), extreme=0.29
        (smallest bucket). Matches coin_size_tier_for_slot_position semantics.
        """
        from coin_manager import get_tier_sizes_mojos_from_cfg
        self._patch_cfg(_StubCfg(reversed_buy=True))
        sizes = get_tier_sizes_mojos_from_cfg(is_cat=False)
        self.assertEqual(
            sizes["inner"], int(Decimal("2.0876") * 10**12),
            "Inner bucket key must hold the LARGEST size (= pos-extreme size "
            "under reverse-buy). Otherwise F70 labels coins incorrectly.",
        )
        self.assertEqual(
            sizes["extreme"], int(Decimal("0.29") * 10**12),
            "Extreme bucket key must hold the SMALLEST size (= pos-inner "
            "size under reverse-buy).",
        )
        self.assertEqual(sizes["mid"], int(Decimal("1.1598") * 10**12))
        self.assertEqual(sizes["outer"], int(Decimal("0.6379") * 10**12))

    def test_reverse_buy_cat_path_unchanged(self):
        """Sell side should be unaffected by reverse-buy."""
        from coin_manager import get_tier_sizes_mojos_from_cfg
        self._patch_cfg(_StubCfg(reversed_buy=True))
        # CAT path needs a price — patch api_server.bot to None so we hit
        # the fallback (XCH sizes used directly)
        with patch.dict(sys.modules, {'api_server': types.ModuleType('api_server')}):
            sys.modules['api_server'].bot = None
            sizes = get_tier_sizes_mojos_from_cfg(is_cat=True)
        # Sell side is never flipped — these should be SELL_*_SIZE_XCH
        # values (non-flipped). They get scaled to CAT mojos via price
        # conversion, but the relative shape stays size-indexed.
        # Just check the dict has the right keys with reasonable values.
        self.assertEqual(set(sizes.keys()), {"inner", "mid", "outer", "extreme"})
        # Inner should be the largest
        self.assertGreater(sizes["inner"], sizes["mid"])
        self.assertGreater(sizes["mid"], sizes["outer"])
        self.assertGreater(sizes["outer"], sizes["extreme"])


if __name__ == "__main__":
    unittest.main()
