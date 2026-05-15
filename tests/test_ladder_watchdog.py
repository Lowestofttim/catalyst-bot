"""Tests for the ladder + coin-accounting watchdog."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from decimal import Decimal

from ladder_watchdog import (
    audit_ladder_shape,
    check_coin_invariants,
    run_periodic_audit,
)


# Reverse-ladder config: inner = largest, extreme = smallest (per user's toggle)
REVERSE_TIER_SIZES = {
    "inner": Decimal("1.6887"),
    "mid": Decimal("0.9288"),
    "outer": Decimal("0.4222"),
    "extreme": Decimal("0.2111"),
}
REVERSE_TIER_COUNTS = {"inner": 10, "mid": 5, "outer": 3, "extreme": 2}


def _offer(price, size_xch, tier=None):
    """Factory for a minimal offer dict."""
    offer = {"price": price, "size_xch": size_xch}
    if tier is not None:
        offer["tier"] = tier
    return offer


class TestHealthyReverseLadder:
    """A well-formed reverse ladder should pass with no issues."""

    def test_perfect_taper_no_issues(self):
        offers = []
        # 10 inner sells at 0.00012670..., size 1.6887
        for i in range(10):
            offers.append(_offer(0.000126 + i * 0.00000001, 1.6887))
        for i in range(5):
            offers.append(_offer(0.000127 + i * 0.00000001, 0.9288))
        for i in range(3):
            offers.append(_offer(0.000128 + i * 0.00000001, 0.4222))
        for i in range(2):
            offers.append(_offer(0.000129 + i * 0.00000001, 0.2111))

        result = audit_ladder_shape(
            side="sell",
            offers=offers,
            tier_sizes_xch=REVERSE_TIER_SIZES,
            tier_counts=REVERSE_TIER_COUNTS,
            reversed_ladder=True,
        )
        assert result.ok is True
        assert len(result.issues) == 0

    def test_db_tier_labels_prevent_interleaved_price_false_positive(self):
        offers = []
        # Live requotes can leave lower-priced mid/outer/extreme offers ahead
        # of inner offers by price order. If the DB tier labels and sizes are
        # correct, watchdog should trust those labels instead of slot position.
        for i in range(5):
            offers.append(_offer(0.000126 + i * 0.00000001, 0.9288, "mid"))
        for i in range(3):
            offers.append(_offer(0.0001265 + i * 0.00000001, 0.4222, "outer"))
        for i in range(2):
            offers.append(_offer(0.0001268 + i * 0.00000001, 0.2111, "extreme"))
        for i in range(10):
            offers.append(_offer(0.000127 + i * 0.00000001, 1.6887, "inner"))

        result = audit_ladder_shape(
            side="sell",
            offers=offers,
            tier_sizes_xch=REVERSE_TIER_SIZES,
            tier_counts=REVERSE_TIER_COUNTS,
            reversed_ladder=True,
        )
        codes = [i.code for i in result.issues]
        assert result.ok is True
        assert "ladder_size_taper_violated" not in codes
        assert result.summary["tier_source"] == "offer"

    def test_db_tier_labels_prevent_partial_ladder_false_positive(self):
        offers = []
        # After a sweep, the near-mid offers can be gone while correctly sized
        # mid-tier leftovers remain. Tier labels should prevent those survivors
        # from being judged as misfit inner slots.
        for i in range(3):
            offers.append(_offer(0.000127 + i * 0.00000001, 0.9288, "mid"))

        result = audit_ladder_shape(
            side="sell",
            offers=offers,
            tier_sizes_xch=REVERSE_TIER_SIZES,
            tier_counts=REVERSE_TIER_COUNTS,
            reversed_ladder=True,
        )
        codes = [i.code for i in result.issues]
        assert result.ok is True
        assert "ladder_count_mismatch" in codes
        assert "ladder_size_taper_violated" not in codes
        assert result.summary["tier_source"] == "offer"


class TestReverseLadderInversion:
    """The 2026-04-17 regression: misfit 3.04 XCH offers at outer price
    slots produce a REVERSE-LADDER INVERSION (extreme median > inner
    median). Watchdog must flag this as ERROR."""

    def test_tonight_regression_detected(self):
        offers = []
        # Inner slots (7): 1.6887 — correct
        for i in range(7):
            offers.append(_offer(0.000126 + i * 0.00000001, 1.6887))
        # Mid slots (5): 0.9288 — correct
        for i in range(5):
            offers.append(_offer(0.000127 + i * 0.00000001, 0.9288))
        # Outer slots (3): **3.04** — WRONG, bigger than inner
        for i in range(3):
            offers.append(_offer(0.000128 + i * 0.00000001, 3.04))
        # Extreme (2): **3.04** — WRONG too
        for i in range(2):
            offers.append(_offer(0.000129 + i * 0.00000001, 3.04))

        result = audit_ladder_shape(
            side="sell",
            offers=offers,
            tier_sizes_xch=REVERSE_TIER_SIZES,
            tier_counts=REVERSE_TIER_COUNTS,
            reversed_ladder=True,
        )
        assert result.ok is False
        assert result.has_errors() is True
        # The error should be ladder_inversion_reverse
        codes = [i.code for i in result.issues]
        assert any(c == "ladder_inversion_reverse" for c in codes), (
            f"Expected ladder_inversion_reverse in {codes}"
        )


class TestStandardLadder:
    """Non-reverse: inner=smallest, extreme=largest."""

    def test_standard_ladder_no_issues(self):
        offers = []
        std_tiers = {
            "inner": Decimal("0.2111"),
            "mid": Decimal("0.4222"),
            "outer": Decimal("0.9288"),
            "extreme": Decimal("1.6887"),
        }
        for i in range(10):
            offers.append(_offer(0.000126 + i * 0.00000001, 0.2111))
        for i in range(5):
            offers.append(_offer(0.000127 + i * 0.00000001, 0.4222))
        for i in range(3):
            offers.append(_offer(0.000128 + i * 0.00000001, 0.9288))
        for i in range(2):
            offers.append(_offer(0.000129 + i * 0.00000001, 1.6887))
        result = audit_ladder_shape(
            side="sell",
            offers=offers,
            tier_sizes_xch=std_tiers,
            tier_counts=REVERSE_TIER_COUNTS,
            reversed_ladder=False,
        )
        assert result.ok is True
        assert len(result.issues) == 0


class TestSizeDrift:
    """Small size drift within tolerance should not fire; large drift should."""

    def test_small_drift_within_tolerance_ok(self):
        # 1% drift — well within 5% default tolerance
        size_with_drift = 1.6887 * 1.01
        offers = [_offer(0.000126 + i * 0.00000001, size_with_drift) for i in range(10)]
        offers += [_offer(0.000127 + i * 0.00000001, 0.9288) for i in range(5)]
        offers += [_offer(0.000128 + i * 0.00000001, 0.4222) for i in range(3)]
        offers += [_offer(0.000129 + i * 0.00000001, 0.2111) for i in range(2)]

        result = audit_ladder_shape(
            side="sell",
            offers=offers,
            tier_sizes_xch=REVERSE_TIER_SIZES,
            tier_counts=REVERSE_TIER_COUNTS,
            reversed_ladder=True,
        )
        assert not result.has_errors()

    def test_20_percent_drift_flagged(self):
        # 20% drift — way beyond 5% default tolerance
        bad_size = 1.6887 * 1.2
        offers = [_offer(0.000126 + i * 0.00000001, bad_size) for i in range(10)]
        offers += [_offer(0.000127 + i * 0.00000001, 0.9288) for i in range(5)]
        offers += [_offer(0.000128 + i * 0.00000001, 0.4222) for i in range(3)]
        offers += [_offer(0.000129 + i * 0.00000001, 0.2111) for i in range(2)]

        result = audit_ladder_shape(
            side="sell",
            offers=offers,
            tier_sizes_xch=REVERSE_TIER_SIZES,
            tier_counts=REVERSE_TIER_COUNTS,
            reversed_ladder=True,
        )
        codes = [i.code for i in result.issues]
        assert "ladder_size_taper_violated" in codes


class TestOfferCountMismatch:
    """Missing or extra offers trigger a WARN."""

    def test_short_ladder_warns(self):
        offers = [_offer(0.000126 + i * 0.00000001, 1.6887) for i in range(5)]
        result = audit_ladder_shape(
            side="sell",
            offers=offers,
            tier_sizes_xch=REVERSE_TIER_SIZES,
            tier_counts=REVERSE_TIER_COUNTS,
            reversed_ladder=True,
        )
        codes = [i.code for i in result.issues]
        assert "ladder_count_mismatch" in codes

    def test_one_off_tolerated(self):
        """±1 from expected count is tolerated (transient sniper probes)."""
        offers = [_offer(0.000126 + i * 0.00000001, 1.6887) for i in range(10)]
        offers += [_offer(0.000127 + i * 0.00000001, 0.9288) for i in range(5)]
        offers += [_offer(0.000128 + i * 0.00000001, 0.4222) for i in range(3)]
        offers += [
            _offer(0.000129 + i * 0.00000001, 0.2111) for i in range(1)
        ]  # 1 instead of 2
        # total = 19, expected = 20 → ±1 OK
        result = audit_ladder_shape(
            side="sell",
            offers=offers,
            tier_sizes_xch=REVERSE_TIER_SIZES,
            tier_counts=REVERSE_TIER_COUNTS,
            reversed_ladder=True,
        )
        codes = [i.code for i in result.issues]
        assert "ladder_count_mismatch" not in codes


class TestCoinInvariants:
    """Coin-accounting cross-view consistency checks."""

    def test_clean_accounting_no_issues(self):
        result = check_coin_invariants(
            wallet_totals={"xch_total": 122, "cat_total": 67},
            inventory={
                "xch": {"free": 98, "locked": 24},
                "cat": {"free": 43, "locked": 24},
            },
            open_offers_count={"buy": 24, "sell": 24},
            db_locked_count={"xch": 24, "cat": 24},
        )
        assert result.ok is True
        assert len(result.issues) == 0

    def test_inventory_mismatch_warns(self):
        result = check_coin_invariants(
            wallet_totals={"xch_total": 130, "cat_total": 67},  # 130, but...
            inventory={
                "xch": {"free": 98, "locked": 24},  # inv = 122
                "cat": {"free": 43, "locked": 24},
            },
            open_offers_count={"buy": 24, "sell": 24},
            db_locked_count={"xch": 24, "cat": 24},
        )
        codes = [i.code for i in result.issues]
        assert "inventory_count_mismatch" in codes

    def test_locked_vs_offers_divergence_warns(self):
        result = check_coin_invariants(
            wallet_totals={"xch_total": 122, "cat_total": 67},
            inventory={
                "xch": {"free": 98, "locked": 24},
                "cat": {"free": 43, "locked": 24},
            },
            open_offers_count={"buy": 24, "sell": 24},
            db_locked_count={"xch": 30, "cat": 24},  # 6 phantom locks
        )
        codes = [i.code for i in result.issues]
        assert "xch_locked_vs_buys_mismatch" in codes

    def test_small_divergence_tolerated(self):
        """±2 tolerance for sniper probes and transient states."""
        result = check_coin_invariants(
            wallet_totals={"xch_total": 122, "cat_total": 67},
            inventory={
                "xch": {"free": 98, "locked": 24},
                "cat": {"free": 43, "locked": 24},
            },
            open_offers_count={"buy": 24, "sell": 24},
            db_locked_count={"xch": 25, "cat": 25},  # +1 each (sniper probes)
        )
        assert result.ok is True
        assert len(result.issues) == 0


class TestCombinedAudit:
    """run_periodic_audit() returns a flat list across all sub-audits."""

    def test_runs_without_error(self):
        issues = run_periodic_audit(
            offers_buy=[_offer(0.00012, 1.6887) for _ in range(24)],
            offers_sell=[_offer(0.00013, 1.6887) for _ in range(24)],
            buy_tier_sizes_xch=REVERSE_TIER_SIZES,
            sell_tier_sizes_xch=REVERSE_TIER_SIZES,
            buy_tier_counts=REVERSE_TIER_COUNTS,
            sell_tier_counts=REVERSE_TIER_COUNTS,
            buy_reversed=True,
            sell_reversed=True,
            wallet_totals={"xch_total": 122, "cat_total": 67},
            inventory={
                "xch": {"free": 98, "locked": 24},
                "cat": {"free": 43, "locked": 24},
            },
            db_locked_count={"xch": 24, "cat": 24},
        )
        # Should be a list of issues (may include size-taper warnings
        # because all offers are 1.6887 in this test, not real taper)
        assert isinstance(issues, list)
