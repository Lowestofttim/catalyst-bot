"""Fill detection and round-trip PnL matching via before/after trade-ID diffs

The `FillTracker` class detects offer fills by diffing the set of trade IDs
present on the wallet between polls. Disappearances that were not caused by
bot-initiated cancels (tracked via `OfferManager._bot_cancelled_ids`) are
treated as candidate fills, then verified on-chain through a Sage -> Dexie ->
Spacescan fallback chain before being recorded.

Key responsibilities:
    - Diff trade-ID snapshots each cycle to find candidate fills
    - Verify fills on-chain via the Sage/Dexie/Spacescan fallback chain
    - Record verified fills and drive `OfferState` transitions
    - Match buy<->sell round-trips for PnL via a 4-pass algorithm

A 3-strike mass-disappearance guard absorbs transient wallet-RPC blips where
large swaths of trades vanish and reappear; the strike counter resets on any
good poll so isolated anomalies don't accumulate.
"""

import time
import datetime as _dt
from decimal import Decimal
from typing import Dict, List, Set, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import cfg
from database import (
    record_fill, get_unmatched_fills, match_round_trip,
    log_event, transition_offer
)


class FillTracker:
    """Detects fills and matches round-trip PnL.

    The core insight: Chia offers don't have a "filled" callback.
    We detect fills by comparing which offer IDs existed last loop
    vs this loop. If an ID disappeared and WE didn't cancel it,
    it was filled by someone.

    The mass disappearance guard handles RPC blips where the wallet
    temporarily returns partial results (making it look like many
    offers filled at once).
    """

    def __init__(self, offer_manager=None):
        # Reference to offer_manager for bot-cancelled checking
        self._offer_manager = offer_manager

        # Previous loop's offer IDs (the "before" snapshot)
        self._previous_ids: Dict[str, Set[str]] = {"buy": set(), "sell": set()}

        # _known_ids REMOVED — was populated but never read for any decision.
        # Baseline-reset approach handles restart detection instead.

        # Mass disappearance guard counter + timeout
        self._mass_disappearance_count: int = 0
        self._mass_disappearance_first_at: float = 0  # timestamp of first trigger

        # Fill timestamps per side (for fill protection cooldown)
        self._last_fill_time: Dict[str, float] = {"buy": 0, "sell": 0}

        # Fill counts from last detection (for GUI display)
        self._last_fill_count: Dict[str, int] = {"buy": 0, "sell": 0}

        # Recent fill history (capped list for GUI display)
        self._fill_history: List[Dict] = []
        self._max_history: int = 50

        # Dexie detail cache: populated by _check_dexie_offer_still_open(),
        # consumed by _record_fill(). Avoids making a second HTTP call for
        # the same offer just to pass detail to the fill classifier.
        self._last_dexie_details: Dict[str, Optional[Dict]] = {}

        # Unverified-fill retry bookkeeping. A disappeared offer whose on-chain
        # verification is "unverified" (Spacescan unreachable, coin data not
        # yet indexed, etc.) used to be retired immediately as cancelled,
        # which permanently erased any real fill that landed during the
        # Spacescan outage. Instead, we now park the trade_id here, retry
        # verification on subsequent cycles, and only retire the offer once
        # we either get a decisive verdict or exhaust the retry budget.
        # Map: trade_id → {"side": str, "attempts": int, "first_seen": float}.
        self._pending_reverify: Dict[str, Dict] = {}
        self._pending_reverify_max_attempts: int = 6  # ~6 cycles before giving up

    # -------------------------------------------------------------------
    # Core fill detection
    # -------------------------------------------------------------------

    def detect_fills(self, current_buy_ids: Set[str], current_sell_ids: Set[str],
                     offer_details_cache: Dict[str, Dict] = None) -> Dict[str, List[Dict]]:
        """Compare current offer IDs against previous loop to detect fills.

        Args:
            current_buy_ids: Set of trade_ids currently open on buy side
            current_sell_ids: Set of trade_ids currently open on sell side
            offer_details_cache: Optional dict of {trade_id: details} for enriching fill records

        Returns dict with 'buy_fills' and 'sell_fills' lists.
        """
        result = {"buy_fills": [], "sell_fills": []}

        # First loop — just establish baseline
        if not self._previous_ids["buy"] and not self._previous_ids["sell"]:
            self._previous_ids["buy"] = current_buy_ids.copy()
            self._previous_ids["sell"] = current_sell_ids.copy()
            log_event("info", "fill_tracker_init",
                      f"Baseline set: {len(current_buy_ids)} buys, {len(current_sell_ids)} sells")
            return result

        # Retry any previously-parked unverified offers before processing new
        # disappearances. A delayed Spacescan success here will be surfaced as
        # a fill on the current cycle exactly like a just-disappeared offer.
        if self._pending_reverify:
            retry_fills = self._retry_pending_reverify(offer_details_cache or {})
            if retry_fills.get("buy_fills"):
                result["buy_fills"].extend(retry_fills["buy_fills"])
            if retry_fills.get("sell_fills"):
                result["sell_fills"].extend(retry_fills["sell_fills"])

        # Calculate disappeared offers
        disappeared_buy = self._previous_ids["buy"] - current_buy_ids
        disappeared_sell = self._previous_ids["sell"] - current_sell_ids

        total_disappeared = len(disappeared_buy) + len(disappeared_sell)
        total_previous = len(self._previous_ids["buy"]) + len(self._previous_ids["sell"])

        # Mass disappearance guard
        if total_previous > 0 and total_disappeared > 0:
            if not self._check_mass_disappearance(total_disappeared, total_previous):
                # Guard triggered — don't update baseline, wait for next loop
                return result

        # Process disappeared offers
        buy_fills = self._process_disappeared(
            disappeared_buy, "buy", offer_details_cache or {}
        )
        sell_fills = self._process_disappeared(
            disappeared_sell, "sell", offer_details_cache or {}
        )

        result["buy_fills"] = buy_fills
        result["sell_fills"] = sell_fills

        # Update fill timestamps and counts
        if buy_fills:
            self._last_fill_time["buy"] = time.time()
            self._last_fill_count["buy"] = len(buy_fills)
        else:
            self._last_fill_count["buy"] = 0

        if sell_fills:
            self._last_fill_time["sell"] = time.time()
            self._last_fill_count["sell"] = len(sell_fills)
        else:
            self._last_fill_count["sell"] = 0

        # Update baseline for next loop
        self._previous_ids["buy"] = current_buy_ids.copy()
        self._previous_ids["sell"] = current_sell_ids.copy()

        return result

    def _check_mass_disappearance(self, disappeared: int, previous: int) -> bool:
        """Mass disappearance guard — returns True if safe to process.

        If >50% of offers vanish at once, it's probably an RPC blip,
        not genuine fills. We require 3 consecutive detections before
        accepting as real.

        Returns False (unsafe) if guard triggered and we should skip.
        """
        if previous <= 0:
            return True

        # If wallet offer sync is degraded and we're using a cached view, do not
        # allow the 3-strike disappearance guard to "confirm" a mass vanish.
        # Repeated stale polls are not evidence that live offers were truly
        # taken/cancelled; they are evidence that Sage is not giving us a fresh
        # open-book view right now.
        if self._offer_manager and hasattr(self._offer_manager, "get_wallet_sync_meta"):
            try:
                sync_meta = self._offer_manager.get_wallet_sync_meta() or {}
            except Exception:
                sync_meta = {}
            if sync_meta and not sync_meta.get("fresh", True):
                self._mass_disappearance_count = 0
                log_event(
                    "warning",
                    "mass_disappearance_blocked",
                    f"Blocked mass disappearance while wallet sync stale: "
                    f"{disappeared}/{previous} offers hidden "
                    f"(using_cache={bool(sync_meta.get('using_cache'))})",
                )
                return False

        ratio = disappeared / previous

        if ratio > 0.5 and disappeared > 1:
            now = time.time()
            if self._mass_disappearance_count == 0:
                self._mass_disappearance_first_at = now
            self._mass_disappearance_count += 1

            # Timeout: if the guard has been triggered for >10 minutes
            # without clearing, accept the disappearance regardless.
            # This prevents indefinite suppression of real fills when
            # the ratio hovers near 50% across multiple cycles.
            guard_age_secs = now - self._mass_disappearance_first_at
            guard_timeout_secs = 600  # 10 minutes

            if self._mass_disappearance_count >= 3:
                # 3 strikes — accept as genuine, reset counter
                log_event("warning", "mass_disappearance_accepted",
                          f"Mass disappearance confirmed after 3 checks: "
                          f"{disappeared}/{previous} offers gone")
                self._mass_disappearance_count = 0
                self._mass_disappearance_first_at = 0
                return True
            elif guard_age_secs > guard_timeout_secs:
                # Timeout — accept despite not reaching 3 consecutive strikes
                log_event("warning", "mass_disappearance_accepted",
                          f"Mass disappearance accepted after {guard_age_secs:.0f}s timeout: "
                          f"{disappeared}/{previous} offers gone "
                          f"(guard count was {self._mass_disappearance_count}/3)")
                self._mass_disappearance_count = 0
                self._mass_disappearance_first_at = 0
                return True
            else:
                # Guard triggered — skip this loop, DON'T update baseline
                log_event("warning", "mass_disappearance_guard",
                          f"Guard triggered ({self._mass_disappearance_count}/3, "
                          f"age {guard_age_secs:.0f}s): "
                          f"{disappeared}/{previous} offers disappeared")
                return False
        else:
            # Normal disappearance — reset counter
            self._mass_disappearance_count = 0
            self._mass_disappearance_first_at = 0
            return True

    def _check_wallet_status_batch(self, trade_ids):
        """Batch wallet status check for disappeared offers using parallel RPC calls.

        Returns dict: {trade_id: (still_exists: bool, closed_nonfill: bool, status_norm: str)}
        Runs individual get_offer RPCs concurrently to reduce total wall-clock time.
        """
        results = {}
        if not trade_ids:
            return results

        try:
            from wallet import get_wallet_type
            if get_wallet_type() != "sage":
                return results  # Only needed for Sage wallet
            from wallet_sage import rpc as _sage_rpc
        except Exception:
            return results

        def _check_one(trade_id):
            try:
                _check = _sage_rpc("get_offer", {"offer_id": trade_id}, timeout=5)
                if _check and isinstance(_check, dict):
                    _status = _check.get("status", "")
                    _status_norm = str(_status).upper()
                    if _status in (0, 1) or _status_norm in (
                        "ACTIVE", "OPEN", "PENDING_ACCEPT", "PENDING_CONFIRM",
                        "PENDING", "IN_PROGRESS"
                    ):
                        return (trade_id, True, False, _status_norm)
                    elif _status in (2, 3, 5) or _status_norm in (
                        "PENDING_CANCEL", "CANCELLED", "CANCELED",
                        "FAILED", "EXPIRED"
                    ):
                        return (trade_id, False, True, _status_norm)
            except Exception as _e:
                log_event("debug", "wallet_batch_check_error",
                          f"Wallet status check failed for {trade_id[:16]}...: {_e}")
            return (trade_id, False, False, "")

        max_workers = min(len(trade_ids), 8)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_check_one, tid): tid for tid in trade_ids}
            for future in as_completed(futures, timeout=30):
                try:
                    tid, still_exists, closed_nonfill, status_norm = future.result()
                    results[tid] = (still_exists, closed_nonfill, status_norm)
                except Exception:
                    tid = futures[future]
                    results[tid] = (False, False, "")
        return results

    def _retry_pending_reverify(self, details_cache: Dict[str, Dict]) -> Dict[str, List[Dict]]:
        """Re-run Spacescan verification for offers parked as 'unverified'.

        Each entry in ``_pending_reverify`` was a disappeared offer whose
        Spacescan verdict was inconclusive. Here we retry:
          - "filled" → record fill, clear entry.
          - "rejected" → retire (expired if local clock says so, else
            cancelled), clear entry.
          - "unverified" → increment attempts; if the budget is exhausted,
            retire conservatively and alert the operator with enough info
            to resolve manually.
        """
        out = {"buy_fills": [], "sell_fills": []}
        if not self._pending_reverify:
            return out

        for trade_id, meta in list(self._pending_reverify.items()):
            side = str(meta.get("side") or "")
            try:
                verdict = self._verify_fill_on_chain(trade_id, side)
            except Exception as _vre:
                log_event("debug", "fill_reverify_error",
                          f"Retry verify raised for {trade_id[:16]}...: {_vre}")
                verdict = "unverified"

            if verdict == "filled":
                try:
                    transition_offer(trade_id, "fill_verified")
                except Exception:
                    pass
                fill_detail = self._record_fill(trade_id, side, details_cache)
                if fill_detail:
                    key = "buy_fills" if side == "buy" else "sell_fills"
                    out[key].append(fill_detail)
                self._pending_reverify.pop(trade_id, None)
                log_event("warning", "fill_recovered_late",
                          f"{side.upper()} offer {trade_id[:16]}... recovered "
                          f"as fill after Spacescan retry (attempts="
                          f"{meta.get('attempts', 0)})",
                          data={"trade_id": trade_id, "side": side})
            elif verdict == "rejected":
                self._last_dexie_details.pop(trade_id, None)
                status = "expired" if meta.get("local_clock_expired") else "cancelled"
                self._retire_local_offer(
                    trade_id,
                    side,
                    details_cache,
                    status=status,
                    event_type="offer_closed_nonfill",
                    severity="info",
                    suffix=("expired on-chain" if status == "expired"
                            else "retired after Spacescan rejection"),
                    data_extra={"verification_state": "rejected"},
                )
                self._pending_reverify.pop(trade_id, None)
            else:
                attempts = int(meta.get("attempts", 0)) + 1
                meta["attempts"] = attempts
                if attempts >= self._pending_reverify_max_attempts:
                    # Spacescan budget exhausted. Before defaulting to a
                    # conservative "cancelled" (which silently loses a real
                    # fill if Spacescan was merely rate-limited), ask Dexie
                    # for the on-chain terminal status. Dexie indexes the
                    # same chain and distinguishes status=3 (cancel) from
                    # status=4 (fill).
                    dexie_terminal = self._dexie_terminal_status(trade_id)

                    if dexie_terminal == "filled":
                        try:
                            transition_offer(trade_id, "fill_verified")
                        except Exception:
                            pass
                        fill_detail = self._record_fill(trade_id, side, details_cache)
                        if fill_detail:
                            key = "buy_fills" if side == "buy" else "sell_fills"
                            out[key].append(fill_detail)
                        log_event("warning", "fill_recovered_via_dexie",
                                  f"{side.upper()} offer {trade_id[:16]}... "
                                  f"Spacescan exhausted after {attempts} retries "
                                  f"but Dexie reports status=4 (COMPLETED) — "
                                  f"recorded as fill.",
                                  data={"trade_id": trade_id, "side": side,
                                        "attempts": attempts,
                                        "source": "dexie_fallback"})
                        self._pending_reverify.pop(trade_id, None)
                        continue

                    if dexie_terminal == "cancelled":
                        status = "expired" if meta.get("local_clock_expired") else "cancelled"
                        self._retire_local_offer(
                            trade_id, side, details_cache,
                            status=status,
                            event_type="offer_closed_nonfill",
                            severity="info",
                            suffix=("expired on-chain" if status == "expired"
                                    else "retired after Dexie confirmed cancel "
                                         "(Spacescan exhausted)"),
                            data_extra={"verification_state": "dexie_confirmed_cancelled",
                                        "attempts": attempts,
                                        "source": "dexie_fallback"},
                        )
                        self._pending_reverify.pop(trade_id, None)
                        continue

                    # Dexie also inconclusive (404, still-open, pending, rate-limited,
                    # or network error). Retire conservatively and alert operator.
                    status = "expired" if meta.get("local_clock_expired") else "cancelled"
                    self._retire_local_offer(
                        trade_id,
                        side,
                        details_cache,
                        status=status,
                        event_type="offer_closed_unverified",
                        severity="error",
                        suffix=("exhausted Spacescan retries — expired on local clock"
                                if status == "expired" else
                                "exhausted Spacescan retries — manual review"),
                        data_extra={"verification_state": "exhausted",
                                    "attempts": attempts},
                    )
                    log_event("error", "fill_verify_exhausted",
                              f"{side.upper()} offer {trade_id[:16]}... failed "
                              f"to verify after {attempts} Spacescan retries "
                              f"AND Dexie fallback was inconclusive — "
                              f"retired as {status}. MANUAL REVIEW RECOMMENDED.",
                              data={"trade_id": trade_id, "side": side,
                                    "attempts": attempts,
                                    "final_status": status})
                    self._pending_reverify.pop(trade_id, None)
        return out

    def _process_disappeared(self, disappeared_ids: Set[str], side: str,
                             details_cache: Dict[str, Dict]) -> List[Dict]:
        """Process disappeared offers — classify as filled, cancelled, or expired.

        Returns list of confirmed fills (not cancellations or expirations).
        """
        fills = []

        # Batch-prefetch DB records for all disappeared offers
        _db_records: Dict[str, dict] = {}
        if disappeared_ids:
            try:
                from database import get_offers_by_trade_ids as _get_batch
                _batch = _get_batch(list(disappeared_ids))
                if _batch:
                    _db_records = {r["trade_id"]: r for r in _batch if r.get("trade_id")}
            except ImportError:
                # get_offers_by_trade_ids not available — fall back to per-offer lookup
                pass
            except Exception as _batch_err:
                log_event("warning", "fill_tracker_batch_prefetch_failed",
                          f"Batch offer prefetch failed ({_batch_err}) — using per-offer lookup")

        # Batch wallet status check (parallel RPC) — avoids N×5s serial calls
        _wallet_status_cache = {}
        try:
            _wallet_status_cache = self._check_wallet_status_batch(disappeared_ids)
        except Exception as _ws_err:
            log_event("debug", "fill_tracker_wallet_batch_failed",
                      f"Wallet status batch check failed ({_ws_err}) — falling back to serial")

        # Parallel Spacescan pre-verification — avoids N×20s serial latency.
        # For a 14-fill sweep this compresses ~4min of HTTP waits to ~30s,
        # which is the main driver of offer-replacement latency after a sweep.
        #
        # We skip obvious non-fills (bot-cancelled, still open in wallet,
        # or closed as non-fill per wallet RPC) so we don't waste calls on
        # trades that the main loop will early-continue anyway. Everything
        # else is verified in parallel up-front and cached; the main loop
        # pulls from the cache instead of calling _verify_fill_on_chain
        # serially. Behaviour with an empty cache is identical to before —
        # this is strictly a latency optimisation, not a correctness change.
        _verify_cache: Dict[str, str] = {}
        _verify_candidates: List[str] = []
        for _pv_tid in disappeared_ids:
            # Do NOT skip bot-cancelled offers here. The in-memory flag is set
            # BEFORE the cancel RPC lands, so a fill that races the cancel
            # request would be silently discarded if we early-exit. Let the
            # normal flow verify on-chain — when Sage/wallet confirms non-fill
            # we short-circuit below, and when Spacescan confirms fill we
            # record it correctly, overriding the cancel assumption.
            if _pv_tid in _wallet_status_cache:
                _pv_still, _pv_closed, _ = _wallet_status_cache[_pv_tid]
                if _pv_still or _pv_closed:
                    continue
            _verify_candidates.append(_pv_tid)

        if len(_verify_candidates) > 1:
            def _verify_one_parallel(_tid: str):
                try:
                    return _tid, self._verify_fill_on_chain(_tid, side)
                except Exception as _ve:
                    log_event("debug", "fill_verify_parallel_error",
                              f"Parallel verify of {_tid[:16]}... failed: {_ve}")
                    return _tid, None  # None sentinel → main loop falls back to serial call

            _max_workers = min(len(_verify_candidates), 6)
            try:
                with ThreadPoolExecutor(max_workers=_max_workers,
                                        thread_name_prefix="fill-verify") as _pool:
                    _futures = {_pool.submit(_verify_one_parallel, _c): _c
                                for _c in _verify_candidates}
                    # Outer cap: 180s total so a pathologically-hung Spacescan
                    # call can't stall fill detection indefinitely. Individual
                    # calls already have their own 20s timeouts; this is a
                    # belt-and-braces cap for the whole batch.
                    for _f in as_completed(_futures, timeout=180):
                        try:
                            _tid, _result = _f.result()
                            if _result is not None:
                                _verify_cache[_tid] = _result
                        except Exception:
                            pass
                log_event("info", "fill_verify_parallel_done",
                          f"Parallel pre-verify: {len(_verify_cache)}/"
                          f"{len(_verify_candidates)} resolved "
                          f"({_max_workers} workers)")
            except Exception as _pv_err:
                log_event("warning", "fill_verify_parallel_failed",
                          f"Parallel pre-verify batch failed ({_pv_err}) — "
                          f"falling back to serial verification in main loop")

        for trade_id in disappeared_ids:
            # Do NOT early-exit on bot-cancelled here. The _bot_cancelled_ids
            # flag is set BEFORE the cancel RPC lands (offer_manager._cancel
            # path), so a fill that beats the cancel is silently lost if we
            # trust the flag alone. Instead, let wallet status + Spacescan
            # verification below decide fill vs. cancel. For offers that
            # really did cancel cleanly, Sage returns a CANCELLED/EXPIRED
            # wallet status and the offer_closed_nonfill branch retires
            # them without a Spacescan round-trip.
            was_cancelled = False
            if self._offer_manager:
                was_cancelled = self._offer_manager.is_bot_cancelled(trade_id)

            # Determine if the offer's max_time has passed according to our
            # DB. We DO NOT retire as expired on this signal alone — a real
            # fill can land in the same second that expiry elapses, and
            # shortcircuiting here silently discards the fill. The flag is
            # used below, AFTER wallet status and Spacescan verification,
            # to choose the correct terminal state for a non-fill.
            local_clock_expired = False
            try:
                _db_rec = _db_records.get(trade_id)
                if _db_rec is None:
                    # Fallback: individual lookup (e.g., very new offer not in batch)
                    try:
                        from database import get_offer as _get_db_offer
                        _db_rec = _get_db_offer(trade_id)
                    except Exception:
                        pass
                if _db_rec:
                    _expires = _db_rec.get("expires_at")
                    if _expires:
                        import datetime as _dt
                        # Handle both "+00:00" and "Z" suffix formats
                        _exp_str = _expires.replace("Z", "+00:00")
                        _exp_ts = _dt.datetime.fromisoformat(_exp_str).timestamp()
                        local_clock_expired = time.time() > _exp_ts
            except Exception as _expiry_err:
                log_event("debug", "expiry_check_error",
                          f"Expiry check failed for {trade_id[:16]}...: {_expiry_err}")
                # Proceed with other verification gates

            # ---- V5 FIX: Wallet-level verification gate ----
            # Before Spacescan, check if the wallet actually confirms the offer
            # is gone. This catches phantom fills caused by DB reconciliation
            # inconsistencies — the offer might still be open in the wallet
            # even if the bot's view lost track of it temporarily.
            offer_still_exists = False
            offer_closed_nonfill = False
            wallet_offer_status = ""
            try:
                if trade_id in _wallet_status_cache:
                    # Use pre-fetched result from parallel batch check
                    offer_still_exists, offer_closed_nonfill, wallet_offer_status = \
                        _wallet_status_cache[trade_id]
                else:
                    # Fallback: serial RPC (e.g., batch check failed or non-Sage wallet)
                    from wallet import get_wallet_type
                    if get_wallet_type() == "sage":
                        from wallet_sage import rpc as _sage_rpc
                        _check = _sage_rpc("get_offer", {"offer_id": trade_id}, timeout=5)
                        if _check and isinstance(_check, dict):
                            _status = _check.get("status", "")
                            _status_norm = str(_status).upper()
                            wallet_offer_status = _status_norm
                            if _status in (0, 1) or _status_norm in (
                                "ACTIVE", "OPEN", "PENDING_ACCEPT", "PENDING_CONFIRM",
                                "PENDING", "IN_PROGRESS"
                            ):
                                offer_still_exists = True
                            elif _status in (2, 3, 5) or _status_norm in (
                                "PENDING_CANCEL", "CANCELLED", "CANCELED",
                                "FAILED", "EXPIRED"
                            ):
                                offer_closed_nonfill = True
            except Exception as _wallet_err:
                log_event("debug", "wallet_serial_check_error",
                          f"Serial wallet check failed for {trade_id[:16]}...: {_wallet_err}")
                # Proceed with normal verification

            if offer_still_exists:
                log_event("info", "fill_wallet_still_open",
                          f"Offer {trade_id[:16]}... still OPEN in wallet — "
                          f"NOT a fill (DB inconsistency)")
                continue
            if offer_closed_nonfill:
                local_status = "expired" if wallet_offer_status == "EXPIRED" else "cancelled"
                self._retire_local_offer(
                    trade_id,
                    side,
                    details_cache,
                    status=local_status,
                    event_type="offer_closed_nonfill",
                    severity="info",
                    suffix=("expired in wallet" if local_status == "expired"
                            else "closed in wallet"),
                    data_extra={"wallet_status": wallet_offer_status or "UNKNOWN"},
                )
                log_event("info", "fill_wallet_closed_nonfill",
                          f"Offer {trade_id[:16]}... is CLOSED in wallet with "
                          f"non-fill status — NOT a fill")
                continue

            # ---- Spacescan Verification Gate (Golden Source of Truth) ----
            # Before recording ANY fill, verify the coin was actually spent
            # on-chain to an external address. This prevents ALL phantom fills.

            # Lifecycle: MEMPOOL_SEEN signal → mempool_observed intermediate state.
            # Offer has left the wallet — awaiting on-chain confirmation.
            try:
                transition_offer(trade_id, "mempool_seen")
            except Exception:
                pass  # additive — never block fill detection

            # Use cached result from parallel pre-verify when available,
            # otherwise fall back to serial call (e.g. single-fill case,
            # or if parallel batch failed).
            verification = _verify_cache.get(trade_id)
            if verification is None:
                verification = self._verify_fill_on_chain(trade_id, side)
            if verification == "filled":
                # Lifecycle: FILL_VERIFIED signal advances mempool_observed → filled.
                # _record_fill also calls update_offer_status("filled") which sets
                # the coarse status column via database migration.
                try:
                    transition_offer(trade_id, "fill_verified")
                except Exception:
                    pass
                if was_cancelled:
                    # Cancel/fill race: the bot issued a cancel but the
                    # counterparty took the offer first. Spacescan confirmed
                    # the on-chain spend as a fill, so we record it and the
                    # local cancel assumption is overridden.
                    log_event("warning", "fill_beat_cancel",
                              f"{side.upper()} offer {trade_id[:16]}... was marked "
                              f"bot-cancelled but Spacescan confirms a fill — "
                              f"recording fill and overriding local cancel state.",
                              data={"trade_id": trade_id, "side": side})
                fill_detail = self._record_fill(trade_id, side, details_cache)
                if fill_detail:
                    fills.append(fill_detail)
            elif verification == "rejected":
                # Lifecycle: FILL_REJECTED signal → phantom_rejected terminal state.
                try:
                    from offer_lifecycle import OfferState
                    from database import update_offer_lifecycle_state as _uls
                    _uls(trade_id, str(OfferState.PHANTOM_REJECTED))
                except Exception:
                    pass
                # Clear any cached Dexie detail — _record_fill() won't run to consume it
                self._last_dexie_details.pop(trade_id, None)
            elif verification == "unverified":
                # Spacescan couldn't decisively classify the on-chain spend.
                # Instead of retiring immediately (which used to misclassify
                # a real fill as cancelled and give the operator no reset
                # route), park the offer for re-verification across the next
                # few cycles. A retry path at the top of detect_fills()
                # re-runs _verify_fill_on_chain for parked entries until
                # they resolve or the attempt budget is exhausted.
                existing = self._pending_reverify.get(trade_id)
                if existing is None:
                    self._pending_reverify[trade_id] = {
                        "side": side,
                        "attempts": 1,
                        "first_seen": time.time(),
                        "local_clock_expired": local_clock_expired,
                    }
                    log_event("warning", "fill_verify_pending",
                              f"{side.upper()} offer {trade_id[:16]}... disappeared "
                              f"but Spacescan unverified — parked for retry "
                              f"(1/{self._pending_reverify_max_attempts})",
                              data={"trade_id": trade_id,
                                    "local_clock_expired": local_clock_expired})
                else:
                    existing["attempts"] = int(existing.get("attempts", 0)) + 1
                    existing["local_clock_expired"] = (
                        existing.get("local_clock_expired") or local_clock_expired
                    )
                    log_event("debug", "fill_verify_pending_retry",
                              f"{side.upper()} offer {trade_id[:16]}... still "
                              f"unverified ({existing['attempts']}/"
                              f"{self._pending_reverify_max_attempts})")
                # Mark lifecycle_state for operator visibility; do NOT
                # collapse to a terminal state yet.
                try:
                    from offer_lifecycle import OfferState
                    from database import update_offer_lifecycle_state as _uls
                    _uls(trade_id, str(getattr(OfferState, "MEMPOOL_OBSERVED", "mempool_observed")))
                except Exception:
                    pass
                # Hold off on clearing cached Dexie details — they'll be
                # consumed if the retry flips to "filled" before the budget
                # exhausts.
            # If verification is unavailable, we retire the stale local row
            # conservatively and let later wallet/Sage cleanup upgrade it.

        return fills

    def _retire_local_offer(self, trade_id: str, side: str,
                            details_cache: Dict[str, Dict], *,
                            status: str, event_type: str, severity: str,
                            suffix: str, data_extra: Optional[Dict] = None) -> None:
        """Retire a disappeared offer locally so counts stay aligned."""
        try:
            from database import update_offer_status
        except Exception:
            update_offer_status = None

        if update_offer_status:
            try:
                update_offer_status(trade_id, status)
            except Exception as e:
                log_event("error", "fill_local_retire_failed",
                          f"Failed to mark {trade_id[:16]}... as {status}: {e}")

        ctx = self._get_offer_context(trade_id, side, details_cache)
        side_upper = str(ctx.get("side") or side or "").upper()
        tier = str(ctx.get("tier") or "").strip()
        price = ctx.get("price")
        size_xch = ctx.get("size_xch")
        size_cat = ctx.get("size_cat")

        parts = [f"{side_upper} offer".strip()]
        if tier and tier.lower() != "unknown":
            parts[-1] += f" ({tier})"
        if size_xch not in (None, "", 0, "0"):
            try:
                size_xch_dec = Decimal(str(size_xch))
                if size_xch_dec > 0:
                    parts.append(f"size {size_xch_dec:.4f} XCH")
            except Exception:
                pass
        if price is not None:
            try:
                price_dec = Decimal(str(price))
                if price_dec > 0:
                    parts.append(f"at {price_dec:.8f}")
            except Exception:
                pass
        parts.append(suffix)

        data = {
            "trade_id": trade_id,
            "side": side,
            "tier": tier or "unknown",
            "price": float(price) if price not in (None, "", 0, "0") else None,
            "size_xch": float(size_xch) if size_xch not in (None, "", 0, "0") else None,
            "size_cat": float(size_cat) if size_cat not in (None, "", 0, "0") else None,
            "local_status": status,
        }
        if data_extra:
            data.update(data_extra)

        log_event(severity, event_type, " ".join(parts), data=data)

    def _get_offer_context(self, trade_id: str, side: str,
                           details_cache: Dict[str, Dict]) -> Dict:
        """Best-effort lookup of offer context for human-friendly activity text."""
        cached = details_cache.get(trade_id) or {}
        price = cached.get("price")
        tier = cached.get("tier")
        size_xch = cached.get("size_xch")
        size_cat = cached.get("size_cat")

        if (
            price in (None, "", 0, "0")
            or size_xch in (None, "", 0, "0")
            or size_cat in (None, "", 0, "0")
            or not tier or tier == "unknown"
        ):
            try:
                from database import get_offer
                db_offer = get_offer(trade_id)
            except Exception:
                db_offer = None
            if db_offer:
                if price in (None, "", 0, "0"):
                    price = db_offer.get("price_xch")
                if size_xch in (None, "", 0, "0"):
                    size_xch = db_offer.get("size_xch")
                if size_cat in (None, "", 0, "0"):
                    size_cat = db_offer.get("size_cat")
                if not tier or tier == "unknown":
                    tier = db_offer.get("tier")

        return {
            "trade_id": trade_id,
            "side": side,
            "price": price,
            "size_xch": size_xch,
            "size_cat": size_cat,
            "tier": tier or "unknown",
        }

    def _verify_fill_on_chain(self, trade_id: str, side: str) -> str:
        """Verify a suspected fill via Spacescan on-chain data.

        THE GOLDEN GATE: No fill is recorded without on-chain confirmation.

        Looks up the coin_id for this offer from the database, then asks
        Spacescan if that coin was spent on-chain to an external address.

        Returns:
            "filled" = Spacescan confirms this was a real fill
            "rejected" = Not a fill / do not retire locally here
            "unverified" = Offer vanished but on-chain verification is unavailable
        """
        try:
            from spacescan import verify_fill as spacescan_verify
        except ImportError:
            log_event("warning", "spacescan_import_failed",
                      "spacescan module not available — cannot verify fill. "
                      "Fill will NOT be recorded (conservative).")
            return "rejected"

        # Check if Spacescan verification is enabled
        if not getattr(cfg, "SPACESCAN_ENABLED", True):
            log_event("warning", "spacescan_disabled",
                      f"Spacescan disabled — cannot verify {side} fill "
                      f"{trade_id[:16]}... so it will NOT be recorded")
            return "rejected"

        # Look up the coin_id from our offers database
        try:
            from database import get_offer
            db_offer = get_offer(trade_id)
        except Exception:
            db_offer = None

        coin_id = None
        if db_offer:
            coin_id = db_offer.get("coin_id")

        # Build ordered candidate list: DB offer coin_id first, then any
        # additional coins locked to this trade (multi-coin offers, late linking).
        candidate_coin_ids: list = []
        try:
            from database import get_locked_coin_ids_for_trade
            candidate_coin_ids = list(get_locked_coin_ids_for_trade(trade_id))
        except Exception:
            pass
        if coin_id and coin_id not in candidate_coin_ids:
            candidate_coin_ids.insert(0, coin_id)
        candidate_coin_ids = [cid for cid in candidate_coin_ids if cid]

        if not candidate_coin_ids:
            # No coin_id recorded — we can't verify on-chain.
            # This happens for offers created before coin tracking was added,
            # or if coin detection failed during creation.
            log_event("warning", "fill_no_coin_id",
                      f"Offer {trade_id[:16]}... has no coin_id — "
                      f"cannot verify on-chain. Fill NOT recorded (conservative). "
                      f"This prevents phantom fills but may miss real fills "
                      f"for old offers without coin tracking.")
            return "rejected"

        # Use the primary candidate to pre-fetch Dexie detail (for the
        # _record_fill cache) and to honour ONE narrow Dexie veto: if
        # Dexie still shows the offer as OPEN, the disappearance is most
        # likely an RPC/cache blip rather than a real on-chain event, so
        # we short-circuit before spending a Spacescan call. All other
        # Dexie states (mismatches, expired-without-completion, etc.)
        # used to veto here but that let stale Dexie data reject real
        # fills before Spacescan (the agreed golden gate) could weigh in.
        # Those cases now fall through to Spacescan for the authoritative
        # answer; the Dexie detail is still cached for _record_fill().
        primary_coin_id = candidate_coin_ids[0]
        dexie_still_open = self._check_dexie_offer_still_open(
            trade_id, db_offer, primary_coin_id
        )
        if dexie_still_open is True:
            return "rejected"

        # Get our wallet address for self-spend detection
        # This is populated dynamically at startup from the wallet RPC
        our_address = getattr(cfg, "WALLET_ADDRESS", "")

        # Ask Spacescan for each candidate coin until one gives a decisive answer.
        last_result = None
        verified_coin_id = primary_coin_id
        for candidate in candidate_coin_ids:
            last_result = spacescan_verify(candidate, our_address)
            if last_result is not None:
                verified_coin_id = candidate
                break  # decisive answer (True=filled, False=not-fill)

        is_real_fill = last_result
        coin_id = verified_coin_id  # use the coin that gave a decisive answer

        if is_real_fill:
            log_event("success", "fill_verified",
                      f"Spacescan CONFIRMED {side} fill for {trade_id[:16]}... "
                      f"(coin {coin_id[:16]}...)")
            return "filled"
        elif is_real_fill is False:
            # Spacescan explicitly said NOT a fill (coin unspent or self-spend).
            #
            # BATCH SAGE FILL EXCEPTION: When SAGE_SET_CHANGE_ADDRESS=True, Sage
            # routes offer settlement XCH change back to our own wallet address.
            # In a batch arb sweep, the spend bundle for the filled offer outputs
            # to OUR address, so Spacescan correctly detects "self-spend" but that
            # self-spend IS the legitimate fill settlement.
            #
            # Mitigation: when this combination is active, cross-check with the
            # Sage wallet directly. If Sage reports the offer as CONFIRMED/filled,
            # override the Spacescan rejection and record the fill.
            if (getattr(cfg, "SAGE_SET_CHANGE_ADDRESS", False) and
                    str(getattr(cfg, "WALLET_TYPE", "")).lower() == "sage"):
                sage_confirmed = self._check_sage_offer_confirmed(trade_id)
                if sage_confirmed:
                    log_event("success", "fill_sage_override",
                              f"Spacescan self-spend BUT Sage confirms FILL for "
                              f"{trade_id[:16]}... (batch settlement via own address). "
                              f"Recording fill.")
                    return "filled"

                # Sage also non-confirmatory — try Dexie as a final tiebreaker.
                # Dexie independently verifies the on-chain spend bundle;
                # its status=4 is authoritative even when Spacescan + Sage disagree.
                try:
                    _dexie_id_false = (db_offer or {}).get("dexie_id")
                    if _dexie_id_false:
                        from dexie_manager import get_offer_detail
                        _detail_f = get_offer_detail(_dexie_id_false, cache_ttl_secs=0, timeout=5)
                        if _detail_f and isinstance(_detail_f, dict):
                            _dexie_trade_f = str(_detail_f.get("trade_id") or "").lower().replace("0x", "")
                            _our_trade_f = str(trade_id).lower().replace("0x", "")
                            _match_f = (_dexie_trade_f == _our_trade_f or not _dexie_trade_f)
                            if _detail_f.get("status") == 4 and _match_f:
                                log_event("success", "fill_dexie_override_false_path",
                                          f"Spacescan self-spend AND Sage non-confirm BUT "
                                          f"Dexie status=4 confirms FILL for "
                                          f"{trade_id[:16]}... — recording fill.")
                                return "filled"
                except Exception as _dexie_err_f:
                    log_event("debug", "fill_dexie_fallback_failed_false_path",
                              f"Dexie fallback (false path) failed for "
                              f"{trade_id[:16]}...: {_dexie_err_f}")

                log_event("info", "fill_rejected_sage_checked",
                          f"Spacescan self-spend AND Sage+Dexie do NOT confirm fill for "
                          f"{trade_id[:16]}... — rejected (likely a cancel).")
                return "rejected"

            # SAGE_SET_CHANGE_ADDRESS not active — Spacescan is authoritative.
            # Per agreed source-of-truth policy (Spacescan golden gate →
            # Sage → Dexie), Dexie CANNOT override an explicit Spacescan
            # rejection: that inversion caused phantom fills from stale
            # Dexie completions. We still surface the disagreement loudly
            # so the operator can manually reconcile if Dexie turns out
            # to be right — but we do NOT book the fill automatically.
            try:
                _dexie_id_rej = (db_offer or {}).get("dexie_id")
                if _dexie_id_rej:
                    from dexie_manager import get_offer_detail
                    _detail_r = get_offer_detail(_dexie_id_rej, cache_ttl_secs=0, timeout=5)
                    if _detail_r and isinstance(_detail_r, dict):
                        _dexie_trade_r = str(_detail_r.get("trade_id") or "").lower().replace("0x", "")
                        _our_trade_r = str(trade_id).lower().replace("0x", "")
                        _match_r = (_dexie_trade_r == _our_trade_r or not _dexie_trade_r)
                        if _detail_r.get("status") == 4 and _match_r:
                            log_event("warning", "fill_spacescan_dexie_disagree",
                                      f"Spacescan REJECTED {side} fill for "
                                      f"{trade_id[:16]}... but Dexie status=4 "
                                      f"suggests COMPLETED. Spacescan is "
                                      f"authoritative — NOT recording fill. "
                                      f"Operator should reconcile manually if "
                                      f"Dexie turns out to be right.",
                                      data={"trade_id": trade_id, "side": side,
                                            "dexie_id": _dexie_id_rej,
                                            "dexie_status": 4,
                                            "spacescan_verdict": "rejected"})
            except Exception as _dexie_err_r:
                log_event("debug", "fill_dexie_fallback_failed_rejected_path",
                          f"Dexie fallback (rejected path) failed for "
                          f"{trade_id[:16]}...: {_dexie_err_r}")

            log_event("info", "fill_rejected",
                      f"Spacescan REJECTED {side} fill for {trade_id[:16]}... "
                      f"(coin {coin_id[:16]}...) — phantom fill prevented!")
            return "rejected"
        else:
            # F63 (2026-04-10): Spacescan returned None (inconclusive). Before
            # giving up, try two fallback verification paths — this is what
            # the operator does manually when checking if a fill was real.

            # Fallback 1: Ask Sage directly if the offer is confirmed/completed.
            # This catches fills where Spacescan can't determine direction
            # (common during AMM-mediated fills via TibetSwap where the on-chain
            # spend pattern doesn't match direct peer-to-peer fills).
            try:
                sage_confirmed = self._check_sage_offer_confirmed(trade_id)
                if sage_confirmed:
                    log_event("success", "fill_verified_via_sage",
                              f"Spacescan inconclusive BUT Sage confirms FILL for "
                              f"{trade_id[:16]}... — recording fill.")
                    return "filled"
            except Exception as _sage_err:
                log_event("debug", "fill_sage_fallback_failed",
                          f"Sage fallback check failed for {trade_id[:16]}...: {_sage_err}")

            # Fallback 2: Check Dexie API for the offer's completion status.
            # If Dexie reports the offer as completed (status=4), that's
            # authoritative — Dexie processes the on-chain spend bundle and
            # knows definitively whether the offer was taken.
            try:
                dexie_id = (db_offer or {}).get("dexie_id")
                if dexie_id:
                    from dexie_manager import get_offer_detail
                    detail = get_offer_detail(dexie_id, cache_ttl_secs=0, timeout=5)
                    if detail and isinstance(detail, dict):
                        # Validate the Dexie detail matches our trade_id
                        # to prevent cross-offer confusion
                        _dexie_trade = str(detail.get("trade_id") or "").lower().replace("0x", "")
                        _our_trade = str(trade_id).lower().replace("0x", "")
                        _trade_match = (
                            _dexie_trade == _our_trade
                            or not _dexie_trade  # no trade_id in response = trust dexie_id match
                        )
                        dexie_status = detail.get("status")
                        if dexie_status == 4 and _trade_match:  # Dexie: 4 = completed/filled
                            log_event("success", "fill_verified_via_dexie",
                                      f"Spacescan inconclusive BUT Dexie confirms FILL "
                                      f"(status=4) for {trade_id[:16]}... — recording fill.")
                            return "filled"
                        elif dexie_status == 0:  # Dexie: 0 = cancelled
                            log_event("info", "fill_rejected_via_dexie",
                                      f"Dexie reports CANCELLED (status=0) for "
                                      f"{trade_id[:16]}... — not a fill.")
                            return "rejected"
            except Exception as _dexie_err:
                log_event("debug", "fill_dexie_fallback_failed",
                          f"Dexie fallback check failed for {trade_id[:16]}...: {_dexie_err}")

            # All sources inconclusive — fail closed.
            log_event("warning", "fill_unverified",
                      f"On-chain verification inconclusive for {side} fill "
                      f"{trade_id[:16]}... — Spacescan, Sage, and Dexie all "
                      f"inconclusive. NOT recording.")
            return "unverified"

    def _check_sage_offer_confirmed(self, trade_id: str) -> bool:
        """Ask Sage directly whether this offer is in a filled/confirmed state.

        Used as a tiebreaker when Spacescan flags a self-spend that might
        actually be a legitimate batch fill settlement.

        Returns True only if Sage unambiguously reports the offer as
        CONFIRMED / COMPLETED (i.e. taken by a counterparty).
        Returns False on any error or if the offer is found in a non-fill state.
        """
        try:
            from wallet import get_all_offers
        except ImportError:
            return False

        try:
            # Try targeted single-offer lookup first (much cheaper than fetching 500)
            try:
                from wallet_sage import rpc as _sage_rpc_direct
                _single = _sage_rpc_direct("get_offer", {"offer_id": trade_id}, timeout=8)
                if _single and isinstance(_single, dict):
                    # Sage wraps offer details inside a "trade_record" key.
                    # Check both top-level (legacy) and nested (current) positions.
                    status_val = _single.get("status") or (
                        (_single.get("trade_record") or {}).get("status")
                    )
                    CONFIRMED_STATUSES = {"confirmed", "completed", "success", "taken"}
                    CONFIRMED_INT = {4}  # Chia TradeStatus: 3=CANCELLED, 4=CONFIRMED
                    if isinstance(status_val, int) and status_val in CONFIRMED_INT:
                        return True
                    if isinstance(status_val, str) and status_val.lower() in CONFIRMED_STATUSES:
                        return True
                    # Found but not confirmed — no need to fetch bulk
                    return False
            except Exception as _sage_err:
                log_event("debug", "sage_single_offer_check_failed",
                          f"Single-offer Sage check failed for {trade_id[:16]}...: {_sage_err}")
                # Fall through to bulk fetch

            # Fetch completed offers from Sage (include_completed=True)
            all_offers = get_all_offers(include_completed=True, start=0, end=500)
        except Exception as exc:
            log_event("warning", "sage_check_failed",
                      f"Failed to fetch Sage offers for self-spend check: {exc}")
            return False

        if not all_offers:
            return False

        norm_trade = str(trade_id).lower().replace("0x", "")
        CONFIRMED_STATUSES = {
            "confirmed", "completed", "success",
            "taken",  # some Sage builds use this
        }
        # Chia numeric enum: 3=CANCELLED, 4=CONFIRMED
        CONFIRMED_INT = {4}

        for offer in all_offers:
            if not isinstance(offer, dict):
                continue
            # Match by trade_id or offer_id
            oid = str(offer.get("trade_id") or offer.get("offer_id") or "").lower().replace("0x", "")
            if oid != norm_trade:
                continue
            # Found the offer — check its status
            status_val = offer.get("status")
            if isinstance(status_val, int) and status_val in CONFIRMED_INT:
                return True
            if isinstance(status_val, str) and status_val.lower() in CONFIRMED_STATUSES:
                return True
            # Found but NOT confirmed
            log_event("info", "sage_offer_status_check",
                      f"Sage offer {trade_id[:16]}... status={repr(status_val)} "
                      f"— not confirmed, treating as non-fill.")
            return False

        # Not found in Sage's offer list at all — treat as non-fill (conservative)
        log_event("info", "sage_offer_not_found",
                  f"Offer {trade_id[:16]}... not found in Sage completed offers — "
                  f"treating as non-fill.")
        return False

    def _dexie_terminal_status(self, trade_id: str) -> str:
        """Ask Dexie whether this offer terminated as a fill or a cancel.

        Used as a fallback when Spacescan verification is exhausted so we
        don't silently lose a real fill to a rate-limit cascade. Dexie
        indexes every on-chain spend and distinguishes status=3 (spend
        was a cancel) from status=4 (spend was a fill) once a block
        confirms. We trust that distinction because Dexie parses the
        coin spends the same way the golden-gate Spacescan path does.

        Returns:
            "filled"    — Dexie status=4
            "cancelled" — Dexie status=3
            "unknown"   — any other state (still open, pending, 404,
                          rate-limited, mismatched trade_id, or network
                          error). The caller should fall back to a
                          conservative exhausted-retire path with the
                          MANUAL REVIEW flag.
        """
        try:
            from database import get_offer
            db_offer = get_offer(trade_id)
        except Exception:
            db_offer = None

        dexie_id = ""
        if isinstance(db_offer, dict):
            dexie_id = str(db_offer.get("dexie_id") or "").strip()
        if not dexie_id:
            cached = self._last_dexie_details.get(trade_id)
            if isinstance(cached, dict):
                dexie_id = str(cached.get("id") or "").strip()
        if not dexie_id:
            return "unknown"

        try:
            from dexie_manager import get_offer_detail
            detail = get_offer_detail(dexie_id)
        except Exception:
            detail = None

        if not isinstance(detail, dict):
            return "unknown"

        # Guard against Dexie returning a detail for a different trade_id
        # (dexie_id collision across re-quotes, stale cache, etc.)
        norm_trade_id = str(trade_id).lower().replace("0x", "")
        detail_trade_id = str(detail.get("trade_id") or "").lower().replace("0x", "")
        if detail_trade_id and detail_trade_id != norm_trade_id:
            return "unknown"

        status = detail.get("status")
        if status == 4:
            # Cache for _record_fill() so it doesn't re-fetch
            self._last_dexie_details[trade_id] = detail
            return "filled"
        if status == 3:
            return "cancelled"
        return "unknown"

    def _check_dexie_offer_still_open(self, trade_id: str, db_offer: Optional[Dict],
                                      coin_id: str) -> Optional[bool]:
        """Narrow pre-Spacescan check: is Dexie still showing this offer as OPEN?

        Returns True ONLY when Dexie unambiguously reports status=0 (ACTIVE)
        for the trade_id/coin we were tracking. In that case the "offer
        disappeared from wallet" signal was almost certainly a Sage/RPC
        cache blip and not a fill, so we short-circuit.

        All other Dexie signals (trade_id mismatch, coin mismatch, expired,
        completed, errored, API failure) return None — we defer to the
        Spacescan golden-gate verification for the authoritative verdict,
        and Dexie is only used later as a tiebreaker when Spacescan cannot
        decide.

        The Dexie detail dict (when successfully fetched) is still cached
        into ``self._last_dexie_details[trade_id]`` so ``_record_fill`` can
        enrich the fill record without a second HTTP call.
        """
        if not db_offer:
            return None

        dexie_id = str(db_offer.get("dexie_id") or "").strip()
        if not dexie_id:
            return None

        try:
            from dexie_manager import get_offer_detail
            detail = get_offer_detail(dexie_id)
        except Exception:
            detail = None

        if not isinstance(detail, dict):
            return None

        # Cache for _record_fill() so it doesn't need a second HTTP fetch
        self._last_dexie_details[trade_id] = detail

        norm_trade_id = str(trade_id).lower().replace("0x", "")
        detail_trade_id = str(detail.get("trade_id") or "").lower().replace("0x", "")
        if detail_trade_id and detail_trade_id != norm_trade_id:
            # Mismatch: log for operator visibility but DO NOT veto.
            log_event("debug", "fill_dexie_trade_mismatch_defer",
                      f"Dexie detail {dexie_id[:16]}... maps to trade "
                      f"{detail_trade_id[:16]}..., not {norm_trade_id[:16]}... "
                      f"Deferring to Spacescan for authoritative verdict.")
            return None

        tracked_coin = str(coin_id or "").lower().replace("0x", "")
        detail_coin_ids = self._extract_dexie_coin_ids(detail)
        if tracked_coin and detail_coin_ids and tracked_coin not in detail_coin_ids:
            log_event("debug", "fill_dexie_coin_mismatch_defer",
                      f"Dexie detail {dexie_id[:16]}... does not reference coin "
                      f"{tracked_coin[:16]}... Deferring to Spacescan.")
            return None

        try:
            status_num = int(detail.get("status"))
        except Exception:
            status_num = None

        if status_num == 0:
            log_event("info", "fill_dexie_still_open",
                      f"Dexie still shows {trade_id[:16]}... as OPEN — "
                      f"disappearance is likely a Sage cache blip, not a fill.")
            return True

        return None

    @staticmethod
    def _parse_iso_ts(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    @staticmethod
    def _extract_dexie_coin_ids(detail: Dict) -> Set[str]:
        coin_ids: Set[str] = set()
        for key in ("involved_coins", "related_offers"):
            for raw in detail.get(key) or []:
                if isinstance(raw, str):
                    coin_ids.add(raw.lower().replace("0x", ""))

        for field in ("input_coins", "output_coins"):
            coins_by_asset = detail.get(field) or {}
            if not isinstance(coins_by_asset, dict):
                continue
            for entries in coins_by_asset.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("id"):
                        coin_ids.add(str(entry["id"]).lower().replace("0x", ""))

        return coin_ids

    def _record_fill(self, trade_id: str, side: str,
                     details_cache: Dict[str, Dict]) -> Optional[Dict]:
        """Record a filled offer to the database and history.

        Returns fill detail dict, or None on error.
        """
        # Get cached details (price, size, etc.)
        cached = details_cache.get(trade_id, {})
        price = cached.get("price", Decimal("0"))
        size_xch = cached.get("size_xch", Decimal("0"))
        size_cat = cached.get("size_cat", Decimal("0"))
        tier = cached.get("tier", "unknown")
        dexie_link = cached.get("dexie_link", "")
        db_offer = None  # Cached DB lookup — reused for coin_id below

        # If cache missed (offer from before bot started or V1 carry-over),
        # try to look up from the offers database table
        if price == Decimal("0") or size_xch == Decimal("0"):
            try:
                from database import get_offer
                db_offer = get_offer(trade_id)
                if db_offer:
                    if price == Decimal("0") and db_offer.get("price_xch"):
                        price = Decimal(str(db_offer["price_xch"]))
                    if size_xch == Decimal("0") and db_offer.get("size_xch"):
                        size_xch = Decimal(str(db_offer["size_xch"]))
                    if size_cat == Decimal("0") and db_offer.get("size_cat"):
                        size_cat = Decimal(str(db_offer["size_cat"]))
                    if tier == "unknown" and db_offer.get("tier"):
                        tier = db_offer["tier"]
                    log_event("info", "fill_cache_miss_recovered",
                              f"Recovered fill details from DB for {trade_id[:16]}... "
                              f"(price={price:.8f}, size={size_xch})")
            except Exception:
                pass  # DB lookup is best-effort

        # If still zero after DB fallback, this offer disappeared but we have
        # no record of it at all. This usually means it's a stale offer from
        # a previous session or an expired offer that the wallet cleaned up.
        # Record it as 'unmatched' so operators can investigate, but exclude
        # from PnL (all PnL queries filter verification_status='verified').
        if price == Decimal("0") and size_xch == Decimal("0"):
            log_event("warning", "fill_no_details",
                      f"Offer {trade_id[:16]}... disappeared but has no price/size data "
                      f"(not in cache or DB) — recording as unmatched for investigation")
            try:
                record_fill(
                    trade_id=trade_id,
                    side=side,
                    price_xch=Decimal("0"),
                    size_xch=Decimal("0"),
                    size_cat=Decimal("0"),
                    cat_asset_id=cfg.CAT_ASSET_ID,
                    tier="unknown",
                    verification_status="unmatched",
                    fee_mojos_xch=0,
                )
            except Exception as e:
                log_event("error", "fill_unmatched_record_failed",
                          f"Failed to record unmatched fill for {trade_id[:16]}...: {e}")
            return None  # Still return None — caller shouldn't treat this as a confirmed fill

        # Record to database
        try:
            # Use the fee stored on the offer row (set at creation time).
            # Falls back to current config if the offer has no stored fee.
            _fee_mojos = 0
            try:
                from database import get_offer as _get_offer_fee
                db_offer = _get_offer_fee(trade_id) if trade_id else None
                if db_offer and int(db_offer.get("fee_mojos_xch") or 0) > 0:
                    _fee_mojos = int(db_offer["fee_mojos_xch"])
                else:
                    _fee_xch = Decimal(str(getattr(cfg, "TRANSACTION_FEE_XCH", "0") or "0"))
                    _fee_mojos = int(_fee_xch * Decimal("1000000000000"))
            except Exception:
                _fee_mojos = 0
            fill_id = record_fill(
                trade_id=trade_id,
                side=side,
                price_xch=price,
                size_xch=size_xch,
                size_cat=size_cat,
                cat_asset_id=cfg.CAT_ASSET_ID,
                tier=tier,
                fee_mojos_xch=_fee_mojos
            )
        except Exception as e:
            log_event("error", "fill_record_failed",
                      f"Failed to record fill for {trade_id}: {e}")
            return None

        # Look up the coin_id that was destroyed by this fill
        coin_id = "unknown"
        # Check details cache first (cheapest)
        if trade_id in details_cache and details_cache[trade_id].get("coin_id"):
            coin_id = details_cache[trade_id]["coin_id"]
        # Fall back to DB lookup (reuse db_offer if already fetched above)
        if coin_id == "unknown":
            try:
                _offer_row = db_offer
                if not _offer_row:
                    from database import get_offer as _get_offer_for_coin
                    _offer_row = _get_offer_for_coin(trade_id)
                if _offer_row and _offer_row.get("coin_id"):
                    coin_id = _offer_row["coin_id"]
            except Exception:
                pass  # coin_id lookup is best-effort

        fill_detail = {
            "fill_id": fill_id,
            "trade_id": trade_id,
            "side": side,
            "price": price,
            "size_xch": size_xch,
            "size_cat": size_cat,
            "tier": tier,
            "coin_id": coin_id,
            "dexie_link": dexie_link,
            "timestamp": time.time(),
        }

        # Add to history (capped)
        self._fill_history.insert(0, fill_detail)
        if len(self._fill_history) > self._max_history:
            self._fill_history = self._fill_history[:self._max_history]

        # Check whether the mempool watcher caught this fill before the
        # block confirmed — useful as a running measure of watcher latency
        # vs. mempool-window length. Failures are non-fatal (the fill has
        # already been verified by this point).
        mempool_warned = False
        try:
            import mempool_watcher as _mw
            _w = getattr(_mw, "_watcher_instance", None)
            if _w is not None and coin_id and coin_id != "unknown":
                mempool_warned = _w.was_fill_warned(coin_id)
        except Exception:
            pass

        coin_str = f" coin={coin_id[:16]}..." if coin_id != "unknown" else ""
        warned_tag = "" if mempool_warned else " (mempool-miss)"
        log_event("info", "offer_filled",
                  f"🎉 {side.upper()} offer filled!{coin_str} "
                  f"Price: {price:.8f} XCH, Size: {size_xch} XCH / {size_cat:.2f} CAT "
                  f"[tier={tier}]{warned_tag}",
                  data={"fill_id": fill_id, "trade_id": trade_id,
                        "coin_id": coin_id, "side": side,
                        "price": float(price) if price else None,
                        "size_xch": float(size_xch) if size_xch else None,
                        "size_cat": float(size_cat) if size_cat else None,
                        "tier": tier,
                        "mempool_warned": mempool_warned})

        # ---- Fill classification (additive, fail-open) -------------------
        # Classify the fill and persist to DB.  Then register with the
        # SweepCoordinator so same-block fills get grouped into a sweep event.
        try:
            from fill_classifier import classify_and_store_fill
            from sweep_coordinator import get_coordinator as _get_sweep_coordinator

            # Use the Dexie detail already fetched by _check_dexie_offer_still_open()
            # (cached in self._last_dexie_details during verification).
            # This avoids a second blocking HTTP call on every fill.
            _dexie_detail = self._last_dexie_details.pop(trade_id, None)

            _classification = classify_and_store_fill(
                fill_id=fill_id,
                trade_id=trade_id,
                fill_detail={**fill_detail, "side": side},
                dexie_detail=_dexie_detail,
            )
            # Stamp side so SweepCoordinator can use it for direction-aware protection
            _classification.side = side

            # Register with sweep coordinator; may upgrade UNKNOWN→DEXIE_COMBINED.
            _sweep_group_id = _get_sweep_coordinator().process_fill(
                fill_id, _classification
            )
            if _sweep_group_id:
                fill_detail["sweep_group_id"] = _sweep_group_id
                fill_detail["fill_classification"] = _classification.classification

            if _classification.classification != "unknown":
                fill_detail["fill_classification"] = _classification.classification
                log_event("info", "fill_classified",
                          f"Fill {fill_id} classified as {_classification.classification} "
                          f"(confidence={_classification.confidence})",
                          data={"fill_id": fill_id,
                                "classification": _classification.classification,
                                "confidence": _classification.confidence,
                                "taker_puzzle_hash": _classification.taker_puzzle_hash,
                                "spent_block_index": _classification.spent_block_index})
        except Exception as _class_err:
            log_event("debug", "fill_classification_error",
                      f"Fill classification failed for fill_id={fill_id}: {_class_err}")
            # Classification is additive — never block fill recording

        return fill_detail

    # -------------------------------------------------------------------
    # Round-trip PnL matching
    # -------------------------------------------------------------------

    def match_round_trips(self) -> List[Dict]:
        """Match unmatched buy fills with sell fills to calculate PnL.

        Matching priority:
        1. Same tier + same size (e.g., sniper buy ↔ sniper sell at 0.2 XCH)
        2. Same size (within 1% tolerance)
        3. FIFO fallback (only if sizes match within 20%)

        Fills with very different sizes are NOT paired — they represent
        one-directional inventory changes, not round-trips.

        PnL = (sell_xch - buy_xch) + (buy_cat - sell_cat) × mid_price
        Positive = profit.

        Returns list of matched round-trips.
        """
        cat_asset_id = cfg.CAT_ASSET_ID
        run_cutoff = getattr(cfg, "RUN_HISTORY_CUTOFF", None)
        unmatched_buys = get_unmatched_fills(cat_asset_id, side="buy", since=run_cutoff)
        unmatched_sells = get_unmatched_fills(cat_asset_id, side="sell", since=run_cutoff)

        if not unmatched_buys or not unmatched_sells:
            return []

        # Filter out zero-value fills (no price/size data — can't calculate PnL)
        valid_buys = [f for f in unmatched_buys
                      if Decimal(str(f.get("size_xch", 0))) > 0
                      and Decimal(str(f.get("price_xch", 0))) > 0]
        valid_sells = [f for f in unmatched_sells
                       if Decimal(str(f.get("size_xch", 0))) > 0
                       and Decimal(str(f.get("price_xch", 0))) > 0]

        skipped_buys = len(unmatched_buys) - len(valid_buys)
        skipped_sells = len(unmatched_sells) - len(valid_sells)
        if skipped_buys or skipped_sells:
            log_event("warning", "pnl_skipped_zero_fills",
                      f"Skipped {skipped_buys} buy + {skipped_sells} sell fills "
                      f"with zero price/size (no data available for PnL)")

        if not valid_buys or not valid_sells:
            return []

        matched = []
        used_sell_ids = set()  # Track which sells have been matched

        # Pass 1: Match same-tier + same-size fills (highest confidence)
        # Pass 2: Match same-size fills regardless of tier (within 1%)
        # Pass 3: Match remaining fills within 20% size tolerance (FIFO)
        # Pass 4: FIFO fallback for asymmetric buy/sell tier sizing.
        #   When BUY_*_SIZE_XCH ≠ SELL_*_SIZE_XCH (e.g. BUY_INNER=0.67 XCH,
        #   SELL_INNER=3.26 XCH), size-based matching never succeeds because
        #   the ratio (4.8x) exceeds the 20% tolerance. Pass 4 matches any
        #   unmatched buy with any unmatched sell purely by chronological
        #   order. The _create_round_trip PnL formula is still mathematically
        #   correct for asymmetric pairs: net_xch = sell_xch - buy_xch, and
        #   the unbalanced CAT position is valued at the current mid price,
        #   giving an accurate mark-to-market realized PnL.
        pass4_eligible = (
            len([f for f in valid_buys
                 if f["fill_id"] not in {m["buy_fill_id"] for m in matched}]) > 0
            and len([f for f in valid_sells
                     if f["fill_id"] not in used_sell_ids]) > 0
        )
        pass_specs = [
            (True, Decimal("0.01")),    # Pass 1: same tier, exact size
            (False, Decimal("0.01")),   # Pass 2: any tier, exact size
            (False, Decimal("0.20")),   # Pass 3: any tier, 20% tolerance
        ]
        if pass4_eligible:
            pass_specs.append((False, None))  # Pass 4: FIFO, no size limit

        for pass_num, (check_tier, size_tolerance) in enumerate(pass_specs, start=1):
            for buy_fill in valid_buys:
                if buy_fill["fill_id"] in {m["buy_fill_id"] for m in matched}:
                    continue  # Already matched

                buy_xch = Decimal(str(buy_fill.get("size_xch", 0)))
                buy_tier = buy_fill.get("tier", "unknown")

                # Find best matching sell
                best_sell = None
                best_size_diff = Decimal("999999")

                for sell_fill in valid_sells:
                    if sell_fill["fill_id"] in used_sell_ids:
                        continue

                    sell_xch = Decimal(str(sell_fill.get("size_xch", 0)))
                    sell_tier = sell_fill.get("tier", "unknown")

                    # Tier check (Pass 1 only)
                    if check_tier and buy_tier != sell_tier:
                        continue

                    # Size tolerance check (Pass 4 skips this — FIFO by time)
                    if size_tolerance is not None:
                        if buy_xch > 0:
                            size_diff = abs(sell_xch - buy_xch) / buy_xch
                        else:
                            continue
                        if size_diff > size_tolerance:
                            continue
                    else:
                        # Pass 4: accept any size; use absolute diff as tiebreak
                        size_diff = abs(sell_xch - buy_xch)

                    if size_diff < best_size_diff:
                        best_sell = sell_fill
                        best_size_diff = size_diff

                if best_sell is not None:
                    rt = self._create_round_trip(buy_fill, best_sell, pass_num)
                    if rt:
                        matched.append(rt)
                        used_sell_ids.add(best_sell["fill_id"])

        # Log unmatched fills (one-directional inventory changes)
        matched_buy_ids = {m["buy_fill_id"] for m in matched}
        unmatched_buy_count = sum(1 for f in valid_buys if f["fill_id"] not in matched_buy_ids)
        unmatched_sell_count = sum(1 for f in valid_sells if f["fill_id"] not in used_sell_ids)
        if unmatched_buy_count or unmatched_sell_count:
            log_event("info", "pnl_unmatched_fills",
                      f"{unmatched_buy_count} buy + {unmatched_sell_count} sell fills "
                      f"unmatched (one-directional inventory build)")

        return matched

    def _create_round_trip(self, buy_fill: Dict, sell_fill: Dict,
                           pass_num: int) -> Optional[Dict]:
        """Calculate PnL and record a round-trip match.

        PnL formula:
        - Net XCH = sell_xch - buy_xch
        - Net CAT = buy_cat - sell_cat (surplus from buying cheaper)
        - Total PnL = net_xch + (net_cat × mid_price)
        """
        buy_xch = Decimal(str(buy_fill.get("size_xch", 0)))
        sell_xch = Decimal(str(sell_fill.get("size_xch", 0)))
        buy_price = Decimal(str(buy_fill.get("price_xch", 0)))
        sell_price = Decimal(str(sell_fill.get("price_xch", 0)))
        buy_cat = Decimal(str(buy_fill.get("size_cat", 0)))
        sell_cat = Decimal(str(sell_fill.get("size_cat", 0)))

        net_xch = sell_xch - buy_xch
        net_cat = buy_cat - sell_cat

        # Value CAT surplus at mid price (average of buy and sell price)
        if buy_price > 0 and sell_price > 0:
            mid_price = (buy_price + sell_price) / 2
        elif sell_price > 0:
            mid_price = sell_price
        elif buy_price > 0:
            mid_price = buy_price
        else:
            mid_price = Decimal("0")

        cat_value_xch = net_cat * mid_price if mid_price > 0 else Decimal("0")
        pnl_xch = net_xch + cat_value_xch

        # Deduct transaction fees from both legs of the round-trip.
        # fee_mojos_xch is stored in mojos (integer); convert to XCH for PnL.
        try:
            buy_fee_mojos = int(buy_fill.get("fee_mojos_xch") or 0)
            sell_fee_mojos = int(sell_fill.get("fee_mojos_xch") or 0)
            total_fee_xch = Decimal(buy_fee_mojos + sell_fee_mojos) / Decimal("1000000000000")
            pnl_xch -= total_fee_xch
        except Exception:
            pass  # Fee deduction is best-effort; don't break PnL matching

        try:
            rt_id = match_round_trip(
                buy_fill_id=buy_fill["fill_id"],
                sell_fill_id=sell_fill["fill_id"],
                pnl_xch=pnl_xch
            )

            buy_tier = buy_fill.get("tier", "?")
            sell_tier = sell_fill.get("tier", "?")
            log_event("info", "round_trip_matched",
                      f"Round-trip PnL: {pnl_xch:+.8f} XCH (incl. fees) "
                      f"(buy {buy_xch} @ {buy_price:.8f} [{buy_tier}], "
                      f"sell {sell_xch} @ {sell_price:.8f} [{sell_tier}], "
                      f"pass={pass_num})")

            return {
                "round_trip_id": rt_id,
                "buy_fill_id": buy_fill["fill_id"],
                "sell_fill_id": sell_fill["fill_id"],
                "buy_price": buy_price,
                "sell_price": sell_price,
                "buy_xch": buy_xch,
                "sell_xch": sell_xch,
                "buy_cat": buy_cat,
                "sell_cat": sell_cat,
                "net_cat": net_cat,
                "pnl_xch": pnl_xch,
            }

        except Exception as e:
            log_event("error", "round_trip_match_failed",
                      f"Failed to match round-trip: {e}")
            return None

    # -------------------------------------------------------------------
    # Fill protection (anti-churn)
    # -------------------------------------------------------------------

    def should_protect_side(self, side: str) -> bool:
        """Check if a side should be protected from requoting.

        After a fill, we hold off on requoting the OTHER side for
        FILL_PROTECT_SECS to prevent churn from arb bots.

        If we just filled a BUY, protect SELL side (and vice versa).
        """
        # Check opposite side's last fill
        opposite = "sell" if side == "buy" else "buy"
        last_fill = self._last_fill_time.get(opposite, 0)

        if last_fill <= 0:
            return False

        elapsed = time.time() - last_fill
        return elapsed < cfg.FILL_PROTECT_SECS

    def time_since_last_fill(self, side: str) -> float:
        """Seconds since last fill on this side."""
        last = self._last_fill_time.get(side, 0)
        if last <= 0:
            return float("inf")
        return time.time() - last

    # -------------------------------------------------------------------
    # State queries
    # -------------------------------------------------------------------

    def get_fill_history(self, limit: int = 20) -> List[Dict]:
        """Get recent fill history for GUI display."""
        return self._fill_history[:limit]

    def get_fill_counts(self) -> Dict[str, int]:
        """Get fill counts from last detection loop."""
        return dict(self._last_fill_count)

    def reset_baseline(self):
        """Reset the before/after baseline (e.g., on bot restart)."""
        self._previous_ids = {"buy": set(), "sell": set()}
        self._mass_disappearance_count = 0
        log_event("info", "fill_tracker_reset", "Fill tracker baseline reset")

    def set_baseline(self, buy_ids: set, sell_ids: set):
        """Pre-seed the baseline with a known-good set of offer IDs.

        Call this before the first detect_fills() when you have a wallet
        snapshot from before startup sync ran. This allows the first
        detect_fills() call to see offers that filled between the last
        bot stop and this start (offline fills), instead of silently
        treating the current state as if nothing changed.

        The set_baseline call bypasses the normal first-call no-op in
        detect_fills() — the tracker will process the next detect_fills()
        call as a real comparison, not as a baseline-init.
        """
        self._previous_ids["buy"] = set(buy_ids)
        self._previous_ids["sell"] = set(sell_ids)
        log_event("info", "fill_tracker_baseline_set",
                  f"Pre-startup baseline set: {len(buy_ids)} buys, "
                  f"{len(sell_ids)} sells — offline fills will be detected on first loop")

    # prune_known_ids REMOVED — _known_ids was dead state (write-only, never read)

