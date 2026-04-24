"""Offer-lifecycle manager for ladder creation, requoting, expiry, and cancellation

The `OfferManager` class bridges pricing and risk output from `PriceEngine` and
`RiskManager` to wallet-RPC offer operations exposed via the `wallet` module.
It owns ladder construction, price-move-triggered requoting, expiry handling,
and batch cancellation, and it maintains `_bot_cancelled_ids` so `FillTracker`
can distinguish genuine fills from bot-initiated cancels.

Key responsibilities:
    - Build buy/sell ladders sized against available coin inventory
    - Requote offers when the mid-price drifts past configured thresholds
    - Cancel offers individually or in batches and track which IDs we cancelled
    - Coordinate with `Sniper` and `BoostManager` to avoid coin double-spend

Thread-safe via `_lock`. All mutating operations should be called while holding
the lock, and any coin reservation crosses through shared state guarded here.
"""

import time
import threading
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List, Tuple, Callable, Any

from config import cfg
from database import (
    add_offer, update_offer_status,
    transition_offer, mark_cancel_attempted,
    get_open_offers, log_event, lock_coin
)
from wallet import (
    create_offer, cancel_offer, cancel_offers_batch,
    get_all_offers, classify_offers_from_list,
    get_offer_bech32, cleanup_expired_offers,
    get_exact_spendable_coins_rpc, get_wallet_type,
    get_owned_coins_detailed,
)


# ---------------------------------------------------------------------------
# Amount conversion helpers (from V1 — critical to get right)
# ---------------------------------------------------------------------------

def xch_to_mojos(amount_xch: Decimal) -> int:
    """Convert XCH to mojos. 1 XCH = 1,000,000,000,000 mojos."""
    return int((amount_xch * Decimal("1000000000000")).to_integral_value(ROUND_DOWN))


def mojos_to_xch(mojos: int) -> Decimal:
    """Convert mojos to XCH."""
    return Decimal(mojos) / Decimal("1000000000000")


def cat_to_mojos(amount: Decimal, decimals: int) -> int:
    """Convert CAT amount to mojos. Uses 10^decimals, NOT 1e12."""
    scale = Decimal(10) ** Decimal(decimals)
    return int((amount * scale).to_integral_value(ROUND_DOWN))


def mojos_to_cat(mojos: int, decimals: int) -> Decimal:
    """Convert mojos to CAT amount."""
    scale = Decimal(10) ** Decimal(decimals)
    return Decimal(mojos) / scale


# Methods that indicate Sage ACCEPTED the cancel submission but the cancel
# transaction is NOT yet confirmed on-chain. The offer may still be live
# and could still fill if the cancel TX fails in the mempool. DB status
# MUST NOT be flipped to "cancelled" for these — a later cancel-confirm
# poll or fill-detector is what authoritatively closes the offer.
CANCEL_PENDING_METHODS = frozenset({
    "submitted_pending_confirm",
    "already_in_mempool",
    "mempool_conflict_inflight",
    # "already_gone_ambiguous" represents a Sage 404 on the cancel RPC:
    # the offer is no longer in the wallet but Sage does NOT tell us
    # whether it was cancelled, filled, or expired. DB status must stay
    # open so fill_tracker (Spacescan) and bot_health (Dexie reconcile)
    # can settle the real state — writing `cancelled` here would silently
    # misclassify any fills that raced the cancel.
    "already_gone_ambiguous",
})


# ---------------------------------------------------------------------------
# Offer Manager
# ---------------------------------------------------------------------------
class OfferManager:
    """Manages the full lifecycle of market-making offers.

    Responsibilities:
    - Create offer ladders (buy and sell sides)
    - Track which offers we created vs filled vs cancelled
    - Handle requoting when price moves beyond threshold
    - Manage offer expiry (staggered expiry to avoid cascades)
    - Queue offers for Dexie posting
    """

    def __init__(self):
        # Track which offers the bot cancelled (vs externally filled).
        # Used by fill_tracker to distinguish own-cancel from counterparty fill.
        self._bot_cancelled_ids: set = set()

        # Cache of offer details for fill recording
        self._offer_details_cache: Dict[str, Dict] = {}

        # Last requote time per side (cooldown enforcement)
        self._last_requote_time: Dict[str, float] = {"buy": 0, "sell": 0}

        # Lock for thread safety during offer operations
        self._lock = threading.Lock()

        # ----- V1 Parity: Retry failed cancels -----
        # Dict of trade_id -> {"attempts": int, "first_failed": float}
        self._pending_cancel_retries: Dict[str, Dict] = {}
        self._max_cancel_retries: int = 5

        # ----- V1 Parity: Recently created offers (anti-overcount) -----
        # Dict of trade_id -> creation_time — offers created this cycle
        # that may not be visible in wallet sync yet
        self._recently_created: Dict[str, float] = {}
        self._recently_created_ttl: int = 600  # 10 minutes — must outlast Sage sync delays

        # ----- Stop signal -----
        # Set by bot_loop.stop() to interrupt long-running ladder creation.
        # Without this, create_ladder's for-loop runs to completion even
        # after stop() is called, because the 10s join timeout expires
        # before the loop finishes. GUI shows "stopped" but the thread
        # keeps creating offers for minutes.
        self._stop_requested: bool = False

        # ----- AMM Monitor reference -----
        # Injected by bot_loop after both modules are instantiated.
        # Used to call check_amm_buffer() before posting each offer slot
        # so we never post inside TibetSwap's arb zone.
        self.amm_monitor = None

        # ----- Dexie Manager reference -----
        # Injected by bot_loop after instantiation.
        # Used to purge cancelled offer IDs from the Dexie post queue so
        # they don't generate spurious "Invalid Offer" 400 errors on flush.
        self.dexie_manager = None

        # ----- Fee coin pool reference -----
        # Injected by bot_loop: self.offer_manager._fee_pool = self.coin_manager.fee_pool
        # Each create/cancel reserves a dedicated fee coin from this pool
        # so concurrent operations don't fight over the same fee coin.
        self._fee_pool = None

        # ----- Shared in-flight coin tracking -----
        # Coins currently selected for offer creation (not yet confirmed).
        # Checked by both main loop and sniper under _lock to prevent
        # double-selecting the same coin in concurrent create paths.
        self._inflight_coin_ids: set = set()

        # ----- Per-cycle used coin exclusion -----
        # Coins successfully used by any offer creation within the current
        # bot cycle.  Unlike _inflight_coin_ids (released after each RPC
        # call) this set persists for the entire cycle so that a second
        # create_ladder call (e.g. the sell side after the buy side) will
        # not re-select a coin that is still pending on-chain confirmation.
        # Cleared at the start of every cycle via clear_cycle_coins().
        self._cycle_used_coin_ids: set = set()

        # ----- Fix F: Slot suspension for coin exhaustion self-heal -----
        # When a specific slot fails to get a unique coin 3 consecutive times,
        # suspend it to prevent infinite retry loops. Slots are unsuspended
        # when coins become available again.
        # Key: f"{side}_{slot}" → consecutive failure count
        self._slot_fail_counts: Dict[str, int] = {}
        # Set of f"{side}_{slot}" keys that are currently suspended
        self._suspended_slots: set = set()
        self._slot_suspend_threshold: int = 3  # consecutive failures before suspension
        # Per-slot warn cooldown: only emit slot_suspended log once every 10 min
        # per slot to prevent repeated log entries when the suspend/unsuspend
        # cycle triggers rapidly during sustained coin exhaustion.
        self._slot_warned_at: Dict[str, float] = {}
        self._slot_warn_cooldown: float = 600.0  # seconds

        # Position-hard-guard log cooldown — when net position exceeds the hard
        # limit, every ladder attempt emits the same block error. During a
        # sustained imbalance the logs flooded with 4 identical ERROR lines per
        # minute. Cooldown to once per 60s per side so the block reason stays
        # visible without drowning other signals.
        self._position_guard_logged_at: Dict[str, float] = {}
        self._position_guard_log_cooldown: float = 60.0  # seconds

        # ----- Wallet sync fail-closed cache -----
        # When Sage get_offers times out, we must not treat that as an empty
        # book. Keep the last successful classified view so callers can fail
        # closed and avoid rebuilding on top of still-live offers.
        self._wallet_sync_cache: Dict[str, List[Dict]] = {
            "buy": [],
            "sell": [],
            "closed": [],
        }
        self._wallet_sync_meta: Dict[str, Any] = {
            "fresh": True,
            "using_cache": False,
            "consecutive_failures": 0,
            "last_error": "",
            "last_success_at": 0.0,
            "last_failure_at": 0.0,
            "cache_size": 0,
        }

    # -------------------------------------------------------------------
    # Per-cycle coin exclusion
    # -------------------------------------------------------------------

    def clear_cycle_coins(self):
        """Reset the per-cycle used-coin set.

        Called by bot_loop at the start of every trading cycle so that
        coins confirmed on-chain since the last cycle become available
        again, while coins used earlier *within* the same cycle stay
        excluded until the cycle boundary.
        """
        self._cycle_used_coin_ids.clear()

    # -------------------------------------------------------------------
    # Fix F: Slot suspension management
    # -------------------------------------------------------------------

    def record_slot_coin_failure(self, side: str, slot: int):
        """Record a coin preselection failure for a slot.

        After _slot_suspend_threshold consecutive failures, the slot is
        suspended to prevent infinite retry loops in recovery mode.
        Suspended slots auto-clear after 20 cycles (self-heal) so the
        ladder doesn't permanently degrade if the topup worker restocks.
        """
        key = f"{side}_{slot}"
        count = self._slot_fail_counts.get(key, 0) + 1
        self._slot_fail_counts[key] = count
        if count >= self._slot_suspend_threshold and key not in self._suspended_slots:
            self._suspended_slots.add(key)
            self._slot_suspended_at = getattr(self, '_slot_suspended_at', {})
            self._slot_suspended_at[key] = time.time()
            # Rate-limit the warning to once per cooldown window per slot.
            # During sustained coin exhaustion the suspend→auto-clear→suspend
            # cycle fires every ~20 cycles; without this guard the same slot
            # generates a new WARNING every ~15 minutes indefinitely.
            _now = time.time()
            _last_warn = self._slot_warned_at.get(key, 0.0)
            if (_now - _last_warn) >= self._slot_warn_cooldown:
                self._slot_warned_at[key] = _now
                log_event("warning", "slot_suspended",
                          f"Slot {side} #{slot} suspended after {count} consecutive "
                          f"coin failures — will retry when coins are available")
        # F63: auto-clear after 20 cycles (~10 minutes at typical loop speed).
        # This prevents permanent ladder degradation if unsuspend_slots
        # never fires or coins are replenished via topup without triggering
        # the explicit unsuspend check.
        if count > self._slot_suspend_threshold + 20:
            self._slot_fail_counts[key] = 0
            self._suspended_slots.discard(key)
            log_event("info", "slot_suspension_expired",
                      f"Slot {side} #{slot} suspension expired after {count} "
                      f"cycles — re-enabling for next attempt")

    def clear_slot_failure(self, side: str, slot: int):
        """Clear the failure counter for a slot after successful creation."""
        key = f"{side}_{slot}"
        self._slot_fail_counts.pop(key, None)
        self._suspended_slots.discard(key)

    def is_slot_suspended(self, side: str, slot: int) -> bool:
        """Check if a slot is currently suspended due to coin exhaustion."""
        return f"{side}_{slot}" in self._suspended_slots

    def get_suspended_slot_count(self, side: str) -> int:
        """Count how many slots are suspended for a given side."""
        prefix = f"{side}_"
        return sum(1 for k in self._suspended_slots if k.startswith(prefix))

    def unsuspend_slots_if_coins_available(self, side: str):
        """Unsuspend slots for a side if spare tier coins have become available.

        Called by bot_loop at the start of each cycle to check whether
        previously exhausted coin pools have been replenished.

        Uses the DB coin tracking (which knows tier designations) rather than
        the raw wallet RPC to avoid counting fee/sniper/reserve coins that
        cannot be used for offer creation — those would cause an endless
        suspend → unsuspend → fail cycle.
        """
        prefix = f"{side}_"
        suspended_for_side = [k for k in self._suspended_slots if k.startswith(prefix)]
        if not suspended_for_side:
            return

        wallet_type = "cat" if side == "sell" else "xch"
        try:
            # Count only tier-designated trading coins (excludes fee, sniper,
            # reserve, dust and unknown coins which cannot fill offer slots).
            from database import get_free_coins
            db_free = get_free_coins(wallet_type)
            _TRADING_DESIGS = {"tier_spare", "tier_active"}
            _SKIP_TIERS = {"none", "sniper", "reserve", "fee"}
            spare_count = sum(
                1 for c in db_free
                if c.get("designation", "") in _TRADING_DESIGS
                and c.get("assigned_tier", "none") not in _SKIP_TIERS
            )
            if spare_count > 0:
                for key in suspended_for_side:
                    self._suspended_slots.discard(key)
                    self._slot_fail_counts.pop(key, None)
                log_event("info", "slots_unsuspended",
                          f"Unsuspended {len(suspended_for_side)} {side} slots — "
                          f"{spare_count} spare tier coins now available")
        except Exception as e:
            log_event("debug", "slot_unsuspend_check_failed",
                      f"Could not check coins for slot unsuspension: {e}")

    # -------------------------------------------------------------------
    # Coin ID Extraction (for before/after snapshot)
    # -------------------------------------------------------------------

    @staticmethod
    def _extract_coin_id_set(rpc_result) -> set:
        """Extract a set of unique coin IDs from a get_spendable_coins RPC response.

        The RPC returns {"success": true, "confirmed_records": [...]}.
        Each record has a nested "coin" dict with parent_coin_info, puzzle_hash, amount.

        IMPORTANT: The wallet does NOT return a "name" field on this Chia version.
        Multiple coins can share the same parent_coin_info (from splits).
        The unique coin ID must be computed as SHA256(parent + puzzle_hash + amount).
        Uses _coin_id_from_record from coin_manager.py which handles this correctly.
        """
        from coin_manager import _coin_id_from_record
        ids = set()
        if not rpc_result or not isinstance(rpc_result, dict):
            return ids
        records = rpc_result.get("confirmed_records") or rpc_result.get("records") or []
        for r in records:
            cid = _coin_id_from_record(r)
            if cid:
                ids.add(cid)
        return ids

    # -------------------------------------------------------------------
    # Coin Selection (V3 — deterministic coin locking via Sage PR#761)
    # -------------------------------------------------------------------

    @staticmethod
    def _coin_designation_priority(designation: str,
                                   assigned_tier: str,
                                   preferred_tier: str = None) -> int:
        """Sort priority for designated free coins."""
        desig = (designation or "unknown").lower()
        tier = (assigned_tier or "none").lower()
        pref = (preferred_tier or "").lower()

        if pref:
            if desig == "tier_spare" and tier == pref:
                return 0
            if desig == "tier_active" and tier == pref:
                return 1
            if desig == "tier_spare":
                return 2
            if desig == "tier_active":
                return 3
            if desig == "dust":
                return 4
            return 5

        if desig == "tier_spare":
            return 0
        if desig == "tier_active":
            return 1
        if desig == "dust":
            return 2
        return 3

    def _select_coin_for_offer(self, wallet_id: int, amount_mojos: int,
                                used_coins: set = None,
                                preferred_tier: str = None,
                                strict_preferred_tier: bool = False,
                                spendable_records: List[Dict] = None,
                                exclude_coin_ids: set = None,
                                max_amount_mojos: int = None,
                                tier_sizes_mojos: Optional[Dict[str, int]] = None) -> Optional[str]:
        """Pre-select the best coin for an offer before creating it.

        Instead of letting the wallet auto-select (and then polling to
        find out which coin it picked), we choose the coin ourselves
        and pass it via coin_ids to make_offer. This gives us:
        - Deterministic coin locking (we know exactly which coin)
        - No polling delay (~45x faster batch creation)
        - No coin reuse risk (we track used coins in-batch)

        Strategy: closest-fit — pick the smallest coin that's large enough.
        This minimises waste (avoids using a 10 XCH coin for a 0.1 XCH offer).

        Args:
            wallet_id: Which wallet to query (1=XCH, CAT wallet ID for CATs)
            amount_mojos: How much this offer needs to spend (in mojos)
            used_coins: Set of coin_ids already used in this batch (reuse guard)
            max_amount_mojos: Upper bound on coin size (exclusive). When set,
                coins larger than this are rejected even as fallback. This
                prevents a 5 XCH coin being selected for a 0.634 XCH offer in
                exact_tier_spend_mode — locking 87% of the coin as change.
                When no coin fits within [amount_mojos, max_amount_mojos],
                returns None so the slot suspends and triggers a topup.
            tier_sizes_mojos: Optional mapping of tier name → mojos used to
                strict-validate candidate coins via
                :func:`coin_classifier.classify_coin`. When provided AND
                ``preferred_tier`` is set, coins classified as a MISFIT for
                that tier (under the SSOT 0.98/1.5 bounds) are rejected —
                even if they satisfy the raw amount_mojos / max_amount_mojos
                window. This prevents the 2026-04-17 regression where the
                offer selector happily accepted a 23.4k CAT coin for an
                "inner" slot even though strict bounds classified it as a
                misfit, producing a ragged ladder shape on Dexie.
                Leave None to preserve legacy behaviour.

        Returns:
            coin_id string if a suitable coin is found, None otherwise.
            When None is returned, the caller should fall back to polling.
        """
        from coin_manager import _coin_id_from_record

        if used_coins is None:
            used_coins = set()

        # SSOT misfit rejection — precompute once so it's cheap to apply per
        # candidate. Only active when the caller supplied both
        # preferred_tier and tier_sizes_mojos. See the F70 docstring above
        # for why this exists.
        #
        # Design note: we only reject TRUE misfits here (coins that fit no
        # configured tier under the 0.98/1.5 bounds). Reserve and dust coins
        # are NOT rejected by this check — other pre-existing filters
        # (designation == "reserve", size floor vs amount_mojos) handle
        # those categories. Narrowing the check to misfits only keeps F70
        # targeted at the ladder-shape regression without breaking legacy
        # callers that use oversize coins for tier-agnostic fallback paths.
        _reject_misfit = bool(preferred_tier and tier_sizes_mojos)
        if _reject_misfit:
            from coin_classifier import classify_coin, CoinDesignation

            _pref_lower = (preferred_tier or "").lower()

            def _coin_fits_preferred_tier(coin_amount_mojos: int) -> bool:
                """Returns True when the coin is usable for ``preferred_tier``.

                Rules, in order:
                  1. Misfits and dust are always rejected (F70 invariant).
                  2. Reserve-sized coins pass here; other selector filters
                     (``max_amount_mojos``, designation == "reserve") decide
                     whether they're actually usable for this slot.
                  3. Tier-fit coins must match ``preferred_tier`` EXACTLY —
                     this is the 2026-04-18 slot-21/23 taper fix. Before
                     this line, a mid-sized coin could back an outer-
                     position slot (reverse-buy: outer position ↔ mid size)
                     simply because it wasn't a misfit. Now we require the
                     classifier's best_tier to equal the caller's preferred
                     tier so wrong-sized coins fail the selector cleanly
                     (→ slot skip → topup backfill) instead of landing on
                     the ladder as a taper violation.
                """
                cls = classify_coin(coin_amount_mojos, tier_sizes_mojos)
                if cls.is_misfit:
                    return False
                # Dust and reserve coins pass F70 here — other selector
                # filters (coin_amount < amount_mojos, max_amount_mojos,
                # designation == "reserve") decide whether they're usable.
                # We don't want to duplicate those rejections here.
                if cls.designation != CoinDesignation.TIER_SPARE:
                    return True
                # For tier-spare coins we require an EXACT match with
                # preferred_tier. This is the 2026-04-18 taper fix: a mid-
                # sized coin was backing an outer-position slot under
                # reverse-buy (outer position ↔ mid size) because it wasn't
                # a misfit. Requiring best_tier == preferred_tier forces
                # the selector to return None when no correctly-sized coin
                # is available, which triggers clean slot-skip → topup
                # backfill instead of building a ragged ladder.
                best = (cls.best_tier or "").lower() if cls.best_tier else ""
                return bool(best) and best == _pref_lower
        else:
            _coin_fits_preferred_tier = None  # type: ignore

        try:
            if spendable_records is None:
                rpc_result = get_exact_spendable_coins_rpc(wallet_id)
                if not rpc_result or not rpc_result.get("success"):
                    return None
                records = rpc_result.get("confirmed_records") or rpc_result.get("records") or []
            else:
                records = spendable_records
            wallet_type = "xch" if wallet_id == cfg.WALLET_ID_XCH else "cat"

            spendable_amounts = {}
            fallback_candidates = []
            for r in records:
                coin_id = _coin_id_from_record(r)
                if not coin_id:
                    continue

                coin_id = coin_id.lower()
                coin_data = r.get("coin", {})
                coin_amount = int(coin_data.get("amount", 0))
                spendable_amounts[coin_id] = coin_amount

                if coin_id in used_coins or coin_amount < amount_mojos:
                    continue
                if coin_id in self._cycle_used_coin_ids:
                    continue
                if exclude_coin_ids and coin_id in exclude_coin_ids:
                    continue
                # Reject coins that are too large when a size cap is set.
                # A 5 XCH coin for a 0.634 XCH offer locks 87% as change and
                # creates a cascading wrong-size cycle. Fail cleanly instead.
                if max_amount_mojos is not None and coin_amount > max_amount_mojos:
                    continue
                # F70 SSOT misfit guard: reject coins that the unified
                # classifier says don't fit the preferred tier. Without this,
                # a 23.4k-CAT change coin from a past fill could be used to
                # back an "inner" slot even though it's 12% below inner's
                # strict floor — producing ragged ladder shape like the
                # 2026-04-17 incident.
                if _coin_fits_preferred_tier is not None:
                    if not _coin_fits_preferred_tier(coin_amount):
                        continue

                fallback_candidates.append((coin_amount - amount_mojos, coin_id, coin_amount))

            pref = (preferred_tier or "").lower()
            strict_pref = bool(pref and strict_preferred_tier)
            db_free_coins = []
            reserve_ids = set()
            try:
                from database import get_free_coins, get_reserve_coins
                db_free_coins = get_free_coins(wallet_type)
                reserve_ids = {
                    str(c.get("coin_id", "")).strip().lower()
                    for c in get_reserve_coins(wallet_type)
                    if c.get("coin_id")
                }
            except Exception as e:
                log_event("debug", "coin_select_db_unavailable",
                          f"DB coin inventory unavailable for {wallet_type}: {e}")

            if db_free_coins:
                designated_candidates = []
                for coin in db_free_coins:
                    coin_id = str(coin.get("coin_id", "")).strip().lower()
                    if not coin_id or coin_id in used_coins:
                        continue

                    designation = (coin.get("designation") or "unknown").lower()
                    assigned_tier = (coin.get("assigned_tier") or "none").lower()
                    if designation == "reserve" or coin_id in reserve_ids:
                        continue
                    if assigned_tier == "sniper" and pref != "sniper":
                        continue
                    if strict_pref:
                        if designation not in ("tier_spare", "tier_active"):
                            continue
                        if assigned_tier != pref:
                            continue

                    coin_amount = spendable_amounts.get(coin_id)
                    if coin_amount is None or coin_amount < amount_mojos:
                        continue
                    if max_amount_mojos is not None and coin_amount > max_amount_mojos:
                        continue
                    # F70 SSOT misfit guard — see the fallback loop above
                    # for the full rationale. Applies here too so that a
                    # misfit-sized coin that WAS incorrectly designated
                    # tier_spare/<tier> (legacy reconcile before the
                    # classify_coin() change took effect) still gets
                    # rejected at selection time.
                    if _coin_fits_preferred_tier is not None:
                        if not _coin_fits_preferred_tier(coin_amount):
                            continue

                    priority = self._coin_designation_priority(
                        designation, assigned_tier, preferred_tier
                    )
                    designated_candidates.append(
                        (priority, coin_amount - amount_mojos, coin_id, coin_amount,
                         designation, assigned_tier)
                    )

                if designated_candidates:
                    designated_candidates.sort(key=lambda x: (x[0], x[1]))
                    _, best_surplus, best_coin_id, best_amount, best_desig, best_tier = designated_candidates[0]
                    log_event("debug", "coin_selected",
                              f"Selected designated coin {best_coin_id[:16]}... "
                              f"({best_amount} mojos, surplus={best_surplus}, "
                              f"{best_desig}/{best_tier})")
                    return best_coin_id

                log_event("debug", "coin_select_none",
                          f"No eligible designated {wallet_type.upper()} coins for "
                          f"{amount_mojos} mojos (preferred_tier={preferred_tier or 'any'}, "
                          f"{len(db_free_coins)} DB free, {len(used_coins)} used) "
                          f"— falling through to any available coin")
                # Don't return None here — fall through to fallback candidates
                # so that coins from other tiers can be used rather than failing
                # the entire offer creation.

            if strict_pref:
                log_event("debug", "coin_select_none",
                          f"No strict {pref} coin available for {amount_mojos} mojos "
                          f"(wallet {wallet_id}, {len(records)} spendable, "
                          f"{len(used_coins)} used in batch)")
                return None

            candidates = [
                item for item in fallback_candidates
                if item[1] not in reserve_ids
            ]

            if candidates:
                candidates.sort(key=lambda x: x[0])
                best_surplus, best_coin_id, best_amount = candidates[0]

                log_event("debug", "coin_selected",
                          f"Selected fallback coin {best_coin_id[:16]}... "
                          f"({best_amount} mojos, surplus={best_surplus}) "
                          f"from {len(candidates)} candidates")
                return best_coin_id

        except Exception as e:
            log_event("warning", "coin_select_error",
                      f"Coin selection failed: {e} — will fall back to polling")
            return None

    @staticmethod
    def _slot_size_variation(slot: int, expected_unique_count: int = 100) -> Decimal:
        """Return a deterministic per-slot size delta for uniqueness.

        The step is adaptive:
        - for small ladders, use larger visible nudges (around 1e-5 XCH)
        - for very large ladders, shrink toward 1e-8 XCH so thousands of
          offers still fit under the 0.001 XCH ceiling
        """
        if slot < 0:
            slot = 0
        if expected_unique_count <= 0:
            expected_unique_count = 1
        min_step = Decimal("0.00000001")
        max_step = Decimal("0.00001000")
        dynamic_step = Decimal("0.001") / Decimal(expected_unique_count)
        step = max(min_step, min(max_step, dynamic_step))
        variation = step * Decimal(slot + 1)
        max_variation = Decimal("0.001")
        if variation > max_variation:
            variation = max_variation
        return variation.quantize(Decimal("0.00000001"))

    @staticmethod
    def _size_key(size_xch: Decimal) -> Decimal:
        """Normalize offer sizes to the on-chain/display precision we care about."""
        return Decimal(str(size_xch)).quantize(Decimal("0.00000001"))

    @staticmethod
    def _requested_amount_from_open_offer(open_offer: Dict, side: str,
                                          decimals: int) -> Optional[int]:
        """Extract the requested-side amount from an open offer in raw mojos."""
        raw_amount = open_offer.get("size_cat") if side == "buy" else open_offer.get("size_xch")
        if raw_amount in (None, ""):
            return None
        amount_decimal = Decimal(str(raw_amount))
        if side == "buy":
            return cat_to_mojos(amount_decimal, decimals)
        return xch_to_mojos(amount_decimal)

    def _allocate_unique_requested_mojos(self, base_requested_mojos: int,
                                         slot: int,
                                         used_requested_amounts: set) -> int:
        """Return a requested amount that doesn't collide with live/batch offers."""
        candidate = int(base_requested_mojos)
        if candidate not in used_requested_amounts:
            used_requested_amounts.add(candidate)
            return candidate

        probe_slot = slot
        for _ in range(1000):
            candidate = int(base_requested_mojos) + max(1, probe_slot + 1)
            if candidate not in used_requested_amounts:
                used_requested_amounts.add(candidate)
                return candidate
            probe_slot += 1
        # Exhausted uniqueness attempts — return last probe
        log_event("warning", "uniqueness_exhausted",
                  "Could not find unique requested_mojos after 1000 attempts")
        used_requested_amounts.add(candidate)
        return candidate

    def _allocate_unique_size_xch(self, base_size: Decimal, slot: int,
                                  tier_mode: bool,
                                  used_size_keys: set,
                                  expected_unique_count: int) -> Decimal:
        """Pick a size variation that does not collide with existing live offers."""
        probe_slot = slot
        for _ in range(1000):
            variation = self._slot_size_variation(
                probe_slot,
                expected_unique_count=expected_unique_count,
            )
            if tier_mode:
                candidate = max(Decimal("0.000001"), base_size - variation)
            else:
                candidate = base_size + variation

            key = self._size_key(candidate)
            if key not in used_size_keys:
                used_size_keys.add(key)
                return key
            probe_slot += 1
        # Exhausted uniqueness attempts — return last probe
        log_event("warning", "uniqueness_exhausted",
                  "Could not find unique size_xch after 1000 attempts")
        used_size_keys.add(key)
        return key

    @staticmethod
    def _get_ladder_parallelism(coin_ids_enabled: bool) -> int:
        """Choose a safe worker count for live offer creation.

        Only allows parallelism when coin_ids are both enabled AND the
        wallet backend actually sends them in the RPC payload. Currently
        only Sage supports coin_ids; Chia wallet silently ignores them,
        so parallel creates would race on coin selection.
        """
        if not coin_ids_enabled:
            return 1
        # Chia wallet doesn't pass coin_ids to the RPC — force serial
        try:
            from wallet import get_wallet_type
            if get_wallet_type() != "sage":
                return 1
        except Exception:
            return 1
        try:
            configured = int(getattr(cfg, "LADDER_CREATE_PARALLELISM", 5) or 5)
        except Exception:
            configured = 5
        return max(1, configured)

    def get_replenishment_slots(self, side: str, total_slots: int,
                                cat_asset_id: str = None,
                                live_offer_ids: set = None) -> List[int]:
        """Plan which canonical ladder slots should be replenished next.

        Uses the live open offer counts per tier to determine which tiers are
        short relative to the full ladder shape. This avoids treating a refill
        of 1-2 offers as a brand new mini-ladder, which would otherwise skew
        replenishment toward inner/outer tiers.

        Args:
            live_offer_ids: Set of trade_ids currently confirmed open in the
                wallet (from this loop's sync_from_wallet call).  When provided,
                DB offers whose trade_id is NOT in this set are treated as
                already-expired and their tier slot is counted as empty.  This
                fixes a 1-cycle reconciliation lag where expired offers still
                show as 'open' in the DB when replenishment runs, causing new
                offers to land in the wrong tier position.
        """
        asset_id = cat_asset_id or cfg.CAT_ASSET_ID

        if total_slots <= 0:
            return []

        tier_slots: Dict[str, List[int]] = {}
        for slot in range(total_slots):
            tier = self._classify_tier(slot, total_slots, side=side)
            tier_slots.setdefault(tier, []).append(slot)

        if not cfg.TIER_ENABLED:
            return list(tier_slots.get("mid", []))

        live_counts = {tier: 0 for tier in tier_slots}
        for offer in get_open_offers(side=side, cat_asset_id=asset_id):
            tier = (offer.get("tier") or "mid").lower()
            if tier not in live_counts:
                continue
            # If we have live wallet IDs, only count offers that are still
            # confirmed open in the wallet.  Offers in DB but gone from the
            # wallet have expired/been filled and their slot is available.
            if live_offer_ids is not None:
                trade_id = offer.get("trade_id") or ""
                if trade_id and trade_id not in live_offer_ids:
                    continue  # expired — don't count, the slot is free
            live_counts[tier] += 1

        planned_slots: List[int] = []
        for tier in ("inner", "mid", "outer", "extreme"):
            slots = tier_slots.get(tier, [])
            if not slots:
                continue
            live_count = live_counts.get(tier, 0)
            needed = len(slots) - live_count
            if needed <= 0:
                continue
            # Fill from the INNERMOST slots (front of the list, closest to mid)
            # so that replenishments after fills land back at the tightest
            # price position rather than the outermost end of the tier.
            #
            # Previous behaviour was slots[live_count:] (tail = outermost),
            # which caused a filled inner-tier offer to be replaced near the
            # outer boundary of that tier — not like-for-like.
            planned_slots.extend(slots[:needed])
        return planned_slots

    @staticmethod
    def _normalize_offer_ref(value: str) -> str:
        """Normalize offer hashes/trade ids for exact Sage offer_id comparison."""
        if not value:
            return ""
        normalized = str(value).strip().lower()
        if normalized.startswith("0x"):
            normalized = normalized[2:]
        return normalized

    @staticmethod
    def _normalize_coin_ref(value: str) -> str:
        """Normalize coin ids to lowercase 0x-prefixed form."""
        if not value:
            return ""
        normalized = str(value).strip().lower()
        if not normalized.startswith("0x"):
            normalized = "0x" + normalized
        return normalized

    def _sort_open_offers_for_requote(self, offers: List[Dict], side: str,
                                      mid_price: Decimal = None) -> List[Dict]:
        """Sort live ladder offers so the most at-risk are cancelled first.

        Fix D: During a requote triggered by AMM drift, the offers closest to
        the new mid price are most at risk of being taken at stale prices.
        Sort by distance from the new mid price (ascending) so these inner
        offers are cancelled first.

        When mid_price is not provided, falls back to tier-based inner-out
        ordering (legacy behaviour).
        """
        if mid_price is not None and mid_price > 0:
            # Sort by distance from new mid price — closest first (most at risk)
            def _key_distance(offer: Dict):
                try:
                    price = Decimal(str(offer.get("price_xch") or "0"))
                except Exception:
                    price = Decimal("0")
                distance = abs(price - mid_price)
                # Tiebreaker: created_at so order is deterministic
                created_at = str(offer.get("created_at") or "")
                return (distance, created_at)

            return sorted(list(offers or []), key=_key_distance)

        # Fallback: tier-based inner-out ordering
        tier_rank = {
            "inner": 0,
            "mid": 1,
            "outer": 2,
            "extreme": 3,
        }

        def _key(offer: Dict):
            tier = str(offer.get("tier") or "mid").lower()
            rank = tier_rank.get(tier, 99)
            try:
                price = Decimal(str(offer.get("price_xch") or "0"))
            except Exception:
                price = Decimal("0")
            price_sort = -price if side == "buy" else price
            created_at = str(offer.get("created_at") or "")
            return (rank, price_sort, created_at)

        return sorted(list(offers or []), key=_key)

    def _get_sage_locked_coin_ids_for_trade(self, wallet_id: int, trade_id: str) -> Optional[List[str]]:
        """Ask Sage which owned coins are locked by a specific offer_id/trade_id."""
        if get_wallet_type() != "sage" or wallet_id is None or not trade_id:
            return None
        try:
            detailed_map = get_owned_coins_detailed(wallet_id)
        except Exception as e:
            log_event("warning", "coin_ids_verify_failed",
                      f"Could not inspect Sage locked coins for {trade_id[:12]}...: {e}")
            return None
        if detailed_map is None:
            return None

        wanted_offer_id = self._normalize_offer_ref(trade_id)
        locked_coin_ids = []
        for coin_id, info in detailed_map.items():
            offer_id = self._normalize_offer_ref((info or {}).get("offer_id"))
            if offer_id == wanted_offer_id:
                locked_coin_ids.append(self._normalize_coin_ref(coin_id))
        return sorted(set(locked_coin_ids))

    def _verify_sage_offer_locked_inputs(self, wallet_id: int, trade_id: str,
                                         selected_coin_id: str,
                                         max_polls: int = 6) -> Dict:
        """Inspect which maker inputs Sage actually locked for a new offer."""
        normalized_selected = self._normalize_coin_ref(selected_coin_id)
        for poll in range(max_polls):
            locked_coin_ids = self._get_sage_locked_coin_ids_for_trade(wallet_id, trade_id)
            if locked_coin_ids:
                return {
                    "verified": True,
                    "locked_coin_ids": locked_coin_ids,
                    "selected_present": normalized_selected in locked_coin_ids,
                }
            if poll < max_polls - 1:
                time.sleep(1)
        return {
            "verified": False,
            "locked_coin_ids": [],
            "selected_present": False,
        }

    # -------------------------------------------------------------------
    # Offer Creation
    # -------------------------------------------------------------------

    def create_offer_with_retry(self, offer_dict: dict, max_retries: int = 2,
                                 expiry_offset: int = 0,
                                 expiry_secs: int = None,
                                 used_coins: set = None,
                                 coin_ids_enabled: bool = False,
                                 selected_coin_id: str = None,
                                 preferred_tier: str = None,
                                 strict_preferred_tier: bool = False) -> Optional[Dict]:
        """Create a Chia offer with automatic retry on transient errors.

        Thread-safe: acquires self._lock to prevent concurrent coin selection
        from different threads (main loop, sniper, boost) choosing the same coin.

        Handles the "Wallet needs to be fully synced" error that occurs
        briefly during heavy operations: retry with backoff until the wallet
        reports fully synced or the retry budget is exhausted.

        Two coin detection modes:
        1. coin_ids mode (V3): Pre-select a coin and pass it to make_offer.
           The wallet locks exactly that coin — no polling needed. ~45x faster.
        2. Polling mode (V2 fallback): Snapshot coins before/after, poll to
           detect which coin disappeared. Used when coin_ids is disabled or
           when coin selection fails.

        Args:
            offer_dict: {str(wallet_id): amount_mojos} — negative=spend, positive=receive
            max_retries: How many times to retry on transient errors
            expiry_offset: Extra seconds added to expiry for staggering
            expiry_secs: Override expiry duration (e.g., short expiry for sniper offers)
            used_coins: Set of coin_ids already used in this batch (reuse guard)
            preferred_tier: Optional target tier ('inner', 'mid', 'outer',
                'extreme', 'sniper'). Matching designated spares are preferred.
            strict_preferred_tier: When True, only coins in preferred_tier are
                eligible. If none are available, offer creation fails cleanly.
            coin_ids_enabled: If True, pre-select coins via _select_coin_for_offer()
            selected_coin_id: Optional coin ID chosen by the caller. When
                provided, this is used directly and we do not re-select.

        Returns the wallet RPC response, or None on failure.
        The response will include a 'locked_coin_id' key if coin detection succeeded.
        """
        # --- Reservation lease ---
        # Acquire a soft capacity hold before hitting the wallet.  This prevents
        # concurrent threads (sniper, boost, main loop) from over-allocating the
        # same balance.  We fail-open on any reservation system error so that a
        # broken DB never blocks offer creation.
        _reservation_id: Optional[str] = None
        try:
            from reservation_manager import ReservationManager as _RM
            _xch_spend = 0
            _cat_spend = 0
            _xch_wid = getattr(cfg, "WALLET_ID_XCH", 1)
            _cat_wid = getattr(cfg, "CAT_WALLET_ID", 2)
            for _wid, _amt in offer_dict.items():
                if int(_amt) < 0:
                    if int(_wid) == _xch_wid:
                        _xch_spend += abs(int(_amt))
                    elif int(_wid) == _cat_wid:
                        _cat_spend += abs(int(_amt))
            if _xch_spend > 0 or _cat_spend > 0:
                _rm = _RM()
                _res = _rm.try_acquire(
                    purpose=f"create_offer_{preferred_tier or 'default'}",
                    xch_mojos=_xch_spend,
                    cat_mojos=_cat_spend,
                    lease_secs=90,
                )
                if _res.success:
                    _reservation_id = _res.reservation_id
        except Exception:
            pass  # fail-open — reservation is a guard, not a blocker

        try:
            return self._create_offer_with_retry_inner(
                offer_dict=offer_dict,
                max_retries=max_retries,
                expiry_offset=expiry_offset,
                expiry_secs=expiry_secs,
                used_coins=used_coins,
                coin_ids_enabled=coin_ids_enabled,
                selected_coin_id=selected_coin_id,
                preferred_tier=preferred_tier,
                strict_preferred_tier=strict_preferred_tier,
            )
        finally:
            if _reservation_id:
                try:
                    from reservation_manager import ReservationManager as _RM2
                    _RM2().release(_reservation_id, status="completed")
                except Exception:
                    pass

    def _create_offer_with_retry_inner(
            self, offer_dict: dict, max_retries: int = 2,
            expiry_offset: int = 0,
            expiry_secs: int = None,
            used_coins: set = None,
            coin_ids_enabled: bool = False,
            selected_coin_id: str = None,
            preferred_tier: str = None,
            strict_preferred_tier: bool = False) -> Optional[Dict]:
        """Internal implementation — called by create_offer_with_retry after
        the reservation lease is acquired.  See create_offer_with_retry for
        full documentation."""
        # On-chain expiry — offers auto-expire and vanish from Dexie.
        # The fill tracker's mass disappearance guard (3-strike rule)
        # handles the phantom fill risk from expired offers.
        # expiry_secs parameter allows override (e.g., shorter for sniper).
        _expiry = expiry_secs if expiry_secs is not None else cfg.OFFER_EXPIRY_SECS
        if _expiry and _expiry > 0:
            # Stagger expiry across offers to avoid mass-expiry cascades
            stagger = expiry_offset * cfg.OFFER_STAGGER_SECS if expiry_offset else 0
            offer_max_time = int(time.time()) + _expiry + stagger
        else:
            offer_max_time = 0

        # Coin selection hints — tell the wallet what size coin to use.
        # Range: 80%-200% of spend amount. Tight enough to pick the right
        # tier, loose enough to not fail when coins aren't perfectly sized.
        # The min_coin_amount of 80% prevents using undersized coins.
        # The max_coin_amount of 200% prevents wasting large reserve coins.
        # If this still fails (e.g. all coins are much larger), the wallet
        # will return an error and we can retry without hints.
        spend_amount = 0
        spend_wallet_id = None
        for wid, amt in offer_dict.items():
            if int(amt) < 0:
                if abs(int(amt)) > spend_amount:
                    spend_amount = abs(int(amt))
                    spend_wallet_id = int(wid)
        # Hint: use coins between 80% and 200% of the spend amount
        min_coin_hint = int(spend_amount * 0.8) if spend_amount > 0 else None
        max_coin_hint = int(spend_amount * 2.0) if spend_amount > 0 else None

        # --- V3 Coin Selection Mode ---
        # When coin_ids_enabled=True, we pre-select a specific coin and pass it
        # to the wallet via coin_ids. The wallet locks exactly that coin, so we
        # don't need before/after snapshot polling. ~45x faster for batch creation.
        # If selection fails, we fall back to the V2 polling mode below.
        #
        # Lock protects coin selection so concurrent threads (main loop, sniper,
        # boost) cannot pick the same coin. Released before wallet RPC calls.
        caller_selected_coin_id = selected_coin_id
        use_coin_ids_mode = False

        with self._lock:
            if caller_selected_coin_id and spend_wallet_id is not None and spend_amount > 0:
                # Check inflight set to prevent sniper/main loop overlap
                if caller_selected_coin_id in self._inflight_coin_ids:
                    log_event("warning", "coin_ids_locked",
                              f"Coin {caller_selected_coin_id[:16]}... already in-flight, skipping")
                    return {"success": False, "error": "coin_inflight"}
                selected_coin_id = caller_selected_coin_id
                use_coin_ids_mode = True
                self._inflight_coin_ids.add(selected_coin_id)
                log_event("debug", "coin_ids_mode",
                          f"Using caller-selected coin: {selected_coin_id[:16]}... "
                          f"for {spend_amount} mojos")
            elif coin_ids_enabled and spend_wallet_id is not None and spend_amount > 0:
                selected_coin_id = self._select_coin_for_offer(
                    spend_wallet_id, spend_amount, used_coins,
                    preferred_tier=preferred_tier,
                    strict_preferred_tier=strict_preferred_tier,
                    exclude_coin_ids=self._inflight_coin_ids,
                )
                if selected_coin_id:
                    use_coin_ids_mode = True
                    self._inflight_coin_ids.add(selected_coin_id)
                    log_event("debug", "coin_ids_mode",
                              f"Using coin_ids mode: {selected_coin_id[:16]}... "
                              f"for {spend_amount} mojos")
                elif strict_preferred_tier and preferred_tier:
                    log_event("warning", "coin_ids_no_preferred_tier",
                              f"No {preferred_tier} coin available for {spend_amount} mojos")
                    return {
                        "success": False,
                        "error": "no_preferred_tier_coin",
                        "preferred_tier": preferred_tier,
                    }
                else:
                    log_event("debug", "coin_ids_fallback",
                              "Coin selection returned None — falling back to polling mode")

        # Track which coin was claimed for inflight cleanup
        _inflight_claimed = selected_coin_id if use_coin_ids_mode else None

        # --- Before snapshot (V2 polling mode only) ---
        # Only needed when NOT using coin_ids mode.
        # get_spendable_coins_rpc returns {"success": true, "confirmed_records": [...]}
        # Each record has nested "coin" dict: {"parent_coin_info": "...", "amount": N}
        # We use "name" (computed coin ID) if available, else "parent_coin_info"
        before_coin_ids = set()
        if not use_coin_ids_mode and spend_wallet_id is not None:
            try:
                rpc_result = get_exact_spendable_coins_rpc(spend_wallet_id)
                before_coin_ids = self._extract_coin_id_set(rpc_result)
            except Exception as e:
                log_event("warning", "coin_snapshot_before_fail",
                          f"Could not snapshot coins before offer: {e}")

        # Offers are created with fee=0 so no fee coin is needed.
        # Fee coins are reserved only for coin management transactions
        # (splits, combines) where a non-zero tx fee is actually charged.
        try:
            for attempt in range(max_retries + 1):
                # Pass coin_ids to wallet if we pre-selected a coin
                if use_coin_ids_mode and selected_coin_id:
                    res = create_offer(offer_dict, validate_only=False, max_time=offer_max_time,
                                       min_coin_amount=min_coin_hint, max_coin_amount=max_coin_hint,
                                       coin_ids=[selected_coin_id])
                else:
                    res = create_offer(offer_dict, validate_only=False, max_time=offer_max_time,
                                       min_coin_amount=min_coin_hint, max_coin_amount=max_coin_hint)

                if res and res.get("success"):
                    # Include expiry info so caller can record it in DB
                    res["offer_max_time"] = offer_max_time

                    # --- Coin detection: two paths ---
                    if use_coin_ids_mode and selected_coin_id:
                        # PATH 1: coin_ids mode — we know which coin we asked Sage to use.
                        # The ladder path will still verify the wallet's exact offer_id
                        # lock attribution before posting the offer live.
                        res["locked_coin_id"] = selected_coin_id
                        log_event("debug", "coin_ids_locked",
                                  f"coin_ids mode: selected coin {selected_coin_id[:16]}... "
                                  f"recorded for post-create verification")
                    elif before_coin_ids and spend_wallet_id is not None:
                        # PATH 2: V2 polling mode — snapshot before/after to detect lock.
                        # Poll until the wallet confirms the coin is actually locked.
                        # The Chia wallet can be slow to propagate coin locks, especially
                        # for CAT wallets. Without this, the next offer may reuse the
                        # same coin (creating overlapping offers on Dexie).
                        locked_coin = None
                        max_lock_polls = 5  # Up to 5 seconds waiting for lock
                        for poll in range(max_lock_polls):
                            time.sleep(1)
                            try:
                                rpc_result = get_exact_spendable_coins_rpc(spend_wallet_id)
                                after_coin_ids = self._extract_coin_id_set(rpc_result)
                                missing = before_coin_ids - after_coin_ids
                                if len(missing) >= 1:
                                    # Pick the coin that disappeared
                                    if len(missing) == 1:
                                        locked_coin = missing.pop()
                                    else:
                                        # Multiple coins vanished between snapshots — expected
                                        # during parallel offer creation (other threads locked
                                        # their own coins in the same interval). Downstream
                                        # offer-id lock attribution still verifies the exact
                                        # coin owned by this offer, so picking one arbitrarily
                                        # is safe. Keep at debug to avoid ladder-burst noise.
                                        log_event("debug", "coin_snapshot_multi",
                                                  f"Parallel offer creation: {len(missing)} coins "
                                                  f"locked between snapshots; picking first")
                                        locked_coin = sorted(missing)[0]
                                    break  # Coin is confirmed locked
                            except Exception as e:
                                log_event("warning", "coin_snapshot_poll_fail",
                                          f"Poll {poll + 1}/{max_lock_polls} failed: {e}")

                        if locked_coin:
                            res["locked_coin_id"] = locked_coin

                            # --- Reuse detection ---
                            # If this coin was already used by a previous offer in this
                            # batch, the wallet didn't properly lock it. Cancel this
                            # duplicate offer and retry after a longer delay.
                            if used_coins and locked_coin in used_coins:
                                trade_record = res.get("trade_record") or {}
                                dup_trade_id = res.get("trade_id") or trade_record.get("trade_id") or ""
                                log_event("warning", "coin_reuse_detected",
                                          f"Coin {locked_coin[:16]}... reused! "
                                          f"Cancelling duplicate offer {dup_trade_id[:12]}... "
                                          f"(attempt {attempt + 1}/{max_retries + 1})")
                                # Cancel the duplicate
                                if dup_trade_id:
                                    try:
                                        cancel_offer(dup_trade_id, secure=False)
                                        time.sleep(2)
                                    except Exception as e:
                                        log_event("warning", "coin_reuse_cancel_failed",
                                                  f"Could not cancel duplicate offer {dup_trade_id[:16]}...: {e}")
                                # Only retry once for reuse — if wallet keeps picking
                                # the same coin, further retries won't help.
                                if attempt < 1:
                                    time.sleep(3)
                                    # Re-snapshot and retry
                                    try:
                                        rpc_result = get_exact_spendable_coins_rpc(spend_wallet_id)
                                        before_coin_ids = self._extract_coin_id_set(rpc_result)
                                    except Exception as e:
                                        log_event("warning", "coin_resnapshot_failed",
                                                  f"Coin re-snapshot after reuse failed: {e}")
                                    continue  # Retry this offer once
                                else:
                                    log_event("warning", "coin_reuse_giving_up",
                                              f"Wallet keeps reusing coin {locked_coin[:16]}... "
                                              f"— skipping this offer slot")
                                    res["success"] = False
                                    res["error"] = "coin_reuse"
                                    return res
                        else:
                            log_event("warning", "coin_lock_timeout",
                                      f"No coin disappeared after {max_lock_polls}s — "
                                      f"wallet may have reused a locked coin")
                    return res

                # Check for specific error types
                error_msg = str(res.get("error", "")) if res else ""

                # If coin_ids mode failed, fall back to polling mode for retry.
                # The pre-selected coin may have been spent by another transaction.
                if use_coin_ids_mode and attempt < max_retries:
                    if caller_selected_coin_id:
                        log_event("warning", "coin_ids_failed",
                                  f"Caller-selected coin {caller_selected_coin_id[:16]}... "
                                  f"failed ({error_msg}) — not falling back to polling "
                                  f"mode to avoid overlapping offers")
                        return res
                    log_event("warning", "coin_ids_failed",
                              f"coin_ids mode failed ({error_msg}), "
                              f"falling back to polling mode for retry")
                    use_coin_ids_mode = False
                    selected_coin_id = None
                    # Take a before-snapshot for polling mode
                    if spend_wallet_id is not None:
                        try:
                            rpc_result = get_exact_spendable_coins_rpc(spend_wallet_id)
                            before_coin_ids = self._extract_coin_id_set(rpc_result)
                        except Exception as e:
                            log_event("warning", "coin_ids_fallback_snapshot_failed",
                                      f"Before-snapshot for polling-mode fallback failed: {e}")
                    time.sleep(2)
                    continue  # Retry in polling mode

                # MEMPOOL_CONFLICT — coin was spent by another transaction.
                # Don't retry with same coins, re-snapshot and try once more.
                if "MEMPOOL_CONFLICT" in error_msg:
                    log_event("warning", "offer_mempool_conflict",
                              "MEMPOOL_CONFLICT: another tx spent one of the coins we tried to use. "
                              "Re-snapshotting coins...")
                    if spend_wallet_id is not None and attempt < max_retries:
                        time.sleep(3)
                        try:
                            rpc_result = get_exact_spendable_coins_rpc(spend_wallet_id)
                            before_coin_ids = self._extract_coin_id_set(rpc_result)
                        except Exception as e:
                            log_event("warning", "mempool_conflict_resnapshot_failed",
                                      f"Coin re-snapshot after MEMPOOL_CONFLICT failed: {e}")
                        continue  # Retry with fresh coin snapshot
                    return res  # Out of retries

                # Insufficient balance — no coins of the right size. Don't retry,
                # the wallet simply doesn't have enough to create this offer.
                if "insufficient balance" in error_msg.lower():
                    log_event("warning", "offer_insufficient_balance",
                              f"Insufficient coins for offer: {error_msg}")
                    return res

                if "fully synced" in error_msg and attempt < max_retries:
                    wait_secs = 3 * (attempt + 1)
                    log_event("warning", "offer_retry",
                              f"Wallet sync error, retrying in {wait_secs}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_secs)
                    continue

                # "spendable balance" error with coin hints → hints filtered out all coins.
                # Retry once WITHOUT hints so the wallet can pick any coin it wants.
                if ("spendable balance" in error_msg or "minimum coin amount" in error_msg.lower()) \
                        and (min_coin_hint or max_coin_hint) and attempt < max_retries:
                    log_event("warning", "offer_hint_retry",
                              f"Coin hints may be too tight (min={min_coin_hint}, max={max_coin_hint}), "
                              f"retrying without hints...")
                    min_coin_hint = None
                    max_coin_hint = None
                    # Re-snapshot before retry since coins may have changed
                    if spend_wallet_id is not None:
                        try:
                            rpc_result = get_exact_spendable_coins_rpc(spend_wallet_id)
                            before_coin_ids = self._extract_coin_id_set(rpc_result)
                        except Exception as e:
                            log_event("warning", "hint_retry_snapshot_failed",
                                      f"Coin re-snapshot after hint retry failed: {e}")
                    time.sleep(2)
                    continue

                # Non-retryable error or out of retries
                error_detail = error_msg or "Unknown error"
                log_event("error", "offer_failed", f"Offer creation failed: {error_detail}")
                return res

            return None
        finally:
            # Always release the inflight lock regardless of outcome.
            # Prevents coin IDs from being permanently locked in _inflight_coin_ids
            # after the RPC call completes (success, failure, or exception).
            if _inflight_claimed:
                with self._lock:
                    self._inflight_coin_ids.discard(_inflight_claimed)

    def create_ladder(self, mid_price: Decimal, side: str,
                      num_offers: int = None, trade_size_xch: Decimal = None,
                      spread_fraction: Decimal = None,
                      cat_asset_id: str = None, cat_decimals: int = None,
                      cat_wallet_id: int = None,
                      risk_manager=None,
                      slot_start: int = 0,
                      total_slots: int = None,
                      coin_ids_enabled: bool = False,
                      slot_sequence: List[int] = None,
                      price_cap: Decimal = None,
                      price_floor: Decimal = None) -> List[Dict]:
        """Create a ladder of offers on one side (buy or sell).

        Places offers at evenly spaced prices from mid_price outward.
        Each offer gets a staggered expiry to avoid mass-expiry cascades.
        If TIER_ENABLED, uses different sizes per tier (inner/mid/outer/extreme).

        Args:
            mid_price: Current mid price in XCH per CAT
            side: 'buy' or 'sell'
            num_offers: Number of offers to create in THIS call
            trade_size_xch: Size per offer in XCH (defaults to config, overridden by tiers)
            spread_fraction: Half-spread as fraction (defaults to config)
            risk_manager: Optional RiskManager for tier sizing
            slot_start: Starting slot index for this batch (used by requote batches)
            total_slots: Total slots in the FULL ladder (for price/tier calculation).
                         When None, defaults to num (the entire ladder in one call).
            coin_ids_enabled: If True, pre-select coins for each offer (V3 fast mode)
            slot_sequence: Optional canonical slot indexes to create. When
                provided, these override slot_start/num sequencing and are used
                for refill/top-up batches so they replenish the intended tiers.

        Returns list of created offer details (trade_id, price, size, etc.)
        """
        # Use config defaults
        if slot_sequence is not None:
            slot_sequence = list(slot_sequence)
            num = len(slot_sequence)
        elif side == "buy":
            num = num_offers or cfg.MAX_ACTIVE_BUY_OFFERS
        else:
            num = num_offers or cfg.MAX_ACTIVE_SELL_OFFERS

        # Compute default_size early — it's needed by both the position guard
        # below AND the main offer-creation loop, so define it once here.
        default_size = trade_size_xch or cfg.DEFAULT_TRADE_XCH

        # F25 (2026-04-08): position rebalance hard guard.
        # Risk_manager already enforces MAX_POSITION_XCH as a soft limit
        # via spread skew. This is a HARD backstop: if creating these
        # offers would push the bot's position past 110% of the
        # configured max position (a 10% buffer above the soft limit),
        # refuse the entire batch.
        #
        # The directional logic:
        #   - net_position > 0  → bot is LONG CAT
        #   - Each BUY offer (if filled) → MORE long  → +size_xch worth of CAT
        #   - Each SELL offer (if filled) → LESS long → -size_xch worth of CAT
        #
        # If we're already long and trying to create buys → check ceiling
        # If we're already short and trying to create sells → check floor
        # The opposite direction is always safe (it reduces position).
        if risk_manager is not None:
            try:
                max_pos_xch = Decimal(str(getattr(cfg, "MAX_POSITION_XCH", "5") or "5"))
                hard_pos_xch = max_pos_xch * Decimal("1.1")
                # net_position is in CAT — convert to XCH equivalent
                net_pos_cat = Decimal(str(risk_manager._net_position_cat))
                if mid_price > 0:
                    net_pos_xch = abs(net_pos_cat) * mid_price
                else:
                    net_pos_xch = Decimal("0")
                # Project the position INCREASE if all these offers fill.
                # Use the REAL tier-summed ladder value when tiered sizing is
                # on (the old `default_size × num` proxy over- or under-counts
                # depending on which tier DEFAULT_TRADE_XCH lands on, and that
                # drift was making the guard fire on legitimate ladders).
                projected_increase_xch = self._estimate_ladder_worst_case_xch(
                    side=side,
                    num=num,
                    slot_start=slot_start,
                    total_slots=total_slots if total_slots is not None else num,
                    slot_sequence=slot_sequence,
                    risk_manager=risk_manager,
                    default_size=default_size,
                )

                # Self-heal: if MAX_POSITION_XCH was set too low relative to
                # the designed ladder (e.g. smart-defaults ran before the
                # consistency clamp existed, or operator set it manually),
                # the guard would block every ladder creation forever. Detect
                # that case at net_pos≈0 and raise the session hard limit to
                # the designed ladder + 5%. Log once per side. Smart Settings
                # now emits a consistent MAX_POSITION_XCH so this only kicks
                # in for legacy configs.
                if (
                    projected_increase_xch > hard_pos_xch
                    and max_pos_xch > 0
                    and net_pos_xch < max_pos_xch * Decimal("0.05")
                ):
                    _healed = projected_increase_xch * Decimal("1.05")
                    if _healed > hard_pos_xch:
                        if not getattr(self, "_max_pos_warned", False):
                            log_event(
                                "warning",
                                "max_position_auto_raised",
                                f"MAX_POSITION_XCH={max_pos_xch} XCH is inconsistent "
                                f"with the configured ladder "
                                f"(side={side}, num={num}, designed worst-case "
                                f"{projected_increase_xch:.4f} XCH > hard limit "
                                f"{hard_pos_xch:.4f} XCH). Session hard limit "
                                f"auto-raised to {_healed:.4f} XCH so the bot "
                                f"can operate. Re-run Smart Settings to persist "
                                f"a consistent MAX_POSITION_XCH."
                            )
                            self._max_pos_warned = True
                        hard_pos_xch = _healed

                # F69 (2026-04-17): net out already-open same-side exposure.
                # A REQUOTE (or top-up of an existing ladder) cancels N existing
                # offers and recreates them at a new price. The new exposure is
                # not ADDED on top of the old — it REPLACES it. Without this
                # subtraction, a legitimate requote during a market move hits
                # the hard guard because "current_position + full_new_ladder >
                # limit", even though the real delta is zero. See emergency
                # requote at 2026-04-17 01:34:10 which blocked 22/24 sell
                # replacements during a 2.6% price shock.
                #
                # We subtract the XCH value of currently-open same-side offers
                # from the projected increase. This is the "delta exposure"
                # the new creation actually adds above the existing ladder.
                same_side_open_xch = Decimal("0")
                try:
                    from database import get_open_offers as _gopen
                    _existing = _gopen(side=side, cat_asset_id=cat_asset_id or cfg.CAT_ASSET_ID)
                    for _off in _existing or []:
                        _sz = _off.get("size_xch") or _off.get("size_xch_mojos")
                        if _sz is None:
                            continue
                        try:
                            # size_xch may be stored as XCH float or mojos int —
                            # prefer the column name we just read. size_xch is
                            # the canonical XCH-unit column in this schema.
                            if isinstance(_sz, (int,)) and _sz > 1_000_000_000:
                                # mojos
                                same_side_open_xch += Decimal(_sz) / Decimal("1000000000000")
                            else:
                                same_side_open_xch += Decimal(str(_sz))
                        except Exception:
                            continue
                except Exception:
                    # Fail open — if we can't read the existing exposure, fall
                    # back to the pre-F69 behaviour. Worse: block a legit
                    # requote. Better than: allow unbounded growth.
                    same_side_open_xch = Decimal("0")

                net_new_exposure_xch = projected_increase_xch - same_side_open_xch
                if net_new_exposure_xch < 0:
                    net_new_exposure_xch = Decimal("0")

                # Only block if we're adding to the position in the wrong direction
                add_long_dir = (side == "buy" and net_pos_cat >= 0) or \
                               (side == "sell" and net_pos_cat <= 0)
                if (
                    add_long_dir
                    and net_pos_xch + net_new_exposure_xch > hard_pos_xch
                    and max_pos_xch > 0
                ):
                    _now = time.time()
                    _last = self._position_guard_logged_at.get(side, 0.0)
                    _should_log = (_now - _last) >= self._position_guard_log_cooldown
                    if _should_log:
                        self._position_guard_logged_at[side] = _now
                        log_event(
                            "error",
                            "position_hard_guard_blocked",
                            f"BLOCKED ladder creation: side={side}, num={num}, "
                            f"size={default_size}, current_position={net_pos_xch:.4f} XCH "
                            f"(net {net_pos_cat:+.0f} CAT), full-ladder value "
                            f"{projected_increase_xch:.4f} XCH, already-open same-side "
                            f"{same_side_open_xch:.4f} XCH, net new exposure "
                            f"{net_new_exposure_xch:.4f} XCH → projected "
                            f"{(net_pos_xch + net_new_exposure_xch):.4f} XCH > "
                            f"hard limit {hard_pos_xch:.4f} XCH (110% of "
                            f"MAX_POSITION_XCH={max_pos_xch}). Allow position to "
                            f"unwind via the opposite side first. "
                            f"(suppressing duplicates for {int(self._position_guard_log_cooldown)}s)",
                        )
                    return []
            except Exception as _pg_err:
                # Fail-open: never block trading on a guard bug
                log_event("debug", "position_hard_guard_failed",
                          f"Position rebalance guard check failed (proceeding): "
                          f"{_pg_err}")

        # total_slots = the full ladder size (for price spacing and tier classification)
        # When called normally: total_slots == num (full ladder in one call)
        # When called from requote: total_slots = 40 but num = 5 (one batch)
        if total_slots is None:
            total_slots = num

        half_spread = spread_fraction or cfg.get_spread_fraction() / Decimal("2")
        asset_id = cat_asset_id or cfg.CAT_ASSET_ID
        decimals = cat_decimals or cfg.CAT_DECIMALS
        wallet_cat = cat_wallet_id or cfg.CAT_WALLET_ID

        created = []
        used_coin_ids = set()  # Track coins locked by this batch to detect reuse
        used_size_keys_by_tier = {}
        existing_size_counts_by_tier = {}
        used_requested_amounts = set()
        exact_tier_spend_mode = bool(cfg.TIER_ENABLED and coin_ids_enabled)
        prep_headroom_pct = Decimal(str(getattr(cfg, "COIN_PREP_HEADROOM_PCT", "0")))
        align_live_offer_to_selected_coin = (
            exact_tier_spend_mode and prep_headroom_pct <= Decimal("0")
        )

        # Snapshot of surviving same-side offers' PRICES grouped by DB tier.
        # Consumed by `_interpolate_refill_price` so that refill slots land
        # INSIDE the existing tier's price band rather than at a fresh
        # grid position anchored on a mid that has drifted. Initial-ladder
        # calls (slot_sequence is None) don't use this — they fall back to
        # the classical `_get_ladder_price` formula. The loop below also
        # populates the size-dedup set that existed previously.
        existing_prices_by_tier: Dict[str, List[Decimal]] = {}
        try:
            for open_offer in get_open_offers(side=side, cat_asset_id=asset_id):
                tier_name = (open_offer.get("tier") or "mid").lower()
                raw_size = open_offer.get("size_xch")
                if raw_size is not None:
                    try:
                        size_key = self._size_key(Decimal(str(raw_size)))
                        used_size_keys_by_tier.setdefault(tier_name, set()).add(size_key)
                    except Exception:
                        pass
                # Price capture — skip non-ladder tiers (sniper/fees/reserve
                # aren't part of the main book and shouldn't anchor refills).
                if tier_name in ("sniper", "fees", "reserve"):
                    continue
                raw_price = open_offer.get("price_xch") or open_offer.get("price")
                if raw_price is None:
                    continue
                try:
                    p = Decimal(str(raw_price))
                    if p > 0:
                        existing_prices_by_tier.setdefault(tier_name, []).append(p)
                except Exception:
                    continue
            existing_size_counts_by_tier = {
                tier_name: len(size_keys)
                for tier_name, size_keys in used_size_keys_by_tier.items()
            }
        except Exception as e:
            log_event("debug", "offer_size_snapshot_fail",
                      f"Could not snapshot existing {side} offer sizes: {e}")

        if exact_tier_spend_mode:
            try:
                for open_offer in get_open_offers(side=side, cat_asset_id=asset_id):
                    requested_mojos = self._requested_amount_from_open_offer(
                        open_offer,
                        side,
                        decimals,
                    )
                    if requested_mojos:
                        used_requested_amounts.add(int(requested_mojos))
            except Exception as e:
                log_event("debug", "offer_requested_snapshot_fail",
                          f"Could not snapshot existing {side} requested amounts: {e}")

        planned_counts_by_tier = {}
        for i in range(num):
            slot = slot_sequence[i] if slot_sequence is not None else (slot_start + i)
            tier = self._classify_tier(slot, total_slots, side=side)
            planned_counts_by_tier[tier] = planned_counts_by_tier.get(tier, 0) + 1

        # ── Phase 1: Pre-compute all offer specs ──────────────────────────
        # Calculate prices, sizes, tiers, and offer dicts for all slots upfront.
        # This is pure math — no RPC calls, instant.
        offer_specs = []
        for i in range(num):
            if self._stop_requested:
                log_event("info", "ladder_interrupted",
                          f"Ladder creation interrupted by stop signal after "
                          f"{len(offer_specs)}/{num} {side} offers planned")
                break

            slot = slot_sequence[i] if slot_sequence is not None else (slot_start + i)

            # Fix F: skip suspended slots (coin exhaustion self-heal)
            if self.is_slot_suspended(side, slot):
                continue

            # Pricing path:
            #   * Initial-ladder call (slot_sequence is None): classical
            #     grid formula anchored on mid_price.
            #   * Refill call (slot_sequence is not None): interpolate
            #     into the surviving tier band so the new offer lands at
            #     a price consistent with the existing ladder, even if
            #     mid has drifted. Falls back to the grid formula when
            #     the target tier is empty or the surrounding data is
            #     insufficient.
            if slot_sequence is not None and existing_prices_by_tier:
                price = self._interpolate_refill_price(
                    slot, side, total_slots,
                    existing_prices_by_tier, mid_price, half_spread,
                )
            else:
                price = self._get_ladder_price(slot, side, mid_price, half_spread, total_slots)
            price = self._apply_price_bounds(
                price,
                side,
                price_cap=price_cap,
                price_floor=price_floor,
            )
            if price is None or price <= 0:
                continue

            # AMM buffer guard — skip slots that would land inside TibetSwap's
            # arb zone. An offer priced within AMM_BUFFER_BPS of the live AMM
            # price will be swept immediately by the TibetSwap arb bot.
            if self.amm_monitor is not None:
                try:
                    buffer_ok = self.amm_monitor.check_amm_buffer(price, side)
                    if buffer_ok is False:
                        continue  # Inside AMM arb band — skip slot
                except Exception:
                    log_event("warning", "amm_buffer_error",
                              f"AMM buffer check failed for {side} — skipping slot")
                    continue  # Fail closed on errors too

            tier = self._classify_tier(slot, total_slots, side=side)
            if cfg.TIER_ENABLED and risk_manager:
                size_xch = risk_manager.get_tier_size(tier, side=side)
            else:
                size_xch = default_size

            # In tiered coin_ids mode we keep the spend side aligned to the
            # prepped tier coin sizes. Even tiny nudges can cause Sage to lock
            # a second helper coin to avoid awkward dust/change.
            if not exact_tier_spend_mode:
                tier_used_sizes = used_size_keys_by_tier.setdefault(tier, set())
                expected_unique_count = (
                    existing_size_counts_by_tier.get(tier, 0)
                    + planned_counts_by_tier.get(tier, 0)
                )
                size_xch = self._allocate_unique_size_xch(
                    size_xch,
                    slot,
                    cfg.TIER_ENABLED and risk_manager,
                    tier_used_sizes,
                    max(1, expected_unique_count),
                )

            cat_amount = size_xch / price

            # Sanity: reject astronomically large CAT amounts that would
            # result from near-zero prices slipping through bounds checks.
            max_cat_sanity = size_xch / Decimal("0.0000001")  # 1e-7 XCH floor
            if cat_amount > max_cat_sanity:
                log_event("warning", "cat_amount_sanity",
                          f"Skipping {side} slot {slot}: cat_amount {cat_amount:.2f} "
                          f"exceeds sanity limit (price {price:.12f} too small)")
                continue

            cat_mojos = cat_to_mojos(cat_amount, decimals)
            cat_amount = mojos_to_cat(cat_mojos, decimals)
            xch_mojos = xch_to_mojos(size_xch)

            if side == "buy":
                offer_dict = {
                    str(cfg.WALLET_ID_XCH): -int(xch_mojos),
                    str(wallet_cat): int(cat_mojos)
                }
            else:
                offer_dict = {
                    str(wallet_cat): -int(cat_mojos),
                    str(cfg.WALLET_ID_XCH): int(xch_mojos)
                }

            offer_specs.append({
                "i": i, "slot": slot, "price": price, "tier": tier,
                "size_xch": size_xch, "cat_amount": cat_amount,
                "offer_dict": offer_dict, "stagger": i,
            })

        if cfg.DRY_RUN:
            for spec in offer_specs:
                log_event("info", "dry_run", f"[DRY RUN] Would create {side} offer at {spec['price']}")
            return created

        # ── Phase 2: Pre-select coins for all offers ──────────────────────
        # Sequential coin selection — each coin must be unique. Fast (~1ms each).
        # Buy offers spend XCH (wallet 1), sell offers spend CAT (cat wallet).
        if coin_ids_enabled:
            spend_wallet_id = cfg.WALLET_ID_XCH if side == "buy" else wallet_cat
            spendable_records = None
            spendable_amounts = {}
            try:
                rpc_result = get_exact_spendable_coins_rpc(spend_wallet_id)
                if rpc_result and rpc_result.get("success"):
                    spendable_records = (
                        rpc_result.get("confirmed_records")
                        or rpc_result.get("records")
                        or []
                    )
                    for record in spendable_records:
                        coin_id = self._extract_coin_id_set({
                            "confirmed_records": [record]
                        })
                        if not coin_id:
                            continue
                        coin_data = record.get("coin", {})
                        try:
                            spendable_amounts[next(iter(coin_id))] = int(coin_data.get("amount", 0))
                        except Exception:
                            continue
                else:
                    log_event("warning", "coin_select_snapshot_fail",
                              f"Could not snapshot spendable coins for wallet {spend_wallet_id} "
                              f"before {side} ladder selection")
            except Exception as e:
                log_event("warning", "coin_select_snapshot_fail",
                          f"Spendable snapshot failed for wallet {spend_wallet_id}: {e}")

            for spec in offer_specs:
                # Find the spending side (negative amount)
                spec_spend_wallet_id = None
                spend_amount = 0
                for wid, amt in spec["offer_dict"].items():
                    if int(amt) < 0:
                        spec_spend_wallet_id = int(wid)
                        spend_amount = abs(int(amt))
                        break

                # Translate the slot's POSITION tier into the COIN SIZE tier
                # the prepared coin pool actually labels its coins with. Under
                # BUY_LADDER_REVERSED an "extreme position" buy slot needs an
                # inner-sized coin (and so on). Single source of truth: the
                # live BUY_*_TIER_COUNT + BUY_LADDER_REVERSED settings drive
                # both prep and selection.
                from coin_manager import coin_size_tier_for_slot_position as _coin_tier
                coin_size_pref = _coin_tier(spec["tier"], side=side)

                # In exact_tier_spend_mode, cap the coin size so we never use
                # a wildly oversized coin (e.g. 5 XCH for a 0.634 XCH offer).
                # When no coin fits within the cap, return None → clean slot
                # failure → slot suspension → topup splits the reserve.
                _max_coin = None
                if exact_tier_spend_mode:
                    _ratio = float(getattr(cfg, "COIN_MAX_SIZE_RATIO", "1.5"))
                    if _ratio > 0:
                        _max_coin = int(spend_amount * _ratio)

                # F70 — pass tier sizes so the selector can do strict SSOT
                # misfit rejection via classify_coin(). Without this, the
                # selector would accept coins that are below inner's 0.98
                # floor even though reconcile (post-fix) would classify
                # them as UNKNOWN.
                _tier_sizes_mojos_for_select = None
                try:
                    from coin_manager import get_tier_sizes_mojos_from_cfg as _gt_mojos
                    _tier_sizes_mojos_for_select = _gt_mojos(is_cat=(side == "sell"))
                except Exception:
                    _tier_sizes_mojos_for_select = None

                coin_id = self._select_coin_for_offer(
                    spec_spend_wallet_id or spend_wallet_id,
                    spend_amount,
                    used_coin_ids,
                    preferred_tier=coin_size_pref,
                    spendable_records=spendable_records,
                    max_amount_mojos=_max_coin,
                    tier_sizes_mojos=_tier_sizes_mojos_for_select,
                )
                spec["coin_id"] = coin_id
                if coin_id:
                    spec["selected_coin_amount"] = spendable_amounts.get(coin_id)
                    used_coin_ids.add(coin_id)
                    if align_live_offer_to_selected_coin:
                        selected_amount = spec.get("selected_coin_amount")
                        if selected_amount:
                            if side == "buy":
                                exact_size_xch = mojos_to_xch(int(selected_amount))
                                exact_cat_amount = exact_size_xch / spec["price"]
                                exact_cat_mojos = cat_to_mojos(exact_cat_amount, decimals)
                                spec["size_xch"] = exact_size_xch
                                spec["cat_amount"] = mojos_to_cat(exact_cat_mojos, decimals)
                                spec["offer_dict"] = {
                                    str(cfg.WALLET_ID_XCH): -int(selected_amount),
                                    str(wallet_cat): int(exact_cat_mojos),
                                }
                            else:
                                exact_cat_amount = mojos_to_cat(int(selected_amount), decimals)
                                exact_xch_mojos = xch_to_mojos(exact_cat_amount * spec["price"])
                                spec["size_xch"] = mojos_to_xch(exact_xch_mojos)
                                spec["cat_amount"] = exact_cat_amount
                                spec["offer_dict"] = {
                                    str(wallet_cat): -int(selected_amount),
                                    str(cfg.WALLET_ID_XCH): int(exact_xch_mojos),
                                }

        # ── Phase 3: Create all offers in parallel ────────────────────────
        # Fire up to 5 concurrent make_offer RPC calls. Each has its own
        # pre-selected coin_id so there's no contention.
        if coin_ids_enabled and exact_tier_spend_mode:
            for spec in offer_specs:
                if not spec.get("coin_id"):
                    continue
                if side == "buy":
                    spend_xch_mojos = abs(int(spec["offer_dict"][str(cfg.WALLET_ID_XCH)]))
                    requested_cat_mojos = int(spec["offer_dict"][str(wallet_cat)])
                    unique_requested_cat_mojos = self._allocate_unique_requested_mojos(
                        requested_cat_mojos,
                        spec["slot"],
                        used_requested_amounts,
                    )
                    spec["size_xch"] = mojos_to_xch(spend_xch_mojos)
                    spec["cat_amount"] = mojos_to_cat(unique_requested_cat_mojos, decimals)
                    if spec["cat_amount"] > 0:
                        spec["price"] = spec["size_xch"] / spec["cat_amount"]
                    spec["offer_dict"] = {
                        str(cfg.WALLET_ID_XCH): -int(spend_xch_mojos),
                        str(wallet_cat): int(unique_requested_cat_mojos),
                    }
                else:
                    spend_cat_mojos = abs(int(spec["offer_dict"][str(wallet_cat)]))
                    requested_xch_mojos = int(spec["offer_dict"][str(cfg.WALLET_ID_XCH)])
                    unique_requested_xch_mojos = self._allocate_unique_requested_mojos(
                        requested_xch_mojos,
                        spec["slot"],
                        used_requested_amounts,
                    )
                    spec["size_xch"] = mojos_to_xch(unique_requested_xch_mojos)
                    spec["cat_amount"] = mojos_to_cat(spend_cat_mojos, decimals)
                    if spec["cat_amount"] > 0:
                        spec["price"] = spec["size_xch"] / spec["cat_amount"]
                    spec["offer_dict"] = {
                        str(wallet_cat): -int(spend_cat_mojos),
                        str(cfg.WALLET_ID_XCH): int(unique_requested_xch_mojos),
                    }

        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading as _threading

        # During requote batches (small num_offers from rolling wave), use
        # serial creation.  Parallel creation with Sage can cause
        # BAD_AGGREGATE_SIGNATURE when multiple concurrent make_offer RPCs
        # contend for the same fee coin.  Full ladder creates (startup/cold)
        # still benefit from parallelism since they run before any cancels.
        _is_requote_batch = (num is not None and num < total_slots)
        if _is_requote_batch:
            max_parallel = 1
        else:
            max_parallel = self._get_ladder_parallelism(coin_ids_enabled)
        _results_lock = _threading.Lock()
        _used_coins_lock = _threading.Lock()
        _results_map = {}  # {i: res}

        def _create_one(spec):
            """Create a single offer (runs in thread pool)."""
            if coin_ids_enabled and not spec.get("coin_id"):
                # Pre-selection returned no coin. In PARALLEL mode this
                # guardrail prevents two workers from competing for the
                # same coin (BAD_AGGREGATE_SIGNATURE). In SERIAL mode
                # (max_parallel == 1, used for requote batches) there is
                # no concurrent worker to race with, so we can safely
                # fall through and let ``create_offer_with_retry`` /
                # Sage's default coin selector pick a coin from the
                # actual wallet. This recovers from the case where the
                # coin_manager's in-memory tier cache disagrees with the
                # DB and leaves the ladder wedged at an under-built size
                # after a fill burst consumes tier coins.
                if max_parallel != 1:
                    msg = (f"No unique pre-selected coin available for {side} "
                           f"slot {spec['slot']} — skipping to avoid overlap")
                    log_event("debug", "coin_select_skip", msg)
                    # Fix F: track consecutive failures for this slot
                    self.record_slot_coin_failure(side, spec["slot"])
                    return spec["i"], {"success": False, "error": "no_unique_coin_preselected"}
                # Serial mode: proceed without pre-selection
                log_event("debug", "coin_select_skip_serial_fallback",
                          f"No pre-selected coin for {side} slot {spec['slot']} "
                          f"— serial mode, letting Sage pick from wallet")

            res = self.create_offer_with_retry(
                spec["offer_dict"],
                expiry_offset=spec["stagger"],
                used_coins=used_coin_ids,
                coin_ids_enabled=coin_ids_enabled,
                selected_coin_id=spec.get("coin_id"),
                preferred_tier=spec["tier"]
            )
            if res and res.get("success"):
                locked_coin_id = res.get("locked_coin_id")
                if locked_coin_id:
                    with _used_coins_lock:
                        used_coin_ids.add(locked_coin_id)
                        self._cycle_used_coin_ids.add(locked_coin_id)
                # Fix F: clear failure counter on successful creation
                self.clear_slot_failure(side, spec["slot"])
            try:
                delay_ms = int(getattr(cfg, "LADDER_CREATE_DELAY_MS", 0) or 0)
            except Exception:
                delay_ms = 0
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
            return spec["i"], res

        log_event("info", "ladder_parallel",
                  f"Creating {len(offer_specs)} {side} offers with {max_parallel} parallel workers")
        _ladder_start = time.time()

        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            futures = [executor.submit(_create_one, spec) for spec in offer_specs]
            for f in as_completed(futures):
                try:
                    idx, res = f.result()
                    with _results_lock:
                        _results_map[idx] = res
                except Exception as e:
                    log_event("warning", "parallel_offer_error", f"Thread error: {e}")

        _ladder_elapsed = time.time() - _ladder_start
        log_event("info", "ladder_parallel_done",
                  f"{len(offer_specs)} {side} offers fired in {_ladder_elapsed:.1f}s")

        # ── Phase 4: Process results (sequential — DB writes) ─────────────
        for spec in offer_specs:
            i = spec["i"]
            res = _results_map.get(i)
            price = spec["price"]
            tier = spec["tier"]
            size_xch = spec["size_xch"]
            cat_amount = spec["cat_amount"]
            slot = spec["slot"]

            if not res or not res.get("success"):
                error_msg = str(res.get("error", "")) if res else ""
                fail_msg = (f"Offer #{i+1}/{num} {side} FAILED: {error_msg[:100]}")
                # Coin exhaustion is an expected operational state (not a code
                # defect) — already tracked by record_slot_coin_failure /
                # slot_suspended, so downgrade to debug to avoid log spam.
                _fail_level = (
                    "debug" if error_msg == "no_unique_coin_preselected"
                    else "error"
                )
                if _fail_level != "debug":
                    print(f"  ❌ {fail_msg}", flush=True)
                log_event(_fail_level, "offer_create_failed", fail_msg)
                continue

            trade_record = res.get("trade_record") or {}
            trade_id = res.get("trade_id") or trade_record.get("trade_id") or ""

            if not trade_id:
                continue

            locked_coin_id = res.get("locked_coin_id")
            verified_locked_coin_ids = []
            if coin_ids_enabled and locked_coin_id and get_wallet_type() == "sage":
                spend_wallet_id = None
                for wid, amt in spec["offer_dict"].items():
                    if int(amt) < 0:
                        spend_wallet_id = int(wid)
                        break

                verification = self._verify_sage_offer_locked_inputs(
                    spend_wallet_id,
                    trade_id,
                    locked_coin_id,
                )
                if verification.get("verified"):
                    verified_locked_coin_ids = verification.get("locked_coin_ids") or []
                    selected_present = verification.get("selected_present", False)
                    if len(verified_locked_coin_ids) > 1 or not selected_present:
                        log_event(
                            "info",
                            "coin_ids_overlap_observed",
                            f"Sage locked {len(verified_locked_coin_ids)} inputs for "
                            f"{trade_id[:12]}... "
                            f"({', '.join(cid[:14] + '...' for cid in verified_locked_coin_ids)}) "
                            f"while selected={locked_coin_id[:14]}...",
                        )

            locked_preview = locked_coin_id[:16] if locked_coin_id else "none"
            ok_msg = (f"Offer #{i+1}/{num} {side} @ {price:.8f} | "
                      f"size={float(size_xch):.4f} XCH | "
                      f"trade_id={trade_id[:16]}... | coin={locked_preview}")
            print(f"  ✅ {ok_msg}", flush=True)
            log_event("success", "offer_created", ok_msg)

            # DB: record offer
            _omt = res.get("offer_max_time", 0)
            if _omt and int(_omt) > 0:
                from datetime import datetime, timezone
                expires_at = datetime.fromtimestamp(int(_omt), tz=timezone.utc).isoformat()
            else:
                expires_at = None

            # Select the coin_id to store in the DB.
            # When Sage bundles both the trade coin and the fee coin as maker
            # inputs, `verified_locked_coin_ids` contains both (sorted by hash).
            # Always prefer the pre-selected trade coin (`locked_coin_id`) when
            # it appears in the verified list — this prevents the fee coin from
            # being recorded as the offer's trade coin (bug: fee-coin backed offers).
            if verified_locked_coin_ids:
                if locked_coin_id and locked_coin_id in verified_locked_coin_ids:
                    db_coin_id = locked_coin_id   # pre-selected trade coin confirmed ✓
                else:
                    # Pre-selected coin not verified (Sage used different coin).
                    # Use whatever Sage locked — and log a warning so we can track.
                    db_coin_id = verified_locked_coin_ids[0]
                    if locked_coin_id:
                        log_event("warning", "trade_coin_not_verified",
                                  f"Pre-selected coin {locked_coin_id[:16]}... was NOT found in "
                                  f"Sage's locked inputs for {trade_id[:12]}... "
                                  f"(Sage locked: {', '.join(c[:14]+'...' for c in verified_locked_coin_ids[:3])}). "
                                  f"Offer may use an unexpected coin.")
            else:
                db_coin_id = locked_coin_id
            db_ok = add_offer(
                trade_id=trade_id, side=side, price_xch=price,
                size_xch=size_xch, size_cat=cat_amount,
                cat_asset_id=asset_id, tier=tier,
                expires_at=expires_at, coin_id=db_coin_id
            )
            if not db_ok:
                # DB insert failed — cancel on-chain offer to prevent wallet/DB
                # divergence (offer exists in wallet but isn't tracked).
                log_event("error", "ladder_db_cancel",
                          f"DB insert failed for {trade_id[:16]}..., cancelling on-chain offer")
                try:
                    self.cancel_offers([trade_id], reason="db_insert_failed")
                except Exception:
                    pass
                continue

            lock_targets = verified_locked_coin_ids or ([locked_coin_id] if locked_coin_id else [])
            for coin_id in lock_targets:
                used_coin_ids.add(coin_id)
                self._cycle_used_coin_ids.add(coin_id)
                try:
                    lock_coin(coin_id, trade_id)
                except Exception as e:
                    log_event("warning", "coin_lock_failed",
                              f"DB coin lock failed for coin {coin_id[:16] if coin_id else 'unknown'}... "
                              f"(offer {trade_id[:16] if trade_id else '?'}...): {e}")

            # Cache for fill tracking
            offer_detail = {
                "trade_id": trade_id, "side": side, "price": price,
                "size_xch": size_xch, "size_cat": cat_amount,
                "tier": tier, "slot": slot, "coin_id": locked_coin_id,
            }
            if verified_locked_coin_ids:
                offer_detail["locked_coin_ids"] = verified_locked_coin_ids

            # Get bech32 for Dexie posting
            offer_bech32 = res.get("offer") or ""
            if not offer_bech32:
                offer_bech32 = get_offer_bech32(trade_id) or ""
            if offer_bech32:
                offer_detail["offer_bech32"] = offer_bech32

            self._offer_details_cache[trade_id] = offer_detail
            self._recently_created[trade_id] = time.time()
            created.append(offer_detail)

        return created

    def _estimate_ladder_worst_case_xch(
        self,
        side: str,
        num: int,
        slot_start: int,
        total_slots: int,
        slot_sequence: Optional[List[int]],
        risk_manager,
        default_size: Optional[Decimal],
    ) -> Decimal:
        """Sum of per-slot tier sizes for the slots this call will create.

        Used by the F25 position hard guard to decide whether the full
        ladder, if all filled, would exceed MAX_POSITION_XCH × 1.1. The
        prior implementation used `default_size × num` which silently
        under- or over-counted depending on which tier DEFAULT_TRADE_XCH
        happened to map to — with reverse-buy on, that drifted enough
        to block legitimate initial ladders. Summing the actual tier
        sizes each slot will use eliminates the drift.

        Falls back to `default_size × num` when tiered sizing is off or
        the tier-size lookup fails, so the guard never becomes blind.
        """
        fallback = (default_size or Decimal("0")) * Decimal(num)
        if not cfg.TIER_ENABLED or risk_manager is None:
            return fallback
        total = Decimal("0")
        for i in range(num):
            if slot_sequence is not None:
                slot = slot_sequence[i]
            else:
                slot = slot_start + i
            tier = self._classify_tier(slot, total_slots, side=side)
            try:
                sz = risk_manager.get_tier_size(tier, side=side)
            except Exception:
                sz = default_size or Decimal("0")
            if sz is None:
                sz = default_size or Decimal("0")
            try:
                total += Decimal(str(sz))
            except Exception:
                total += default_size or Decimal("0")
        return total if total > 0 else fallback

    def _get_ladder_price(self, slot: int, side: str, mid_price: Decimal,
                           half_spread: Decimal, max_offers: int) -> Optional[Decimal]:
        """Calculate the price for a specific ladder slot.

        Arithmetic ladder: steady increase from tight (near mid) to wide.
        Slot 0 (inner) starts at MIN_EDGE_BPS from mid.
        Slot N-1 (extreme) reaches the full adjusted half_spread.

        This creates a smooth orderbook: tight offers near mid price
        (where most fills happen) and wider offers at the extremes.
        """
        if max_offers <= 0:
            return None

        # Inner edge: minimum distance from mid (closest offer)
        inner_edge = cfg.MIN_EDGE_BPS / Decimal("10000")

        # Outer edge: the full adjusted spread (farthest offer)
        outer_edge = half_spread

        # Safety: if min edge >= full spread, just use the full spread everywhere
        if inner_edge >= outer_edge:
            distance = outer_edge
        elif max_offers == 1:
            # Single offer: place at inner edge (tight)
            distance = inner_edge
        else:
            # Steady linear increase from inner_edge to outer_edge
            step = (outer_edge - inner_edge) / Decimal(max_offers - 1)
            distance = inner_edge + step * Decimal(slot)

        if side == "buy":
            price = mid_price * (Decimal("1") - distance)
        else:
            price = mid_price * (Decimal("1") + distance)

        if price <= 0:
            return None

        return price

    def _interpolate_refill_price(
        self,
        slot: int,
        side: str,
        total_slots: int,
        existing_prices_by_tier: Dict[str, List[Decimal]],
        mid_price: Decimal,
        half_spread: Decimal,
    ) -> Optional[Decimal]:
        """Price a refill slot by interpolating into the existing ladder.

        When a filled slot gets refilled, the mid price has usually drifted
        since the ladder was built. Pricing the replacement from the live
        mid produces an offer that interleaves with surviving offers at
        their original prices, scrambling the tier ordering on Dexie and
        tripping the ladder-taper watchdog.

        This helper anchors refills to the tier they belong to:

        * **≥2 surviving same-tier offers** → interpolate between the
          tier's closest-to-mid and furthest-from-mid prices, by the
          slot's rank-within-tier. New offers slot into the gap in the
          tier's existing price band.
        * **Exactly 1 surviving same-tier offer** → use it as one anchor
          and the adjacent tier's boundary as the other. If no neighbour
          is usable, fall back to the live-mid grid price.
        * **0 survivors in the target tier** → fall back to
          ``_get_ladder_price`` anchored on ``mid_price``. The refill
          repopulates the empty tier from scratch at the current market.

        Sanity guard: if the interpolated price falls outside
        ``mid × (1 ± MAX_SPREAD_BPS/10000)`` returns None so the caller
        skips the slot. The graduated-requote path will rebuild the
        whole ladder when drift is that large — patching would produce
        economically bad offers.
        """
        target_tier = self._classify_tier(slot, total_slots, side=side)

        # Build this side's tier-count map so we can compute rank-within-tier.
        tier_order = ("inner", "mid", "outer", "extreme")
        prefix = "BUY_" if (side or "").lower() == "buy" else "SELL_"
        tier_sizes = {
            t: int(getattr(cfg, f"{prefix}{t.upper()}_TIER_COUNT", 0) or 0)
            for t in tier_order
        }
        if sum(tier_sizes.values()) == 0:
            # Legacy shared counts (pre-F62).
            tier_sizes = {
                t: int(getattr(cfg, f"{t.upper()}_TIER_COUNT", 0) or 0)
                for t in tier_order
            }

        tier_size = tier_sizes.get(target_tier, 0)
        if tier_size <= 0:
            # No tier structure configured → no sensible interpolation.
            return self._get_ladder_price(slot, side, mid_price, half_spread, total_slots)

        # Rank-within-tier is slot minus the total slots in prior tiers.
        rank_within_tier = slot
        for t in tier_order:
            if t == target_tier:
                break
            rank_within_tier -= tier_sizes.get(t, 0)
        if rank_within_tier < 0 or rank_within_tier >= tier_size:
            # Inconsistency — safer to fall back than extrapolate.
            return self._get_ladder_price(slot, side, mid_price, half_spread, total_slots)

        # Sort prices by distance-from-mid. Buy side: closest-to-mid is
        # HIGHEST price (just below mid). Sell side: closest-to-mid is
        # LOWEST price (just above mid).
        def _dist_key(p: Decimal) -> Decimal:
            return -p if (side or "").lower() == "buy" else p

        same_tier_prices = existing_prices_by_tier.get(target_tier) or []
        sorted_tier = sorted(same_tier_prices, key=_dist_key)

        # Locate adjacent tiers so a lone survivor can still interpolate.
        prev_tier = None
        next_tier = None
        for i, t in enumerate(tier_order):
            if t == target_tier:
                if i > 0:
                    prev_tier = tier_order[i - 1]
                if i < len(tier_order) - 1:
                    next_tier = tier_order[i + 1]
                break

        inner_anchor: Optional[Decimal] = None
        outer_anchor: Optional[Decimal] = None

        if len(sorted_tier) >= 2:
            inner_anchor = sorted_tier[0]
            outer_anchor = sorted_tier[-1]
        elif len(sorted_tier) == 1:
            sole = sorted_tier[0]
            prev_prices = sorted(existing_prices_by_tier.get(prev_tier, []), key=_dist_key) if prev_tier else []
            next_prices = sorted(existing_prices_by_tier.get(next_tier, []), key=_dist_key) if next_tier else []
            # Use the adjacent-tier boundary on whichever side the survivor
            # doesn't cover. If we have neighbour boundaries on both sides,
            # prefer those as anchors — the sole survivor is already in the
            # band and the interpolation across the full tier width is
            # more accurate.
            if prev_prices:
                inner_anchor = prev_prices[-1]
            else:
                inner_anchor = sole
            if next_prices:
                outer_anchor = next_prices[0]
            else:
                outer_anchor = sole
            if inner_anchor == outer_anchor:
                # Both fell back to the sole survivor → can't interpolate.
                return self._get_ladder_price(slot, side, mid_price, half_spread, total_slots)
        else:
            # Empty tier → fresh grid price anchored on current mid.
            return self._get_ladder_price(slot, side, mid_price, half_spread, total_slots)

        if inner_anchor is None or outer_anchor is None:
            return self._get_ladder_price(slot, side, mid_price, half_spread, total_slots)

        # Linear interpolation by rank-within-tier. Formula is side-agnostic
        # because `sorted_tier` is always [closest, …, furthest] regardless
        # of which direction "closest" means numerically.
        denom = Decimal(max(1, tier_size - 1))
        fraction = Decimal(rank_within_tier) / denom
        inner_d = Decimal(str(inner_anchor))
        outer_d = Decimal(str(outer_anchor))
        price = inner_d + (outer_d - inner_d) * fraction

        if price <= 0:
            return None

        # Economic sanity guard — reject prices outside MAX_SPREAD_BPS of
        # mid. A stale tier + big mid move can produce interpolated prices
        # well off the current market. Returning None skips this slot;
        # the graduated-requote path will catch up when drift is real.
        try:
            max_bps = Decimal(str(getattr(cfg, "MAX_SPREAD_BPS", 2500) or 2500))
            if max_bps > 0:
                offset = max_bps / Decimal("10000")
                lower = mid_price * (Decimal("1") - offset)
                upper = mid_price * (Decimal("1") + offset)
                if price < lower or price > upper:
                    return None
        except Exception:
            # Sanity check is best-effort — never block an otherwise valid
            # refill just because the MAX_SPREAD_BPS config is odd.
            pass

        return price

    def _apply_price_bounds(self, price: Optional[Decimal], side: str,
                            price_cap: Decimal = None,
                            price_floor: Decimal = None) -> Optional[Decimal]:
        """Clamp ladder prices so the main book never crosses a surviving probe."""
        if price is None:
            return None

        if side == "buy" and price_cap is not None:
            cap = Decimal(str(price_cap))
            if cap > 0:
                price = min(price, cap)
        if side == "sell" and price_floor is not None:
            floor = Decimal(str(price_floor))
            if floor > 0:
                price = max(price, floor)
        return price

    def _classify_tier(self, slot: int, total: int, side: str = None) -> str:
        """Classify an offer's tier based on its position in the ladder.

        `side` selects per-side BUY_*_TIER_COUNT vs SELL_*_TIER_COUNT keys
        so the buy and sell ladders can have independent tier shapes.
        Falls back to per-tier MAX of both sides if `side` is None — this
        keeps existing call sites that don't yet pass side from breaking.
        """
        if not cfg.TIER_ENABLED:
            return "mid"
        if total <= 0:
            return "mid"

        side_norm = (side or "").lower()
        if side_norm == "buy":
            prefix = "BUY_"
        elif side_norm == "sell":
            prefix = "SELL_"
        else:
            prefix = None

        if prefix is None:
            configured = {
                tier: max(
                    int(getattr(cfg, f"BUY_{tier.upper()}_TIER_COUNT", 0) or 0),
                    int(getattr(cfg, f"SELL_{tier.upper()}_TIER_COUNT", 0) or 0),
                )
                for tier in ("inner", "mid", "outer", "extreme")
            }
        else:
            configured = {
                tier: int(getattr(cfg, f"{prefix}{tier.upper()}_TIER_COUNT", 0) or 0)
                for tier in ("inner", "mid", "outer", "extreme")
            }
        if any(v > 0 for v in configured.values()):
            remaining = total
            running = 0
            tier_dist = {}
            for tier in ("inner", "mid", "outer", "extreme"):
                take = min(max(0, configured[tier]), remaining)
                tier_dist[tier] = take
                running += take
                remaining -= take
            if remaining > 0:
                tier_dist["extreme"] += remaining

            running = 0
            for tier in ("inner", "mid", "outer", "extreme"):
                running += tier_dist[tier]
                if slot < running:
                    return tier
            return "extreme"

        ratio = slot / total
        if ratio < 0.1:
            return "inner"
        elif ratio < 0.4:
            return "mid"
        elif ratio < 0.7:
            return "outer"
        else:
            return "extreme"

    # -------------------------------------------------------------------
    # Requoting (cancel + recreate when price moves)
    # -------------------------------------------------------------------

    def should_requote(self, side: str, current_price: Decimal,
                       last_quoted_price: Decimal) -> bool:
        """Check if offers on this side need requoting.

        Requoting happens when the mid price has moved more than
        REQUOTE_BPS from where we last placed offers.

        Returns True/False for backward compatibility.
        Use should_requote_graduated() for the severity level.
        """
        if not cfg.AUTO_REQUOTE:
            return False

        # Cooldown check
        elapsed = time.time() - self._last_requote_time.get(side, 0)
        if elapsed < cfg.REQUOTE_COOLDOWN_SECS:
            return False

        # Price movement check
        if last_quoted_price <= 0:
            return False

        move_fraction = abs(current_price - last_quoted_price) / last_quoted_price
        requote_fraction = cfg.get_requote_fraction()

        return move_fraction > requote_fraction

    def should_requote_graduated(self, side: str, current_price: Decimal,
                                 last_quoted_price: Decimal):
        """Like should_requote but returns a RequoteSeverity level.

        Determines HOW MUCH of the book needs adjusting based on
        the magnitude of price drift:
          NONE      — no action (drift < inner threshold)
          INNER     — adjust inner tier only
          INNER_MID — adjust inner + mid tiers
          FULL      — adjust all tiers (still budget-capped)
          EMERGENCY — offers may be arbable, cancel immediately
        """
        from reaction_strategy import RequoteSeverity, classify_drift

        if not cfg.AUTO_REQUOTE:
            return RequoteSeverity.NONE

        # Cooldown check
        elapsed = time.time() - self._last_requote_time.get(side, 0)
        if elapsed < cfg.REQUOTE_COOLDOWN_SECS:
            return RequoteSeverity.NONE

        if last_quoted_price <= 0:
            return RequoteSeverity.NONE

        move_fraction = abs(current_price - last_quoted_price) / last_quoted_price
        return classify_drift(
            move_fraction,
            inner_threshold=getattr(cfg, "REQUOTE_DRIFT_INNER", Decimal("0.003")),
            mid_threshold=getattr(cfg, "REQUOTE_DRIFT_MID", Decimal("0.008")),
            full_threshold=getattr(cfg, "REQUOTE_DRIFT_FULL", Decimal("0.02")),
            emergency_threshold=getattr(cfg, "REQUOTE_DRIFT_EMERGENCY", Decimal("0.05")),
        )

    def requote_side(self, side: str, current_price: Decimal,
                     dexie_manager=None, risk_manager=None,
                     spread_fraction: Decimal = None,
                     price_cap: Decimal = None,
                     price_floor: Decimal = None,
                     live_offer_ids: set = None,
                     max_offers: int = 0,
                     allowed_tiers: set = None) -> List[Dict]:
        """Single-pass requote: create new offers then fire-and-forget cancel
        old ones.

        Simplified from the rolling-wave approach.  One pass through:
            1. Count spare coins available for this side
            2. Create new offers at the updated price (limited by spares)
            3. Post them to Dexie immediately
            4. Fire-and-forget cancel matching old offers (skip_confirmation)
            5. Return — the trim pass (step 12a) handles any residual excess

        No rolling waves, no cancel-first fallback, no inter-batch coin
        polling, no overalloc guard.  The trim pass already runs every
        cycle and cancels furthest-from-mid offers above the per-side cap,
        which naturally cleans up any slow-confirming cancels.

        Args:
            max_offers: If > 0, create/cancel at most this many offers.
                        0 means no limit.
            allowed_tiers: If provided, only target offers in these tiers
                           for replacement (graduated response).

        Returns dict with offers, fully_replaced, replaced_count, target_count.
        """
        # NOTE: _last_requote_time is set only when we actually do work (create or cancel).
        # Early returns (no spares, create failed) intentionally leave it unchanged so the
        # next cycle's cooldown check doesn't see a false "just requoted" timestamp and
        # suppress a genuine retry when conditions improve.

        # ── Gather open offers to replace ──
        all_open = get_open_offers(side=side, cat_asset_id=cfg.CAT_ASSET_ID)
        open_offers = [o for o in all_open
                       if o.get("tier") not in ("boost", "sniper")]
        # Filter against live wallet snapshot — avoid targeting offers that
        # already filled/expired this cycle (DB lags 1 cycle behind wallet).
        if live_offer_ids is not None:
            open_offers = [o for o in open_offers
                           if o.get("trade_id") in live_offer_ids]
        # Sort most-at-risk first so cancels prioritise the stale-est offers.
        open_offers = self._sort_open_offers_for_requote(
            open_offers, side, mid_price=current_price)

        # ── Graduated response: tier filter + budget cap ──
        # Track pre-filter count so the cold-start detection below can tell
        # "this side is truly empty" (do a full rebuild) apart from "the tier
        # filter drained the scope but other tiers are still live" (do
        # nothing — the ladder-fill path will refill the cancelled tier on
        # the next cycle with proper tier-matched coins).
        pre_filter_offer_count = len(open_offers)
        if allowed_tiers:
            _before = len(open_offers)
            open_offers = [o for o in open_offers
                           if str(o.get("tier") or "mid").lower()
                           in allowed_tiers]
            if len(open_offers) < _before:
                log_event("info", "requote_tier_filter",
                          f"Tier filter ({', '.join(sorted(allowed_tiers))}): "
                          f"{_before} → {len(open_offers)} offers to process")
        # `original_target_count` captures how many offers the requote
        # SHOULD replace before the per-cycle budget cap trims the list.
        # It is returned alongside the truncated target_count so callers
        # can tell the difference between a true full-replace and "we
        # capped at the budget and the rest of the old quotes are still
        # live." Previously only the truncated count was exposed, so a
        # capped FULL requote could report fully_replaced=True while 20+
        # stale offers sat exposed at the old mid until the next cycle.
        original_target_count = len(open_offers)
        if max_offers > 0 and len(open_offers) > max_offers:
            _full = len(open_offers)
            open_offers = open_offers[:max_offers]
            log_event("info", "requote_budget_cap",
                      f"Budget cap: processing {max_offers} of {_full} offers "
                      f"this cycle (rest deferred)")

        target_count = len(open_offers)

        log_event("info", "requote_start",
                  f"Requote {side}: {target_count} offers to replace, "
                  f"new price {current_price:.8f}")

        # ── Tier filter emptied the requote scope, but side is not cold ──
        # Happens after a defensive-cancel clears one tier in the wallet just
        # before this requote runs. Pre-fix behavior: fell through to the
        # cold-start branch below and rebuilt the FULL ladder, colliding with
        # the untouched outer/mid/extreme offers and producing duplicate-offer
        # storms that the trim pass had to clean up (post-mortem 2026-04-22
        # 05:10 cascade). Correct behavior: let the standard ladder-fill path
        # restore the cancelled tier on the next cycle with tier-matched
        # coins.
        if not open_offers and allowed_tiers and pre_filter_offer_count > 0:
            log_event("info", "requote_tier_empty_skip",
                      f"Tier filter drained requote scope for {side} "
                      f"({pre_filter_offer_count} non-matching offers still "
                      f"live) — deferring to ladder-fill on next cycle")
            return {
                "offers": [],
                "fully_replaced": False,
                "replaced_count": 0,
                "target_count": 0,
                "original_target_count": 0,
                "tier_filter_drained": True,
            }

        # ── Wallet-truth cold-start guard ──
        # Before the "no offers anywhere" branch triggers a full ladder
        # rebuild, double-check against the wallet snapshot. During the
        # post-mortem cascade, the DB briefly showed zero open offers for a
        # side (cancelled records) while Sage still held live "zombie"
        # offers — a cold-start rebuild at that moment would stack a full
        # ladder on top of the zombies and overshoot the cap. If the
        # wallet says offers are still live, defer; the regular
        # reconcile + ladder-fill paths will catch up on the next cycle.
        if not open_offers and live_offer_ids is not None and len(live_offer_ids) > 0:
            log_event("info", "requote_skip_wallet_has_offers",
                      f"DB shows {side} empty but wallet still holds "
                      f"{len(live_offer_ids)} live offer(s) — deferring "
                      f"cold-start rebuild to avoid zombie pile-up")
            return {
                "offers": [],
                "fully_replaced": False,
                "replaced_count": 0,
                "target_count": 0,
                "original_target_count": 0,
                "tier_filter_drained": False,
            }

        # ── Cold start: no existing offers → full ladder ──
        if not open_offers:
            log_event("info", "requote_cold_start",
                      f"No existing offers for {side} — creating full ladder")
            fresh = self.create_ladder(
                current_price, side,
                risk_manager=risk_manager,
                spread_fraction=spread_fraction,
                coin_ids_enabled=cfg.COIN_IDS_ENABLED,
                price_cap=price_cap,
                price_floor=price_floor)
            if dexie_manager and fresh:
                for offer in fresh:
                    bech32 = offer.get("offer_bech32", "")
                    trade_id = offer.get("trade_id", "")
                    if bech32 and trade_id:
                        dexie_manager.queue_post(bech32, trade_id)
                log_event("info", "requote_cold_start_queued",
                          f"Queued {len(fresh)} fresh {side} offers to Dexie")
            # Cold start did real work — stamp the cooldown timer
            with self._lock:
                self._last_requote_time[side] = time.time()
            return {
                "offers": fresh,
                "fully_replaced": True,
                "replaced_count": len(fresh),
                "target_count": 0,
                "original_target_count": 0,
                "tier_filter_drained": False,
            }

        # ── Count spare coins ──
        # Use DB coin tracking (which knows tier designations) rather than the
        # raw wallet RPC so that fee/sniper/reserve coins are not counted as
        # usable — they fail preselection and produce wasted RPC round-trips.
        wallet_type_str = "cat" if side == "sell" else "xch"
        spare_count = 0
        try:
            from database import get_free_coins
            _db_free = get_free_coins(wallet_type_str)
            _TRADING_DESIGS = {"tier_spare", "tier_active"}
            _SKIP_TIERS = {"none", "sniper", "reserve", "fee"}
            spare_count = sum(
                1 for c in _db_free
                if c.get("designation", "") in _TRADING_DESIGS
                and c.get("assigned_tier", "none") not in _SKIP_TIERS
            )
        except Exception:
            # Fallback to raw RPC count if DB query fails
            try:
                wallet_id = (cfg.CAT_WALLET_ID if side == "sell"
                             else cfg.WALLET_ID_XCH)
                _resp = get_exact_spendable_coins_rpc(wallet_id)
                if _resp:
                    _coins = (_resp.get("confirmed_records",
                              _resp.get("coin_records",
                              _resp.get("records", []))))
                    spare_count = len(_coins) if _coins else 0
                    if get_wallet_type() != "sage":
                        try:
                            _open = len(get_open_offers(
                                side=side,
                                cat_asset_id=cfg.CAT_ASSET_ID))
                            spare_count = max(0, spare_count - _open)
                        except Exception:
                            pass
            except Exception:
                pass

        log_event("info", "requote_spare_coins",
                  f"Spare {side} coins: {spare_count} (tier-designated)")

        if spare_count == 0:
            log_event("info", "requote_no_spares",
                      f"Requote {side}: 0 spare coins — cannot create "
                      f"replacements, trim pass will clean excess if needed")
            return {
                "offers": [],
                "fully_replaced": False,
                "replaced_count": 0,
                "target_count": target_count,
                "original_target_count": original_target_count,
                "tier_filter_drained": False,
            }

        # ── Step 1: Create new offers at new price ──
        create_count = min(target_count, spare_count)
        log_event("info", "requote_creating",
                  f"Requote {side}: creating {create_count} new offers "
                  f"({spare_count} spares, target {target_count})")

        # Use the full ladder size for tier classification so that
        # each replacement offer lands in the correct tier (inner/mid/outer/
        # extreme) with the right coin-size cap.  Using target_count (the
        # number of offers being replaced) caused all slots to classify as
        # "inner" whenever target_count ≤ BUY_INNER_TIER_COUNT, which forced
        # an inner-sized coin cap on every offer regardless of its real
        # position — rejecting all available larger coins and returning 0.
        _full_slots = (cfg.MAX_ACTIVE_BUY_OFFERS if side == "buy"
                       else cfg.MAX_ACTIVE_SELL_OFFERS)
        new_offers = self.create_ladder(
            current_price, side, num_offers=create_count,
            slot_start=0, total_slots=_full_slots,
            risk_manager=risk_manager,
            spread_fraction=spread_fraction,
            coin_ids_enabled=cfg.COIN_IDS_ENABLED,
            price_cap=price_cap,
            price_floor=price_floor,
        )

        if not new_offers:
            log_event("info", "requote_create_failed",
                      f"Requote {side}: create_ladder returned 0 offers "
                      f"— keeping old offers in place")
            return {
                "offers": [],
                "fully_replaced": False,
                "replaced_count": 0,
                "target_count": target_count,
                "original_target_count": original_target_count,
                "tier_filter_drained": False,
            }

        # ── Step 2: Post new offers to Dexie ──
        if dexie_manager:
            for offer in new_offers:
                bech32 = offer.get("offer_bech32", "")
                trade_id = offer.get("trade_id", "")
                if bech32 and trade_id:
                    dexie_manager.queue_post(bech32, trade_id)

        # ── Step 3: Fire-and-forget cancel matching old offers ──
        # Cancel the same number as created so the book stays near cap.
        # skip_confirmation=True: don't block for on-chain confirmation.
        # If cancels are slow the trim pass handles the transient excess.
        cancel_count = min(len(new_offers), len(open_offers))
        cancel_ids = [o["trade_id"] for o in open_offers[:cancel_count]
                      if o.get("trade_id")]
        if cancel_ids:
            log_event("info", "requote_cancel",
                      f"Requote {side}: fire-and-forget cancel of "
                      f"{len(cancel_ids)} old offers")
            self.cancel_offers(cancel_ids, reason="requote",
                               skip_confirmation=True)

        log_event("info", "requote_done",
                  f"Requote {side} complete: created {len(new_offers)} new, "
                  f"cancelled {len(cancel_ids)} old "
                  f"(trim pass handles residual excess)")
        # Requote did real work — stamp the cooldown timer now (not at entry)
        with self._lock:
            self._last_requote_time[side] = time.time()
        # fully_replaced is True only when we replaced every offer we
        # INTENDED to replace BEFORE the per-cycle budget cap truncated
        # the list. Using the truncated target_count here used to mark a
        # capped FULL requote as "fully replaced" while 20+ old offers
        # sat exposed at the stale mid — the caller then advanced its
        # drift baseline and skipped them until another trigger fired.
        return {
            "offers": new_offers,
            "fully_replaced": len(new_offers) >= original_target_count,
            "replaced_count": len(new_offers),
            "target_count": target_count,
            "original_target_count": original_target_count,
            "tier_filter_drained": False,
        }

    # -------------------------------------------------------------------
    # Cancellation
    # -------------------------------------------------------------------

    def cancel_offers(self, trade_ids: List[str], reason: str = "manual",
                      force_storm: bool = False,
                      skip_confirmation: bool = False) -> Dict:
        """Cancel a list of offers.

        Marks them as bot-cancelled so fill detection doesn't count them as fills.
        Sequential cancellation with delays (parallel breaks the wallet).

        F20 (2026-04-08): cancel-storm protection. If a single call asks
        to cancel more than CANCEL_STORM_THRESHOLD_PCT of the live book
        in one shot AND the caller didn't pass force_storm=True, the
        call is REFUSED with a critical alert. Reasons that legitimately
        cancel large fractions (Cancel All button, reserve floor breach,
        circuit breaker, shutdown) all explicitly pass force_storm=True.
        Routine requote/expiry/sniper paths do NOT — so a bug there
        that tries to nuke the book gets caught here instead of executing.
        """
        if not trade_ids:
            return {}

        # F20: cancel-storm protection
        if not force_storm:
            try:
                # Count how many offers we currently have live (DB view)
                from database import get_open_offers as _gso
                live_count = len(_gso(cat_asset_id=cfg.CAT_ASSET_ID))
            except Exception:
                live_count = 0
            if live_count > 0:
                pct = (len(trade_ids) / live_count) * 100
                threshold_pct = float(getattr(cfg, "CANCEL_STORM_THRESHOLD_PCT", 80) or 80)
                if pct >= threshold_pct and len(trade_ids) >= 5:
                    log_event(
                        "error",
                        "cancel_storm_blocked",
                        f"BLOCKED cancel storm: caller {reason} tried to cancel "
                        f"{len(trade_ids)}/{live_count} offers ({pct:.0f}%) in one "
                        f"shot. Threshold is {threshold_pct:.0f}%. Refusing — pass "
                        f"force_storm=True if this is intentional (e.g. Cancel All, "
                        f"reserve floor breach, shutdown).",
                    )
                    return {tid: {"success": False, "error": "cancel_storm_blocked"}
                            for tid in trade_ids}

        log_event("info", "cancel_start",
                  f"Cancelling {len(trade_ids)} offers (reason: {reason})")

        # Mark as bot-cancelled BEFORE cancelling (for fill detection)
        for tid in trade_ids:
            self._bot_cancelled_ids.add(tid)
            # Lifecycle: CANCEL_SENT signal transitions open → cancel_requested
            # so the dashboard can distinguish "in progress" from "confirmed".
            try:
                transition_offer(tid, "cancel_sent")
            except Exception:
                pass  # lifecycle update is additive — never block cancel
            # Stamp cancel attempt time so bot_health.check_pending_cancels()
            # can throttle retries (don't re-cancel a still-pending offer
            # every cycle — wait the configured backoff first).
            try:
                mark_cancel_attempted(tid)
            except Exception:
                pass

        # NOTE: Sage's cancel endpoints don't accept coin_ids — fee coin
        # is always auto-selected.  Bulk cancel (≥3 offers) uses a single
        # transaction so only 1 fee coin is consumed.  Creates DO get
        # dedicated fee coins via make_offer's coin_ids to prevent overlap.
        results = cancel_offers_batch(trade_ids, secure=True,
                                      skip_confirmation=skip_confirmation)

        # Log results summary
        successes = sum(1 for r in results.values() if r and r.get("success"))
        failures = len(results) - successes
        log_event("info", "cancel_result",
                  f"Cancel results: {successes} succeeded, {failures} failed "
                  f"(reason: {reason})")

        # Update database status + coin tracking
        for tid, result in results.items():
            if result and result.get("success"):
                method = str(result.get("method") or "")
                if method in CANCEL_PENDING_METHODS:
                    # Submission accepted but not on-chain yet. Leave DB
                    # status alone — a later cancel reconcile or fill
                    # detection will catch the true state once the TX
                    # confirms (or times out in the mempool). For the
                    # specific "already_gone_ambiguous" case (Sage 404)
                    # also drop the local bot-cancel flag so fill_tracker
                    # does not skip on-chain verification on the next pass.
                    log_event("debug", "cancel_pending_mempool",
                              f"Cancel for {tid[:16]}... submitted but not "
                              f"yet confirmed (method={method}); leaving DB "
                              f"status open")
                    if method == "already_gone_ambiguous":
                        self._bot_cancelled_ids.discard(tid)
                    continue
                # update_offer_status propagates lifecycle_state → "cancelled" automatically
                update_offer_status(tid, "cancelled")
            else:
                # Cancel failed — queue for retry (V1 parity)
                # Lifecycle: CANCEL_FAILED signal reverts cancel_requested → open
                self._bot_cancelled_ids.discard(tid)
                try:
                    transition_offer(tid, "cancel_failed")
                except Exception:
                    pass
                if tid not in self._pending_cancel_retries:
                    self._pending_cancel_retries[tid] = {
                        "attempts": 1,
                        "first_failed": time.time(),
                    }
                    log_event("warning", "cancel_failed_queued",
                              f"Cancel failed for {tid[:16]}... — queued for retry")

        if successes > 0:
            log_event("info", "offers_cancelled",
                      f"Cancelled {successes} offers (reason: {reason})")
            # F75: request a fast reconcile so the returned coins are
            # picked up into their tier pools before the next rebuild
            # attempt. Without this, the normal 2-cycle reconcile
            # cadence races the rebuild and the bot tries to create
            # ladder slots before seeing the newly-freed backing coins.
            try:
                from coin_manager import request_fast_reconcile
                request_fast_reconcile(reason=f"cancel:{reason}")
            except Exception:
                pass  # best-effort; the normal cadence still runs
            # Purge successfully cancelled offers from the Dexie post queue so
            # they don't generate "Invalid Offer" 400 errors on the next flush.
            if self.dexie_manager is not None:
                cancelled_ids = [tid for tid, r in results.items()
                                 if r and r.get("success")]
                try:
                    self.dexie_manager.purge_trade_ids(cancelled_ids)
                except Exception:
                    pass  # non-critical — flush will handle the 400 gracefully
        if failures > 0:
            log_event("warning", "offers_cancel_pending",
                      f"{failures} offers failed to cancel and remain queued for retry "
                      f"(reason: {reason})")

        return results

    def cancel_all(
        self,
        cat_asset_id: str = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        side_filter: str = "",
    ) -> Dict:
        """Cancel all open offers (or only one side's offers) in controlled batches.

        Sage wallet struggles with bulk cancels — each cancel is an on-chain
        transaction and too many at once can cause long pending states. This
        method cancels in measured batches with a short pause between batches
        so we can push harder than one-by-one shutdown without blind fire-and-forget.

        First checks the database. If the DB has no open offers (e.g. pre-existing
        offers that were never inserted), falls back to fetching directly from the
        wallet RPC.

        Args:
            side_filter: If "buy", cancel only buy offers. If "sell", cancel only
                         sell offers. Empty string (default) cancels all sides.
                         Used by the position circuit breaker to cancel only the
                         accumulating side while keeping the correcting side live.
        """
        def emit_progress(**payload):
            if not progress_callback:
                return
            try:
                progress_callback(payload)
            except Exception as e:
                log_event("debug", "cancel_progress_callback_failed",
                          f"Cancel progress callback raised: {e}")

        def apply_batch_results(batch_results: Dict[str, Dict]) -> None:
            for tid, result in batch_results.items():
                if result and result.get("success"):
                    method = str(result.get("method") or "")
                    if method in CANCEL_PENDING_METHODS:
                        # Submission accepted but not on-chain yet. Leave
                        # DB status alone — a later cancel reconcile or
                        # fill detection will catch the true state once
                        # the TX confirms (or times out in the mempool).
                        # For Sage 404 ("already_gone_ambiguous") also drop
                        # the local bot-cancel marker so fill_tracker does
                        # not skip on-chain verification.
                        log_event("debug", "cancel_pending_mempool",
                                  f"Cancel for {tid[:16]}... submitted but "
                                  f"not yet confirmed (method={method}); "
                                  f"leaving DB status open")
                        if method == "already_gone_ambiguous":
                            self._bot_cancelled_ids.discard(tid)
                    else:
                        update_offer_status(tid, "cancelled")
                else:
                    self._bot_cancelled_ids.discard(tid)
                    if tid not in self._pending_cancel_retries:
                        self._pending_cancel_retries[tid] = {
                            "attempts": 1,
                            "first_failed": time.time(),
                        }

        asset_id = cat_asset_id or cfg.CAT_ASSET_ID
        open_offers = get_open_offers(cat_asset_id=asset_id)

        # Apply side filter if specified (e.g. position circuit breaker only
        # wants to cancel buys when over-long, leaving sells live)
        _side = str(side_filter or "").strip().lower()
        if _side in ("buy", "sell"):
            open_offers = [o for o in open_offers if o.get("side", "") == _side]
            log_event("info", "cancel_all",
                      f"Side filter '{_side}' applied — cancelling {len(open_offers)} "
                      f"{_side} offers only")

        trade_ids = [o["trade_id"] for o in open_offers]

        # Fallback: if DB has nothing, check the wallet directly
        if not trade_ids:
            log_event("info", "cancel_all", "DB has 0 open offers — fetching from wallet RPC")
            try:
                all_wallet = get_all_offers(include_completed=False, start=0, end=500)
                if all_wallet:
                    open_buys, open_sells, _ = classify_offers_from_list(
                        all_wallet, asset_id)
                    if _side == "buy":
                        side_offers = open_buys
                    elif _side == "sell":
                        side_offers = open_sells
                    else:
                        side_offers = open_buys + open_sells
                    for o in side_offers:
                        tid = o.get("trade_id", "")
                        if tid and tid not in trade_ids:
                            trade_ids.append(tid)
                    if trade_ids:
                        log_event("info", "cancel_all",
                                  f"Found {len(trade_ids)} open offers from wallet RPC")
            except Exception as e:
                log_event("error", "cancel_all", f"Wallet RPC fallback failed: {e}")

        if not trade_ids:
            emit_progress(
                running=False,
                complete=True,
                phase="complete",
                total=0,
                batch_size=0,
                total_batches=0,
                current_batch=0,
                cancelled=0,
                failed=0,
                message="No active offers found to cancel.",
            )
            return {}

        # Mark all as bot-cancelled BEFORE starting (for fill detection)
        # and stamp cancel_sent / cancel_last_attempt_at via the lifecycle
        # helpers so the bot_health pending-cancel verifier can re-drive
        # zombie recoveries if Sage doesn't confirm. Without these stamps
        # a fee-starved bulk cancel (stop, CB, or >20-offer cancel storm)
        # leaves stale offers live with no automatic retry/escalation
        # because the verifier only acts on rows whose cancel-attempt
        # timestamp is set.
        for tid in trade_ids:
            self._bot_cancelled_ids.add(tid)
            try:
                transition_offer(tid, "cancel_sent")
            except Exception:
                pass  # lifecycle update is additive — never block cancel
            try:
                mark_cancel_attempted(tid)
            except Exception:
                pass

        # Send ALL offers in a single bulk cancel RPC to Sage, then
        # wait for on-chain confirmation. Sage handles bulk cancels natively
        # and batching just adds unnecessary delay.
        total = len(trade_ids)
        all_results = {}

        log_event("info", "cancel_all_bulk",
                  f"Cancelling all {total} offers in one bulk request")
        emit_progress(
            running=True,
            complete=False,
            phase="cancelling",
            total=total,
            batch_size=total,
            total_batches=1,
            current_batch=1,
            cancelled=0,
            failed=0,
            message=f"Cancelling {total} offers...",
        )

        try:
            bulk_results = cancel_offers_batch(trade_ids, secure=True)
            all_results.update(bulk_results)
            apply_batch_results(bulk_results)
        except Exception as e:
            log_event("warning", "cancel_all_error",
                      f"Bulk cancel error: {e}")
            for tid in trade_ids:
                if tid not in all_results:
                    all_results[tid] = {"success": False, "error": str(e)}
            apply_batch_results({tid: all_results[tid] for tid in trade_ids
                                 if not (all_results.get(tid, {}).get("success"))})

        # F50 (2026-04-09): summarise by confirmed vs pending vs failed.
        # Previously we lumped confirmed + pending together as "succeeded"
        # which misled the operator into thinking cancels completed on-chain
        # when they were actually stuck in a congested mempool. The user hit
        # this after a stop-then-cancel-all flow where only ~half the cancels
        # made it into a block — the UI cheerfully reported "46 succeeded"
        # but Sage still showed 23 active. Now we distinguish.
        CONFIRMED_METHODS = {
            "confirmed_by_status",
            "confirmed_by_unlock",
            "confirmed_coins_returned",
            "confirmed_by_coin_delta",
            "bulk",
        }
        # Reuse the module-level constant to avoid drift.
        PENDING_METHODS = CANCEL_PENDING_METHODS

        def _classify(r):
            if not isinstance(r, dict):
                return "failed"
            if not r.get("success"):
                return "failed"
            method = r.get("method", "")
            if method in CONFIRMED_METHODS:
                return "confirmed"
            if method in PENDING_METHODS:
                return "pending"
            return "confirmed"

        confirmed_count = sum(1 for r in all_results.values() if _classify(r) == "confirmed")
        pending_count   = sum(1 for r in all_results.values() if _classify(r) == "pending")
        failed_count    = sum(1 for r in all_results.values() if _classify(r) == "failed")
        # successes = anything accepted by Sage (confirmed + pending) for
        # backwards-compat with callers that don't know about pending.
        successes = confirmed_count + pending_count
        failures = failed_count

        if pending_count > 0:
            # Some cancels are still in the mempool — warn the operator to
            # wait a block or two and re-check before concluding.
            log_event(
                "warning",
                "cancel_all_done",
                f"Cancel all finished: {confirmed_count} confirmed on-chain, "
                f"{pending_count} PENDING in mempool (may still fail due to "
                f"mempool conflict or fee rejection), {failed_count} failed. "
                f"Re-check offers in 1-2 minutes to verify pending cancels "
                f"actually confirmed."
            )
            final_message = (
                f"Cancel all finished: {confirmed_count} confirmed, "
                f"{pending_count} pending on-chain, {failed_count} failed. "
                f"Wait 1-2 minutes then re-check."
            )
        else:
            log_event("info", "cancel_all_done",
                      f"Cancel all complete: {confirmed_count} confirmed on-chain, "
                      f"{failed_count} failed")
            final_message = (
                f"Cancel all complete: {confirmed_count} confirmed"
                + (f", {failed_count} failed" if failed_count else "") + "."
            )

        emit_progress(
            running=False,
            complete=True,
            phase="complete",
            total=total,
            batch_size=total,
            total_batches=1,
            current_batch=1,
            cancelled=successes,
            confirmed=confirmed_count,
            pending=pending_count,
            failed=failures,
            message=final_message,
        )

        return all_results

    # -------------------------------------------------------------------
    # -------------------------------------------------------------------
    # Cache maintenance
    # -------------------------------------------------------------------

    def prune_caches(self, active_trade_ids: set = None):
        """Prune unbounded in-memory caches to prevent memory growth.

        Called periodically from housekeeping.
        """
        # Prune _bot_cancelled_ids — only remove IDs that are confirmed gone
        # AND are NOT pending a cancel retry. Removing an ID whose cancel is
        # still in-flight would cause fill_tracker to misinterpret the eventual
        # disappearance as a real fill (phantom fill bug).
        if active_trade_ids is not None and len(self._bot_cancelled_ids) > 500:
            pending_retry_ids = set(self._pending_cancel_retries.keys())
            safe_to_prune = self._bot_cancelled_ids - active_trade_ids - pending_retry_ids
            # Keep IDs that are still in active offers (cancel not confirmed yet)
            # or still queued for retry
            self._bot_cancelled_ids -= safe_to_prune

        # Prune _offer_details_cache — remove entries not in active offers
        if active_trade_ids is not None and len(self._offer_details_cache) > 200:
            stale = [k for k in self._offer_details_cache if k not in active_trade_ids]
            for k in stale:
                del self._offer_details_cache[k]

        # Prune _recently_created — remove expired entries
        now = time.time()
        expired = [k for k, t in self._recently_created.items()
                   if now - t > self._recently_created_ttl]
        for k in expired:
            del self._recently_created[k]

        # NOTE: _inflight_coin_ids is deliberately NOT cleared here.
        # Each _create_offer_with_retry_inner call adds a coin under
        # self._lock and has its own try/finally that discards it on
        # every exit path (success, failure, exception). A periodic
        # `clear()` here would race with slow in-flight creates that
        # have released the lock for the RPC call — clearing during
        # that RPC window would let another thread re-pick the same
        # coin and cause a MEMPOOL_CONFLICT or double-spend.

    # -------------------------------------------------------------------
    # Expiry management
    # -------------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Find and cancel expired offers.

        The Chia wallet doesn't auto-expire offers — they stay "open" forever.
        We must check valid_times.max_time manually and cancel stale ones.
        """
        count = cleanup_expired_offers()

        # Also update our database for any that expired
        open_offers = get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
        expired_count = 0
        for offer in open_offers:
            expires_at = offer.get("expires_at")
            if expires_at:
                from datetime import datetime, timezone
                try:
                    exp_time = datetime.fromisoformat(expires_at)
                    # Ensure timezone-aware comparison
                    if exp_time.tzinfo is None:
                        exp_time = exp_time.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > exp_time:
                        update_offer_status(offer["trade_id"], "expired")
                        # Expired offers unlock the coin (no on-chain tx for expiry).
                        # Mark the coin as free so it can be reused.
                        _expired_coin_id = offer.get("coin_id")
                        if _expired_coin_id:
                            try:
                                from database import free_coin as _free_coin
                                _free_coin(_expired_coin_id)
                                log_event("debug", "coin_freed_on_expire",
                                          f"Coin {_expired_coin_id[:16]}... freed "
                                          f"(offer {offer['trade_id'][:12]}... expired)")
                            except Exception as e:
                                log_event("debug", "coin_free_on_expire_failed",
                                          f"Could not free coin on offer expiry (non-critical): {e}")
                        expired_count += 1
                except (ValueError, TypeError):
                    pass

        if expired_count > 0:
            log_event("info", "offers_expired", f"Cleaned up {expired_count} expired offers")

        return count + expired_count

    # -------------------------------------------------------------------
    # Offer state queries
    # -------------------------------------------------------------------

    def is_bot_cancelled(self, trade_id: str) -> bool:
        """Return True if this trade_id was cancelled by the bot.

        Non-destructive: does NOT remove the ID on read. The ID is removed
        when prune_caches() runs (safely, excluding pending retry IDs).
        This prevents phantom fills when a cancel takes multiple cycles
        to confirm on-chain.
        """
        return trade_id in self._bot_cancelled_ids

    def get_cached_details(self, trade_id: str) -> Optional[Dict]:
        """Get cached offer details for a trade_id."""
        return self._offer_details_cache.get(trade_id)

    def get_open_offer_count(self, side: str = None) -> int:
        """Count open offers, optionally by side."""
        offers = get_open_offers(side=side, cat_asset_id=cfg.CAT_ASSET_ID)
        return len(offers)

    # -------------------------------------------------------------------
    # Pre-emptive offer refresh (V1 parity: detect_expiring_offers)
    # -------------------------------------------------------------------

    def detect_expiring_offers(self, open_offers: list,
                                refresh_before_secs: int = None) -> List[str]:
        """Find offers approaching expiry so we can replace them BEFORE they die.

        V1 had this as detect_expiring_offers() — it's critical for continuous
        market presence. Without it, offers expire and there's a window with
        nothing on the book until the next cycle creates replacements.

        Args:
            open_offers: List of offer records from wallet sync
            refresh_before_secs: How far ahead to look (default: 5 min before expiry)

        Returns list of trade_ids that are about to expire.
        """
        if refresh_before_secs is None:
            refresh_before_secs = getattr(cfg, "OFFER_REFRESH_BEFORE", 1800)

        now = int(time.time())
        expiring = []

        for offer in open_offers:
            # Check valid_times.max_time from the wallet RPC record
            valid_times = offer.get("valid_times") or {}
            max_time = valid_times.get("max_time", 0)

            if max_time and max_time > 0:
                time_left = max_time - now
                if 0 < time_left < refresh_before_secs:
                    tid = offer.get("trade_id", "")
                    if tid:
                        expiring.append(tid)

        if expiring:
            log_event("info", "expiring_soon",
                      f"Found {len(expiring)} offers expiring within {refresh_before_secs}s")

        return expiring

    # -------------------------------------------------------------------
    # Trim excess offers (Fix 3: belt-and-braces overshoot guard)
    # -------------------------------------------------------------------

    def trim_excess_offers(self, mid_price: Decimal,
                           wallet_buys: list = None,
                           wallet_sells: list = None) -> int:
        """Cancel any offers above the configured per-side cap.

        Belt-and-braces guard against the requote overshoot the bot got
        into on 2026-04-07: when cancels were slow to confirm, repeated
        create-first requote rounds left the live book at 29 sells against
        a 24 cap. The over-allocation guard only blocked NEW creation; it
        never trimmed the excess. This method does the trim.

        When ``wallet_buys`` / ``wallet_sells`` are provided (from the
        wallet sync step), they are used as the ground-truth open-offer
        count instead of the DB.  This closes the gap where the DB has
        already marked a cancel-pending offer as "cancelled" but the
        wallet still holds it open — the DB would show 12 (under cap)
        while the wallet shows 20 (8 excess).

        Strategy: pick the offers furthest from `mid_price` on each side
        (least useful market-making) and cancel them until count == cap.

        Returns: total number of offers asked to cancel (across both sides).
        """
        # SINGLE SOURCE OF TRUTH: cap comes from the sum of tier counts in
        # the live ladder settings, not a separate MAX_ACTIVE_* key. This
        # ensures trim never fights the ladder the user asked for.
        def _ladder_cap(side: str) -> int:
            try:
                if side == "buy":
                    total = (
                        int(getattr(cfg, "BUY_INNER_TIER_COUNT", 0) or 0)
                        + int(getattr(cfg, "BUY_MID_TIER_COUNT", 0) or 0)
                        + int(getattr(cfg, "BUY_OUTER_TIER_COUNT", 0) or 0)
                        + int(getattr(cfg, "BUY_EXTREME_TIER_COUNT", 0) or 0)
                    )
                else:
                    total = (
                        int(getattr(cfg, "SELL_INNER_TIER_COUNT", 0) or 0)
                        + int(getattr(cfg, "SELL_MID_TIER_COUNT", 0) or 0)
                        + int(getattr(cfg, "SELL_OUTER_TIER_COUNT", 0) or 0)
                        + int(getattr(cfg, "SELL_EXTREME_TIER_COUNT", 0) or 0)
                    )
                if total > 0:
                    return total
            except Exception:
                pass
            # Fallback to legacy caps if tier counts are not available
            if side == "buy":
                return int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25) or 25)
            return int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25) or 25)

        max_buy = _ladder_cap("buy")
        max_sell = _ladder_cap("sell")

        try:
            mid_d = Decimal(str(mid_price or 0))
        except Exception:
            mid_d = Decimal("0")

        total_trimmed = 0

        _wallet_map = {"buy": wallet_buys, "sell": wallet_sells}

        for side, cap in (("buy", max_buy), ("sell", max_sell)):
            # Prefer wallet ground truth over DB — the wallet shows what
            # is ACTUALLY open on-chain, while the DB might have already
            # marked cancel-pending offers as "cancelled".
            _w_offers = _wallet_map.get(side)
            if _w_offers is not None:
                open_offers_all = list(_w_offers)
            else:
                try:
                    open_offers_all = get_open_offers(side=side,
                                                      cat_asset_id=cfg.CAT_ASSET_ID) or []
                except Exception as e:
                    log_event("warning", "trim_excess_query_failed",
                              f"trim_excess_offers: could not query open {side} offers: {e}")
                    continue

            # Exclude sniper-tier offers from the ladder cap check — snipers
            # are a separate pool and must not cause ladder offers to be
            # cancelled.
            open_offers = [
                o for o in open_offers_all
                if (o.get("tier") or "").lower() != "sniper"
            ]

            # Exclude offers already pending cancel (fire-and-forget from
            # requote).  Without this, trim re-cancels the same offers,
            # wasting RPCs and filling the retry queue with noise.
            _pending = self._bot_cancelled_ids
            open_offers = [
                o for o in open_offers
                if o.get("trade_id") not in _pending
            ]

            excess = len(open_offers) - cap
            if excess <= 0:
                continue

            # Fee coin dedication (via FeeCoinPool) eliminates the
            # MEMPOOL_CONFLICT risk that previously required a per-cycle cap.
            # Each cancel batch now reserves its own fee coin, so we can
            # trim all excess in one shot instead of spreading across cycles.

            def _distance_from_mid(o):
                try:
                    p = Decimal(str(o.get("price_xch") or o.get("price") or 0))
                    if p <= 0 or mid_d <= 0:
                        return Decimal("0")
                    return abs(p - mid_d)
                except Exception:
                    return Decimal("0")

            # Sort furthest-from-mid first; those carry the least
            # market-making value, so they're the safest to drop.
            sorted_offers = sorted(open_offers, key=_distance_from_mid, reverse=True)
            to_cancel = sorted_offers[:excess]
            cancel_ids = [o.get("trade_id") for o in to_cancel if o.get("trade_id")]

            if not cancel_ids:
                continue

            log_event("info", "trim_excess_offers",
                      f"Trim pass: {side} open={len(open_offers)} > cap={cap}, "
                      f"cancelling {len(cancel_ids)} furthest-from-mid offer(s)")

            try:
                self.cancel_offers(cancel_ids, reason="trim_excess",
                                   skip_confirmation=True)
                total_trimmed += len(cancel_ids)
            except Exception as e:
                log_event("error", "trim_excess_cancel_failed",
                          f"trim_excess_offers: cancel call failed for {side}: {e}")

        return total_trimmed

    # -------------------------------------------------------------------
    # Retry failed cancels (V1 parity: retry_failed_cancels)
    # -------------------------------------------------------------------

    def retry_failed_cancels(self) -> int:
        """Retry cancel requests that previously failed.

        V1 tracked these in _pending_retries and retried each loop.
        Without this, failed cancels leave "ghost offers" that fill max slots
        and the bot gradually degrades.

        Returns number of successfully retried cancels.
        """
        if not self._pending_cancel_retries:
            return 0

        success_count = 0
        to_remove = []

        for trade_id, info in list(self._pending_cancel_retries.items()):
            attempts = info.get("attempts", 0)

            try:
                from database import get_offer
                existing = get_offer(trade_id)
            except Exception:
                existing = None

            if existing and (existing.get("status") == "filled" or existing.get("filled_at")):
                log_event(
                    "info",
                    "cancel_retry_skipped_filled",
                    f"Skipping cancel retry for {trade_id[:16]}... because the offer is already recorded as filled",
                )
                self._bot_cancelled_ids.discard(trade_id)
                to_remove.append(trade_id)
                continue

            if attempts >= self._max_cancel_retries:
                # Give up after max attempts — mark as cancelled anyway
                log_event("warning", "cancel_retry_exhausted",
                          f"Giving up cancel retry for {trade_id[:16]}... "
                          f"after {attempts} attempts; leaving status unchanged "
                          f"until wallet sync proves the offer is gone")
                self._bot_cancelled_ids.discard(trade_id)
                to_remove.append(trade_id)
                continue

            # Try cancelling again
            res = cancel_offer(trade_id, secure=True, timeout=30)
            info["attempts"] = attempts + 1

            if res and res.get("success"):
                method = str(res.get("method") or "")
                if method in CANCEL_PENDING_METHODS:
                    # Submission accepted but not on-chain yet. Don't mark
                    # DB cancelled — a later bot_health cancel reconcile or
                    # fill_tracker sweep will settle the true state once
                    # the TX confirms (or the mempool entry times out).
                    log_event("debug", "cancel_retry_pending_mempool",
                              f"Cancel retry for {trade_id[:16]}... submitted but "
                              f"not yet confirmed (method={method}); leaving DB "
                              f"status open for bot_health to verify")
                    try:
                        mark_cancel_attempted(trade_id)
                    except Exception:
                        pass
                    # Keep in retry list but reset attempt counter so we
                    # don't re-fire before the pending TX has a chance to
                    # confirm. bot_health.check_pending_cancels will close
                    # the loop when Dexie reports the final state.
                    to_remove.append(trade_id)
                    continue
                if res.get("already_gone") or res.get("uncertain"):
                    # Sage returned 404 "Missing offer" or an uncertain HTTP
                    # status. "Missing offer" can mean filled, cancelled or
                    # expired — we DO NOT know which. Leave DB status open
                    # so fill_tracker (Spacescan golden gate) or bot_health
                    # (Dexie reconcile) can decide. Keep _bot_cancelled_ids
                    # cleared so fill_tracker doesn't short-circuit on it.
                    log_event("warning", "cancel_retry_ambiguous",
                              f"Cancel retry for {trade_id[:16]}... returned "
                              f"ambiguous Sage response (already_gone={bool(res.get('already_gone'))}, "
                              f"uncertain={bool(res.get('uncertain'))}); deferring "
                              f"to fill/cancel reconcile",
                              data={"trade_id": trade_id, "res_method": method})
                    self._bot_cancelled_ids.discard(trade_id)
                    try:
                        mark_cancel_attempted(trade_id)
                    except Exception:
                        pass
                    to_remove.append(trade_id)
                    continue
                log_event("info", "cancel_retry_success",
                          f"Cancel retry succeeded for {trade_id[:16]}... "
                          f"(attempt {info['attempts']}, method={method or 'unspecified'})")
                update_offer_status(trade_id, "cancelled")
                success_count += 1
                to_remove.append(trade_id)
            else:
                log_event("debug", "cancel_retry_failed",
                          f"Cancel retry failed for {trade_id[:16]}... "
                          f"(attempt {info['attempts']}/{self._max_cancel_retries})")

        # Clean up completed/exhausted retries
        for tid in to_remove:
            self._pending_cancel_retries.pop(tid, None)

        return success_count

    # -------------------------------------------------------------------
    # Recently-created tracking (V1 parity: prevents over-creation)
    # -------------------------------------------------------------------

    def clean_visible_recently_created(self, visible_ids: set):
        """Remove recently-created offers that now appear in wallet sync.

        Without this, offers get double-counted: once in the wallet sync
        count and once in the recently-created count. This would make the
        bot think it has more offers than it really does and skip creating.
        """
        to_remove = [tid for tid in self._recently_created if tid in visible_ids]
        for tid in to_remove:
            self._recently_created.pop(tid, None)

    def get_recently_created_count(self, side: str) -> int:
        """Count offers created recently that might not be visible in wallet yet.

        V1 tracked this to prevent creating too many offers when the wallet
        RPC hasn't caught up yet. Only counts offers NOT yet visible in
        wallet sync (clean_visible_recently_created removes the visible ones).
        """
        now = time.time()
        count = 0
        expired_keys = []

        for tid, info_time in self._recently_created.items():
            if now - info_time > self._recently_created_ttl:
                expired_keys.append(tid)
            else:
                detail = self._offer_details_cache.get(tid, {})
                if detail.get("side") == side:
                    count += 1

        # Prune expired entries
        for k in expired_keys:
            self._recently_created.pop(k, None)

        return count

    # -------------------------------------------------------------------
    # Wallet sync
    # -------------------------------------------------------------------

    def get_wallet_sync_meta(self) -> Dict[str, Any]:
        """Return lightweight metadata about the last wallet offer sync."""
        return dict(self._wallet_sync_meta)

    def sync_from_wallet(self) -> Tuple[List, List, List]:
        """Sync offer state from the Chia wallet RPC.

        Fetches all offers from the wallet and classifies them.
        Returns (open_buys, open_sells, closed).

        CRITICAL: Uses include_completed=False to only get open offers.
        With include_completed=True, old cancelled/completed offers flood
        the result window (end=500) and push genuinely open offers out
        of the results — the exact V1 truncation bug but at 200 instead
        of 50. By excluding completed, we only get what matters.
        """
        # Only fetch non-completed offers — avoids truncation by old cancelled offers
        all_offers = get_all_offers(include_completed=False, start=0, end=500)
        if all_offers is None:
            err = str(getattr(get_all_offers, "_last_error", "") or "wallet get_offers unavailable")
            self._wallet_sync_meta["fresh"] = False
            self._wallet_sync_meta["using_cache"] = bool(
                self._wallet_sync_cache["buy"] or
                self._wallet_sync_cache["sell"] or
                self._wallet_sync_cache["closed"]
            )
            self._wallet_sync_meta["consecutive_failures"] = int(self._wallet_sync_meta.get("consecutive_failures", 0) or 0) + 1
            self._wallet_sync_meta["last_error"] = err
            self._wallet_sync_meta["last_failure_at"] = time.time()
            self._wallet_sync_meta["cache_size"] = (
                len(self._wallet_sync_cache["buy"]) +
                len(self._wallet_sync_cache["sell"])
            )

            if self._wallet_sync_meta["consecutive_failures"] == 1:
                if self._wallet_sync_meta["using_cache"]:
                    log_event(
                        "warning",
                        "wallet_sync_cache",
                        f"Wallet offer sync failed — using last known offer book. {err}",
                    )
                else:
                    log_event(
                        "warning",
                        "wallet_sync_unavailable",
                        f"Wallet offer sync failed and no cached book is available. {err}",
                    )

            return (
                [dict(o) for o in self._wallet_sync_cache["buy"]],
                [dict(o) for o in self._wallet_sync_cache["sell"]],
                [dict(o) for o in self._wallet_sync_cache["closed"]],
            )

        open_buy, open_sell, closed = classify_offers_from_list(all_offers, cfg.CAT_ASSET_ID)

        previous_failures = int(self._wallet_sync_meta.get("consecutive_failures", 0) or 0)
        self._wallet_sync_cache["buy"] = [dict(o) for o in open_buy]
        self._wallet_sync_cache["sell"] = [dict(o) for o in open_sell]
        self._wallet_sync_cache["closed"] = [dict(o) for o in closed]
        self._wallet_sync_meta.update({
            "fresh": True,
            "using_cache": False,
            "consecutive_failures": 0,
            "last_error": "",
            "last_success_at": time.time(),
            "cache_size": len(open_buy) + len(open_sell),
        })

        if previous_failures > 0:
            log_event(
                "info",
                "wallet_sync_recovered",
                f"Wallet offer sync recovered after {previous_failures} failed poll(s)",
            )

        return open_buy, open_sell, closed

