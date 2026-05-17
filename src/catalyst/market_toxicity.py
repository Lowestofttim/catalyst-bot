"""Adverse-selection scoring for CATalyst market making.

The guard is intentionally small and explainable. It reads the live context
that `bot_loop` already has, scores risky flow by side, then returns a snapshot
that `risk_manager` can use to widen spreads and pause fresh exposure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    clear_condition: str = ""
    cooldown_secs_if_clear: int = 0
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
            "clear_condition": self.clear_condition,
            "cooldown_secs_if_clear": self.cooldown_secs_if_clear,
            "updated_at": self.updated_at,
            "enabled": self.enabled,
        }


class MarketToxicityGuard:
    """Score toxic flow and produce side-specific spread/throttle guidance."""

    def __init__(self) -> None:
        self._snapshot = ToxicitySnapshot()

    def reset(self) -> None:
        """Clear per-run toxicity memory for a fresh bot session."""
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
            reasons.append(
                {
                    "key": key,
                    "side": side,
                    "score": int(pts),
                    "detail": detail,
                }
            )

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
            side for side in SIDES if float(throttle_until.get(side, 0) or 0) > now
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
            clear_condition=self._clear_condition(reasons),
            cooldown_secs_if_clear=self._cooldown_secs_if_clear(score),
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
                add(
                    side,
                    "fast_fills",
                    points,
                    f"{side} fill landed {age:.0f}s after creation",
                )
        for side, count in counts.items():
            if count >= 2:
                add(
                    side,
                    "fast_fill_cluster",
                    Decimal("12") * (count - 1),
                    f"{count} recent {side} fills arrived quickly",
                )

    def _score_one_sided_flow(self, context: ToxicityContext, add) -> None:
        mode = str(context.liquidity_mode or "two_sided").lower()
        if mode in ("buy_only", "sell_only"):
            return
        side_counts = {
            "buy": sum(
                1
                for f in context.recent_fills
                if str(f.get("side", "")).lower() == "buy"
            ),
            "sell": sum(
                1
                for f in context.recent_fills
                if str(f.get("side", "")).lower() == "sell"
            ),
        }
        total = side_counts["buy"] + side_counts["sell"]
        if total < 3:
            return
        for side in SIDES:
            if Decimal(side_counts[side]) / Decimal(total) >= Decimal("0.75"):
                add(
                    side,
                    "one_sided_flow",
                    18,
                    f"{side_counts[side]}/{total} recent fills are {side}",
                )

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
                add(
                    "buy",
                    "post_fill_adverse_move",
                    min(abs(move_bps) / Decimal("4"), Decimal("35")),
                    f"price moved {abs(move_bps):.0f} bps lower after buy fill",
                )
            elif side == "sell" and move_bps > Decimal("50"):
                add(
                    "sell",
                    "post_fill_adverse_move",
                    min(move_bps / Decimal("4"), Decimal("35")),
                    f"price moved {move_bps:.0f} bps higher after sell fill",
                )

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
                add(
                    side,
                    "inventory_pressure",
                    25,
                    f"position is {pos_pct:.0f}% of configured limit",
                )

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
                by_side[side].append(
                    _dec(offer.get("size_xch") or offer.get("xch_amount"))
                )
        for side in SIDES:
            available = spendable[side]
            if available <= 0:
                continue
            total_exposed = sum(by_side[side], Decimal("0"))
            largest = max(by_side[side], default=Decimal("0"))
            exposure_pct = total_exposed / available * Decimal("100")
            largest_pct = largest / available * Decimal("100")
            if exposure_pct >= Decimal("50"):
                add(
                    side,
                    "small_balance_exposure",
                    38,
                    f"{side} offers expose {exposure_pct:.0f}% of spendable balance",
                )
            elif exposure_pct >= Decimal("25"):
                add(
                    side,
                    "small_balance_exposure",
                    22,
                    f"{side} offers expose {exposure_pct:.0f}% of spendable balance",
                )
            if largest_pct >= Decimal("60"):
                add(
                    side,
                    "large_offer_vs_balance",
                    38,
                    f"largest {side} offer is {largest_pct:.0f}% of spendable balance",
                )
            elif largest_pct >= Decimal("35"):
                add(
                    side,
                    "large_offer_vs_balance",
                    24,
                    f"largest {side} offer is {largest_pct:.0f}% of spendable balance",
                )

    def _score_sweeps(self, context: ToxicityContext, add) -> None:
        for event in context.recent_sweep_events or []:
            side = str(event.get("side") or "").lower()
            fill_count = int(
                _dec(event.get("side_fill_count", event.get("fill_count")), "0")
            )
            if side in SIDES and fill_count >= 2:
                add(
                    side,
                    "same_block_sweep",
                    30 + min(20, 5 * fill_count),
                    f"{fill_count} {side} fills grouped in the same sweep",
                )

    def _score_public_depth(self, context: ToxicityContext, add, add_both) -> None:
        intel = context.market_intel or {}
        thin_side = str(intel.get("thin_side") or "").lower()
        if thin_side in SIDES:
            add(
                thin_side,
                "thin_public_depth",
                18,
                f"Dexie public depth is thin on {thin_side}",
            )
        buy_depth = _dec(intel.get("buy_depth_xch"))
        sell_depth = _dec(intel.get("sell_depth_xch"))
        if buy_depth > 0 and sell_depth > 0:
            ratio = buy_depth / sell_depth
            if ratio >= Decimal("5"):
                add(
                    "sell",
                    "public_depth_imbalance",
                    18,
                    "public buy depth dominates sell depth",
                )
            elif ratio <= Decimal("0.2"):
                add(
                    "buy",
                    "public_depth_imbalance",
                    18,
                    "public sell depth dominates buy depth",
                )
        whale_signals = {"buy": [], "sell": []}
        taker_pressure = self._recent_taker_pressure(context)
        for whale in intel.get("whale_orders") or []:
            side = str(whale.get("side") or "").lower()
            is_ours = whale.get("is_ours")
            if is_ours is True or str(is_ours).strip().lower() in {"1", "true", "yes"}:
                continue
            if side in SIDES:
                signal = self._score_public_whale_order(
                    context=context,
                    intel=intel,
                    whale=whale,
                    side=side,
                    taker_pressure=taker_pressure.get(side, 0),
                )
                if signal:
                    whale_signals[side].append(signal)
        for side, signals in whale_signals.items():
            if not signals:
                continue
            total_score = min(
                Decimal("70"),
                sum((_dec(s.get("score")) for s in signals), Decimal("0")),
            )
            count = len(signals)
            detail = (
                signals[0]["detail"]
                if count == 1
                else self._public_whale_group_detail(side, signals)
            )
            add(side, "whale_public_offer", total_score, detail)

    def _score_public_whale_order(
        self,
        context: ToxicityContext,
        intel: Dict[str, Any],
        whale: Dict[str, Any],
        side: str,
        taker_pressure: int,
    ) -> Optional[Dict[str, Any]]:
        """Score one public large order using market-relative size and freshness.

        A plain ">= 1 XCH" public order is normal in a liquid book. It becomes
        adverse-selection evidence only when it is large relative to available
        depth or our own ladder, and close enough to the live market to attract
        near-term taking.
        """

        amount = _dec(whale.get("xch_amount"))
        price = _dec(whale.get("price"))
        mid = _dec(context.mid_price)
        if amount <= 0:
            return None

        same_depth = _dec(intel.get(f"{side}_depth_xch"))
        other_side = "sell" if side == "buy" else "buy"
        other_depth = _dec(intel.get(f"{other_side}_depth_xch"))
        total_depth = same_depth + other_depth
        depth_share = (
            amount / same_depth * Decimal("100") if same_depth > 0 else Decimal("0")
        )
        total_share = (
            amount / total_depth * Decimal("100") if total_depth > 0 else Decimal("0")
        )

        side_offers = [
            _dec(o.get("size_xch") or o.get("xch_amount"))
            for o in context.open_offers
            if str(o.get("side") or "").lower() == side
        ]
        avg_our_offer = (
            sum(side_offers, Decimal("0")) / Decimal(len(side_offers))
            if side_offers
            else Decimal("0")
        )
        large_vs_ours = avg_our_offer > 0 and amount >= avg_our_offer * Decimal("2")

        if not (
            depth_share >= Decimal("8") or total_share >= Decimal("5") or large_vs_ours
        ):
            return None

        proximity_bps = self._public_order_proximity_bps(side, price, mid)
        if (
            proximity_bps is not None
            and proximity_bps > Decimal("600")
            and taker_pressure <= 0
        ):
            return None

        if depth_share >= Decimal("30"):
            score = Decimal("40")
        elif depth_share >= Decimal("20"):
            score = Decimal("32")
        elif depth_share >= Decimal("12"):
            score = Decimal("24")
        elif depth_share >= Decimal("8"):
            score = Decimal("18")
        elif total_share >= Decimal("5"):
            score = Decimal("14")
        elif large_vs_ours:
            score = Decimal("10")
        else:
            score = Decimal("0")

        if proximity_bps is not None:
            if proximity_bps <= Decimal("75"):
                score += Decimal("6")
            elif proximity_bps <= Decimal("200"):
                score += Decimal("3")
            elif proximity_bps > Decimal("400"):
                score -= Decimal("8")

        age = self._offer_age_secs(whale, context.now)
        if age is None:
            age_factor = Decimal("0.85")
        elif age <= Decimal("900"):
            age_factor = Decimal("1.0")
        elif age <= Decimal("3600"):
            age_factor = Decimal("0.70")
        elif age <= Decimal("21600"):
            age_factor = Decimal("0.45")
        else:
            age_factor = Decimal("0.20")
        score *= age_factor

        if taker_pressure > 0:
            score = score * Decimal("1.25") + Decimal("8")

        score_i = _bounded_score(score)
        if score_i < 8:
            return None

        detail_parts = [
            f"{amount:.2f} XCH public {side} offer",
        ]
        if proximity_bps is not None and proximity_bps <= Decimal("300"):
            detail_parts.append(f"near live market ({proximity_bps:.0f} bps from mid)")
        elif proximity_bps is not None:
            detail_parts.append(f"{proximity_bps:.0f} bps from mid")
        if same_depth > 0:
            detail_parts.append(f"{depth_share:.1f}% of {side} depth")
        if large_vs_ours:
            detail_parts.append("large vs our ladder")
        if age is not None:
            detail_parts.append(f"age {self._format_age(age)}")
        if taker_pressure > 0:
            detail_parts.append(f"recent {side} taker pressure")

        return {
            "score": score_i,
            "amount": amount,
            "depth_share": depth_share,
            "proximity_bps": proximity_bps,
            "detail": "; ".join(detail_parts),
        }

    @staticmethod
    def _public_order_proximity_bps(
        side: str, price: Decimal, mid: Decimal
    ) -> Optional[Decimal]:
        if price <= 0 or mid <= 0:
            return None
        if side == "buy":
            return max(Decimal("0"), (mid - price) / mid * Decimal("10000"))
        return max(Decimal("0"), (price - mid) / mid * Decimal("10000"))

    def _recent_taker_pressure(self, context: ToxicityContext) -> Dict[str, int]:
        pressure = {"buy": 0, "sell": 0}
        for fill in context.recent_fills or []:
            side = str(fill.get("side") or "").lower()
            if side not in SIDES:
                continue
            age = _dec(fill.get("age_secs"), "9999")
            if age <= Decimal("600"):
                pressure[side] += 1
        for event in context.recent_sweep_events or []:
            side = str(event.get("side") or "").lower()
            if side not in SIDES:
                continue
            pressure[side] += max(
                1, int(_dec(event.get("side_fill_count", event.get("fill_count")), "1"))
            )
        return pressure

    def _public_whale_group_detail(
        self, side: str, signals: List[Dict[str, Any]]
    ) -> str:
        total_amount = sum((_dec(s.get("amount")) for s in signals), Decimal("0"))
        max_share = max(
            (_dec(s.get("depth_share")) for s in signals), default=Decimal("0")
        )
        near_count = sum(
            1
            for s in signals
            if s.get("proximity_bps") is not None
            and _dec(s.get("proximity_bps")) <= Decimal("300")
        )
        count = len(signals)
        near_text = f", {near_count} near live market" if near_count > 0 else ""
        return (
            f"{count} market-relative public {side} offers visible on Dexie"
            f" ({total_amount:.2f} XCH total{near_text}; largest {max_share:.1f}% of {side} depth)"
        )

    def _offer_age_secs(self, item: Dict[str, Any], now: float) -> Optional[Decimal]:
        age = _dec(item.get("age_secs"), "-1")
        if age >= 0:
            return age
        raw = str(item.get("created_at") or item.get("date_found") or "").strip()
        if not raw:
            return None
        try:
            if raw.isdigit():
                ts = float(raw)
                if ts > 10_000_000_000:
                    ts /= 1000
            else:
                normalized = raw.replace("Z", "+00:00")
                try:
                    dt = datetime.fromisoformat(normalized)
                except ValueError:
                    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
            return max(Decimal("0"), _dec(now) - _dec(ts))
        except Exception:
            return None

    @staticmethod
    def _format_age(age: Decimal) -> str:
        if age < Decimal("90"):
            return f"{age:.0f}s"
        mins = age / Decimal("60")
        if mins < Decimal("90"):
            return f"{mins:.0f}m"
        hours = mins / Decimal("60")
        return f"{hours:.1f}h"

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

    def _cooldown_secs_if_clear(self, score: int) -> int:
        if score <= 0:
            return 0
        decay = max(1, int(_dec(getattr(cfg, "TOXICITY_DECAY_PER_LOOP", 8), "8")))
        loop_secs = max(1, int(_dec(getattr(cfg, "LOOP_SECONDS", 45), "45")))
        loops = (int(score) + decay - 1) // decay
        return int(loops * loop_secs)

    @staticmethod
    def _clear_condition(reasons: List[Dict[str, Any]]) -> str:
        keys = {str(r.get("key") or "") for r in reasons if isinstance(r, dict)}
        sides = sorted(
            {
                str(r.get("side") or "").upper()
                for r in reasons
                if isinstance(r, dict) and str(r.get("side") or "").lower() in SIDES
            }
        )
        side_text = "/".join(sides) if sides else "risk-side"
        if "whale_public_offer" in keys:
            return (
                f"{side_text} public orders are no longer large relative to live depth, "
                "near the touch, or paired with recent same-side taking"
            )
        if keys:
            return "current adverse-selection signals disappear from the selected CAT market"
        return ""

    def _next_throttle_until(
        self, now: float, buy_score: int, sell_score: int, signal_keys: Dict[str, set]
    ) -> Dict[str, float]:
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
