"""Adverse-selection scoring for CATalyst market making.

The guard is intentionally small and explainable. It reads the live context
that `bot_loop` already has, scores risky flow by side, then returns a snapshot
that `risk_manager` can use to widen spreads and pause fresh exposure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

try:
    from config import cfg
except Exception:  # pragma: no cover - only used during isolated import failures
    cfg = object()


SIDES = ("buy", "sell")


def _dec(value: Any, default: str = "0") -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _bounded_score(value: Decimal | int | float) -> int:
    return int(max(Decimal("0"), min(Decimal("100"), _dec(value))))


@dataclass
class ToxicityContext:
    now: float
    loop_count: int
    mid_price: Decimal
    tibet_price: Decimal = Decimal("0")
    dexie_price: Decimal = Decimal("0")
    arb_gap_bps: Decimal = Decimal("0")
    open_offers: List[Dict[str, Any]] = field(default_factory=list)
    recent_fills: List[Dict[str, Any]] = field(default_factory=list)
    recent_created_offers: List[Dict[str, Any]] = field(default_factory=list)
    market_intel: Dict[str, Any] = field(default_factory=dict)
    orderbook_snapshot: Dict[str, Any] = field(default_factory=dict)
    inventory_state: Dict[str, Any] = field(default_factory=dict)
    wallet_health: Dict[str, Any] = field(default_factory=dict)
    recent_sweep_events: List[Dict[str, Any]] = field(default_factory=list)
    liquidity_mode: str = "two_sided"


@dataclass
class ToxicitySnapshot:
    score: int = 0
    buy_score: int = 0
    sell_score: int = 0
    level: str = "normal"
    buy_spread_multiplier: Decimal = Decimal("1.0")
    sell_spread_multiplier: Decimal = Decimal("1.0")
    throttled_sides: List[str] = field(default_factory=list)
    throttle_until: Dict[str, float] = field(default_factory=dict)
    reasons: List[Dict[str, Any]] = field(default_factory=list)
    suggested_action: str = "No toxicity action needed."
    updated_at: float = 0.0
    enabled: bool = True

    def is_side_throttled(self, side: str, now: Optional[float] = None) -> bool:
        if side not in SIDES:
            return False
        if side not in self.throttled_sides:
            return False
        if now is None:
            return True
        return float(self.throttle_until.get(side, 0) or 0) > float(now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "buy_score": self.buy_score,
            "sell_score": self.sell_score,
            "level": self.level,
            "buy_spread_multiplier": str(self.buy_spread_multiplier),
            "sell_spread_multiplier": str(self.sell_spread_multiplier),
            "throttled_sides": list(self.throttled_sides),
            "throttle_until": dict(self.throttle_until),
            "reasons": list(self.reasons),
            "suggested_action": self.suggested_action,
            "updated_at": self.updated_at,
            "enabled": self.enabled,
        }


class MarketToxicityGuard:
    """Score toxic flow and produce side-specific spread/throttle guidance."""

    def __init__(self) -> None:
        self._snapshot = ToxicitySnapshot()

    def get_snapshot(self) -> ToxicitySnapshot:
        return self._snapshot

    def is_side_throttled(self, side: str, now: Optional[float] = None) -> bool:
        return self._snapshot.is_side_throttled(side, now)

    def update(self, context: ToxicityContext) -> ToxicitySnapshot:
        now = float(context.now or 0)
        if not bool(getattr(cfg, "MARKET_TOXICITY_ENABLED", True)):
            self._snapshot = ToxicitySnapshot(updated_at=now, enabled=False)
            return self._snapshot

        decay = _dec(getattr(cfg, "TOXICITY_DECAY_PER_LOOP", 8), "8")
        # Current evidence is rescored fresh each loop; only the previous peak decays.
        buy_memory = max(Decimal("0"), _dec(self._snapshot.buy_score) - decay)
        sell_memory = max(Decimal("0"), _dec(self._snapshot.sell_score) - decay)
        buy_evidence = Decimal("0")
        sell_evidence = Decimal("0")
        reasons: List[Dict[str, Any]] = []
        signal_keys = {"buy": set(), "sell": set()}

        def add(side: str, key: str, points: Decimal | int, detail: str) -> None:
            nonlocal buy_evidence, sell_evidence
            if side not in SIDES:
                return
            pts = _dec(points)
            if pts <= 0:
                return
            if side == "buy":
                buy_evidence += pts
            else:
                sell_evidence += pts
            signal_keys[side].add(key)
            reasons.append({
                "key": key,
                "side": side,
                "score": int(pts),
                "detail": detail,
            })

        def add_both(key: str, points: Decimal | int, detail: str) -> None:
            add("buy", key, points, detail)
            add("sell", key, points, detail)

        self._score_fast_fills(context, add)
        self._score_one_sided_flow(context, add)
        self._score_adverse_moves(context, add)
        self._score_arb_gap(context, add_both)
        self._score_inventory_pressure(context, add)
        self._score_small_balance_exposure(context, add)
        self._score_sweeps(context, add)
        self._score_public_depth(context, add, add_both)
        self._score_data_quality(context, add_both)

        buy_score = max(buy_memory, buy_evidence)
        sell_score = max(sell_memory, sell_evidence)
        buy_i = _bounded_score(buy_score)
        sell_i = _bounded_score(sell_score)
        score = max(buy_i, sell_i)
        level = self._level(score)
        throttle_until = self._next_throttle_until(
            now=now,
            buy_score=buy_i,
            sell_score=sell_i,
            signal_keys=signal_keys,
        )
        throttled_sides = [
            side for side in SIDES
            if float(throttle_until.get(side, 0) or 0) > now
        ]

        snap = ToxicitySnapshot(
            score=score,
            buy_score=buy_i,
            sell_score=sell_i,
            level=level,
            buy_spread_multiplier=self._side_multiplier("buy", buy_i, sell_i),
            sell_spread_multiplier=self._side_multiplier("sell", sell_i, buy_i),
            throttled_sides=throttled_sides,
            throttle_until=throttle_until,
            reasons=reasons[:12],
            suggested_action=self._suggested_action(level, throttled_sides),
            updated_at=now,
            enabled=True,
        )
        self._snapshot = snap
        return snap

    def _score_fast_fills(self, context: ToxicityContext, add) -> None:
        counts = {"buy": 0, "sell": 0}
        for fill in context.recent_fills or []:
            side = str(fill.get("side") or "").lower()
            if side not in SIDES:
                continue
            age = _dec(fill.get("age_secs"), "9999")
            if age <= Decimal("20"):
                points = Decimal("35")
            elif age <= Decimal("60"):
                points = Decimal("22")
            else:
                points = Decimal("0")
            if points:
                counts[side] += 1
                add(side, "fast_fills", points,
                    f"{side} fill landed {age:.0f}s after creation")
        for side, count in counts.items():
            if count >= 2:
                add(side, "fast_fill_cluster", Decimal("12") * (count - 1),
                    f"{count} recent {side} fills arrived quickly")

    def _score_one_sided_flow(self, context: ToxicityContext, add) -> None:
        mode = str(context.liquidity_mode or "two_sided").lower()
        if mode in ("buy_only", "sell_only"):
            return
        side_counts = {
            "buy": sum(1 for f in context.recent_fills if str(f.get("side", "")).lower() == "buy"),
            "sell": sum(1 for f in context.recent_fills if str(f.get("side", "")).lower() == "sell"),
        }
        total = side_counts["buy"] + side_counts["sell"]
        if total < 3:
            return
        for side in SIDES:
            if Decimal(side_counts[side]) / Decimal(total) >= Decimal("0.75"):
                add(side, "one_sided_flow", 18, f"{side_counts[side]}/{total} recent fills are {side}")

    def _score_adverse_moves(self, context: ToxicityContext, add) -> None:
        mid = _dec(context.mid_price)
        if mid <= 0:
            return
        for fill in context.recent_fills or []:
            side = str(fill.get("side") or "").lower()
            if side not in SIDES:
                continue
            price = _dec(fill.get("price") or fill.get("price_xch"))
            if price <= 0:
                continue
            move_bps = (mid - price) / price * Decimal("10000")
            if side == "buy" and move_bps < Decimal("-50"):
                add("buy", "post_fill_adverse_move", min(abs(move_bps) / Decimal("4"), Decimal("35")),
                    f"price moved {abs(move_bps):.0f} bps lower after buy fill")
            elif side == "sell" and move_bps > Decimal("50"):
                add("sell", "post_fill_adverse_move", min(move_bps / Decimal("4"), Decimal("35")),
                    f"price moved {move_bps:.0f} bps higher after sell fill")

    def _score_arb_gap(self, context: ToxicityContext, add_both) -> None:
        gap = abs(_dec(context.arb_gap_bps))
        if gap >= Decimal("1000"):
            add_both("dexie_tibet_dislocation", 55, f"Dexie/Tibet gap is {gap:.0f} bps")
        elif gap >= Decimal("500"):
            add_both("dexie_tibet_dislocation", 32, f"Dexie/Tibet gap is {gap:.0f} bps")
        elif gap >= Decimal("200"):
            add_both("dexie_tibet_dislocation", 12, f"Dexie/Tibet gap is {gap:.0f} bps")

    def _score_inventory_pressure(self, context: ToxicityContext, add) -> None:
        inv = context.inventory_state or {}
        pos_pct = abs(_dec(inv.get("position_pct")))
        if pos_pct >= Decimal("90"):
            side = str(inv.get("pressure_side") or "").lower()
            if side in SIDES:
                add(side, "inventory_pressure", 25, f"position is {pos_pct:.0f}% of configured limit")

    def _score_small_balance_exposure(self, context: ToxicityContext, add) -> None:
        inv = context.inventory_state or {}
        spendable = {
            "buy": _dec(inv.get("xch_spendable")),
            "sell": _dec(inv.get("cat_spendable_xch") or inv.get("sell_spendable_xch")),
        }
        by_side = {"buy": [], "sell": []}
        for offer in context.open_offers or []:
            side = str(offer.get("side") or "").lower()
            if side in SIDES:
                by_side[side].append(_dec(offer.get("size_xch") or offer.get("xch_amount")))
        for side in SIDES:
            available = spendable[side]
            if available <= 0:
                continue
            total_exposed = sum(by_side[side], Decimal("0"))
            largest = max(by_side[side], default=Decimal("0"))
            exposure_pct = total_exposed / available * Decimal("100")
            largest_pct = largest / available * Decimal("100")
            if exposure_pct >= Decimal("50"):
                add(side, "small_balance_exposure", 38,
                    f"{side} offers expose {exposure_pct:.0f}% of spendable balance")
            elif exposure_pct >= Decimal("25"):
                add(side, "small_balance_exposure", 22,
                    f"{side} offers expose {exposure_pct:.0f}% of spendable balance")
            if largest_pct >= Decimal("60"):
                add(side, "large_offer_vs_balance", 38,
                    f"largest {side} offer is {largest_pct:.0f}% of spendable balance")
            elif largest_pct >= Decimal("35"):
                add(side, "large_offer_vs_balance", 24,
                    f"largest {side} offer is {largest_pct:.0f}% of spendable balance")

    def _score_sweeps(self, context: ToxicityContext, add) -> None:
        for event in context.recent_sweep_events or []:
            side = str(event.get("side") or "").lower()
            fill_count = int(_dec(event.get("fill_count"), "0"))
            if side in SIDES and fill_count >= 2:
                add(side, "same_block_sweep", 30 + min(20, 5 * fill_count),
                    f"{fill_count} {side} fills grouped in the same sweep")

    def _score_public_depth(self, context: ToxicityContext, add, add_both) -> None:
        intel = context.market_intel or {}
        thin_side = str(intel.get("thin_side") or "").lower()
        if thin_side in SIDES:
            add(thin_side, "thin_public_depth", 18, f"Dexie public depth is thin on {thin_side}")
        buy_depth = _dec(intel.get("buy_depth_xch"))
        sell_depth = _dec(intel.get("sell_depth_xch"))
        if buy_depth > 0 and sell_depth > 0:
            ratio = buy_depth / sell_depth
            if ratio >= Decimal("5"):
                add("sell", "public_depth_imbalance", 18, "public buy depth dominates sell depth")
            elif ratio <= Decimal("0.2"):
                add("buy", "public_depth_imbalance", 18, "public sell depth dominates buy depth")
        for whale in intel.get("whale_orders") or []:
            side = str(whale.get("side") or "").lower()
            is_ours = whale.get("is_ours")
            if is_ours is True or str(is_ours).strip().lower() in {"1", "true", "yes"}:
                continue
            if side in SIDES:
                add(side, "whale_public_offer", 12, f"large public {side} offer visible on Dexie")

    def _score_data_quality(self, context: ToxicityContext, add_both) -> None:
        intel = context.market_intel or {}
        if not intel:
            return
        refreshes = int(_dec(intel.get("orderbook_refreshes"), "0"))
        age = _dec(intel.get("orderbook_age_secs"), "0")
        if refreshes <= 0:
            add_both("market_data_quality", 8, "Dexie orderbook not refreshed yet")
        elif age > Decimal("120"):
            add_both("market_data_quality", 12, f"Dexie orderbook is {age:.0f}s old")
        snapshot = context.orderbook_snapshot or {}
        if snapshot.get("buy_truncated") or snapshot.get("sell_truncated"):
            add_both("market_data_quality", 8, "Dexie orderbook page is truncated")

    def _next_throttle_until(self, now: float, buy_score: int, sell_score: int,
                             signal_keys: Dict[str, set]) -> Dict[str, float]:
        previous = {
            side: float(self._snapshot.throttle_until.get(side, 0) or 0)
            for side in SIDES
        }
        threshold = int(_dec(getattr(cfg, "TOXICITY_THROTTLE_START", 75), "75"))
        cancel_threshold = int(_dec(getattr(cfg, "TOXICITY_CANCEL_START", 90), "90"))
        secs = float(_dec(getattr(cfg, "TOXICITY_THROTTLE_SECS", 120), "120"))
        min_signals = int(_dec(getattr(cfg, "TOXICITY_MIN_THROTTLE_SIGNALS", 2), "2"))
        scores = {"buy": buy_score, "sell": sell_score}
        result = dict(previous)
        for side in SIDES:
            enough_signals = len(signal_keys[side]) >= min_signals
            extreme = scores[side] >= cancel_threshold
            if scores[side] >= threshold and (enough_signals or extreme):
                result[side] = max(result.get(side, 0), now + secs)
            elif result.get(side, 0) <= now:
                result[side] = 0
        return result

    def _side_multiplier(self, side: str, side_score: int, other_score: int) -> Decimal:
        cap = _dec(getattr(cfg, "TOXICITY_MAX_SPREAD_MULTIPLIER", "2.0"), "2.0")
        widen = int(_dec(getattr(cfg, "TOXICITY_WIDEN_START", 30), "30"))
        elevated = int(_dec(getattr(cfg, "TOXICITY_ELEVATED_START", 55), "55"))
        throttle = int(_dec(getattr(cfg, "TOXICITY_THROTTLE_START", 75), "75"))
        cancel = int(_dec(getattr(cfg, "TOXICITY_CANCEL_START", 90), "90"))
        if side_score >= cancel:
            mult = Decimal("2.00")
        elif side_score >= throttle:
            mult = Decimal("1.75")
        elif side_score >= elevated:
            mult = Decimal("1.35")
        elif side_score >= widen:
            mult = Decimal("1.10")
        elif other_score >= cancel:
            mult = Decimal("1.35")
        elif other_score >= throttle:
            mult = Decimal("1.20")
        elif other_score >= elevated:
            mult = Decimal("1.10")
        else:
            mult = Decimal("1.00")
        return min(mult, cap)

    def _level(self, score: int) -> str:
        widen = int(_dec(getattr(cfg, "TOXICITY_WIDEN_START", 30), "30"))
        elevated = int(_dec(getattr(cfg, "TOXICITY_ELEVATED_START", 55), "55"))
        throttle = int(_dec(getattr(cfg, "TOXICITY_THROTTLE_START", 75), "75"))
        cancel = int(_dec(getattr(cfg, "TOXICITY_CANCEL_START", 90), "90"))
        if score >= cancel:
            return "extreme"
        if score >= throttle:
            return "high"
        if score >= elevated:
            return "elevated"
        if score >= widen:
            return "mild"
        return "normal"

    @staticmethod
    def _suggested_action(level: str, throttled_sides: List[str]) -> str:
        if throttled_sides:
            return f"Throttle new {', '.join(throttled_sides)} offers until toxicity cools."
        if level in ("elevated", "high", "extreme"):
            return "Widen spreads and monitor fills before adding more exposure."
        if level == "mild":
            return "Slightly widen spreads while conditions settle."
        return "No toxicity action needed."
