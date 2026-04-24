"""Inventory-aware spread adjustment, circuit breakers, and market-health grading

The `RiskManager` class is the pricing brain that sits between `price_engine`
and `offer_manager`. Given a base spread, it layers inventory skew, volatility
scaling, pool-depth adjustment, competitor adjustment, and boost-convergence
on top, producing the per-side adjusted spread used to place offers. It also
tracks circuit-breaker state and grades current market health for downstream
consumers (GUI, bot_loop gating).

Key responsibilities:
    - Compute adjusted buy/sell spreads from base config + live signals
    - Track net inventory position and apply soft/hard position breakers
      (soft at 1.0x configured limit, hard at 1.5x)
    - Expose market-health scores and circuit-breaker status to callers
    - Consume an injected `dexie_manager` for competitor spread signals

`bot_loop` is lazy-imported to break a circular dependency; `dexie_manager`
is supplied via the constructor rather than imported at module scope.
"""

import threading
import time
from decimal import Decimal
from typing import Dict, Optional

from config import cfg
from database import (
    record_inventory_snapshot, get_net_position,
    log_event
)


def _bps_to_pct(val):
    """Convert a BPS value to a formatted % string."""
    try:
        n = float(val) / 100
        if n < 1:
            return f"{n:.2f}%"
        return f"{n:.1f}%"
    except (ValueError, TypeError):
        return str(val)


class RiskManager:
    """Manages trading risk through inventory awareness and dynamic spreads.

    Core concepts:
    - Net position: Sum of all fills. Positive = long CAT, negative = short CAT.
    - Skew: When long, make sells more attractive (tighter spread) and buys
      less attractive (wider spread). Vice versa when short.
    - Volatility scaling: Widen spreads when price is volatile to avoid
      getting picked off by informed traders.
    - Circuit breakers: Hard stops when position gets too large.
    """

    def __init__(self, price_engine=None, market_intel=None):
        self._price_engine = price_engine
        self._market_intel = market_intel
        self._boost_manager = None  # Set after construction for convergence

        # Current net CAT position (from database)
        self._net_position_cat: Decimal = Decimal("0")

        # Cached volatility
        self._cached_volatility: Decimal = Decimal("0")
        self._volatility_updated: float = 0

        # Circuit breaker state
        self._circuit_breaker_active: bool = False
        self._circuit_breaker_reason: str = ""
        self._circuit_breaker_time: float = 0
        # CB type controls which sides are blocked:
        #   "price"  → full halt (both sides) — price is outside safe range
        #   "position" → partial halt — only the accumulating side is blocked;
        #                the correcting (position-reducing) side must continue
        #   ""       → treat as full halt (safe default)
        self._circuit_breaker_type: str = ""
        self._circuit_breaker_blocked_side: str = ""  # "buy", "sell", or "" (both)
        self._cb_clear_streak: int = 0   # Consecutive cycles where CB would clear
        self._cb_clear_threshold: int = 3  # Require 3 consecutive OK cycles before clearing

        # Soft-position warning gate — reset when CB clears so warnings
        # can fire again on the next soft-limit breach.
        self._soft_position_warned: bool = False

        # Thread safety — main loop writes CB state; sniper/watcher thread reads it
        self._cb_lock = threading.Lock()

        # Startup position baseline — set on first position check.
        # Initialized here to avoid hasattr race if multiple threads
        # call _check_position_limit simultaneously on first cycle.
        self._startup_position_xch = None

        # Arb gap (Dexie vs Tibet) in BPS
        self._arb_gap_bps: Decimal = Decimal("0")

        # Recent fill rate (fills per hour, updated externally)
        self._recent_fill_rate: Decimal = Decimal("0")

        # Last inventory snapshot time
        self._last_snapshot_time: float = 0

        # Pool depth ratio (our trade size vs Tibet pool depth)
        self._pool_depth_ratio: Decimal = Decimal("0")
        self._pool_depth_updated: float = 0

        # Competitor spread data (from market_intel)
        self._competitor_adjustment_bps: Decimal = Decimal("0")
        self._competitor_updated: float = 0

    # -------------------------------------------------------------------
    # Inventory tracking
    # -------------------------------------------------------------------

    def update_inventory(self) -> Dict:
        """Refresh net position from database.

        Net position tracks cumulative fills:
        - Each BUY fill: +cat_amount, -xch_amount
        - Each SELL fill: -cat_amount, +xch_amount

        Returns current inventory state.
        """
        try:
            new_pos = get_net_position(cfg.CAT_ASSET_ID)
        except Exception as e:
            log_event("warning", "inventory_update_failed",
                      f"Failed to get net position: {e}")
            return self.get_inventory_state()

        # Acquire CB lock briefly to pair with reset_position() so a reset
        # can't land between this read and write and be silently overwritten.
        with self._cb_lock:
            self._net_position_cat = new_pos

        return self.get_inventory_state()

    def reset_position(self) -> None:
        """Reset net position and startup baseline atomically.

        Used by the API position-reset endpoints. Resets the inherited
        startup baseline too — otherwise the effective position limit
        stays inflated by the old baseline and lets the bot exceed
        MAX_POSITION_XCH after a reset.
        """
        with self._cb_lock:
            self._net_position_cat = Decimal("0")
            self._startup_position_xch = None
            self._soft_position_warned = False
        log_event("info", "position_reset",
                  "Net position and startup baseline reset to zero")

    def reset_session(self) -> None:
        """Full session reset — clear ALL cached state for a clean start.

        Called on every bot start and on CAT switch so stale inventory,
        volatility, circuit-breaker, and market data from the previous
        session/token don't leak into the new one.
        """
        with self._cb_lock:
            # Position
            self._net_position_cat = Decimal("0")
            self._startup_position_xch = None
            self._soft_position_warned = False
            # Circuit breaker
            self._circuit_breaker_active = False
            self._circuit_breaker_reason = ""
            self._circuit_breaker_time = 0
            self._circuit_breaker_type = ""
            self._circuit_breaker_blocked_side = ""
            self._cb_clear_streak = 0
        # Market data caches (outside lock — not read by sniper thread)
        self._cached_volatility = Decimal("0")
        self._volatility_updated = 0
        self._arb_gap_bps = Decimal("0")
        self._recent_fill_rate = Decimal("0")
        self._last_snapshot_time = 0
        self._pool_depth_ratio = Decimal("0")
        self._pool_depth_updated = 0
        self._competitor_adjustment_bps = Decimal("0")
        self._competitor_updated = 0
        log_event("info", "risk_session_reset",
                  "Risk manager session state fully reset")

    def record_snapshot(self, xch_balance: Optional[Decimal] = None,
                        cat_balance: Optional[Decimal] = None,
                        mid_price: Decimal = Decimal("0")):
        """Record an inventory snapshot to the database.

        Called periodically (every few loops) for historical tracking.
        """
        now = time.time()
        # Don't snapshot more than once per minute
        if now - self._last_snapshot_time < 60:
            return

        self._last_snapshot_time = now

        # Calculate unrealised PnL if we have price
        unrealised_pnl = Decimal("0")
        if mid_price > 0 and self._net_position_cat != 0:
            # If we're long CAT, unrealised = cat_position × current_price
            unrealised_pnl = self._net_position_cat * mid_price

        try:
            record_inventory_snapshot(
                cat_asset_id=cfg.CAT_ASSET_ID,
                net_position=self._net_position_cat,
                xch_balance=xch_balance,
                cat_balance=cat_balance,
                mid_price=mid_price,
                unrealised_pnl=unrealised_pnl
            )
        except Exception as e:
            log_event("warning", "snapshot_failed", f"Failed to record snapshot: {e}")

    def get_inventory_state(self) -> Dict:
        """Get current inventory state for GUI/API."""
        return {
            "net_position_cat": str(self._net_position_cat),
            "inventory_enabled": cfg.INVENTORY_ENABLED,
            "max_position_xch": str(cfg.MAX_POSITION_XCH),
            "skew_intensity": str(cfg.SKEW_INTENSITY),
            "circuit_breaker_active": self._circuit_breaker_active,
            "circuit_breaker_reason": self._circuit_breaker_reason,
            "pool_depth_ratio": str(self._pool_depth_ratio),
            "competitor_aware": getattr(cfg, "COMPETITOR_AWARE_ENABLED", False),
        }

    # -------------------------------------------------------------------
    # Spread adjustment
    # -------------------------------------------------------------------

    def get_adjusted_spread(self, side: str) -> Decimal:
        """Calculate the spread for a side, adjusted for all smart factors.

        The base spread comes from config. Then we apply (in order):
        1. Inventory skew (if enabled)
        2. Volatility scaling (if enabled)
        3. Pool depth adjustment (NEW — Tibet AMM depth awareness)
        4. Competitor adjustment (NEW — Dexie orderbook intelligence)

        Returns the adjusted spread as a fraction (e.g., 0.08 for 800 BPS).
        """
        base = self._get_base_spread()

        # Apply inventory skew
        if cfg.INVENTORY_ENABLED:
            base = self._apply_inventory_skew(base, side)

        # Apply volatility scaling (side-aware: the fill-rate widening
        # is skipped on the "corrective" side when we carry inventory,
        # so the side we need to attract fills to doesn't keep widening
        # itself out of the market during a fill burst on the other side).
        if cfg.DYNAMIC_SPREAD_ENABLED:
            base = self._apply_volatility_scaling(base, side=side)

        # Apply pool depth adjustment (NEW — if pool is shallow, widen to protect)
        if cfg.DYNAMIC_SPREAD_ENABLED:
            base = self._apply_pool_depth_adjustment(base)

        # Apply competitor intelligence adjustment (NEW — react to market)
        if getattr(cfg, "COMPETITOR_AWARE_ENABLED", False) and self._market_intel:
            base = self._apply_competitor_adjustment(base, side)

        # Apply boost convergence factor (if boost is tightening the spread)
        if self._boost_manager:
            convergence = self._boost_manager.get_convergence_factor()
            if convergence < Decimal("1.0"):
                base = base * convergence

        # Clamp to min/max
        min_spread = cfg.MIN_SPREAD_BPS / Decimal("10000")
        max_spread = cfg.MAX_SPREAD_BPS / Decimal("10000")
        base = max(min_spread, min(base, max_spread))

        # Ensure adjusted spread is wider than the inner edge (MIN_EDGE_BPS).
        # The offer ladder places offers from inner_edge to this spread,
        # so if spread <= inner_edge, all offers collapse to the same price.
        inner_edge = cfg.MIN_EDGE_BPS / Decimal("10000")
        min_outer = inner_edge * Decimal("1.5")  # At least 50% wider than inner edge
        if base < min_outer:
            base = min_outer

        return base

    def _get_base_spread(self) -> Decimal:
        """Get the base spread fraction from config."""
        if cfg.DYNAMIC_SPREAD_ENABLED:
            return cfg.BASE_SPREAD_BPS / Decimal("10000")
        return cfg.SPREAD_BPS / Decimal("10000")

    def _apply_inventory_skew(self, spread: Decimal, side: str) -> Decimal:
        """Skew the spread based on inventory position.

        When we're long CAT (positive position):
        - Widen BUY spread (less eager to buy more)
        - Tighten SELL spread (more eager to reduce position)

        When we're short CAT (negative position):
        - Tighten BUY spread (more eager to buy)
        - Widen SELL spread (less eager to sell more)

        The skew intensity controls how aggressively we adjust.
        """
        if self._net_position_cat == 0 or cfg.SKEW_INTENSITY == 0:
            return spread

        # Normalise position: 0 = neutral, 1 = max long, -1 = max short
        max_pos = cfg.MAX_POSITION_XCH
        if not max_pos or max_pos <= 0:
            return spread

        # Use price to convert CAT position to XCH equivalent
        price = Decimal("0")
        if self._price_engine:
            price = self._price_engine.get_last_price()
            if price is None:
                price = Decimal("0")
            elif not isinstance(price, Decimal):
                try:
                    price = Decimal(str(price))
                except Exception:
                    price = Decimal("0")

        if price <= 0:
            return spread

        position_xch = self._net_position_cat * price
        normalised = position_xch / max_pos  # -1 to +1 range (can exceed)
        normalised = max(Decimal("-1"), min(normalised, Decimal("1")))

        # Calculate skew amount
        skew = normalised * cfg.SKEW_INTENSITY * spread

        if side == "buy":
            # Long → widen buy (add skew), Short → tighten buy (subtract skew)
            adjusted = spread + skew
        else:
            # Long → tighten sell (subtract skew), Short → widen sell (add skew)
            adjusted = spread - skew

        # Never let spread go below minimum edge
        min_edge = cfg.MIN_EDGE_BPS / Decimal("10000")
        adjusted = max(adjusted, min_edge)

        return adjusted

    def _apply_volatility_scaling(self, spread: Decimal,
                                  side: Optional[str] = None) -> Decimal:
        """Scale spread based on recent price volatility.

        Higher volatility → wider spreads to protect against adverse selection.
        Low volatility → can tighten spreads for more fills.

        ``side`` is optional. When provided together with a non-zero inventory
        position, the fill-rate widening add-on below is skipped on the
        corrective side (sells when long CAT, buys when short CAT) so a
        directional fill burst doesn't widen the side we need to attract
        trades to. Volatility and arb-gap buffers still apply globally.
        """
        vol = self._get_volatility()
        if vol <= 0:
            return spread

        # Volatility multiplier: 1.0 at baseline, scales up with vol
        # Baseline volatility = 2% (normal for small-cap CATs)
        baseline_vol = Decimal("0.02")

        if vol > baseline_vol:
            # Scale spread up proportionally to excess volatility
            vol_ratio = vol / baseline_vol
            # Cap at 3x multiplier
            multiplier = min(vol_ratio, Decimal("3"))
            spread = spread * multiplier
        elif vol < baseline_vol / 2:
            # Very low vol — can tighten slightly (down to 0.8x)
            vol_ratio = vol / baseline_vol
            multiplier = max(Decimal("0.8"), vol_ratio + Decimal("0.5"))
            spread = spread * multiplier

        # Add arb gap buffer (if Dexie-Tibet gap is large, widen more)
        # NOTE: For small-cap CATs, a 10-20% arb gap between Dexie and
        # TibetSwap is normal (thin liquidity, infrequent arb).  The old
        # formula (/10000, cap 5%) was far too aggressive — a 15% gap
        # added a full 5% buffer on top of the base spread, effectively
        # doubling it.  New formula: gentler ramp (/30000) and lower cap
        # (1.5%) so spreads stay competitive in normal conditions while
        # still widening on genuinely extreme dislocations.
        if self._arb_gap_bps > Decimal("200"):
            arb_buffer = (self._arb_gap_bps - Decimal("200")) / Decimal("30000")
            # Cap arb buffer at 1.5% (150 BPS)
            arb_buffer = min(arb_buffer, Decimal("0.015"))
            spread = spread + arb_buffer

        # Add fill rate buffer (if recent fills are consistently high, widen).
        # SKIP this add-on on the corrective side when we carry inventory —
        # otherwise a burst of fills on one side (e.g. lots of sells while
        # long CAT) keeps widening the OTHER side (buys) that we would want
        # to stay attractive so the position naturally unwinds. Inventory
        # skew already tightens the corrective side; letting fill-rate
        # widening fight that was the behaviour Codex flagged in the
        # 2026-04-22 audit.
        is_corrective_side = False
        if side and self._net_position_cat != 0:
            if side == "sell" and self._net_position_cat > 0:
                is_corrective_side = True   # long CAT → sells unwind the position
            elif side == "buy" and self._net_position_cat < 0:
                is_corrective_side = True   # short CAT → buys unwind the position

        if not is_corrective_side:
            fill_start = Decimal(str(getattr(cfg, "DYNAMIC_FILL_RATE_START_PER_HOUR", "4")))
            fill_full = Decimal(str(getattr(cfg, "DYNAMIC_FILL_RATE_FULL_PER_HOUR", "12")))
            fill_max_buffer = (
                Decimal(str(getattr(cfg, "DYNAMIC_FILL_RATE_MAX_BPS", "100")))
                / Decimal("10000")
            )
            if self._recent_fill_rate > fill_start and fill_max_buffer > 0:
                fill_span = max(Decimal("0.1"), fill_full - fill_start)
                fill_ratio = (self._recent_fill_rate - fill_start) / fill_span
                fill_ratio = max(Decimal("0"), min(fill_ratio, Decimal("1")))
                spread = spread + (fill_max_buffer * fill_ratio)

        return spread

    def _get_volatility(self) -> Decimal:
        """Get recent price volatility, with caching.

        Refreshes every 5 minutes. Uses two sources, in order of
        preference:

          1. F37 (2026-04-08) — Dexie v3 historical_trades. Real
             trade-flow variance for the actual pair. This is the
             most accurate signal because it's based on actual
             executed trades, not bot-observed mid snapshots.

          2. price_engine.get_volatility — fallback that infers
             volatility from price snapshots stored in the local DB.
        """
        now = time.time()
        if now - self._volatility_updated < 300 and self._cached_volatility > 0:
            return self._cached_volatility

        # F37: try Dexie v3 historical trades first
        try:
            dm = getattr(self, "_dexie_manager", None)
            ticker_id = getattr(cfg, "CAT_TICKER_ID", "")
            if dm and ticker_id:
                metrics = dm.compute_v3_trade_metrics(
                    ticker_id, hours=float(cfg.VOLATILITY_WINDOW_HOURS)
                )
                if metrics and metrics.get("trades_in_window", 0) >= 5:
                    # Convert percent to fraction (e.g. 3.5% → 0.035)
                    vol_pct = float(metrics.get("price_stdev_pct", 0))
                    vol_dec = Decimal(str(vol_pct / 100.0))
                    if vol_dec > 0:
                        self._cached_volatility = vol_dec
                        self._volatility_updated = now
                        log_event(
                            "debug",
                            "vol_from_v3_trades",
                            f"Volatility from Dexie v3 trades: "
                            f"{vol_pct:.2f}% (n={metrics['trades_in_window']})",
                        )
                        return vol_dec
        except Exception:
            pass

        # Fallback: legacy price_engine snapshot-based volatility
        if self._price_engine:
            try:
                vol = self._price_engine.get_volatility(
                    cfg.CAT_ASSET_ID,
                    hours=float(cfg.VOLATILITY_WINDOW_HOURS)
                )
                if vol is None:
                    vol = Decimal("0")
                elif not isinstance(vol, Decimal):
                    vol = Decimal(str(vol))
                self._cached_volatility = vol
                self._volatility_updated = now
                return vol
            except Exception:
                pass

        cached = self._cached_volatility
        if cached is None:
            return Decimal("0")
        if isinstance(cached, Decimal):
            return cached
        try:
            return Decimal(str(cached))
        except Exception:
            return Decimal("0")

    # -------------------------------------------------------------------
    # Pool Depth Adjustment (NEW — ecosystem intelligence)
    # -------------------------------------------------------------------

    def _apply_pool_depth_adjustment(self, spread: Decimal) -> Decimal:
        """Adjust spread based on TibetSwap pool depth.

        If our trade size is a significant fraction of the AMM pool,
        we're causing price impact and should widen our spread to compensate.

        - Ratio < 1%: no adjustment (deep pool)
        - Ratio 1-5%: widen by up to 20%
        - Ratio 5-10%: widen by up to 50%
        - Ratio > 10%: widen by up to 100%
        """
        ratio = self._get_pool_depth_ratio()
        if ratio <= Decimal("0.01"):
            return spread  # Deep pool, no adjustment

        if ratio <= Decimal("0.05"):
            # Moderate impact — widen by (ratio / 0.05) * 20%
            multiplier = Decimal("1") + (ratio / Decimal("0.05")) * Decimal("0.20")
        elif ratio <= Decimal("0.10"):
            # Significant impact — widen by 20-50%
            multiplier = Decimal("1.20") + ((ratio - Decimal("0.05")) / Decimal("0.05")) * Decimal("0.30")
        else:
            # Large impact — widen substantially
            multiplier = Decimal("1.50") + min((ratio - Decimal("0.10")) / Decimal("0.10"), Decimal("0.50"))

        # Cap multiplier at 2x
        multiplier = min(multiplier, Decimal("2.0"))

        return spread * multiplier

    def _get_pool_depth_ratio(self) -> Decimal:
        """Get the cached pool depth ratio, refreshing every 5 minutes."""
        now = time.time()
        cached_ratio = self._pool_depth_ratio
        if cached_ratio is None:
            cached_ratio = Decimal("0")
            self._pool_depth_ratio = cached_ratio

        if now - self._pool_depth_updated < 300 and cached_ratio >= 0:
            return self._pool_depth_ratio

        if self._price_engine:
            try:
                ratio = self._price_engine.get_pool_depth_ratio()
                if ratio is None:
                    ratio = Decimal("0")
                elif not isinstance(ratio, Decimal):
                    ratio = Decimal(str(ratio))
                self._pool_depth_ratio = ratio
                self._pool_depth_updated = now
            except Exception:
                pass

        return self._pool_depth_ratio if self._pool_depth_ratio is not None else Decimal("0")

    # -------------------------------------------------------------------
    # Competitor-Aware Spread Adjustment (NEW — ecosystem intelligence)
    # -------------------------------------------------------------------

    def _apply_competitor_adjustment(self, spread: Decimal, side: str) -> Decimal:
        """Adjust spread based on live competitor data from Dexie orderbook.

        Uses market_intel module to see what other market makers are quoting.
        If competitors are wider, we can widen (more profit per fill).
        If competitors are tighter, we can tighten (more fills).
        """
        if not self._market_intel:
            return spread

        try:
            our_spread_bps = spread * Decimal("10000")

            # Get current mid price
            mid_price = Decimal("0")
            if self._price_engine:
                mid_price = self._price_engine.get_last_price() or Decimal("0")
            if mid_price is None:
                mid_price = Decimal("0")
            elif not isinstance(mid_price, Decimal):
                mid_price = Decimal(str(mid_price))

            # Get recommendation from market intel
            adjustment_bps = self._market_intel.get_spread_recommendation(
                side, our_spread_bps, mid_price
            )

            if adjustment_bps is None:
                return spread
            if not isinstance(adjustment_bps, Decimal):
                adjustment_bps = Decimal(str(adjustment_bps))

            if adjustment_bps == 0:
                return spread

            # Apply adjustment (convert BPS to fraction)
            adjustment_fraction = adjustment_bps / Decimal("10000")
            adjusted = spread + adjustment_fraction

            # Never let competitor adjustment push below minimum edge
            min_edge = cfg.MIN_EDGE_BPS / Decimal("10000")
            adjusted = max(adjusted, min_edge)

            return adjusted

        except Exception:
            return spread

    def update_market_intel(self, market_intel):
        """Set or update the market intelligence module reference."""
        self._market_intel = market_intel

    # -------------------------------------------------------------------
    # Tiered order sizing
    # -------------------------------------------------------------------

    def get_tier_size(self, tier: str, side: str = "sell") -> Decimal:
        """Get the order size for a given tier.

        F62 (2026-04-09): per-side tier sizes. Reads through the per-side
        helpers in config.py which prefer BUY_/SELL_ specific fields and
        fall back to the shared legacy keys (with reverse-buy flipping)
        when the per-side fields are zero. This lets Smart Settings size
        each side independently from its own balance — the buy ladder can
        have smaller offers than the sell ladder (or vice versa) so the
        full wallet gets deployed.
        """
        if not cfg.TIER_ENABLED:
            return cfg.DEFAULT_TRADE_XCH

        from config import get_buy_tier_size_xch, get_sell_tier_size_xch
        if side == "buy":
            val = get_buy_tier_size_xch(tier)
        else:
            val = get_sell_tier_size_xch(tier)
        if val and val > 0:
            return val
        return cfg.DEFAULT_TRADE_XCH

    # -------------------------------------------------------------------
    # Circuit breakers
    # -------------------------------------------------------------------

    def check_circuit_breakers(self, mid_price: Decimal = Decimal("0")) -> bool:
        """Check all circuit breaker conditions.

        Returns True if trading should be halted.
        """
        # 1. Max position check
        if self._check_position_limit(mid_price):
            return True

        # 2. Hard price limits
        if self._check_price_limits(mid_price):
            return True

        # Clear circuit breaker only after N consecutive OK cycles (hysteresis)
        with self._cb_lock:
            if self._circuit_breaker_active:
                self._cb_clear_streak += 1
                if self._cb_clear_streak >= self._cb_clear_threshold:
                    # Inline _clear_circuit_breaker logic (already hold lock)
                    elapsed = time.time() - self._circuit_breaker_time
                    self._circuit_breaker_active = False
                    self._circuit_breaker_type = ""
                    self._circuit_breaker_blocked_side = ""
                    self._soft_position_warned = False
                    log_event("info", "circuit_breaker_cleared",
                              f"Circuit breaker cleared after {elapsed:.0f}s")
                    self._circuit_breaker_reason = ""
                    self._cb_clear_streak = 0
            else:
                self._cb_clear_streak = 0  # Reset when CB not active

        return False

    def _check_position_limit(self, mid_price: Decimal) -> bool:
        """Trip circuit breaker only if position FAR exceeds MAX_POSITION_XCH.

        TIERED RESPONSE — avoids stopping the whole bot on normal fill clusters
        during volatile markets:
          Soft (≤ 1.0×): is_side_enabled() already disables the over-exposed
                         side so no new same-direction offers are created.
                         No circuit breaker here — book keeps running.
          Hard (> 1.5×): Full circuit breaker trips and all offers cancelled.
                         Reserved for runaway growth well beyond the user limit.

        STARTUP AWARENESS: The net position comes from ALL historical fills
        in the database (cumulative buys - sells). On a fresh restart with
        no open offers, this legacy position shouldn't block the bot from
        starting. We log a prominent warning but only trip the breaker if
        the position GROWS beyond the startup baseline + the limit.
        """
        if cfg.MAX_POSITION_XCH <= 0:
            return False

        price = mid_price
        if price <= 0 and self._price_engine:
            price = self._price_engine.get_last_price()
        if price <= 0:
            return False

        position_xch = abs(self._net_position_cat * price)

        # On first check, record the startup baseline position.
        # This is the inherited position from historical fills that the
        # bot had BEFORE this session started creating new offers.
        if self._startup_position_xch is None:
            self._startup_position_xch = position_xch
            if position_xch > cfg.MAX_POSITION_XCH:
                log_event("warning", "position_inherited",
                          f"Inherited position: {position_xch:.4f} XCH "
                          f"(exceeds limit of {cfg.MAX_POSITION_XCH} XCH). "
                          f"This is from historical fills. Bot will use this "
                          f"as the baseline and monitor for GROWTH beyond it. "
                          f"Consider increasing MAX_POSITION_XCH in .env or "
                          f"resetting fills if this position is stale.")

        # Effective limit: the LARGER of the configured limit or the
        # inherited startup position + 10% headroom. This prevents the
        # bot from being permanently stuck when historical fills exceed
        # the configured limit, while still protecting against runaway growth.
        effective_limit = cfg.MAX_POSITION_XCH
        if self._startup_position_xch is not None and self._startup_position_xch > effective_limit:
            effective_limit = self._startup_position_xch * Decimal("1.1")

        # Hard limit: trip full circuit breaker only at 1.5× effective limit.
        # At 1.0× the soft path (is_side_enabled) already stops new same-side
        # offers — no need to cancel the whole book for a normal fill cluster.
        hard_limit = effective_limit * Decimal("1.5")
        if position_xch > hard_limit:
            # Determine which side caused the overshoot so we can keep the
            # correcting side running.
            # net_position_cat > 0 → over-long CAT → buying pushed us over limit
            # net_position_cat < 0 → over-short CAT → selling pushed us over limit
            if self._net_position_cat > 0:
                blocked_side = "buy"   # over-long: block more buys, keep selling
            elif self._net_position_cat < 0:
                blocked_side = "sell"  # over-short: block more sells, keep buying
            else:
                # F4 (2026-04-08): defensive dead branch.
                # position_xch = abs(net_position_cat * price), so if
                # net_position_cat == 0 then position_xch == 0 and the
                # hard_limit > 0 check above cannot be true. We keep this
                # branch as defense-in-depth in case future refactoring
                # decouples position_xch from net_position_cat (e.g. adding
                # a fee/funding component). Full halt is the safest fallback
                # because we have no signal about which side to throttle.
                blocked_side = ""      # unknown — full halt to be safe
            self._trip_circuit_breaker(
                f"Position hard limit exceeded: {position_xch:.4f} XCH > "
                f"{hard_limit:.4f} XCH (1.5× limit of {effective_limit:.4f} XCH)",
                cb_type="position",
                blocked_side=blocked_side,
            )
            return True

        # Soft warning: position between 1.0× and 1.5× — log once, no halt.
        if position_xch > effective_limit:
            if not getattr(self, "_soft_position_warned", False):
                log_event("warning", "position_soft_limit",
                          f"Position {position_xch:.4f} XCH exceeds soft limit "
                          f"{effective_limit:.4f} XCH — same-side offers paused "
                          f"(circuit breaker trips at {hard_limit:.4f} XCH)")
                self._soft_position_warned = True
        else:
            self._soft_position_warned = False

        return False

    def _check_price_limits(self, mid_price: Decimal) -> bool:
        """Trip circuit breaker if price outside dynamic or hard limits.

        Check order:
          1. Dynamic limits (from price_engine reference price ± DYNAMIC_LIMIT_PCT)
          2. Hard limits (.env HARD_MIN/HARD_MAX — absolute backstop)
        """
        if mid_price <= 0:
            return False

        # --- Dynamic limits (primary) ---
        if self._price_engine:
            dyn_min, dyn_max = self._price_engine.get_dynamic_limits()
            if dyn_min is not None and mid_price < dyn_min:
                self._trip_circuit_breaker(
                    f"Price below dynamic minimum: {mid_price:.8f} < {dyn_min:.8f} "
                    f"(±{cfg.DYNAMIC_LIMIT_PCT}% band)"
                )
                return True
            if dyn_max is not None and mid_price > dyn_max:
                self._trip_circuit_breaker(
                    f"Price above dynamic maximum: {mid_price:.8f} > {dyn_max:.8f} "
                    f"(±{cfg.DYNAMIC_LIMIT_PCT}% band)"
                )
                return True

        # --- Hard limits (absolute backstop) ---
        if cfg.HARD_MIN_PRICE_XCH > 0 and mid_price < cfg.HARD_MIN_PRICE_XCH:
            self._trip_circuit_breaker(
                f"Price below hard minimum: {mid_price:.8f} < {cfg.HARD_MIN_PRICE_XCH}"
            )
            return True

        if cfg.HARD_MAX_PRICE_XCH > 0 and mid_price > cfg.HARD_MAX_PRICE_XCH:
            self._trip_circuit_breaker(
                f"Price above hard maximum: {mid_price:.8f} > {cfg.HARD_MAX_PRICE_XCH}"
            )
            return True

        return False

    def _trip_circuit_breaker(self, reason: str, cb_type: str = "price",
                              blocked_side: str = "") -> None:
        """Activate circuit breaker.

        Args:
            reason:       Human-readable explanation logged and shown in GUI.
            cb_type:      "price" (full halt) or "position" (partial halt).
            blocked_side: For position CB — "buy" if over-long (buying pushed
                          position past limit), "sell" if over-short.
                          Empty string means both sides are blocked (full halt).
        """
        with self._cb_lock:
            if self._circuit_breaker_active:
                # Allow escalation: position CB → price CB
                if cb_type == "price" and self._circuit_breaker_type == "position":
                    self._circuit_breaker_type = cb_type
                    self._circuit_breaker_blocked_side = blocked_side
                    self._circuit_breaker_reason = reason
                    log_event("warning", "cb_escalated",
                              f"CB escalated from position to price: {reason}")
                return
            self._circuit_breaker_active = True
            self._circuit_breaker_reason = reason
            self._circuit_breaker_time = time.time()
            self._circuit_breaker_type = cb_type
            self._cb_clear_streak = 0  # Reset hysteresis counter on any new trip
            self._circuit_breaker_blocked_side = blocked_side
            side_note = (f" — only '{blocked_side}' side blocked, correcting side continues"
                         if blocked_side else " — full halt (both sides)")
            log_event("warning", "circuit_breaker_tripped",
                      f"⚠️ Circuit breaker tripped ({cb_type}): {reason}{side_note}")

    def _clear_circuit_breaker(self):
        """Deactivate circuit breaker."""
        with self._cb_lock:
            if self._circuit_breaker_active:
                elapsed = time.time() - self._circuit_breaker_time
                self._circuit_breaker_active = False
                self._circuit_breaker_type = ""
                self._circuit_breaker_blocked_side = ""
                self._soft_position_warned = False
                log_event("info", "circuit_breaker_cleared",
                          f"Circuit breaker cleared after {elapsed:.0f}s")
                self._circuit_breaker_reason = ""

    def circuit_breaker_active(self) -> bool:
        """Check if circuit breaker is currently active (any type)."""
        with self._cb_lock:
            return self._circuit_breaker_active

    def get_circuit_breaker_blocked_side(self) -> str:
        """Return which side is blocked by an active circuit breaker.

        Returns:
            "buy"  — only buy-side creation is blocked (position CB, over-long)
            "sell" — only sell-side creation is blocked (position CB, over-short)
            ""     — both sides are blocked (price CB or unknown type)
        """
        with self._cb_lock:
            if not self._circuit_breaker_active:
                return ""
            return self._circuit_breaker_blocked_side

    def is_full_halt(self) -> bool:
        """True if the active circuit breaker blocks ALL trading (price CB).

        False if it's a position CB that only blocks one side.
        A position CB is self-correcting: the other side should keep running.
        """
        with self._cb_lock:
            if not self._circuit_breaker_active:
                return False
            return self._circuit_breaker_type != "position"

    def trip_price_rail_breach(self, reason: str) -> None:
        """Trip the price circuit breaker for a price-engine rail breach.

        Used by bot_loop when price_engine.get_price() returns None because
        _apply_safety_guards rejected the latest fetch (dynamic band, hard
        min/max, or step-change). The bot_loop early-return path would
        otherwise leave stale offers exposed at the now-wrong mid; routing
        the breach through the CB lets _safeguard_offers_for_circuit_breaker
        cancel them.

        Always full-halt (blocked_side="") because a rejected price means
        we don't trust the value enough to keep ANY side quoting.
        """
        self._trip_circuit_breaker(
            reason=reason,
            cb_type="price",
            blocked_side="",
        )

    # -------------------------------------------------------------------
    # Side enablement (inventory-aware)
    # -------------------------------------------------------------------

    def update_arb_gap(self, arb_gap_bps: Decimal):
        """Update the current arb gap between Dexie and TibetSwap."""
        self._arb_gap_bps = arb_gap_bps

    def update_fill_rate(self, fills_per_hour: Decimal):
        """Update recent fill rate (fills per hour)."""
        self._recent_fill_rate = fills_per_hour

    def get_circuit_breaker_state(self) -> dict:
        """Return circuit breaker state as a dict.

        Returns:
            {
                "active": bool,
                "blocked_side": "buy" | "sell" | "" (both/none),
                "type": "price" | "position" | "",
                "reason": str,
            }

        blocked_side meanings:
            "buy"  — only buy creation is blocked (position CB, over-long)
            "sell" — only sell creation is blocked (position CB, over-short)
            ""     — both sides blocked (price CB) OR no CB active
        """
        with self._cb_lock:
            return {
                "active": self._circuit_breaker_active,
                "blocked_side": self._circuit_breaker_blocked_side,
                "type": self._circuit_breaker_type,
                "reason": self._circuit_breaker_reason,
            }

    def should_enable_side(self, side: str, mid_price: Decimal = Decimal("0")) -> bool:
        """Check if a side should be enabled based on inventory and circuit breakers.

        Enforcement order:
          1. Circuit breaker (hard stop) — checked regardless of INVENTORY_ENABLED.
             If CB is active and this side is the blocked side, return False.
             The correcting (opposite) side is NEVER blocked by a position CB.
          2. Inventory soft limits (0.9× MAX_POSITION_XCH) — only when
             INVENTORY_ENABLED is True.
        """
        # --- 1. Circuit breaker (hard stop, always enforced) ---
        with self._cb_lock:
            cb_active = self._circuit_breaker_active
            blocked = self._circuit_breaker_blocked_side
        if cb_active:
            if blocked == "":
                # Full halt (price CB or unknown type) — both sides blocked
                return False
            if blocked == side:
                # Position CB — only the accumulating side is blocked
                return False
            # blocked != side: this is the correcting side — let it run

        # --- 2. Inventory soft limits ---
        if not cfg.INVENTORY_ENABLED:
            return True  # No inventory management

        if cfg.MAX_POSITION_XCH <= 0:
            return True

        price = mid_price
        if price <= 0 and self._price_engine:
            price = self._price_engine.get_last_price()
        if price <= 0:
            return True

        position_xch = self._net_position_cat * price

        # Use effective limit (accounts for inherited startup position)
        effective_limit = cfg.MAX_POSITION_XCH
        if self._startup_position_xch is not None and self._startup_position_xch > effective_limit:
            effective_limit = self._startup_position_xch * Decimal("1.1")

        # If we're at max long, disable buying (we have enough CAT)
        if side == "buy" and position_xch > effective_limit * Decimal("0.9"):
            return False

        # If we're at max short, disable selling
        if side == "sell" and position_xch < -effective_limit * Decimal("0.9"):
            return False

        return True

    # -------------------------------------------------------------------
    # Market Health Assessment (Dashboard Command Centre)
    # -------------------------------------------------------------------

    def get_market_health(self, loop_count: int = 0) -> Dict:
        """Evaluate overall market health for the dashboard traffic light.

        Args:
            loop_count: Current bot loop count. During the first few loops
                        (< 3) certain amber warnings are suppressed to let
                        the bot settle in (e.g. arb gap while sniper closes it).

        Returns a dict with:
        - status: "green", "amber", or "red"
        - message: Short human-readable summary
        - conditions: List of specific condition messages (for amber/red)
        - metrics: Key market numbers for display

        GREEN = everything normal
        AMBER = something needs attention but bot is operational
        RED   = critical issue, bot may not be functioning correctly
        """
        conditions = []
        metrics = {}

        # --- Gather spread data ---
        try:
            buy_spread = self.get_adjusted_spread("buy")
            sell_spread = self.get_adjusted_spread("sell")
            total_spread_bps = (buy_spread + sell_spread) * Decimal("10000")
            metrics["your_spread_bps"] = str(total_spread_bps)
            metrics["buy_spread_bps"] = str(buy_spread * Decimal("10000"))
            metrics["sell_spread_bps"] = str(sell_spread * Decimal("10000"))
        except Exception:
            buy_spread = sell_spread = Decimal("0")
            metrics["your_spread_bps"] = "0"
            metrics["buy_spread_bps"] = "0"
            metrics["sell_spread_bps"] = "0"

        # Actual inner spread: real bid-ask gap from live offers (not configured spread).
        # The configured spread is a target; the actual gap depends on which offers
        # are live, price anchoring, and rounding. This is what a taker actually sees.
        try:
            _bot = getattr(self, "_bot_ref", None)
            if _bot is None:
                # Try to get from module-level bot reference
                import bot_loop as _bl
                _bot = getattr(_bl, "bot", None)
            if _bot:
                _best_bid = Decimal(str(_bot._bot_state.get("our_best_bid", "0") or "0"))
                _best_ask = Decimal(str(_bot._bot_state.get("our_best_ask", "0") or "0"))
                _mid = Decimal(str(_bot._bot_state.get("mid_price", "0") or "0"))
                if _best_bid > 0 and _best_ask > 0 and _mid > 0:
                    _actual_gap_bps = (_best_ask - _best_bid) / _mid * Decimal("10000")
                    metrics["your_spread_bps"] = str(_actual_gap_bps)
        except Exception:
            pass  # Fall through to configured spread

        # --- Circuit breaker ---
        metrics["circuit_breaker_active"] = self._circuit_breaker_active
        metrics["circuit_breaker_reason"] = self._circuit_breaker_reason

        # --- Inventory & risk ---
        metrics["net_position_cat"] = str(self._net_position_cat)
        metrics["max_position_xch"] = str(cfg.MAX_POSITION_XCH)
        metrics["arb_gap_bps"] = str(self._arb_gap_bps)
        metrics["pool_depth_ratio"] = str(self._pool_depth_ratio)
        metrics["fill_rate_per_hour"] = str(self._recent_fill_rate)
        metrics["volatility"] = str(self._cached_volatility)

        # F44 (2026-04-08): wire Dexie v3 historical-trades market metrics
        # so the dashboard / Smart Settings / Advisor can compare the
        # bot's own fills against the broader market. trades_per_hour
        # is the *whole-market* fill rate for this CAT pair across all
        # Dexie traders. price_stdev_pct is the realised market
        # volatility (already used by _get_volatility but exposed here
        # too so the GUI can show "Market vs Bot"). All keys are
        # written even when v3 is unavailable so callers don't need
        # try/except — they get nullable strings.
        metrics["market_fill_rate_per_hour"] = None
        metrics["market_volatility_pct"] = None
        metrics["market_trades_in_window"] = None
        metrics["market_high_low_pct"] = None
        metrics["market_data_source"] = "unavailable"
        try:
            dm = getattr(self, "_dexie_manager", None)
            ticker_id = getattr(cfg, "CAT_TICKER_ID", "")
            if dm and ticker_id:
                v3 = dm.compute_v3_trade_metrics(
                    ticker_id,
                    hours=float(getattr(cfg, "VOLATILITY_WINDOW_HOURS", 24.0)),
                )
                if v3:
                    metrics["market_fill_rate_per_hour"] = f"{v3.get('trades_per_hour', 0):.3f}"
                    metrics["market_volatility_pct"] = f"{v3.get('price_stdev_pct', 0):.3f}"
                    metrics["market_trades_in_window"] = int(v3.get("trades_in_window", 0))
                    metrics["market_high_low_pct"] = f"{v3.get('high_low_pct', 0):.3f}"
                    metrics["market_data_source"] = "dexie_v3"
        except Exception:
            pass

        # --- Competitor & orderbook data (from market_intel if available) ---
        metrics["competitor_count"] = 0
        metrics["competitor_sides"] = "none"
        metrics["market_spread_bps"] = "0"
        metrics["overall_spread_bps"] = "0"
        metrics["market_intel_state"] = "searching"
        metrics["market_intel_refreshes"] = 0
        metrics["market_intel_age_secs"] = None
        if self._market_intel:
            try:
                summary = self._market_intel.get_market_summary()
                refreshes = int(summary.get("orderbook_refreshes", 0) or 0)
                metrics["market_intel_refreshes"] = refreshes
                metrics["market_intel_age_secs"] = summary.get("orderbook_age_secs")
                metrics["market_intel_state"] = "ready" if refreshes > 0 else "searching"

                # Use competitor-only counts (non-bot offers)
                comp_buys = int(summary.get("num_competitor_buys", 0))
                comp_sells = int(summary.get("num_competitor_sells", 0))
                comp_total = comp_buys + comp_sells
                metrics["competitor_count"] = comp_total

                if comp_buys > 0 and comp_sells > 0:
                    metrics["competitor_sides"] = "both"
                elif comp_buys > 0:
                    metrics["competitor_sides"] = "buy only"
                elif comp_sells > 0:
                    metrics["competitor_sides"] = "sell only"

                # Competitor spread (from non-bot best bid/ask)
                comp_spread = summary.get("competitor_spread_bps", "0")
                metrics["market_spread_bps"] = str(comp_spread)

                # Overall orderbook spread (including our own offers)
                metrics["overall_spread_bps"] = str(summary.get("overall_spread_bps", "0"))
            except Exception:
                pass

        # --- Evaluate RED conditions (critical) ---
        if self._circuit_breaker_active:
            conditions.append(("red", f"Circuit breaker tripped: {self._circuit_breaker_reason} — Go to Settings → Safety to review price limits and reset."))

        # Position vs limit
        try:
            max_pos = cfg.MAX_POSITION_XCH
            if max_pos and max_pos > 0 and self._price_engine:
                price = self._price_engine.get_last_price()
                if price > 0:
                    pos_xch = abs(self._net_position_cat * price)
                    pos_pct = (pos_xch / max_pos) * Decimal("100")
                    metrics["position_pct"] = str(pos_pct)
                    if pos_pct > Decimal("100"):
                        conditions.append(("red", f"Position limit breached ({pos_pct:.0f}% of limit) — Reduce MAX_POSITION_XCH in Settings → Risk or allow bot to rebalance."))
                    elif pos_pct > Decimal("80"):
                        conditions.append(("amber", f"Position nearing limit ({pos_pct:.0f}%) — Monitor closely; tighter spreads attract rebalancing fills."))
        except Exception:
            pass

        # --- Evaluate AMBER conditions (attention needed) ---

        # Spread at clamp limits
        try:
            if cfg.DYNAMIC_SPREAD_ENABLED:
                min_bps = cfg.MIN_SPREAD_BPS
                max_bps = cfg.MAX_SPREAD_BPS
                buy_bps = buy_spread * Decimal("10000")
                sell_bps = sell_spread * Decimal("10000")
                if buy_bps <= min_bps or sell_bps <= min_bps:
                    conditions.append(("amber", "Spread compressed to minimum clamp — raise MIN_SPREAD_BPS in Settings → Spread if this persists."))
                if buy_bps >= max_bps or sell_bps >= max_bps:
                    conditions.append(("amber", "High volatility — spreads widened to maximum clamp (protective). Reduce MAX_SPREAD_BPS only if comfortable with the risk."))
        except Exception:
            pass

        # Arb gap
        # Treat gaps above 2.0% as attention-worthy in Market Health.
        # Smaller gaps can be normal on thinner CAT pairs and shouldn't
        # flip the dashboard amber on their own.
        # Suppress during first 3 loops — the sniper is still actively
        # closing the gap and the bot needs time to settle.
        if self._arb_gap_bps > Decimal("200") and loop_count >= 3:
            conditions.append(("amber", f"Arb gap {_bps_to_pct(self._arb_gap_bps)} between Dexie & TibetSwap — sniper is active. Check SNIPER_* settings if this persists."))

        # Competitor count — informational only, more competitors = healthier market
        # (No amber/red condition — this is displayed as a metric, not a warning)

        # Pool depth concern
        if self._pool_depth_ratio > Decimal("0.05"):
            pct = self._pool_depth_ratio * 100
            conditions.append(("amber", f"Trade size {pct:.1f}% of TibetSwap pool (high price impact) — reduce INNER_SIZE_XCH in Settings → Tiers."))

        # --- Determine overall status ---
        has_red = any(lvl == "red" for lvl, _ in conditions)
        has_amber = any(lvl == "amber" for lvl, _ in conditions)

        if has_red:
            status = "red"
            message = next(txt for lvl, txt in conditions if lvl == "red")
        elif has_amber:
            amber_count = len([c for c in conditions if c[0] == "amber"])
            status = "amber"
            message = f"{amber_count} item{'s' if amber_count > 1 else ''} need{'s' if amber_count == 1 else ''} attention"
        else:
            status = "green"
            message = "Market healthy — bot operating normally"

        if metrics.get("market_intel_state") != "ready" and not has_red and not has_amber:
            message = "Searching market orderbook..."

        # During settling period (first 3 loops), show a calmer status
        if loop_count > 0 and loop_count < 3 and not has_red:
            if status == "green":
                message = "Bot settling in — calibrating spreads and market data..."
            # Don't override amber if there are non-arb-gap warnings (e.g. circuit breaker)

        return {
            "status": status,
            "message": message,
            "conditions": [{"level": lvl, "text": txt} for lvl, txt in conditions],
            "metrics": metrics,
        }

