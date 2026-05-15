"""Regression tests for ``get_buy_tier_size_xch`` — POSITION-semantic reads.

Bug history
-----------
``BUY_*_SIZE_XCH`` is stored POSITION-indexed: ``BUY_INNER_SIZE_XCH`` holds the
size for the inner POSITION (slot closest to mid). Smart Defaults writes it
that way, and the UI swap in ``handleReverseLadderToggle`` preserves that
convention: toggling reverse-buy ON swaps the stored values so
``BUY_INNER_SIZE_XCH`` ends up SMALL (inner-position offers a small amount
close to mid under reverse-buy) and ``BUY_EXTREME_SIZE_XCH`` ends up LARGE.

F79 (2026-04-18) briefly added a flip inside ``get_buy_tier_size_xch`` that
interpreted storage as SIZE-indexed and applied a position→size map before
the field read — returning ``BUY_EXTREME_SIZE_XCH`` when asked about the
inner POSITION under reverse-buy. That inverted Smart Defaults' write
convention and caused coin_prep_worker to request ~2× the XCH actually
intended (e.g. the user saw "fits 128 XCH wallet" in Smart Settings then
got "pool exceeds available wallet" from the F62 pre-flight). F79 was
reverted on 2026-04-19; storage is position-indexed end-to-end.

Legacy fallback (pre-F62 configs without ``BUY_*_SIZE_XCH``) still applies
the reverse-buy flip against the shared ``<TIER>_SIZE_XCH`` fields because
those legacy values had an inner=biggest convention.
"""

import os
import sys
import unittest
from decimal import Decimal


class _StubCfg:
    """Minimal cfg object with just the fields config helpers need."""

    def __init__(
        self,
        *,
        reversed_buy: bool,
        buy_inner=None,
        buy_mid=None,
        buy_outer=None,
        buy_extreme=None,
        sell_inner=None,
        sell_mid=None,
        sell_outer=None,
        sell_extreme=None,
        inner=None,
        mid=None,
        outer=None,
        extreme=None,
    ):
        self.BUY_LADDER_REVERSED = reversed_buy
        self.BUY_INNER_SIZE_XCH = (
            Decimal(buy_inner) if buy_inner is not None else Decimal("0")
        )
        self.BUY_MID_SIZE_XCH = (
            Decimal(buy_mid) if buy_mid is not None else Decimal("0")
        )
        self.BUY_OUTER_SIZE_XCH = (
            Decimal(buy_outer) if buy_outer is not None else Decimal("0")
        )
        self.BUY_EXTREME_SIZE_XCH = (
            Decimal(buy_extreme) if buy_extreme is not None else Decimal("0")
        )
        self.SELL_INNER_SIZE_XCH = (
            Decimal(sell_inner) if sell_inner is not None else Decimal("0")
        )
        self.SELL_MID_SIZE_XCH = (
            Decimal(sell_mid) if sell_mid is not None else Decimal("0")
        )
        self.SELL_OUTER_SIZE_XCH = (
            Decimal(sell_outer) if sell_outer is not None else Decimal("0")
        )
        self.SELL_EXTREME_SIZE_XCH = (
            Decimal(sell_extreme) if sell_extreme is not None else Decimal("0")
        )
        self.INNER_SIZE_XCH = Decimal(inner) if inner is not None else Decimal("0")
        self.MID_SIZE_XCH = Decimal(mid) if mid is not None else Decimal("0")
        self.OUTER_SIZE_XCH = Decimal(outer) if outer is not None else Decimal("0")
        self.EXTREME_SIZE_XCH = (
            Decimal(extreme) if extreme is not None else Decimal("0")
        )


class GetBuyTierSizePositionIndexedTests(unittest.TestCase):
    """Storage is POSITION-indexed; the getter returns the stored value
    for the asked-about position directly."""

    @classmethod
    def setUpClass(cls):
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())

    def setUp(self):
        import config

        self._real_cfg = config.cfg

    def tearDown(self):
        import config

        config.cfg = self._real_cfg

    def _patch_cfg(self, stub):
        import config

        config.cfg = stub

    # ── Non-reverse: inner POSITION has the biggest size ──────────────

    def test_non_reversed_returns_field_directly(self):
        from config import get_buy_tier_size_xch

        # Normal ladder: inner (near mid) = biggest, extreme (far) = smallest
        self._patch_cfg(
            _StubCfg(
                reversed_buy=False,
                buy_inner="2.0876",
                buy_mid="1.1598",
                buy_outer="0.6379",
                buy_extreme="0.29",
            )
        )
        self.assertEqual(get_buy_tier_size_xch("inner"), Decimal("2.0876"))
        self.assertEqual(get_buy_tier_size_xch("mid"), Decimal("1.1598"))
        self.assertEqual(get_buy_tier_size_xch("outer"), Decimal("0.6379"))
        self.assertEqual(get_buy_tier_size_xch("extreme"), Decimal("0.29"))

    # ── Reverse-buy: Smart Defaults stores SMALL in BUY_INNER_SIZE_XCH ─

    def test_reversed_inner_position_returns_small_stored_value(self):
        """Under reverse-buy, inner position offers a SMALL amount close to
        mid. Smart Defaults writes that small value directly into
        ``BUY_INNER_SIZE_XCH`` (the UI's handleReverseLadderToggle swap
        keeps the stored convention position-indexed).

        The getter returns the field verbatim — no flip — so downstream
        callers that ask "what size is the inner POSITION slot?" get the
        correct small value.
        """
        from config import get_buy_tier_size_xch

        self._patch_cfg(
            _StubCfg(
                reversed_buy=True,
                # Reverse convention (swap applied at storage): inner=small, extreme=big
                buy_inner="0.29",
                buy_mid="0.6379",
                buy_outer="1.1598",
                buy_extreme="2.0876",
            )
        )
        self.assertEqual(
            get_buy_tier_size_xch("inner"),
            Decimal("0.29"),
            "Under reverse-buy, BUY_INNER_SIZE_XCH stores the (small) inner-"
            "position offer size; the getter must return it as-is.",
        )
        self.assertEqual(get_buy_tier_size_xch("extreme"), Decimal("2.0876"))

    def test_reversed_extreme_position_returns_large_stored_value(self):
        """Under reverse-buy, extreme position offers a LARGE amount far
        from mid. Smart Defaults writes that large value into
        ``BUY_EXTREME_SIZE_XCH`` directly."""
        from config import get_buy_tier_size_xch

        self._patch_cfg(
            _StubCfg(
                reversed_buy=True,
                buy_inner="0.29",
                buy_mid="0.6379",
                buy_outer="1.1598",
                buy_extreme="2.0876",
            )
        )
        self.assertEqual(
            get_buy_tier_size_xch("extreme"),
            Decimal("2.0876"),
            "Under reverse-buy, BUY_EXTREME_SIZE_XCH stores the (large) "
            "extreme-position offer size; the getter must return it as-is.",
        )

    def test_reversed_mid_outer_return_stored_values(self):
        from config import get_buy_tier_size_xch

        self._patch_cfg(
            _StubCfg(
                reversed_buy=True,
                buy_inner="0.29",
                buy_mid="0.6379",
                buy_outer="1.1598",
                buy_extreme="2.0876",
            )
        )
        self.assertEqual(get_buy_tier_size_xch("mid"), Decimal("0.6379"))
        self.assertEqual(get_buy_tier_size_xch("outer"), Decimal("1.1598"))

    # ── Legacy fallback path (pre-F62 configs) still flips ───────────

    def test_legacy_fallback_under_reverse(self):
        """When BUY_*_SIZE_XCH is unset, fall back to legacy
        ``<TIER>_SIZE_XCH`` with the reverse-buy flip applied — legacy
        storage only ever held inner=biggest values, so the flip is needed
        to get the reversed shape."""
        from config import get_buy_tier_size_xch

        self._patch_cfg(
            _StubCfg(
                reversed_buy=True,
                inner="2.0",
                mid="1.0",
                outer="0.5",
                extreme="0.25",
            )
        )
        # Position inner under reverse → legacy extreme size (small)
        self.assertEqual(get_buy_tier_size_xch("inner"), Decimal("0.25"))
        self.assertEqual(get_buy_tier_size_xch("extreme"), Decimal("2.0"))

    def test_legacy_fallback_non_reverse(self):
        from config import get_buy_tier_size_xch

        self._patch_cfg(
            _StubCfg(
                reversed_buy=False,
                inner="2.0",
                mid="1.0",
                outer="0.5",
                extreme="0.25",
            )
        )
        self.assertEqual(get_buy_tier_size_xch("inner"), Decimal("2.0"))
        self.assertEqual(get_buy_tier_size_xch("extreme"), Decimal("0.25"))

    # ── Sell side never flips ─────────────────────────────────────────

    def test_sell_side_ignores_reverse_buy(self):
        from config import get_sell_tier_size_xch

        self._patch_cfg(
            _StubCfg(
                reversed_buy=True,  # should NOT affect sell
                sell_inner="2.0876",
                sell_extreme="0.29",
            )
        )
        self.assertEqual(get_sell_tier_size_xch("inner"), Decimal("2.0876"))
        self.assertEqual(get_sell_tier_size_xch("extreme"), Decimal("0.29"))


if __name__ == "__main__":
    unittest.main()
