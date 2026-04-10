"""
V2 Offer Manager — Offer Lifecycle Management

Handles the full lifecycle of market-making offers:
create → track → requote → expire → cancel

Extracted from V1's api_server.py (the biggest chunk of the monolith).

Usage:
    from offer_manager import OfferManager
    manager = OfferManager()
    manager.create_ladder(mid_price, "buy", num_offers=5)
    manager.check_requotes(current_price)
"""

import time
import threading
import traceback
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, List, Tuple, Callable, Any

from config import cfg
from database import (
    add_offer, update_offer_status,
    transition_offer,
    get_open_offers, get_offer, log_event, lock_coin
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
        # Track which offers the bot cancelled (vs externally filled)
        # This is critical for fill detection — see CHIA_DEV_GUIDE.md Section 5
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
        """Unsuspend slots for a side if spare coins have become available.

        Called by bot_loop at the start of each cycle to check whether
        previously exhausted coin pools have been replenished.
        """
        prefix = f"{side}_"
        suspended_for_side = [k for k in self._suspended_slots if k.startswith(prefix)]
        if not suspended_for_side:
            return

        # Check if any coins are now available
        wallet_id = cfg.CAT_WALLET_ID if side == "sell" else cfg.WALLET_ID_XCH
        try:
            coins_resp = get_exact_spendable_coins_rpc(wallet_id)
            if not coins_resp:
                return
            coins_list = (coins_resp.get("confirmed_records",
                          coins_resp.get("coin_records",
                          coins_resp.get("records", []))))
            spare_count = len(coins_list) if coins_list else 0
            if get_wallet_type() != "sage":
                try:
                    open_count = len(get_open_offers(side=side,
                                                     cat_asset_id=cfg.CAT_ASSET_ID))
                    spare_count = max(0, spare_count - open_count)
                except Exception:
                    pass
            if spare_count > 0:
                for key in suspended_for_side:
                    self._suspended_slots.discard(key)
                    self._slot_fail_counts.pop(key, None)
                log_event("info", "slots_unsuspended",
                          f"Unsuspended {len(suspended_for_side)} {side} slots — "
                          f"{spare_count} spare coins now available")
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
                                exclude_coin_ids: set = None) -> Optional[str]:
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

        Returns:
            coin_id string if a suitable coin is found, None otherwise.
            When None is returned, the caller should fall back to polling.
        """
        from coin_manager import _coin_id_from_record

        if used_coins is None:
            used_coins = set()

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
            if live_count >= len(slots):
                continue
            planned_slots.extend(slots[live_count:])
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
        briefly during heavy operations. See CHIA_DEV_GUIDE.md Section 10.

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
                              f"Coin selection returned None — falling back to polling mode")

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
                                        log_event("warning", "coin_snapshot_multi",
                                                  f"Expected 1 locked coin, found {len(missing)} missing")
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
                              f"MEMPOOL_CONFLICT: another tx spent one of the coins we tried to use. "
                              f"Re-snapshotting coins...")
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
                # Project the position INCREASE if all these offers fill
                projected_increase_xch = (default_size or Decimal("0")) * Decimal(num)
                # Only block if we're adding to the position in the wrong direction
                add_long_dir = (side == "buy" and net_pos_cat >= 0) or \
                               (side == "sell" and net_pos_cat <= 0)
                if (
                    add_long_dir
                    and net_pos_xch + projected_increase_xch > hard_pos_xch
                    and max_pos_xch > 0
                ):
                    log_event(
                        "error",
                        "position_hard_guard_blocked",
                        f"BLOCKED ladder creation: side={side}, num={num}, "
                        f"size={default_size}, current_position={net_pos_xch:.4f} XCH "
                        f"(net {net_pos_cat:+.0f} CAT), would add up to "
                        f"{projected_increase_xch:.4f} XCH worth → projected "
                        f"{(net_pos_xch + projected_increase_xch):.4f} XCH > "
                        f"hard limit {hard_pos_xch:.4f} XCH (110% of "
                        f"MAX_POSITION_XCH={max_pos_xch}). Allow position to "
                        f"unwind via the opposite side first.",
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

        default_size = trade_size_xch or cfg.DEFAULT_TRADE_XCH
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

        try:
            for open_offer in get_open_offers(side=side, cat_asset_id=asset_id):
                tier_name = open_offer.get("tier") or "mid"
                raw_size = open_offer.get("size_xch")
                if raw_size is None:
                    continue
                try:
                    size_key = self._size_key(Decimal(str(raw_size)))
                except Exception:
                    continue
                used_size_keys_by_tier.setdefault(tier_name, set()).add(size_key)
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
                log_event("warning", "ladder_interrupted",
                          f"Ladder creation interrupted by stop signal after "
                          f"{len(offer_specs)}/{num} {side} offers planned")
                break

            slot = slot_sequence[i] if slot_sequence is not None else (slot_start + i)

            # Fix F: skip suspended slots (coin exhaustion self-heal)
            if self.is_slot_suspended(side, slot):
                continue

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
                        continue  # Inside AMM arb band
                    if buffer_ok is None:
                        log_event("warning", "amm_buffer_unknown",
                                  f"Skipping {side} slot: AMM buffer data unavailable")
                        continue  # Fail closed — no data
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
                coin_id = self._select_coin_for_offer(
                    spec_spend_wallet_id or spend_wallet_id,
                    spend_amount,
                    used_coin_ids,
                    preferred_tier=coin_size_pref,
                    spendable_records=spendable_records,
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

        max_parallel = self._get_ladder_parallelism(coin_ids_enabled)
        _results_lock = _threading.Lock()
        _used_coins_lock = _threading.Lock()
        _results_map = {}  # {i: res}

        def _create_one(spec):
            """Create a single offer (runs in thread pool)."""
            if coin_ids_enabled and not spec.get("coin_id"):
                msg = (f"No unique pre-selected coin available for {side} "
                       f"slot {spec['slot']} — skipping to avoid overlap")
                log_event("warning", "coin_select_skip", msg)
                # Fix F: track consecutive failures for this slot
                self.record_slot_coin_failure(side, spec["slot"])
                return spec["i"], {"success": False, "error": "no_unique_coin_preselected"}

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
                print(f"  ❌ {fail_msg}", flush=True)
                log_event("error", "offer_create_failed", fail_msg)
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
                            "warning",
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

            # Prefer the first verified coin_id (covers multi-coin offers);
            # fall back to locked_coin_id if verification list is empty.
            db_coin_id = verified_locked_coin_ids[0] if verified_locked_coin_ids else locked_coin_id
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

    def requote_side(self, side: str, current_price: Decimal,
                     dexie_manager=None, risk_manager=None,
                     spread_fraction: Decimal = None,
                     price_cap: Decimal = None,
                     price_floor: Decimal = None,
                     live_offer_ids: set = None) -> List[Dict]:
        """Create-first requote — new offers go up BEFORE old ones come down.

        Strategy: use available spare coins to create new offers at the updated
        price, post them to Dexie immediately, THEN cancel old offers.  This
        keeps the orderbook continuously populated — there is never a gap where
        no offers exist.

        Flow per batch:
            1. Create new offers using spare/free coins
            2. Post new offers to Dexie immediately
            3. Cancel the same number of old offers (frees their coins for next batch)
            4. Brief wait for freed coins to become spendable
            5. Repeat until all old offers are replaced

        Requires spare coins to bootstrap the first batch. If no spare coins
        exist, falls back to cancel-first for just that batch.

        Note on coin IDs after cancel: Secure cancel (on-chain) DESTROYS the
        locked coins and creates NEW coins with different IDs. The recycled
        coins need time to confirm on-chain before they become spendable.

        Returns the full list of newly created offers.
        """
        with self._lock:
            self._last_requote_time[side] = time.time()

        # Get current open offers on this side FROM DATABASE
        # Exclude boost and sniper offers — they're managed separately
        all_open = get_open_offers(side=side, cat_asset_id=cfg.CAT_ASSET_ID)
        open_offers = [o for o in all_open if o.get("tier") not in ("boost", "sniper")]
        # Filter against live wallet snapshot to avoid trying to cancel offers
        # that already filled/expired this cycle (DB lags 1 cycle behind wallet).
        # Without this, cancel failures abort the requote prematurely.
        if live_offer_ids is not None:
            open_offers = [o for o in open_offers
                           if o.get("trade_id") in live_offer_ids]
        open_offers = self._sort_open_offers_for_requote(open_offers, side,
                                                          mid_price=current_price)
        total_to_replace = len(open_offers)

        log_event("info", "requote_start",
                  f"Create-first requote {side}: {total_to_replace} offers to replace "
                  f"in batches of {cfg.REQUOTE_BATCH_SIZE}, "
                  f"new price {current_price:.8f}")

        if not open_offers:
            log_event("info", "requote_no_cancel", f"No DB offers to cancel for {side}")
            fresh = self.create_ladder(current_price, side,
                                       risk_manager=risk_manager,
                                       spread_fraction=spread_fraction,
                                       coin_ids_enabled=cfg.COIN_IDS_ENABLED,
                                       price_cap=price_cap,
                                       price_floor=price_floor)
            # CRITICAL: queue these to Dexie/Splash. The batched paths below
            # do this themselves, but this early-return path was historically
            # missing the queue step — every cold-start cycle (no existing
            # offers to replace) created the ladder but never published it.
            # Symptom: bot_health_dexie_gap warning "wallet N/N vs Dexie 0/0".
            if dexie_manager and fresh:
                for offer in fresh:
                    bech32 = offer.get("offer_bech32", "")
                    trade_id = offer.get("trade_id", "")
                    if bech32 and trade_id:
                        dexie_manager.queue_post(bech32, trade_id)
                log_event("info", "requote_no_cancel_queued",
                          f"Queued {len(fresh)} fresh {side} offers to Dexie "
                          f"(cold-start path)")
            return {
                "offers": fresh,
                "fully_replaced": True,
                "replaced_count": len(fresh),
                "target_count": 0,
            }

        # Helper: count spare coins available right now for this side
        wallet_id = cfg.CAT_WALLET_ID if side == "sell" else cfg.WALLET_ID_XCH

        def _count_spare_coins() -> int:
            """Re-count spare coins available for create-first mode."""
            try:
                _resp = get_exact_spendable_coins_rpc(wallet_id)
                if not _resp:
                    return 0
                _coins = (_resp.get("confirmed_records",
                          _resp.get("coin_records",
                          _resp.get("records", []))))
                _count = len(_coins) if _coins else 0
                # On Chia wallet, spendable includes locked coins — subtract open offers.
                # On Sage, get_exact_spendable_coins_rpc uses filter_mode="selectable"
                # which already excludes locked coins — do NOT double-deduct.
                if get_wallet_type() != "sage":
                    try:
                        _open = len(get_open_offers(side=side,
                                                     cat_asset_id=cfg.CAT_ASSET_ID))
                        _count = max(0, _count - _open)
                    except Exception:
                        pass
                return _count
            except Exception:
                return 0

        spare_count = _count_spare_coins()

        log_event("info", "requote_spare_coins",
                  f"Spare {side} coins available: {spare_count} "
                  f"(need any >0 for rolling-wave create-first)")

        # Split into batches — rolling wave approach (Fix E).
        # Instead of requiring a full batch_size worth of spares, use
        # whatever spares are available to create a partial wave, then
        # cancel old offers to free coins for the next wave.
        batch_size = cfg.REQUOTE_BATCH_SIZE
        all_new_offers = []
        batches_done = 0
        offers_remaining = list(open_offers)  # mutable working copy
        initial_had_spares = spare_count > 0

        while offers_remaining:
            # Check stop signal between batches
            if self._stop_requested:
                log_event("warning", "requote_interrupted",
                          f"Requote interrupted by stop signal after "
                          f"{batches_done} batches ({len(all_new_offers)} new offers)")
                break

            batch_num = batches_done + 1
            total_batches_est = (len(offers_remaining) + batch_size - 1) // batch_size + batches_done

            # Re-count spare coins at the start of each wave (Fix E)
            # After a cancel, freed coins may now be available.
            if batches_done > 0:
                # Clear cycle-used coins so freed coins from cancels are eligible
                self._cycle_used_coin_ids.clear()
                spare_count = _count_spare_coins()

            # Determine this wave's size: use available spares (up to batch_size)
            # for create-first, or fall back to cancel-first if zero spares
            wave_size = min(batch_size, len(offers_remaining))
            can_create_first = spare_count > 0

            if can_create_first:
                # Rolling wave: create using whatever spares we have
                create_count = min(wave_size, spare_count)
                batch_offers = offers_remaining[:create_count]
                batch_count = len(batch_offers)
                batch_trade_ids = [o["trade_id"] for o in batch_offers]

                # ---- CREATE-FIRST: new offers go up before old ones come down ----

                # Guard: if repeated failed cancels left us over-allocated, stop
                # creating more offers until the count comes back down.
                max_side = (cfg.MAX_ACTIVE_BUY_OFFERS if side == "buy"
                            else cfg.MAX_ACTIVE_SELL_OFFERS)
                current_open = len(get_open_offers(side=side,
                                                   cat_asset_id=cfg.CAT_ASSET_ID))
                batch_limit = max_side + cfg.REQUOTE_BATCH_SIZE
                if current_open >= batch_limit:
                    log_event("warning", "requote_overalloc_guard",
                              f"Requote {side}: open={current_open} >= cap={batch_limit}, "
                              f"skipping new offer creation this batch to prevent "
                              f"over-allocation")
                    self.cancel_offers(batch_trade_ids, reason="requote_overalloc")
                    break

                # Step 1: Create new offers using available spare coins
                slot_start_idx = total_to_replace - len(offers_remaining)
                log_event("info", "requote_batch_create",
                          f"Rolling wave {side} batch {batch_num}: "
                          f"creating {batch_count} new offers using {spare_count} "
                          f"available spares (create-first)")
                new_offers = self.create_ladder(
                    current_price, side, num_offers=batch_count,
                    slot_start=slot_start_idx, total_slots=total_to_replace,
                    risk_manager=risk_manager,
                    spread_fraction=spread_fraction,
                    coin_ids_enabled=cfg.COIN_IDS_ENABLED,
                    price_cap=price_cap,
                    price_floor=price_floor,
                )

                # Atomicity guard: if we didn't create the full batch,
                # roll back partial offers and keep old ones live.
                if len(new_offers) < batch_count:
                    log_event("error", "requote_undercreated",
                              f"Rolling wave {side} batch {batch_num}: "
                              f"only created {len(new_offers)}/{batch_count} offers — "
                              f"rolling back partial creates, keeping old offers live")
                    if new_offers:
                        partial_ids = [o["trade_id"] for o in new_offers
                                       if o.get("trade_id")]
                        if partial_ids:
                            self.cancel_offers(partial_ids,
                                               reason="requote_rollback")
                    return {
                        "offers": all_new_offers,
                        "fully_replaced": False,
                        "replaced_count": len(all_new_offers),
                        "target_count": total_to_replace,
                    }

                all_new_offers.extend(new_offers)

                # Step 2: Post new offers to Dexie immediately
                if dexie_manager:
                    for offer in new_offers:
                        bech32 = offer.get("offer_bech32", "")
                        trade_id = offer.get("trade_id", "")
                        if bech32 and trade_id:
                            dexie_manager.queue_post(bech32, trade_id)

                # Step 3: NOW cancel the old offers (orderbook stays populated)
                log_event("info", "requote_batch_cancel",
                          f"Rolling wave {side} batch {batch_num}: "
                          f"cancelling {batch_count} old offers")
                cancel_results = self.cancel_offers(batch_trade_ids, reason="requote")

                cancel_ok = sum(1 for r in cancel_results.values()
                                if r and r.get("success"))
                cancel_failed = batch_count - cancel_ok

                if cancel_failed > 0:
                    log_event("warning", "requote_cancel_failed_continue",
                              f"Rolling wave {side} batch {batch_num}: "
                              f"{cancel_failed}/{batch_count} cancels failed — "
                              f"queued for background retry, continuing requote. "
                              f"Trim pass will correct any over-cap state.")

                log_event("info", "requote_batch_done",
                          f"Rolling wave {side} batch {batch_num}: "
                          f"created {len(new_offers)}, cancelled {batch_count}, "
                          f"{cancel_ok} coins freed (create-first)")

                # Remove processed offers from the working list
                offers_remaining = offers_remaining[batch_count:]

            else:
                # ---- CANCEL-FIRST FALLBACK: no spare coins available ----
                batch_offers = offers_remaining[:wave_size]
                batch_count = len(batch_offers)
                batch_trade_ids = [o["trade_id"] for o in batch_offers]
                slot_start_idx = total_to_replace - len(offers_remaining)

                log_event("info", "requote_batch_cancel",
                          f"Rolling wave {side} batch {batch_num}: "
                          f"cancelling {batch_count} offers (cancel-first — "
                          f"0 spare coins)")
                self.cancel_offers(batch_trade_ids, reason="requote")

                # Wait for wallet to free the coins from cancelled offers
                max_poll_secs = max(cfg.REQUOTE_COIN_FREE_WAIT, 15)
                poll_start = time.time()
                coins_ready = False
                for poll_i in range(max_poll_secs // 3):
                    time.sleep(3)
                    try:
                        coins_resp = get_exact_spendable_coins_rpc(wallet_id)
                        coins_list = (coins_resp.get("confirmed_records",
                                      coins_resp.get("coin_records",
                                      coins_resp.get("records", [])))
                                      if coins_resp else [])
                        if len(coins_list) > 0:
                            coins_ready = True
                            log_event("debug", "requote_coins_freed",
                                      f"Coins available after "
                                      f"{int(time.time() - poll_start)}s "
                                      f"({len(coins_list)} spendable)")
                            break
                    except Exception as e:
                        log_event("debug", "requote_coin_poll_failed",
                                  f"Requote coin-free poll failed: {e}")
                if not coins_ready:
                    log_event("warning", "requote_coins_slow",
                              f"Coins not yet freed after {max_poll_secs}s — "
                              f"creating offers anyway (may partially fail)")

                # Clear cycle-used coins so freed coins are eligible
                self._cycle_used_coin_ids.clear()

                # Create replacement offers
                new_offers = self.create_ladder(
                    current_price, side, num_offers=batch_count,
                    slot_start=slot_start_idx, total_slots=total_to_replace,
                    risk_manager=risk_manager,
                    spread_fraction=spread_fraction,
                    coin_ids_enabled=cfg.COIN_IDS_ENABLED,
                    price_cap=price_cap,
                    price_floor=price_floor,
                )
                all_new_offers.extend(new_offers)

                # Post to Dexie
                if dexie_manager:
                    for offer in new_offers:
                        bech32 = offer.get("offer_bech32", "")
                        trade_id = offer.get("trade_id", "")
                        if bech32 and trade_id:
                            dexie_manager.queue_post(bech32, trade_id)

                log_event("info", "requote_batch_done",
                          f"Rolling wave {side} batch {batch_num}: "
                          f"cancelled {batch_count}, created {len(new_offers)} "
                          f"(cancel-first fallback)")

                # Remove processed offers from the working list
                offers_remaining = offers_remaining[batch_count:]

            batches_done += 1

            # Brief wait for cancelled coins to recycle before next wave.
            if offers_remaining:
                time.sleep(3)

        mode = ("rolling-wave create-first" if initial_had_spares
                else "cancel-first → rolling-wave")
        log_event("info", "requote_done",
                  f"Requote {side} complete: "
                  f"replaced {total_to_replace} old → {len(all_new_offers)} new "
                  f"in {batches_done} batches ({mode})")
        return {
            "offers": all_new_offers,
            "fully_replaced": len(all_new_offers) >= total_to_replace,
            "replaced_count": len(all_new_offers),
            "target_count": total_to_replace,
        }

    # -------------------------------------------------------------------
    # Cancellation
    # -------------------------------------------------------------------

    def cancel_offers(self, trade_ids: List[str], reason: str = "manual",
                      force_storm: bool = False) -> Dict:
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

        # Sequential cancel (NEVER parallel — breaks the Chia wallet)
        results = cancel_offers_batch(trade_ids, secure=True)

        # Log results summary
        successes = sum(1 for r in results.values() if r and r.get("success"))
        failures = len(results) - successes
        log_event("info", "cancel_result",
                  f"Cancel results: {successes} succeeded, {failures} failed "
                  f"(reason: {reason})")

        # Update database status + coin tracking
        for tid, result in results.items():
            if result and result.get("success"):
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
        for tid in trade_ids:
            self._bot_cancelled_ids.add(tid)

        # Cancel in controlled batches — tuned for live Sage cancel testing.
        # The batch call already performs on-chain confirmation, so only keep
        # a short breathing gap between batches.
        BATCH_SIZE = 25
        BASE_BATCH_DELAY = 5.0
        total = len(trade_ids)
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        all_results = {}

        log_event("info", "cancel_all_batched",
                  f"Cancelling {total} offers in batches of {BATCH_SIZE}")
        emit_progress(
            running=True,
            complete=False,
            phase="starting",
            total=total,
            batch_size=BATCH_SIZE,
            total_batches=total_batches,
            current_batch=0,
            cancelled=0,
            failed=0,
            message=f"Starting cancel all for {total} offers in batches of {BATCH_SIZE}.",
        )

        for i in range(0, total, BATCH_SIZE):
            batch = trade_ids[i:i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            next_batch_delay = BASE_BATCH_DELAY

            try:
                batch_results = cancel_offers_batch(batch, secure=True)
                all_results.update(batch_results)
                apply_batch_results(batch_results)

                # F50 (2026-04-09): distinguish on-chain-confirmed cancels
                # from still-in-flight cancels (cancel TX submitted to
                # mempool but not yet confirmed). The old code counted
                # both as "cancelled", which was misleading when the
                # mempool was congested and half the cancels were
                # actually pending or outright failing due to conflicts.
                #
                # Confirmed methods mean the offer is definitively gone
                # from Sage's active list. Pending methods mean the
                # cancel TX exists but the offer is still ACTIVE in
                # Sage's status until the block confirms. Failed means
                # the cancel didn't even reach the mempool or was
                # rejected.
                CONFIRMED_METHODS = {
                    "confirmed_by_status",
                    "confirmed_by_unlock",
                    "bulk",  # bulk-cancel RPC reports its own confirmation
                }
                PENDING_METHODS = {
                    "submitted_pending_confirm",
                }

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
                    return "confirmed"  # unknown success method — assume confirmed

                batch_confirmed = sum(1 for r in batch_results.values() if _classify(r) == "confirmed")
                batch_pending   = sum(1 for r in batch_results.values() if _classify(r) == "pending")
                batch_failures  = sum(1 for r in batch_results.values() if _classify(r) == "failed")
                # Legacy "batch_successes" for UI backwards compat = confirmed + pending
                batch_successes = batch_confirmed + batch_pending

                running_confirmed = sum(1 for r in all_results.values() if _classify(r) == "confirmed")
                running_pending   = sum(1 for r in all_results.values() if _classify(r) == "pending")
                running_failures  = sum(1 for r in all_results.values() if _classify(r) == "failed")
                running_successes = running_confirmed + running_pending

                if batch_pending > 0:
                    log_event("info", "cancel_all_batch",
                              f"Batch {batch_num}/{total_batches}: "
                              f"{batch_confirmed}/{len(batch)} cancels confirmed on-chain, "
                              f"{batch_pending} pending (cancel TX in mempool), "
                              f"{batch_failures} failed")
                else:
                    log_event("info", "cancel_all_batch",
                              f"Batch {batch_num}/{total_batches}: "
                              f"{batch_confirmed}/{len(batch)} cancels confirmed on-chain "
                              f"({batch_failures} failed)")
                if batch_failures > 0 or batch_pending > 0:
                    next_batch_delay = 10.0
                emit_progress(
                    running=True,
                    complete=False,
                    phase="running",
                    total=total,
                    batch_size=BATCH_SIZE,
                    total_batches=total_batches,
                    current_batch=batch_num,
                    batch_cancelled=batch_successes,
                    batch_confirmed=batch_confirmed,
                    batch_pending=batch_pending,
                    batch_failed=batch_failures,
                    cancelled=running_successes,
                    confirmed=running_confirmed,
                    pending=running_pending,
                    failed=running_failures,
                    message=(
                        f"Batch {batch_num}/{total_batches}: "
                        f"{batch_confirmed} confirmed"
                        + (f", {batch_pending} pending" if batch_pending else "")
                        + (f", {batch_failures} failed" if batch_failures else "")
                    ),
                )
            except Exception as e:
                log_event("warning", "cancel_all_batch_error",
                          f"Batch {batch_num} error: {e}")
                for tid in batch:
                    all_results[tid] = {"success": False, "error": str(e)}
                apply_batch_results({tid: all_results[tid] for tid in batch})
                running_successes = sum(1 for r in all_results.values()
                                        if r and r.get("success"))
                running_failures = len(all_results) - running_successes
                emit_progress(
                    running=True,
                    complete=False,
                    phase="running",
                    total=total,
                    batch_size=BATCH_SIZE,
                    total_batches=total_batches,
                    current_batch=batch_num,
                    batch_cancelled=0,
                    batch_failed=len(batch),
                    cancelled=running_successes,
                    failed=running_failures,
                    message=f"Batch {batch_num}/{total_batches} failed: {e}",
                )

            # Wait between batches (except after the last one)
            if i + BATCH_SIZE < total:
                emit_progress(
                    running=True,
                    complete=False,
                    phase="waiting",
                    total=total,
                    batch_size=BATCH_SIZE,
                    total_batches=total_batches,
                    current_batch=batch_num,
                    cancelled=sum(1 for r in all_results.values()
                                  if r and r.get("success")),
                    failed=sum(1 for r in all_results.values()
                               if not (r and r.get("success"))),
                    message=(
                        f"Batch {batch_num}/{total_batches} sent. "
                        f"Waiting {int(next_batch_delay)}s before the next batch."
                    ),
                )
                time.sleep(next_batch_delay)

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
            "bulk",
        }
        PENDING_METHODS = {
            "submitted_pending_confirm",
        }

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
            batch_size=BATCH_SIZE,
            total_batches=total_batches,
            current_batch=total_batches,
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
        We must check valid_times.max_time manually.
        See CHIA_DEV_GUIDE.md Section 4.
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

    def trim_excess_offers(self, mid_price: Decimal) -> int:
        """Cancel any offers above the configured per-side cap.

        Belt-and-braces guard against the requote overshoot the bot got
        into on 2026-04-07: when cancels were slow to confirm, repeated
        create-first requote rounds left the live book at 29 sells against
        a 24 cap. The over-allocation guard only blocked NEW creation; it
        never trimmed the excess. This method does the trim.

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

        for side, cap in (("buy", max_buy), ("sell", max_sell)):
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

            excess = len(open_offers) - cap
            if excess <= 0:
                continue

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

            log_event("warning", "trim_excess_offers",
                      f"Trim pass: {side} open={len(open_offers)} > cap={cap}, "
                      f"cancelling {len(cancel_ids)} furthest-from-mid offer(s)")

            try:
                self.cancel_offers(cancel_ids, reason="trim_excess")
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
                log_event("info", "cancel_retry_success",
                          f"Cancel retry succeeded for {trade_id[:16]}... "
                          f"(attempt {info['attempts']})")
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

