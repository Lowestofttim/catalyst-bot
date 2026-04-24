"""Fast arb-closer that posts single probe offers at the best bid/ask on signal

`Sniper` reacts to drift signals from `mempool_watcher` and `amm_monitor` by
creating a single probe offer at the new best bid or ask and posting it to
Dexie immediately, bypassing the main-loop cadence. This closes the latency
gap between an observed AMM swap and our book being refreshed by the normal
cycle.

Key responsibilities:
    - Listen for mempool / AMM drift signals and decide whether to fire
    - Create at most 2 active sniper offers (max 1 per side)
    - Size each probe against inventory skew and the observed arb gap
    - Post directly to Dexie and track sniper fills separately in PnL

Offer-side dependencies (`offer_manager`, `risk_manager`, `dexie_manager`,
`splash_manager`) are injected via the constructor rather than imported at
module scope.
"""

import threading
import time
from decimal import Decimal
from typing import Optional, Dict, List

from config import cfg
from database import log_event, add_offer, lock_coin
from offer_manager import xch_to_mojos, cat_to_mojos, mojos_to_cat


def _bps_to_pct(val):
    """Convert a BPS value to a formatted % string."""
    try:
        n = float(val) / 100
        if n < 1:
            return f"{n:.2f}%"
        return f"{n:.1f}%"
    except (ValueError, TypeError):
        return str(val)


class Sniper:
    """Fast arb closer — creates immediate offers when price swaps detected.

    Called by the price watcher (or bot loop) when TibetSwap activity is
    detected. Creates a single buy+sell at best bid/ask and posts to Dexie
    immediately (bypassing the queue for speed).
    """

    def __init__(self, offer_manager=None, risk_manager=None, dexie_manager=None,
                 splash_manager=None):
        self._offer_manager = offer_manager
        self._risk_manager = risk_manager
        self._dexie_manager = dexie_manager
        self._splash_manager = splash_manager

        # Cooldown tracking
        self._last_snipe_time: float = 0

        # Edge-only sniper mode: at most one live sniper per side.
        # The sniper is for price discovery, not a second mini ladder.
        self._active_snipe_ids: List[str] = []
        self._active_snipe_sides: Dict[str, str] = {}  # trade_id -> "buy"/"sell"
        self._max_active_snipes: int = 2  # Total cap: one buy + one sell
        self._max_per_side: int = 1  # Per-side cap: edge probe only

        # Stats
        self._total_snipes: int = 0
        self._total_skipped: int = 0
        self._snipe_history: List[Dict] = []
        self._max_history: int = 50

        # Thread safety — watcher thread writes, main loop reads/prunes
        self._snipe_lock = threading.Lock()

    def try_snipe(self, bid_price: Decimal, ask_price: Decimal,
                  arb_gap_bps: Decimal = Decimal("0")) -> List[Dict]:
        """Attempt to create sniper offers at given bid/ask prices.

        Args:
            bid_price: Best bid price (XCH per CAT) — our buy price
            ask_price: Best ask price (XCH per CAT) — our sell price
            arb_gap_bps: Current arb gap in BPS (for size scaling)

        Returns list of created sniper offers (may be empty).
        """
        # Liquidity mode gate — arb discovery requires both sides of the
        # book, so sniper is a no-op in single-sided modes. Silent early
        # return so we don't clutter the skip counter with configs that
        # could never fire.
        _mode = (getattr(cfg, "LIQUIDITY_MODE", "two_sided") or "two_sided").lower()
        if _mode in ("buy_only", "sell_only"):
            return []

        now = time.time()

        # Cooldown check
        if (now - self._last_snipe_time) < cfg.SNIPER_COOLDOWN_SECS:
            return []

        # Cap check + per-side counts — read under the lock so they agree
        # with writes from other threads (watcher vs main loop).
        with self._snipe_lock:
            if len(self._active_snipe_ids) >= self._max_active_snipes:
                return []
            _buy_count = sum(1 for s in self._active_snipe_sides.values() if s == "buy")
            _sell_count = sum(1 for s in self._active_snipe_sides.values() if s == "sell")

        # Skip if coin operations are running
        if self._offer_manager and hasattr(self._offer_manager, '_lock'):
            pass  # Offer manager handles its own locking

        # Skip if circuit breaker is active.
        # try_snipe creates both sides, so a full halt blocks everything.
        # Side-specific blocking is handled per-side in the buy/sell blocks below.
        if self._risk_manager and self._risk_manager.is_full_halt():
            self._total_skipped += 1
            return []

        # Validate prices
        if bid_price <= 0 or ask_price <= 0:
            return []

        # Size determination — V2 scales with arb gap
        trade_xch = self._calculate_snipe_size(arb_gap_bps)

        created = []

        # Determine which side (if any) is blocked by a position circuit breaker
        _cb_blocked_side = (
            self._risk_manager.get_circuit_breaker_blocked_side()
            if self._risk_manager else None
        )

        # ---- Sniper BUY (per-side cap check) ----
        if (cfg.ENABLE_BUY and _buy_count < self._max_per_side
                and self._should_snipe_side("buy")
                and _cb_blocked_side != "buy"):
            buy_result = self._create_snipe_offer("buy", bid_price, trade_xch)
            if buy_result:
                created.append(buy_result)

        # ---- Sniper SELL (per-side cap check) ----
        if (cfg.ENABLE_SELL and _sell_count < self._max_per_side
                and self._should_snipe_side("sell")
                and _cb_blocked_side != "sell"):
            sell_result = self._create_snipe_offer("sell", ask_price, trade_xch)
            if sell_result:
                created.append(sell_result)

        self._publish_immediately(created)

        if created:
            self._last_snipe_time = now
            self._total_snipes += 1

            # Track active sniper offer IDs with side and record history
            with self._snipe_lock:
                for offer in created:
                    tid = offer.get("trade_id", "")
                    side = offer.get("side", "")
                    if tid:
                        self._active_snipe_ids.append(tid)
                        if side:
                            self._active_snipe_sides[tid] = side

                for offer in created:
                    self._snipe_history.insert(0, {
                        "side": offer.get("side"),
                        "price": str(offer.get("price")),
                        "size_xch": str(trade_xch),
                        "arb_gap_bps": str(arb_gap_bps),
                        "timestamp": now,
                    })

                # In-place trim so readers holding a reference still see
                # the same list object.
                if len(self._snipe_history) > self._max_history:
                    del self._snipe_history[self._max_history:]

            log_event("info", "sniper_fired",
                      f"⚡ Sniper created {len(created)} offers "
                      f"(arb gap: {_bps_to_pct(arb_gap_bps)}, size: {trade_xch} XCH)")

        return created

    def try_snipe_single(self, side: str, price: Decimal,
                          arb_gap_bps: Decimal = Decimal("0")) -> List[Dict]:
        """Create a single sniper offer on ONE side at a specific price.

        V2 rework: Instead of placing both buy+sell at mid-prices that
        nobody would take, this places ONE aggressive offer on the side
        that's mispriced after a TibetSwap swap.

        The price should be competitive — e.g., just above Tibet for a buy,
        just below Tibet for a sell — so it becomes the best bid/ask on Dexie.

        Args:
            side: "buy" or "sell"
            price: The aggressive price to place the offer at
            arb_gap_bps: Current arb gap in BPS (for size scaling)

        Returns list with 0 or 1 created sniper offers.
        """
        # Liquidity mode gate — see try_snipe for rationale.
        _mode = (getattr(cfg, "LIQUIDITY_MODE", "two_sided") or "two_sided").lower()
        if _mode in ("buy_only", "sell_only"):
            return []

        now = time.time()

        # Cooldown check
        if (now - self._last_snipe_time) < cfg.SNIPER_COOLDOWN_SECS:
            return []

        # Global cap + per-side count — read under the lock for consistency
        with self._snipe_lock:
            if len(self._active_snipe_ids) >= self._max_active_snipes:
                return []
            _side_count = sum(1 for s in self._active_snipe_sides.values() if s == side)
        if _side_count >= self._max_per_side:
            return []

        # Circuit breaker check — side-aware so the correcting side is not blocked
        if self._risk_manager:
            if self._risk_manager.is_full_halt():
                # Full halt (both sides blocked) — stop all sniping
                self._total_skipped += 1
                return []
            blocked_side = self._risk_manager.get_circuit_breaker_blocked_side()
            if blocked_side and blocked_side == side:
                # Only this side is blocked (position CB) — the correcting side continues
                self._total_skipped += 1
                return []

        # Validate
        if price <= 0:
            return []

        # Inventory check for this specific side
        if not self._should_snipe_side(side):
            self._total_skipped += 1
            return []

        # Size determination
        trade_xch = self._calculate_snipe_size(arb_gap_bps)

        # Create the offer
        result = self._create_snipe_offer(side, price, trade_xch)

        created = [result] if result else []

        self._publish_immediately(created)

        if created:
            self._last_snipe_time = now
            self._total_snipes += 1

            with self._snipe_lock:
                for offer in created:
                    tid = offer.get("trade_id", "")
                    if tid:
                        self._active_snipe_ids.append(tid)
                        self._active_snipe_sides[tid] = side

                self._snipe_history.insert(0, {
                    "side": side,
                    "price": str(price),
                    "size_xch": str(trade_xch),
                    "arb_gap_bps": str(arb_gap_bps),
                    "timestamp": now,
                    "mode": "single_side",
                })
                if len(self._snipe_history) > self._max_history:
                    del self._snipe_history[self._max_history:]

            log_event("info", "sniper_fired",
                      f"⚡ Sniper {side.upper()} at {price:.8f} "
                      f"(arb gap: {_bps_to_pct(arb_gap_bps)}, size: {trade_xch} XCH)")

        return created

    def _calculate_snipe_size(self, arb_gap_bps: Decimal) -> Decimal:
        """Calculate the dedicated sniper offer size.

        Sniper offers use their own configured pool size rather than reusing
        the main ladder trade size. This lets the bot keep a tiny set of
        probe/arb coins separate from the normal trading tiers.
        """
        del arb_gap_bps
        base = getattr(cfg, "SNIPER_SIZE_XCH", None)
        if base is None or Decimal(str(base)) <= 0:
            base = cfg.DEFAULT_TRADE_XCH
        base = Decimal(str(base))
        return min(base, cfg.MAX_TRADE_XCH)

    def _publish_immediately(self, created: List[Dict]):
        """Push sniper probes to Dexie first, then Splash as broadcast follow-up."""
        if not created:
            return

        for offer in created:
            bech32 = offer.get("offer_bech32", "")
            trade_id = offer.get("trade_id", "")
            if not bech32 or not trade_id:
                continue

            if self._dexie_manager and cfg.DEXIE_AUTO_POST:
                try:
                    self._dexie_manager._post_single(bech32, trade_id, force=True)
                except Exception as e:
                    log_event("warning", "sniper_dexie_failed",
                              f"Sniper Dexie post failed for {trade_id[:12]}...: {e}")

            if self._splash_manager and getattr(cfg, "SPLASH_ENABLED", False):
                try:
                    self._splash_manager._post_single(bech32, trade_id, force=True)
                except Exception as e:
                    log_event("warning", "sniper_splash_failed",
                              f"Sniper Splash post failed for {trade_id[:12]}...: {e}")

    def _should_snipe_side(self, side: str) -> bool:
        """Check if sniping this side is allowed (inventory check).

        V2: Won't snipe if it would push inventory too far.
        """
        if not self._risk_manager:
            return True

        if not cfg.INVENTORY_ENABLED:
            return True

        # Use risk manager's side enablement check
        return self._risk_manager.should_enable_side(side)

    def _create_snipe_offer(self, side: str, price: Decimal,
                             trade_xch: Decimal) -> Optional[Dict]:
        """Create a single sniper offer.

        Returns offer detail dict, or None if creation failed.
        """
        if not self._offer_manager:
            return None

        cat_amount = trade_xch / price
        cat_mojos = cat_to_mojos(cat_amount, cfg.CAT_DECIMALS)
        cat_amount = mojos_to_cat(cat_mojos, cfg.CAT_DECIMALS)
        xch_mojos = xch_to_mojos(trade_xch)

        # Amount validation — reject zero, negative, or absurdly large values
        if int(cat_mojos) <= 0 or int(xch_mojos) <= 0:
            log_event("warning", "sniper_bad_amount",
                      f"⚡ Sniper {side} rejected: invalid mojos "
                      f"(cat={cat_mojos}, xch={xch_mojos}, price={price})")
            return None
        if int(xch_mojos) > 1_000_000_000_000_000:  # > 1000 XCH sanity cap
            log_event("warning", "sniper_bad_amount",
                      f"⚡ Sniper {side} rejected: xch_mojos too large ({xch_mojos})")
            return None

        if side == "buy":
            offer_dict = {
                str(cfg.WALLET_ID_XCH): -int(xch_mojos),
                str(cfg.CAT_WALLET_ID): int(cat_mojos)
            }
        else:
            offer_dict = {
                str(cfg.CAT_WALLET_ID): -int(cat_mojos),
                str(cfg.WALLET_ID_XCH): int(xch_mojos)
            }

        if cfg.DRY_RUN:
            log_event("info", "sniper_dry_run",
                      f"⚡ [DRY RUN] Would snipe {side} at {price:.8f}")
            return None

        # 30-min expiry — sniper offers auto-cleanup if cancel fails
        _sniper_expiry = cfg.SNIPER_EXPIRY_SECS if cfg.SNIPER_EXPIRY_SECS > 0 else None
        res = self._offer_manager.create_offer_with_retry(
            offer_dict,
            expiry_secs=_sniper_expiry,
            coin_ids_enabled=cfg.COIN_IDS_ENABLED,
            preferred_tier="sniper",
            strict_preferred_tier=True,
        )
        if not res or not res.get("success"):
            log_event("warning", "sniper_failed",
                      f"⚡ Sniper {side} creation failed: {res}")
            return None

        trade_record = res.get("trade_record") or {}
        trade_id = res.get("trade_id") or trade_record.get("trade_id") or ""
        offer_bech32 = res.get("offer", "")
        locked_coin_id = res.get("locked_coin_id")

        # Record to database so fill_tracker has price/size for PnL matching
        import datetime
        _exp_dt = None
        if _sniper_expiry:
            _exp_dt = (datetime.datetime.now(datetime.timezone.utc) +
                       datetime.timedelta(seconds=_sniper_expiry)).isoformat()
        if trade_id:
            db_ok = add_offer(
                trade_id=trade_id,
                side=side,
                price_xch=price,
                size_xch=trade_xch,
                size_cat=cat_amount,
                cat_asset_id=cfg.CAT_ASSET_ID,
                tier="sniper",
                expires_at=_exp_dt,
                coin_id=locked_coin_id,
            )
            if not db_ok:
                # DB insert failed — cancel on-chain offer to prevent wallet/DB divergence
                log_event("error", "sniper_db_cancel",
                          f"DB insert failed for sniper {trade_id[:16]}..., cancelling on-chain offer")
                try:
                    self._offer_manager.cancel_offers([trade_id], reason="db_insert_failed")
                except Exception as _cancel_err:
                    # Compensating cancel failed — this IS the scenario the
                    # cancel was meant to prevent (wallet/DB divergence). Log
                    # loudly so recovery/reconciliation can clean it up later.
                    log_event("error", "sniper_compensating_cancel_failed",
                              f"Sniper compensating cancel FAILED for "
                              f"{trade_id[:16]}... — offer now orphaned in "
                              f"wallet (not in DB): {_cancel_err}")
                return None
            if locked_coin_id:
                lock_coin(locked_coin_id, trade_id)
                # Register in cycle exclusion set so ladder won't re-select this coin
                self._offer_manager._cycle_used_coin_ids.add(locked_coin_id)
            # Also cache details for fill_tracker's offer_details_cache
            if self._offer_manager:
                self._offer_manager._offer_details_cache[trade_id] = {
                    "price": price,
                    "size_xch": trade_xch,
                    "size_cat": cat_amount,
                    "tier": "sniper",
                    "dexie_link": "",
                }

        log_event("info", "sniper_created",
                  f"⚡ Sniper {side.upper()} at {price:.8f} XCH "
                  f"({trade_xch} XCH / {cat_amount:.2f} CAT)")

        return {
            "trade_id": trade_id,
            "side": side,
            "price": price,
            "size_xch": trade_xch,
            "size_cat": cat_amount,
            "offer_bech32": offer_bech32,
            "coin_id": locked_coin_id,
            "is_sniper": True,
        }

    # -------------------------------------------------------------------
    # State queries
    # -------------------------------------------------------------------

    def prune_active_snipes(self, open_trade_ids: set):
        """Remove sniper IDs that are no longer open (filled or cancelled).

        Call this each cycle with the set of currently open trade IDs.
        Keeps the active snipe count accurate so the cap works properly.
        """
        with self._snipe_lock:
            before = len(self._active_snipe_ids)
            removed_ids = [tid for tid in self._active_snipe_ids if tid not in open_trade_ids]
            self._active_snipe_ids = [
                tid for tid in self._active_snipe_ids if tid in open_trade_ids
            ]
            # Also clean up the per-side tracking dict
            for tid in removed_ids:
                self._active_snipe_sides.pop(tid, None)
            pruned = before - len(self._active_snipe_ids)
            if pruned > 0:
                _buy_active = sum(1 for s in self._active_snipe_sides.values() if s == "buy")
                _sell_active = sum(1 for s in self._active_snipe_sides.values() if s == "sell")
                log_event("debug", "sniper_pruned",
                          f"Pruned {pruned} closed sniper offers "
                          f"({_buy_active}b/{_sell_active}s active, "
                          f"{len(self._active_snipe_ids)}/{self._max_active_snipes} total)")

    def get_stats(self) -> Dict:
        """Get sniper statistics for GUI (thread-safe snapshot)."""
        with self._snipe_lock:
            active_count = len(self._active_snipe_ids)
            recent = list(self._snipe_history[:10])
        return {
            "total_snipes": self._total_snipes,
            "total_skipped": self._total_skipped,
            "active_snipes": active_count,
            "max_active_snipes": self._max_active_snipes,
            "cooldown_secs": cfg.SNIPER_COOLDOWN_SECS,
            "size_xch": str(getattr(cfg, "SNIPER_SIZE_XCH", "0.001")),
            "last_snipe_time": self._last_snipe_time,
            "recent_snipes": recent,
        }

