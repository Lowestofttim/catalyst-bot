"""Adaptive gap-closer that probes for the tightest safe spread, then cascades

`BoostManager` maintains a single pair of probe offers (1 buy + 1 sell) that
start wide and tighten cycle by cycle until they hit the arb-profitability
floor. Once the floor is proven, the main book is physically cascaded behind
the probe price so the visible ladder follows the discovered best spread
without ever overshooting into arb territory.

Key responsibilities:
    - Maintain the 1-buy / 1-sell probe pair (create, refresh, cancel)
    - Step the probe spread tighter on survival and wider on fill
    - Detect the arb floor from TibetSwap's gap and hold there
    - Cascade inner-tier main-book offers behind the proven price

Offer-side dependencies (`risk_manager`, `dexie_manager`, `offer_manager`)
are injected through the constructor; this module holds no direct imports
of them at module scope.
"""

import time
import threading
from decimal import Decimal
from typing import Optional, Dict, List

from config import cfg
from database import log_event, add_offer, lock_coin, get_open_offers as db_get_open_offers
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


class BoostManager:
    """Adaptive gap-closing offers to improve Dexie ranking position.

    Gap-closer offers start wide and probe tighter over time, using
    TibetSwap's arb gap as an intelligent floor. Tracked separately
    from normal offers (tier="boost" in database).
    """

    def __init__(self, offer_manager=None, dexie_manager=None,
                 risk_manager=None):
        self._offer_manager = offer_manager
        self._dexie_manager = dexie_manager
        self._risk_manager = risk_manager

        # Re-entrant lock protecting mutation of _active_boost_ids,
        # _boost_active, _gap_spread_bps, _boost_id_expiry, convergence,
        # and cascade tracking.  RLock so helpers called under the lock
        # (e.g. _create_gap_closer_pair → _create_single_offer) can
        # re-acquire without deadlocking.
        self._lock = threading.RLock()

        # State
        self._boost_active: bool = False
        self._active_boost_ids: List[str] = []
        self._boost_mid_price: Decimal = Decimal("0")  # Price of current offers

        # ---- Adaptive gap-closer spread (changes over time!) ----
        self._gap_spread_bps: int = 0        # Current gap-closer spread
        self._start_spread_bps: int = 0      # What we started at (ceiling)
        self._arb_floor_bps: int = 0         # arb gap + buffer (floor)
        self._steps_taken: int = 0           # How many tightening steps done
        self._custom_size_xch: Optional[Decimal] = None  # User override

        # Stats
        self._total_refreshes: int = 0
        self._total_arb_warnings: int = 0
        self._arb_count: int = 0             # Times arbed since activation

        # ---- Stability tracking ----
        self._stable_since: float = 0        # When offers last became stable
        self._last_step_time: float = 0      # When last step was taken

        # ---- Expiry tracking ----
        # Maps trade_id → expected expiry timestamp (unix seconds).
        # Used by prune_active_boosts to distinguish natural expiry from arb fills.
        self._boost_id_expiry: dict = {}

        # ---- Cascade tracking ----
        # After the probe proves a level (~60s), the main book requotes
        # behind it. We track which spread level was already cascaded
        # to avoid re-triggering on the same level.
        self._cascade_done_at_spread: int = -1  # Spread BPS last cascaded at
        self._cascade_count: int = 0            # Total cascades this session

        # ---- Main book convergence ----
        # As gap-closer proves safe levels, the main book's spread converges
        self._convergence_factor: Decimal = Decimal("1.0")  # 1.0 = no change
        self._convergence_min: Decimal = Decimal("0.15")     # Floor: 15% of original
        self._last_convergence_time: float = 0

        # ---- Vulnerability flag ----
        # Set when a probe gets arbed — signals the bot loop to check
        # whether inner-tier main-book offers are also exposed.
        self._inner_vulnerability_flag: bool = False

    # -------------------------------------------------------------------
    # Activate / Deactivate
    # -------------------------------------------------------------------

    def activate(self, mid_price: Decimal, arb_gap_bps: Decimal = Decimal("0"),
                 main_spread_bps: int = 0,
                 size_xch_override: Optional[Decimal] = None,
                 start_pct_override: Optional[int] = None) -> Dict:
        """Turn gap-closer ON — creates initial offers and begins probing.

        Args:
            mid_price: Current mid price to centre offers around
            arb_gap_bps: Current arb gap in BPS (TibetSwap vs Dexie)
            main_spread_bps: Current main book spread in BPS (for calculating start)
            size_xch_override: Custom offer size (None = use config)
            start_pct_override: Custom starting % of main spread (None = use config)

        Returns dict with results and any warnings.
        """
        if self._boost_active and self._active_boost_ids:
            return {
                "success": False,
                "error": "Close the Gap already active",
                "active_count": len(self._active_boost_ids),
            }

        if mid_price <= 0:
            return {"success": False, "error": "No valid mid price available"}

        # Circuit breaker check — refuse to create pair if either side blocked.
        if self._cb_blocks_boost():
            return {
                "success": False,
                "error": "Circuit breaker active — Close the Gap cannot create offers",
            }

        # Store user overrides
        self._custom_size_xch = size_xch_override

        # Calculate starting spread
        start_pct = start_pct_override or getattr(cfg, "GAP_CLOSE_START_PCT", 75)
        if main_spread_bps > 0:
            # Start at X% of main book spread
            self._gap_spread_bps = max(1, int(main_spread_bps * start_pct / 100))
        else:
            # Fallback if no main spread available
            self._gap_spread_bps = getattr(cfg, "BOOST_SPREAD_BPS", 200)

        self._start_spread_bps = self._gap_spread_bps  # Remember initial spread

        # Widen ceiling — when probes get arbed, the spread widens by 20% per
        # arb. Capped at the LARGER of (start spread) and (main book spread)
        # so an aggressive find-floor start (e.g. 30bps) can still widen up
        # toward the original ladder spread (e.g. 480bps) if every probe
        # gets eaten. Without this, an aggressive start gets stuck at the
        # initial value because each arb-widen is clamped back down to 30bps.
        self._widen_ceiling_bps = max(self._start_spread_bps, max(1, int(main_spread_bps)))

        # Calculate arb floor (never go tighter than this)
        buffer = getattr(cfg, "GAP_CLOSE_SAFETY_BUFFER_BPS", 20)
        self._arb_floor_bps = max(1, int(arb_gap_bps) + buffer)

        # Clamp starting spread: can't start below the arb floor
        if self._gap_spread_bps < self._arb_floor_bps:
            self._gap_spread_bps = self._arb_floor_bps

        # Warnings
        warnings = []
        if int(arb_gap_bps) > self._gap_spread_bps:
            warnings.append(
                f"Arb gap ({_bps_to_pct(int(arb_gap_bps))}) is wider than starting spread "
                f"({_bps_to_pct(self._gap_spread_bps)}) — offers may be arbed initially"
            )
            self._total_arb_warnings += 1

        # Create the initial offers
        created = self._create_gap_closer_pair(mid_price)

        if created:
            self._boost_active = True
            self._stable_since = time.time()
            self._steps_taken = 0
            self._arb_count = 0
            self._last_step_time = time.time()
            self._subprobe_attempted = False  # reset for this run
            # Reset convergence
            self._convergence_factor = Decimal("1.0")

            log_event("info", "gap_closer_activated",
                      f"📈 Close the Gap ON — {len(created)} offers at "
                      f"{_bps_to_pct(self._gap_spread_bps)} "
                      f"(target floor: {_bps_to_pct(self._arb_floor_bps)})",
                      data={"spread_bps": self._gap_spread_bps, "arb_floor_bps": self._arb_floor_bps,
                            "steps_taken": 0, "start_spread_bps": self._start_spread_bps})
            print(f"📈 Close the Gap ON: {len(created)} offers at "
                  f"{_bps_to_pct(self._gap_spread_bps)} spread "
                  f"(started at {start_pct}% of main book, "
                  f"arb floor: {_bps_to_pct(self._arb_floor_bps)})", flush=True)

        size_xch = self._effective_size_xch()
        half_spread = Decimal(str(self._gap_spread_bps)) / Decimal("20000")
        buy_price = mid_price * (Decimal("1") - half_spread)
        sell_price = mid_price * (Decimal("1") + half_spread)

        return {
            "success": len(created) > 0,
            "created": len(created),
            "buy_price": str(buy_price),
            "sell_price": str(sell_price),
            "spread_bps": self._gap_spread_bps,
            "start_spread_bps": self._start_spread_bps,
            "arb_floor_bps": self._arb_floor_bps,
            "main_spread_bps": main_spread_bps,
            "size_xch": str(size_xch),
            "warnings": warnings,
        }

    def _effective_size_xch(self) -> Decimal:
        """Return offer size — custom override, then SNIPER_SIZE_XCH (same pool)."""
        if self._custom_size_xch is not None:
            return self._custom_size_xch
        return Decimal(str(getattr(cfg, "SNIPER_SIZE_XCH", "0.001")))

    def _cb_blocks_boost(self) -> bool:
        """Return True if the circuit breaker should block boost activity.

        Boost creates a buy+sell PAIR, so we must pause entirely if either
        side is blocked (partial halts included).  A blocked side would leave
        only one leg on the book, defeating the gap-closer premise and
        potentially worsening the imbalance that tripped the CB.
        """
        rm = self._risk_manager
        if rm is None:
            return False
        try:
            if rm.is_full_halt():
                return True
            blocked = rm.get_circuit_breaker_blocked_side() or ""
            if blocked in ("buy", "sell"):
                return True
        except Exception:
            # Fail open on unexpected errors — boost already has a refresh
            # cycle that will catch up once the CB surfaces correctly.
            return False
        return False

    def deactivate(self, preserve_convergence: bool = False) -> Dict:
        """Turn gap-closer OFF — cancel boost offers and reset state.

        Args:
            preserve_convergence: If True (auto-stop at floor), keep the
                convergence factor so the main book stays at the proven
                tighter spread.  The incremental reaction strategy will
                adjust the remaining ladder naturally.
                If False (manual stop / error), reset to 1.0 so the main
                book returns to its original spread immediately.
        """
        with self._lock:
            if not self._boost_active and not self._active_boost_ids:
                return {"success": True, "message": "Close the Gap already inactive"}

            cancelled = 0
            failed = 0

            # Snapshot IDs under the lock so the network cancel runs on a
            # stable list (we release the lock for the cancel RPC below to
            # avoid holding it across a wallet call).
            to_cancel = list(self._active_boost_ids)
            offer_mgr = self._offer_manager

        if to_cancel and offer_mgr:
            for tid in to_cancel:
                offer_mgr._bot_cancelled_ids.add(tid)
            result = offer_mgr.cancel_offers(
                to_cancel, reason="gap_closer_deactivate",
                skip_confirmation=True,
            )
            # cancel_offers returns {trade_id: {"success": bool, ...}, ...}
            # — NOT {"cancelled": N, "failed": N}. Count by iterating values.
            if isinstance(result, dict):
                for _tid, _res in result.items():
                    if isinstance(_res, dict) and _res.get("success"):
                        cancelled += 1
                    else:
                        failed += 1

        with self._lock:
            self._boost_active = False
            self._active_boost_ids.clear()
            self._boost_mid_price = Decimal("0")
            self._gap_spread_bps = 0
            self._start_spread_bps = 0
            self._arb_floor_bps = 0
            self._custom_size_xch = None
            if preserve_convergence:
                # Keep convergence factor — ladder will catch up via
                # incremental reaction strategy over the next cycles.
                log_event("info", "gap_closer_convergence_preserved",
                          f"📈 Convergence factor preserved at "
                          f"{self._convergence_factor:.2f} after floor handoff")
            else:
                # Manual stop — reset to original spread immediately
                self._convergence_factor = Decimal("1.0")
            self._stable_since = 0
            self._cascade_done_at_spread = -1
            steps_taken = self._steps_taken
            arb_count = self._arb_count

        mode = "floor handoff — convergence preserved" if preserve_convergence \
               else "spread reset to normal"
        log_event("info", "gap_closer_deactivated",
                  f"📈 Close the Gap OFF — cancelled {cancelled} offers, {mode}")
        print(f"📈 Close the Gap OFF: cancelled {cancelled} offers "
              f"({steps_taken} steps taken, "
              f"{arb_count} times arbed) — {mode}", flush=True)

        return {
            "success": True,
            "cancelled": cancelled,
            "failed": failed,
        }

    # -------------------------------------------------------------------
    # Floor handoff — plant inner-tier offers at the proven safe price
    # -------------------------------------------------------------------

    def _handoff_to_inner_tier(self):
        """When the gap-closer finds the floor, create real inner-tier
        offers at the proven safe price and cancel the furthest inner-tier
        offers to maintain the ladder's offer count.

        The incremental reaction strategy will then naturally adjust
        the remaining ladder over the following cycles.
        """
        om = self._offer_manager
        if not om or not self._risk_manager:
            log_event("warning", "gap_closer_handoff_skip",
                      "📈 Handoff skipped — missing offer_manager or risk_manager")
            return

        mid_price = self._boost_mid_price
        if mid_price <= 0:
            return

        # Hand off at the TIGHTEST spread the probes survived. If the below-
        # floor sub-probe ran and is still alive (gap_spread < arb_floor with
        # no widening having pushed us back up), use that — it's a stronger
        # safety proof than the calculated floor. Otherwise fall back to the
        # calculated floor, which is what we held stably.
        proven_spread_bps = min(self._gap_spread_bps, self._arb_floor_bps) \
            if self._gap_spread_bps > 0 else self._arb_floor_bps
        handoff_count = 0

        for side in ("buy", "sell"):
            if side == "buy" and not cfg.ENABLE_BUY:
                continue
            if side == "sell" and not cfg.ENABLE_SELL:
                continue

            # --- Step 1: Find the furthest inner-tier offer to swap out ---
            try:
                open_offers = db_get_open_offers(side=side, cat_asset_id=cfg.CAT_ASSET_ID)
            except Exception:
                open_offers = []

            # Filter to main-book inner-tier offers only (exclude boost/sniper)
            inner_offers = [
                o for o in open_offers
                if str(o.get("tier", "mid")).lower() == "inner"
                   and o.get("trade_id") not in set(self._active_boost_ids)
            ]

            if not inner_offers:
                # No inner offers to swap — just create a new one
                pass

            # Sort inner offers by distance from mid (furthest first)
            for o in inner_offers:
                p = None
                tid = o.get("trade_id", "")
                if tid:
                    cached = om._offer_details_cache.get(tid, {})
                    p = cached.get("price")
                if p is not None:
                    try:
                        o["_distance"] = abs(Decimal(str(p)) - mid_price)
                    except Exception:
                        o["_distance"] = Decimal("0")
                else:
                    o["_distance"] = Decimal("0")
            inner_offers.sort(key=lambda o: o.get("_distance", 0), reverse=True)

            # --- Step 2: Create the new inner offer at the proven price ---
            try:
                # Use normal ladder create for a single inner-tier offer
                new_offers = om.create_ladder(
                    mid_price, side,
                    num_offers=1,
                    spread_fraction=Decimal(str(proven_spread_bps)) / Decimal("10000"),
                    risk_manager=self._risk_manager,
                    coin_ids_enabled=cfg.COIN_IDS_ENABLED
                )
                created = len(new_offers) if new_offers else 0
            except Exception as e:
                log_event("warning", "gap_closer_handoff_create_fail",
                          f"📈 Handoff {side} create failed: {e}")
                created = 0

            if created == 0:
                log_event("info", "gap_closer_handoff_no_coins",
                          f"📈 Handoff {side}: no spare coins — "
                          f"ladder will catch up via reaction strategy")
                continue

            # Post new offer to Dexie
            if new_offers and self._dexie_manager and cfg.DEXIE_AUTO_POST:
                for offer in new_offers:
                    bech32 = offer.get("offer_bech32", offer.get("offer", ""))
                    trade_id = offer.get("trade_id", "")
                    if bech32 and trade_id:
                        self._dexie_manager.queue_post(bech32, trade_id)

            handoff_count += created

            # --- Step 3: Cancel the furthest inner offer to maintain count ---
            if inner_offers:
                furthest = inner_offers[0]
                cancel_tid = furthest.get("trade_id")
                if cancel_tid:
                    om._bot_cancelled_ids.add(cancel_tid)
                    om.cancel_offers([cancel_tid], reason="gap_closer_handoff_swap",
                                     skip_confirmation=True)
                    log_event("info", "gap_closer_handoff_swap",
                              f"📈 Handoff {side}: planted inner offer at "
                              f"{_bps_to_pct(proven_spread_bps)}, "
                              f"cancelled furthest inner {cancel_tid[:16]}…")
                    print(f"📈 Handoff {side}: swapped furthest inner for "
                          f"new offer at {_bps_to_pct(proven_spread_bps)}",
                          flush=True)
            else:
                log_event("info", "gap_closer_handoff_new",
                          f"📈 Handoff {side}: planted new inner offer at "
                          f"{_bps_to_pct(proven_spread_bps)} (no existing inner to swap)")
                print(f"📈 Handoff {side}: new inner offer at "
                      f"{_bps_to_pct(proven_spread_bps)}", flush=True)

        if handoff_count > 0:
            log_event("info", "gap_closer_handoff_complete",
                      f"📈 Floor handoff complete: {handoff_count} inner-tier "
                      f"offer(s) planted at {_bps_to_pct(proven_spread_bps)}. "
                      f"Ladder will adjust via incremental reaction strategy.",
                      data={"proven_spread_bps": proven_spread_bps,
                            "handoff_count": handoff_count})

    # -------------------------------------------------------------------
    # Adaptive step — gradually probe tighter
    # -------------------------------------------------------------------

    def step_tighter(self, current_arb_gap_bps: Decimal) -> bool:
        """Check if gap-closer offers should tighten one step.

        Called each bot loop cycle when gap-closer is active.
        Tightens the gap-closer spread by STEP_PCT if offers have survived
        the cooldown period without being arbed.

        Args:
            current_arb_gap_bps: Latest arb gap from price engine

        Returns True if a step was taken (offers recreated at tighter spread).
        """
        if not self._boost_active or self._gap_spread_bps == 0:
            return False

        # Circuit breaker — skip stepping while CB is active. Existing offers
        # stay put; they will be refreshed (or not) next cycle once CB clears.
        if self._cb_blocks_boost():
            return False

        now = time.time()

        # Update arb floor from latest data
        buffer = getattr(cfg, "GAP_CLOSE_SAFETY_BUFFER_BPS", 20)
        self._arb_floor_bps = max(1, int(current_arb_gap_bps) + buffer)

        # Need active offers for stability proof
        if len(self._active_boost_ids) == 0:
            return False

        # Stability check: offers must have survived N seconds
        cooldown = getattr(cfg, "GAP_CLOSE_STEP_COOLDOWN_SECS", 300)
        if self._stable_since == 0:
            self._stable_since = now
            return False
        if (now - self._stable_since) < cooldown:
            return False

        # Cooldown since last step
        if (now - self._last_step_time) < cooldown:
            return False

        # Already at or below the arb floor?
        # If we've also passed both cooldown guards to get here, that means
        # offers have been sitting at the floor for a full cooldown with no arb.
        # Try ONE below-floor sub-probe to discover the *real* floor — the
        # calculated floor is just (arb_gap + buffer), which may overestimate
        # what the market actually punishes. If the sub-probe survives, the
        # real floor is below us and we can keep going. If it gets arbed, we
        # bounce back up to the calculated floor and hand off there.
        if self._gap_spread_bps <= self._arb_floor_bps:
            below_mult = float(getattr(cfg, "GAP_CLOSE_BELOW_FLOOR_MULT", 0.5))
            below_spread = max(1, int(self._arb_floor_bps * below_mult))
            already_subprobed = getattr(self, "_subprobe_attempted", False)

            if not already_subprobed and below_spread < self._gap_spread_bps:
                # Fire one sub-probe below the calculated floor
                self._subprobe_attempted = True
                old_spread = self._gap_spread_bps
                self._gap_spread_bps = below_spread
                self._steps_taken += 1
                self._last_step_time = now

                if self._active_boost_ids and self._offer_manager:
                    for tid in self._active_boost_ids:
                        self._offer_manager._bot_cancelled_ids.add(tid)
                    self._offer_manager.cancel_offers(
                        self._active_boost_ids,
                        reason="gap_closer_subprobe",
                        skip_confirmation=True,
                    )
                self._active_boost_ids.clear()
                self._create_gap_closer_pair(self._boost_mid_price)
                self._stable_since = time.time()

                log_event("info", "gap_closer_subprobe",
                          f"📈 Below-floor sub-probe: {_bps_to_pct(old_spread)} "
                          f"→ {_bps_to_pct(below_spread)} (calculated floor was "
                          f"{_bps_to_pct(self._arb_floor_bps)} — testing whether "
                          f"the market actually punishes this price)",
                          data={"spread_bps": below_spread,
                                "arb_floor_bps": self._arb_floor_bps,
                                "steps_taken": self._steps_taken})
                print(f"📈 Sub-probe below floor: {_bps_to_pct(old_spread)} → "
                      f"{_bps_to_pct(below_spread)}", flush=True)
                return True  # acted this cycle

            # Already attempted sub-probe (or it would be no tighter) —
            # complete the test. If the sub-probe got arbed, _on_arbed()
            # has already widened us back above the floor; if it survived,
            # we're sitting at sub-probe spread and that's our new known-
            # safe price. Either way, hand off and stop.
            stable_secs = int(now - self._stable_since)
            log_event("info", "gap_closer_auto_stop",
                      f"📈 Close the Gap complete — held floor at "
                      f"{_bps_to_pct(self._gap_spread_bps)} for {stable_secs}s "
                      f"with no arb after {self._steps_taken} step(s). "
                      f"Handing off to inner tier.",
                      data={"spread_bps": self._gap_spread_bps,
                            "arb_floor_bps": self._arb_floor_bps,
                            "steps_taken": self._steps_taken,
                            "stable_secs": stable_secs})
            print(f"📈 Close the Gap complete — floor held for {stable_secs}s, "
                  f"handing off to inner tier.", flush=True)

            # --- Floor handoff: plant inner-tier offers at proven price ---
            self._handoff_to_inner_tier()

            # Deactivate but PRESERVE convergence factor — let the
            # incremental reaction strategy adjust the rest of the ladder
            # naturally over the following cycles.
            self.deactivate(preserve_convergence=True)
            return False

        # ---- Calculate new spread (tighten by STEP_PCT) ----
        step_pct = getattr(cfg, "GAP_CLOSE_STEP_PCT", 10)
        old_spread = self._gap_spread_bps
        new_spread = max(1, int(old_spread * (100 - step_pct) / 100))

        # Clamp to arb floor
        new_spread = max(new_spread, self._arb_floor_bps)

        # No change? Already at floor
        if new_spread >= old_spread:
            return False

        # ---- Execute the step: cancel old offers, create new at tighter spread ----
        self._gap_spread_bps = new_spread
        self._steps_taken += 1
        self._last_step_time = now

        # Fire-and-forget cancel old offers, then immediately create the new
        # probe pair using DIFFERENT sniper coins. Why fire-and-forget:
        # Sage's cancel-confirm path waits up to 90s for the cancel tx to
        # confirm and the original coin to return — during that window the
        # inside of the book is EMPTY because the new probes haven't been
        # placed yet. Probes are sniper-tier (we have 25 in the pool) so the
        # selector picks a fresh coin for the new probe; the old coin is
        # still mid-cancel but we don't need it. The cancel will confirm in
        # the background and free its coin back into the pool.
        if self._active_boost_ids and self._offer_manager:
            for tid in self._active_boost_ids:
                self._offer_manager._bot_cancelled_ids.add(tid)
            self._offer_manager.cancel_offers(
                self._active_boost_ids,
                reason="gap_closer_step",
                skip_confirmation=True,
            )
        self._active_boost_ids.clear()

        # Recreate at new tighter spread
        self._create_gap_closer_pair(self._boost_mid_price)

        # Reset stability timer for the new spread
        self._stable_since = time.time()

        log_event("info", "gap_closer_step",
                  f"📈 Step {self._steps_taken}: {_bps_to_pct(old_spread)} → {_bps_to_pct(new_spread)} | arb floor: {_bps_to_pct(self._arb_floor_bps)}",
                  data={"spread_bps": new_spread, "arb_floor_bps": self._arb_floor_bps,
                        "steps_taken": self._steps_taken, "start_spread_bps": self._start_spread_bps})
        print(f"📈 Close the Gap step {self._steps_taken}: "
              f"{_bps_to_pct(old_spread)} → {_bps_to_pct(new_spread)} "
              f"[arb floor: {_bps_to_pct(self._arb_floor_bps)}]", flush=True)

        return True

    # -------------------------------------------------------------------
    # Persistent refresh — keep offers alive and centred
    # -------------------------------------------------------------------

    def refresh_if_needed(self, current_mid_price: Decimal) -> bool:
        """Check if gap-closer offers need refreshing and recreate if so.

        Called every bot loop cycle when gap-closer is active. Handles:
          1. Offers expired or filled → recreate at current price
          2. Price moved significantly → cancel old, create new at current price
          3. Everything fine → do nothing

        Returns True if offers were refreshed, False if no action taken.
        """
        if not self._boost_active:
            return False

        if current_mid_price <= 0:
            return False

        # Circuit breaker — skip refresh while CB is active so we don't
        # recreate an imbalanced pair while the bot is trying to correct.
        if self._cb_blocks_boost():
            return False

        # ---- Check 1: Are offers still alive? ----
        needs_refresh = False
        refresh_reason = ""

        if len(self._active_boost_ids) == 0:
            needs_refresh = True
            refresh_reason = "all offers gone (expired/filled)"

        elif len(self._active_boost_ids) < 2:
            needs_refresh = True
            refresh_reason = f"partial loss ({len(self._active_boost_ids)}/2 remaining)"

        # ---- Check 1b: REMOVED — no expiry, no pre-emptive refresh needed ----
        # Offers no longer expire, so this check is unnecessary.

        # ---- Check 2: Has price moved enough to re-centre? ----
        if not needs_refresh and self._boost_mid_price > 0 and self._gap_spread_bps > 0:
            spread_bps = Decimal(str(self._gap_spread_bps))
            recentre_threshold_bps = spread_bps / Decimal("2")

            move_bps = (abs(current_mid_price - self._boost_mid_price)
                        / self._boost_mid_price * Decimal("10000"))

            if move_bps > recentre_threshold_bps:
                needs_refresh = True
                refresh_reason = (f"price moved {_bps_to_pct(move_bps)} "
                                  f"(threshold: {_bps_to_pct(recentre_threshold_bps)})")

        if not needs_refresh:
            return False

        # ---- Refresh: CREATE NEW first, THEN cancel old ----
        # Same pattern as cascade: never leave a gap in the orderbook.
        print(f"📈 Gap closer refresh: {refresh_reason}", flush=True)
        log_event("info", "gap_closer_refresh",
                  f"📈 Gap closer refreshing — {refresh_reason}")

        old_ids = list(self._active_boost_ids) if self._active_boost_ids else []

        # Step 1: Create new offers FIRST (before cancelling old ones)
        self._active_boost_ids.clear()
        created = self._create_gap_closer_pair(current_mid_price)

        # Step 2: Cancel old offers AFTER new ones exist
        if old_ids and self._offer_manager:
            time.sleep(0.5)
            for tid in old_ids:
                self._offer_manager._bot_cancelled_ids.add(tid)
            self._offer_manager.cancel_offers(
                old_ids, reason="gap_closer_refresh",
                skip_confirmation=True,
            )

        if created:
            self._total_refreshes += 1
            log_event("info", "gap_closer_refreshed",
                      f"📈 Gap closer refreshed: {len(created)} offers at "
                      f"{_bps_to_pct(self._gap_spread_bps)}, mid {current_mid_price:.8f}")
            print(f"📈 Gap closer refreshed: {len(created)} offers at "
                  f"{_bps_to_pct(self._gap_spread_bps)}", flush=True)
        else:
            log_event("warning", "gap_closer_refresh_failed",
                      "📈 Gap closer refresh failed — will retry next cycle")
            print("⚠️ Gap closer refresh failed — will retry next cycle", flush=True)

        return True

    # -------------------------------------------------------------------
    # Pruning — keep in-memory list in sync with wallet
    # -------------------------------------------------------------------

    def prune_active_boosts(self, open_trade_ids: set):
        """Remove gap-closer IDs no longer open (filled or cancelled).

        Also detects arb fills: if an offer disappeared but was NOT
        bot-cancelled, it was arbed. This triggers spread widening.
        """
        with self._lock:
            before = len(self._active_boost_ids)

            bot_cancelled = set()
            if self._offer_manager:
                bot_cancelled = self._offer_manager._bot_cancelled_ids

            now = time.time()
            # Iterate over a snapshot so _on_arbed() cannot mutate the list
            # out from under us.
            for tid in list(self._active_boost_ids):
                if tid not in open_trade_ids and tid not in bot_cancelled:
                    # Before declaring arb, check if this offer simply expired.
                    # Gap closer offers carry a 60-second expiry; when the time
                    # is up they vanish from the wallet without a cancel tx.
                    expiry_time = self._boost_id_expiry.get(tid, 0)
                    if expiry_time > 0 and now >= (expiry_time - 5):
                        # Natural expiry — not an arb fill, don't widen spread
                        log_event("debug", "gap_closer_offer_expired",
                                  f"Gap closer offer {tid[:16]}… expired naturally (not arbed)")
                    else:
                        # Disappeared before expiry — this was arbed
                        self._arb_count += 1
                        self._on_arbed()

            self._active_boost_ids = [
                tid for tid in self._active_boost_ids if tid in open_trade_ids
            ]
            # Clean up stale expiry entries older than 5 minutes
            self._boost_id_expiry = {
                k: v for k, v in self._boost_id_expiry.items()
                if v > now - 300
            }
            pruned = before - len(self._active_boost_ids)

        if pruned > 0:
            log_event("debug", "gap_closer_pruned",
                      f"Pruned {pruned} closed gap-closer offers "
                      f"({len(self._active_boost_ids)} remaining)")

    # -------------------------------------------------------------------
    # Internal: create offers at current gap-closer spread
    # -------------------------------------------------------------------

    def _create_gap_closer_pair(self, mid_price: Decimal) -> List[Dict]:
        """Create 1 buy + 1 sell at current gap-closer spread.

        Uses self._gap_spread_bps (the adaptive spread that changes over time).
        """
        if self._gap_spread_bps <= 0:
            return []

        size_xch = self._effective_size_xch()

        half_spread = Decimal(str(self._gap_spread_bps)) / Decimal("20000")
        buy_price = mid_price * (Decimal("1") - half_spread)
        sell_price = mid_price * (Decimal("1") + half_spread)

        created = []

        if cfg.ENABLE_BUY:
            buy_result = self._create_single_offer("buy", buy_price, size_xch)
            if buy_result:
                created.append(buy_result)

        if cfg.ENABLE_SELL:
            sell_result = self._create_single_offer("sell", sell_price, size_xch)
            if sell_result:
                created.append(sell_result)

        # Post to Dexie IMMEDIATELY (bypass queue for speed)
        if created and self._dexie_manager and cfg.DEXIE_AUTO_POST:
            for offer in created:
                bech32 = offer.get("offer_bech32", "")
                trade_id = offer.get("trade_id", "")
                if bech32 and trade_id:
                    self._dexie_manager._post_single(bech32, trade_id, force=True)

        if created:
            self._boost_mid_price = mid_price
            for offer in created:
                tid = offer.get("trade_id", "")
                if tid:
                    self._active_boost_ids.append(tid)

        return created

    # -------------------------------------------------------------------
    # Single offer creation helper
    # -------------------------------------------------------------------

    def _create_single_offer(self, side: str, price: Decimal,
                             size_xch: Decimal) -> Optional[Dict]:
        """Create a single gap-closer offer. Returns offer dict or None."""
        if not self._offer_manager:
            return None

        cat_amount = size_xch / price
        cat_mojos = cat_to_mojos(cat_amount, cfg.CAT_DECIMALS)
        cat_amount = mojos_to_cat(cat_mojos, cfg.CAT_DECIMALS)
        xch_mojos = xch_to_mojos(size_xch)

        # Amount validation — reject zero, negative, or absurdly large values
        if int(cat_mojos) <= 0 or int(xch_mojos) <= 0:
            log_event("warning", "gap_closer_bad_amount",
                      f"📈 Gap closer {side} rejected: invalid mojos "
                      f"(cat={cat_mojos}, xch={xch_mojos}, price={price})")
            return None
        if int(xch_mojos) > 1_000_000_000_000_000:  # > 1000 XCH sanity cap
            log_event("warning", "gap_closer_bad_amount",
                      f"📈 Gap closer {side} rejected: xch_mojos too large ({xch_mojos})")
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
            log_event("info", "gap_closer_dry_run",
                      f"📈 [DRY RUN] Would create {side} at {price:.8f}")
            return None

        # Use sniper expiry so probes survive long enough between steps.
        # Previously used cooldown+60 (120s) which was too fragile — offers
        # could expire mid-proof if the bot loop was busy with other work.
        offer_expiry = getattr(cfg, "SNIPER_EXPIRY_SECS", 600)
        # Pin coin selection to the sniper pool. Probes are sniper-sized
        # (SNIPER_SIZE_XCH) so without this hint the closest-fit selector
        # would *usually* pick a sniper coin, but could silently spill into
        # an inner-tier coin if the sniper pool is empty — burning a much
        # larger coin to back a 0.001 XCH probe. strict=True fails the
        # creation cleanly instead so the GUI surfaces the depleted pool.
        res = self._offer_manager.create_offer_with_retry(
            offer_dict,
            coin_ids_enabled=cfg.COIN_IDS_ENABLED,
            expiry_secs=offer_expiry,
            preferred_tier="sniper",
            strict_preferred_tier=True,
        )

        if not res or not res.get("success"):
            log_event("warning", "gap_closer_create_failed",
                      f"📈 Gap closer {side} creation failed: {res}")
            return None

        trade_record = res.get("trade_record") or {}
        trade_id = res.get("trade_id") or trade_record.get("trade_id") or ""
        offer_bech32 = res.get("offer", "")
        locked_coin_id = res.get("locked_coin_id")

        if trade_id:
            # Track when this offer expires so prune_active_boosts won't mistake
            # natural expiry for an arb fill.
            self._boost_id_expiry[trade_id] = time.time() + offer_expiry

            # expires_at matches the on-chain expiry so cleanup and dashboard
            # reporting stay consistent with the actual wallet offer lifetime.
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            _expiry_iso = (_dt.now(_tz.utc) + _td(seconds=offer_expiry)).strftime("%Y-%m-%d %H:%M:%S")
            db_ok = add_offer(
                trade_id=trade_id,
                side=side,
                price_xch=price,
                size_xch=size_xch,
                size_cat=cat_amount,
                cat_asset_id=cfg.CAT_ASSET_ID,
                tier="boost",
                expires_at=_expiry_iso,
                coin_id=locked_coin_id,
            )
            if not db_ok:
                # DB insert failed — cancel the on-chain offer to prevent
                # wallet/DB divergence (offer exists in wallet but not in DB).
                log_event("error", "boost_db_cancel",
                          f"DB insert failed for boost {trade_id[:16]}..., cancelling on-chain offer")
                if self._offer_manager:
                    self._offer_manager._bot_cancelled_ids.add(trade_id)
                    self._offer_manager.cancel_offers([trade_id], reason="boost_db_insert_failed")
                return None
            if locked_coin_id and self._offer_manager:
                # Register in cycle exclusion set so ladder won't re-select this coin
                self._offer_manager._cycle_used_coin_ids.add(locked_coin_id)
                try:
                    lock_coin(locked_coin_id, trade_id)
                except Exception:
                    pass
            if self._offer_manager:
                self._offer_manager._offer_details_cache[trade_id] = {
                    "price": price,
                    "size_xch": size_xch,
                    "size_cat": cat_amount,
                    "tier": "boost",
                    "dexie_link": "",
                }

        log_event("info", "gap_closer_created",
                  f"📈 Gap closer {side.upper()} at {price:.8f} XCH "
                  f"({size_xch} XCH / {cat_amount:.2f} CAT)")

        return {
            "trade_id": trade_id,
            "side": side,
            "price": price,
            "size_xch": size_xch,
            "size_cat": cat_amount,
            "offer_bech32": offer_bech32,
        }

    # -------------------------------------------------------------------
    # Arb response — widen when offers get snapped
    # -------------------------------------------------------------------

    def _on_arbed(self):
        """Called when a gap-closer offer was arbed (filled by bots).

        Widens the gap-closer spread by 20% and resets stability timer.
        Also widens the main book convergence factor.
        """
        with self._lock:
            if self._gap_spread_bps <= 0:
                return

            old_spread = self._gap_spread_bps

            # Widen gap-closer spread by 20%
            new_spread = int(self._gap_spread_bps * 1.2)

            # Cap widening at widen_ceiling (max of start spread or main book
            # spread at activation time). With aggressive find-floor starts
            # near the floor, widening must be allowed to climb above the
            # initial spread or we'd be permanently stuck just above the
            # floor with offers getting eaten every cycle.
            ceiling = getattr(self, "_widen_ceiling_bps", self._start_spread_bps)
            new_spread = min(new_spread, ceiling)
            self._gap_spread_bps = new_spread

            # Reset stability timer — need to prove stability again
            self._stable_since = 0
            # Reset cascade — need to re-prove at the new wider spread
            self._cascade_done_at_spread = -1

            # Also widen main book convergence by 20%
            old_factor = self._convergence_factor
            self._convergence_factor = min(
                Decimal("1.0"),
                self._convergence_factor + Decimal("0.20")
            )

        log_event("warning", "gap_closer_arbed",
                  f"⚠️ Gap closer arbed! Widening: "
                  f"{_bps_to_pct(old_spread)} → {_bps_to_pct(new_spread)} "
                  f"(convergence: {old_factor:.2f} → {self._convergence_factor:.2f})",
                  data={"spread_bps": new_spread, "arb_floor_bps": self._arb_floor_bps,
                        "steps_taken": self._steps_taken, "start_spread_bps": self._start_spread_bps})
        print(f"⚠️ Gap closer arbed! Backing off: "
              f"{_bps_to_pct(old_spread)} → {_bps_to_pct(new_spread)} "
              f"(arb floor: {_bps_to_pct(self._arb_floor_bps)})", flush=True)

        # Flag for the bot loop: if our probe got arbed, any inner-tier
        # offers at a similar or tighter spread could also be vulnerable.
        # The bot loop checks this flag and triggers an emergency check
        # on inner offers during the next cycle.
        self._inner_vulnerability_flag = True

    def consume_inner_vulnerability_flag(self) -> bool:
        """Check and clear the inner-vulnerability flag.

        Returns True if the flag was set (probe was arbed and inner-tier
        offers should be checked for exposure).  Clears the flag after
        reading so it only fires once.
        """
        with self._lock:
            if self._inner_vulnerability_flag:
                self._inner_vulnerability_flag = False
                return True
            return False

    # -------------------------------------------------------------------
    # Main book convergence — follows gap-closer's proven levels
    # -------------------------------------------------------------------

    def update_convergence(self) -> bool:
        """Tighten main book spread if gap-closer has proven a safe level.

        The convergence factor tracks toward the ratio of gap-closer spread
        to original main spread. Only tightens if gap-closer has been stable.

        Returns True if convergence factor changed.
        """
        if not self._boost_active:
            return False

        now = time.time()

        # Need active offers for stability proof
        if len(self._active_boost_ids) == 0:
            return False

        # Check stability (gap closer offers must have survived)
        if self._stable_since == 0:
            return False
        stability_required = getattr(cfg, "GAP_CLOSE_CONVERGENCE_SECS",
                                     getattr(cfg, "GAP_CLOSE_STEP_COOLDOWN_SECS", 60))
        if (now - self._stable_since) < stability_required:
            return False

        # Cooldown between convergence steps (separate from gap closer step cooldown)
        convergence_cooldown = getattr(cfg, "GAP_CLOSE_CONVERGENCE_SECS",
                                       getattr(cfg, "GAP_CLOSE_STEP_COOLDOWN_SECS", 60))
        if (now - self._last_convergence_time) < convergence_cooldown:
            return False

        # Already fully converged?
        if self._convergence_factor <= self._convergence_min:
            return False

        # ---- Tighten main book spread ----
        step_pct = getattr(cfg, "GAP_CLOSE_CONVERGENCE_STEP_PCT", 20)
        step_dec = Decimal(str(step_pct)) / Decimal("100")
        old_factor = self._convergence_factor
        self._convergence_factor = max(
            self._convergence_min,
            self._convergence_factor - step_dec
        )
        self._last_convergence_time = now

        pct = self._convergence_factor * Decimal("100")
        log_event("info", "gap_closer_convergence",
                  f"📈 Main book converging: {old_factor:.2f} → "
                  f"{self._convergence_factor:.2f} "
                  f"(main spread now {pct:.0f}% of original)")
        print(f"📈 Main book converging: now {pct:.0f}% of original spread",
              flush=True)

        return True

    def get_convergence_factor(self) -> Decimal:
        """Get current convergence multiplier for risk_manager.

        1.0 = no change, 0.5 = halved, 0.15 = minimum.
        """
        if not self._boost_active:
            return Decimal("1.0")
        return self._convergence_factor

    # -------------------------------------------------------------------
    # Cascade — physically move main book behind proven probe level
    # -------------------------------------------------------------------

    def should_cascade(self) -> bool:
        """Check if the main book should cascade (requote) behind the probe.

        The cascade triggers when:
          1. Gap closer is active with surviving offers
          2. Probe has been stable for CASCADE_WAIT_SECS (~60s)
          3. Main book hasn't been cascaded at this spread level yet

        This is SEPARATE from the slow step/convergence system.
        The cascade physically moves the main book offers to match
        the probe's proven price level, rather than waiting for
        gradual convergence.

        Returns True if bot_loop should force-requote the main book.
        """
        if not self._boost_active:
            return False

        # Need active probe offers as proof the level is safe
        if len(self._active_boost_ids) == 0:
            return False

        # Already cascaded at this spread level? Don't repeat
        if self._cascade_done_at_spread == self._gap_spread_bps:
            return False

        # Stability check: probe must have survived the wait period
        if self._stable_since == 0:
            return False

        cascade_wait = getattr(cfg, "GAP_CLOSE_CASCADE_WAIT_SECS", 60)
        stable_secs = time.time() - self._stable_since
        if stable_secs < cascade_wait:
            return False

        return True

    def cascade_main_book(self, mid_price: Decimal,
                          open_buys: list, open_sells: list) -> Dict:
        """Cascade the main book behind the proven probe level.

        CRITICAL: Creates new offers FIRST, then cancels stale ones.
        Never wipes the orderbook. Works in batches using spare coins.

        Strategy per side:
          1. Find which existing offers are "stale" (furthest from mid)
          2. Check how many spare coins we have for new offers
          3. Create new tighter offers (up to batch_size or spare coin count)
          4. Cancel the same number of stale offers we just replaced
          5. If more stale offers remain, they'll be handled next cycle

        Args:
            mid_price: Current mid price to space new offers around
            open_buys: List of currently open buy offer dicts
            open_sells: List of currently open sell offer dicts

        Returns dict with created/cancelled counts per side.
        """
        if not self._boost_active or self._gap_spread_bps <= 0:
            return {"success": False, "reason": "gap closer not active"}

        if not self._offer_manager or not self._risk_manager:
            return {"success": False, "reason": "missing dependencies"}

        batch_size = getattr(cfg, "GAP_CLOSE_CASCADE_BATCH_SIZE", 5)
        results = {"buy": {"created": 0, "cancelled": 0},
                   "sell": {"created": 0, "cancelled": 0}}

        for side in ["buy", "sell"]:
            if side == "buy" and not cfg.ENABLE_BUY:
                continue
            if side == "sell" and not cfg.ENABLE_SELL:
                continue

            offers = open_buys if side == "buy" else open_sells

            # Skip boost/sniper offers — only cascade the main book
            main_offers = [o for o in offers
                           if o.get("trade_id") not in set(self._active_boost_ids)]

            if not main_offers:
                continue

            # Get the target spread from risk_manager (uses convergence factor)
            target_spread = self._risk_manager.get_adjusted_spread(side)

            # Identify stale offers: those whose price is furthest from mid
            # compared to where they'd be at the new tighter spread.
            # For buys: stale = price too low (too far below mid)
            # For sells: stale = price too high (too far above mid)
            stale = self._find_stale_offers(main_offers, mid_price, side,
                                             target_spread)

            if not stale:
                continue

            # Limit to batch_size
            to_replace = stale[:batch_size]

            # Step 1: CREATE new offers at tighter prices FIRST
            # Use the offer_manager's create_ladder with a small count
            new_count = len(to_replace)
            try:
                new_offers = self._offer_manager.create_ladder(
                    mid_price, side,
                    num_offers=new_count,
                    spread_fraction=target_spread,
                    risk_manager=self._risk_manager,
                    coin_ids_enabled=cfg.COIN_IDS_ENABLED
                )
                created = len(new_offers) if new_offers else 0
                results[side]["created"] = created
            except Exception as e:
                log_event("warning", "cascade_create_failed",
                          f"Cascade {side} create failed: {e}")
                created = 0

            if created == 0:
                # No spare coins — skip cancellation too, try again next cycle
                log_event("info", "cascade_no_coins",
                          f"📈 Cascade {side}: no spare coins for new offers — "
                          f"will retry next cycle")
                continue

            # Post new offers to Dexie immediately
            if new_offers and self._dexie_manager and cfg.DEXIE_AUTO_POST:
                for offer in new_offers:
                    bech32 = offer.get("offer_bech32", offer.get("offer", ""))
                    trade_id = offer.get("trade_id", "")
                    if bech32 and trade_id:
                        self._dexie_manager.queue_post(bech32, trade_id)

            # Step 2: CANCEL the stale offers we just replaced
            # Only cancel as many as we successfully created
            cancel_ids = [o.get("trade_id") for o in to_replace[:created]
                          if o.get("trade_id")]
            if cancel_ids:
                for tid in cancel_ids:
                    self._offer_manager._bot_cancelled_ids.add(tid)
                cancel_result = self._offer_manager.cancel_offers(
                    cancel_ids, reason="cascade_replace"
                )
                cancelled = sum(
                    1 for r in (cancel_result or {}).values()
                    if r and r.get("success")
                )
                results[side]["cancelled"] = cancelled

            log_event("info", "cascade_batch",
                      f"📈 Cascade {side}: created {created} new, "
                      f"cancelled {results[side]['cancelled']} stale "
                      f"({len(stale) - created} remaining)")

        total_created = results["buy"]["created"] + results["sell"]["created"]
        total_cancelled = results["buy"]["cancelled"] + results["sell"]["cancelled"]

        if total_created > 0:
            self._cascade_done_at_spread = self._gap_spread_bps
            self._cascade_count += 1
            print(f"📈 Cascade #{self._cascade_count}: "
                  f"+{total_created} new, -{total_cancelled} stale "
                  f"(probe at {_bps_to_pct(self._gap_spread_bps)})",
                  flush=True)

        results["success"] = total_created > 0
        results["total_created"] = total_created
        results["total_cancelled"] = total_cancelled
        return results

    def _find_stale_offers(self, offers: list, mid_price: Decimal,
                            side: str, target_spread: Decimal) -> list:
        """Find main book offers that are stale (too far from mid).

        An offer is "stale" if its price is further from mid than where
        the outermost tier would be at the new target spread.

        Returns offers sorted by staleness (most stale first).
        """
        if mid_price <= 0 or not offers:
            return []

        # Calculate the outer boundary of the new target spread
        # The furthest offer should be at mid × (1 ± spread)
        # Anything beyond that is stale
        stale = []

        for offer in offers:
            # Get the offer's price from the details cache or offer dict
            price = None
            trade_id = offer.get("trade_id", "")

            if trade_id and self._offer_manager:
                cached = self._offer_manager._offer_details_cache.get(trade_id, {})
                price = cached.get("price")

            if price is None:
                # Try to calculate from the offer's amounts
                continue

            if not isinstance(price, Decimal):
                try:
                    price = Decimal(str(price))
                except Exception:
                    continue

            if price <= 0:
                continue

            # Calculate how far this offer is from mid (in BPS)
            distance_bps = abs(price - mid_price) / mid_price * Decimal("10000")

            # Target outer boundary (in BPS) — use spread × 2 as the "acceptable" range
            # since tiers spread from inner to outer
            target_bps = target_spread * Decimal("10000")

            # An offer is stale if it's beyond the new target range
            if distance_bps > target_bps:
                stale.append({
                    **offer,
                    "_distance_bps": float(distance_bps),
                    "_price": price,
                })

        # Sort by distance (most stale = furthest from mid first)
        stale.sort(key=lambda o: o.get("_distance_bps", 0), reverse=True)

        return stale

    # -------------------------------------------------------------------
    # State query
    # -------------------------------------------------------------------

    def get_state(self) -> Dict:
        """Get gap-closer state for GUI (thread-safe snapshot)."""
        with self._lock:
            stable_secs = 0
            if self._stable_since > 0:
                stable_secs = time.time() - self._stable_since

            cooldown = getattr(cfg, "GAP_CLOSE_STEP_COOLDOWN_SECS", 300)
            now = time.time()
            # The actual blocker is whichever timer has more time left:
            # 1) cooldown since last step, 2) stability proof since last arb/refresh
            step_wait = max(0, cooldown - (now - self._last_step_time))
            stable_wait = max(0, cooldown - stable_secs) if self._stable_since > 0 else cooldown
            secs_until_step = max(0, int(max(step_wait, stable_wait)))

            return {
                "active": self._boost_active,
                "boost_count": len(self._active_boost_ids),
                "boost_mid_price": str(self._boost_mid_price),
                # Adaptive spread info
                "current_spread_bps": self._gap_spread_bps,
                "start_spread_bps": self._start_spread_bps,
                "arb_floor_bps": self._arb_floor_bps,
                "steps_taken": self._steps_taken,
                "arb_count": self._arb_count,
                "secs_until_step": secs_until_step,
                # Offer details
                "size_xch": str(self._effective_size_xch()),
                "active_ids": list(self._active_boost_ids),
                # Main book convergence
                "convergence_factor": str(self._convergence_factor),
                "convergence_pct": str(self._convergence_factor * Decimal("100")),
                # Stats
                "total_refreshes": self._total_refreshes,
                "total_arb_warnings": self._total_arb_warnings,
                "stable_secs": int(stable_secs),
                # Cascade info
                "cascade_count": self._cascade_count,
                "cascade_ready": self.should_cascade(),
            }

