"""Dexie.space offer-posting manager and historical data client

`DexieManager` queues offers for posting to the Dexie orderbook, dedupes
submissions via a SHA-256 fingerprint of the bech32 offer string, and
posts with retry and backoff. It also fetches Dexie v3 historical trades
and pair data used by volatility tracking and pair intelligence. The
trade-id -> dexie-id mapping is persisted through `database.py` and
hydrated on startup so restarts do not lose posted-state.

Key responsibilities:
    - Queue and flush offer posts decoupled from offer creation
    - Deduplicate via bech32 SHA-256 fingerprints
    - Persist and hydrate the trade_id -> dexie_id mapping
    - Retrieve v3 historical trades and pairs for downstream consumers
"""

import time
import hashlib
import requests
import threading
from typing import Dict, List, Optional

from config import cfg
from database import update_offer_dexie, log_event


_offer_detail_cache: Dict[str, Dict] = {}
_offer_detail_cache_at: Dict[str, float] = {}


class DexieManager:
    """Manages posting offers to the Dexie.space DEX.

    Key responsibilities:
    - Queue offers for posting (decoupled from offer creation)
    - Post with retries and error handling
    - Track posted fingerprints (prevent duplicate posts)
    - Persist trade_id → dexie_id mapping in database
    """

    def __init__(self):
        # Post queue (cleared after each flush)
        self._queue: List[Dict] = []

        # Fingerprints of already-posted offers (sha256 of bech32 string)
        self._posted_fingerprints: set = set()

        # In-memory trade_id → dexie_offer_id mapping
        self._trade_dexie_map: Dict[str, str] = {}

        # Lock for thread safety
        self._lock = threading.Lock()

        # Rate limit cooldown (epoch time until which we skip Dexie calls)
        self._rate_limited_until: float = 0.0

        # Stats
        self._total_posted: int = 0
        self._total_failed: int = 0
        self._total_skipped: int = 0

        # F37 (2026-04-08): Dexie v3 historical trades cache.
        # Real trade-flow data for the current pair, refreshed periodically.
        # Used by risk_manager to compute REAL volatility (vs estimating
        # from Tibet reserve drift) and by the Advisor for fill-rate context.
        self._v3_trades_cache: Dict[str, Dict] = {}  # ticker_id → {trades, fetched_at}
        self._v3_trades_ttl_secs: float = 300.0  # 5 minutes

        # F38 (2026-04-08): Dexie v3 pairs cache for pair selector enrichment.
        self._v3_pairs_cache: Optional[Dict] = None
        self._v3_pairs_fetched_at: float = 0.0
        self._v3_pairs_ttl_secs: float = 600.0  # 10 minutes

    # -------------------------------------------------------------------
    # Queue management
    # -------------------------------------------------------------------

    def queue_post(self, offer_bech32: str, trade_id: str = None,
                   force: bool = False):
        """Queue an offer for posting to Dexie.

        Args:
            offer_bech32: The offer1... bech32 string
            trade_id: Chia trade_id (for tracking)
            force: If True, post even if fingerprint matches
        """
        if not offer_bech32 or not isinstance(offer_bech32, str):
            return

        with self._lock:
            self._queue.append({
                "offer": offer_bech32.strip(),
                "trade_id": trade_id,
                "force": force,
            })
            # Bounded queue. Without a cap, operators who disable
            # DEXIE_AUTO_POST (or who pause posting during an outage)
            # would see offer blobs accumulate indefinitely — each
            # offer1... bech32 is multi-KB of memory, so a long run
            # can hold onto 100+ MB of stale offers that will never
            # be flushed. Drop the OLDEST entries once the cap is
            # reached; trade_ids stay tracked via offer_manager so
            # Dexie visibility comes back on the next successful
            # repost cycle when auto-post re-enables.
            _max_queue = 500
            if len(self._queue) > _max_queue:
                overflow = len(self._queue) - _max_queue
                self._queue = self._queue[-_max_queue:]
                if not hasattr(self, "_queue_cap_logged"):
                    self._queue_cap_logged = False
                if not self._queue_cap_logged:
                    self._queue_cap_logged = True
                    log_event(
                        "warning",
                        "dexie_queue_cap_hit",
                        f"Dexie post queue exceeded {_max_queue} entries "
                        f"(dropped {overflow} oldest). DEXIE_AUTO_POST may "
                        f"be off or the API has been unreachable for a "
                        f"while. Oldest offers will be reposted on the "
                        f"next successful flush via the startup-repost "
                        f"path.",
                        data={"cap": _max_queue,
                              "dropped": overflow,
                              "queue_len_after": len(self._queue)},
                    )

    def purge_trade_ids(self, trade_ids):
        """Remove queued (not-yet-posted) entries for specific trade IDs.

        Call this when an offer is cancelled so it doesn't generate an
        "Invalid Offer" 400 error on the next Dexie flush cycle.

        Args:
            trade_ids: iterable of trade_id strings to remove from the queue
        """
        if not trade_ids:
            return
        ids = set(trade_ids)
        with self._lock:
            before = len(self._queue)
            self._queue = [
                item for item in self._queue
                if item.get("trade_id") not in ids
            ]
            removed = before - len(self._queue)
        if removed:
            log_event("debug", "dexie_queue_purged",
                      f"Removed {removed} cancelled offer(s) from Dexie queue")

    def flush_queue(self, flush_all: bool = False) -> Dict:
        """Post all queued offers to Dexie.

        For large batches (>10 items), uses concurrent workers to speed up
        posting. Small batches post sequentially to avoid unnecessary overhead.

        Args:
            flush_all: If True, ignore MAX_POSTS_PER_LOOP and flush everything.
                      Used during startup repost to avoid needing multiple flushes.

        Returns summary: {posted: N, failed: N, skipped: N}
        """
        if not cfg.DEXIE_POST_ENABLED:
            return {"posted": 0, "failed": 0, "skipped": 0, "disabled": True}

        # Grab items from queue
        with self._lock:
            if flush_all:
                batch = list(self._queue)
                self._queue = []
            else:
                max_posts = cfg.MAX_POSTS_PER_LOOP
                batch = list(self._queue[:max_posts])
                self._queue = self._queue[max_posts:]

        if not batch:
            return {"posted": 0, "failed": 0, "skipped": 0}

        posted = 0
        failed = 0
        skipped = 0
        failed_items = []  # Items to re-queue on failure

        _MAX_DEXIE_RETRIES = 3  # Max times an item can be re-queued

        def _process_one(item):
            """Post a single item — used by both sequential and parallel paths."""
            offer_bech32 = item["offer"]
            trade_id = item.get("trade_id")
            force = item.get("force", False)
            return self._post_single(offer_bech32, trade_id, force)

        def _handle_result(result, item):
            nonlocal posted, failed, skipped
            # Parallel workers call this concurrently — serialise via lock.
            with self._lock:
                if result.get("skipped"):
                    skipped += 1
                    self._total_skipped += 1
                elif result.get("success"):
                    posted += 1
                    self._total_posted += 1
                else:
                    failed += 1
                    self._total_failed += 1
                    # Permanent failures (HTTP 4xx) — don't requeue, the
                    # offer blob is invalid and will never succeed.
                    if result.get("permanent"):
                        return
                    # Re-queue transient failures for next cycle if under retry limit
                    retries = item.get("_dexie_retries", 0)
                    if retries < _MAX_DEXIE_RETRIES:
                        item["_dexie_retries"] = retries + 1
                        failed_items.append(item)

        # Parallel posting for large batches (startup repost)
        if len(batch) > 10:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            workers = min(8, len(batch))  # Up to 8 concurrent posts
            log_event("info", "dexie_flush_parallel",
                      f"Parallel flush: {len(batch)} offers with {workers} workers")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_process_one, item): item for item in batch}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        result = future.result()
                        _handle_result(result, item)
                    except Exception as e:
                        log_event("warning", "dexie_parallel_error",
                                  f"Parallel Dexie post failed: {e}")
                        with self._lock:
                            failed += 1
                            self._total_failed += 1
                            retries = item.get("_dexie_retries", 0)
                            if retries < _MAX_DEXIE_RETRIES:
                                item["_dexie_retries"] = retries + 1
                                failed_items.append(item)
        else:
            # Sequential for small batches (normal cycle)
            for item in batch:
                result = _process_one(item)
                _handle_result(result, item)

        # Re-queue failed items for retry on the next cycle
        if failed_items:
            with self._lock:
                self._queue.extend(failed_items)
            log_event("info", "dexie_requeue",
                      f"Re-queued {len(failed_items)} failed Dexie posts for next cycle")

        summary = {"posted": posted, "failed": failed, "skipped": skipped,
                    "requeued": len(failed_items)}
        if posted > 0:
            log_event("info", "dexie_flush",
                      f"Posted {posted} queued offers to Dexie ({skipped} skipped, {failed} failed)")
        return summary

    # -------------------------------------------------------------------
    # Core posting
    # -------------------------------------------------------------------

    def _post_single(self, offer_bech32: str, trade_id: str = None,
                     force: bool = False) -> Dict:
        """Post a single offer to Dexie with retries.

        Returns result dict with success/skipped/error fields.
        """
        # Respect 429 cooldown — don't hammer Dexie during backoff
        if time.time() < self._rate_limited_until:
            remaining = int(self._rate_limited_until - time.time())
            return {"success": False, "error": f"rate_limited (cooldown {remaining}s remaining)"}

        # Validate bech32 format
        if not offer_bech32.lower().startswith("offer1"):
            return {"success": False, "error": "not_bech32_offer1"}

        # Fingerprint check (prevent duplicate posts, thread-safe)
        fp = self._fingerprint(offer_bech32)
        with self._lock:
            if not force and fp in self._posted_fingerprints:
                return {"success": True, "skipped": True, "reason": "already_posted"}

        url = f"{cfg.DEXIE_API_BASE.rstrip('/')}/v1/offers"
        payload = {"offer": offer_bech32}
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "x-bot-tag": cfg.BOT_TAG,
        }

        last_err = None
        _permanent = False  # Set True for HTTP 4xx — don't requeue
        for attempt in range(cfg.DEXIE_POST_RETRIES + 1):
            try:
                r = requests.post(
                    url, json=payload, headers=headers,
                    timeout=cfg.DEXIE_POST_TIMEOUT
                )

                if 200 <= r.status_code < 300:
                    data = self._safe_json(r)

                    # Extract Dexie offer ID
                    dexie_id = None
                    if isinstance(data, dict):
                        dexie_id = data.get("id") or data.get("offer_id")

                    if not dexie_id:
                        _tid_log = (trade_id[:16] + "...") if trade_id else "?"
                        log_event("warning", "dexie_no_id",
                                  f"Dexie returned 2xx but no offer ID found "
                                  f"(trade: {_tid_log}, response keys: {list(data.keys()) if isinstance(data, dict) else 'non-dict'})")

                    # Mark as posted (thread-safe for parallel flush)
                    with self._lock:
                        self._posted_fingerprints.add(fp)

                    # Track mapping (thread-safe for parallel flush)
                    if trade_id and dexie_id:
                        with self._lock:
                            self._trade_dexie_map[trade_id] = str(dexie_id)
                        # Update database
                        try:
                            update_offer_dexie(trade_id, str(dexie_id))
                        except Exception as e:
                            log_event("warning", "dexie_db_update_failed",
                                      f"Failed to update dexie_id in DB: {e}")

                    log_event("info", "dexie_posted",
                              f"✅ Posted to Dexie: {str(dexie_id)[:20]}... "
                              f"(trade: {trade_id[:16]}...)" if trade_id else
                              f"✅ Posted to Dexie: {str(dexie_id)[:20]}...")

                    return {
                        "success": True,
                        "dexie_id": dexie_id,
                        "trade_id": trade_id,
                    }

                # 429 — back off longer and let the cooldown apply
                if r.status_code == 429:
                    try:
                        retry_after = min(int(r.headers.get("Retry-After", "30")), 120)
                    except (ValueError, TypeError):
                        retry_after = 30  # Fallback if Retry-After is HTTP-date format
                    self._rate_limited_until = time.time() + retry_after
                    last_err = f"HTTP 429 rate limited (backing off {retry_after}s)"
                    log_event("warning", "dexie_rate_limited",
                              f"Dexie returned 429 — backing off {retry_after}s")
                    break  # Don't burn remaining retries

                # 4xx (except 429) — permanent client error, don't retry.
                # "Invalid Offer" (400) means the offer blob references spent
                # coins or is otherwise un-takeable.  Retrying won't help.
                if 400 <= r.status_code < 500:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                    _permanent = True
                    break

                last_err = f"HTTP {r.status_code}: {r.text[:200]}"

            except requests.Timeout:
                last_err = f"Timeout after {cfg.DEXIE_POST_TIMEOUT}s"
            except requests.ConnectionError as e:
                last_err = f"Connection error: {e}"
            except Exception as e:
                last_err = f"Unexpected error: {e}"

            # Retry with sleep
            if attempt < cfg.DEXIE_POST_RETRIES:
                time.sleep(cfg.DEXIE_POST_RETRY_SLEEP)

        # "Invalid Offer" 400s are an expected race condition: the offer may
        # have been filled or cancelled between creation and the Dexie flush.
        # Downgrade to debug so these don't flood the log during high-churn
        # periods; genuine network/server failures stay at warning/error.
        _invalid_offer = _permanent and "invalid offer" in last_err.lower()
        if _invalid_offer:
            _level = "debug"
        elif _permanent:
            _level = "warning"
        else:
            _level = "error"
        _retries_text = "1 attempt (permanent 4xx)" if _permanent else f"{cfg.DEXIE_POST_RETRIES + 1} attempts"
        log_event(_level, "dexie_post_failed",
                  f"Failed to post to Dexie after {_retries_text}: {last_err}")
        return {"success": False, "error": last_err, "permanent": _permanent}

    # -------------------------------------------------------------------
    # Repost active offers (recovery after outage)
    # -------------------------------------------------------------------

    def repost_active_offers(self, active_offers: List[Dict]):
        """Re-post all currently active offers to Dexie.

        Used after startup or Dexie outage to ensure all our offers
        are visible on the orderbook.

        Args:
            active_offers: List of offer dicts. Accepts both our internal
            format (keys: 'trade_id', 'offer_bech32') and raw Sage wallet
            dicts (keys: 'offer_id', 'offer') so the /api/dexie/repost
            endpoint works correctly regardless of where the offer list
            came from.
        """
        count = 0
        for offer in active_offers:
            # Support both our internal DB format and raw Sage wallet dicts
            bech32 = offer.get("offer_bech32") or offer.get("offer") or ""
            trade_id = offer.get("trade_id") or offer.get("offer_id") or ""

            if bech32 and trade_id:
                self.queue_post(bech32, trade_id, force=True)
                count += 1

        if count > 0:
            log_event("info", "dexie_repost_queued",
                      f"Queued {count} active offers for Dexie repost")

    # -------------------------------------------------------------------
    # Tracking queries
    # -------------------------------------------------------------------

    def get_dexie_id(self, trade_id: str) -> Optional[str]:
        """Get Dexie offer ID for a given trade_id."""
        return self._trade_dexie_map.get(trade_id)

    def get_dexie_link(self, trade_id: str) -> Optional[str]:
        """Get full Dexie URL for a given trade_id."""
        dexie_id = self._trade_dexie_map.get(trade_id)
        if dexie_id:
            return f"https://dexie.space/offers/{dexie_id}"
        return None

    def get_stats(self) -> Dict:
        """Get posting statistics (thread-safe snapshot).

        F53 (2026-04-09): the `total_posted` / `total_failed` /
        `total_skipped` / `fingerprints_cached` counters are all
        in-memory and SESSION-SCOPED — they reset every time the bot
        process restarts. `tracked_mappings`, however, is hydrated from
        the offers table at startup (bot_loop.py:2385), so it includes
        Dexie IDs persisted from previous sessions.

        This naming mismatch confused operators: the diagnostics panel
        would show "0 posted, 47 tracked" right after a restart even
        though the bot clearly had 47 live offers. The new keys below
        keep backward compat (old fields still present) while adding
        clear session-scoped aliases and a hydration marker.
        """
        with self._lock:
            tracked = len(self._trade_dexie_map)
            return {
                # Legacy field names kept for backward compatibility.
                "total_posted": self._total_posted,
                "total_failed": self._total_failed,
                "total_skipped": self._total_skipped,
                "queue_size": len(self._queue),
                "tracked_mappings": tracked,
                "fingerprints_cached": len(self._posted_fingerprints),
                # F53: clarified labels. The session counters reset on
                # restart; tracked_mappings is hydrated from the DB on
                # startup (see bot_loop.py:2385).
                "session_posted": self._total_posted,
                "session_failed": self._total_failed,
                "session_skipped": self._total_skipped,
                "known_mappings": tracked,
                "hydrated_from_db": tracked > self._total_posted,
            }

    # -------------------------------------------------------------------
    # F37 (2026-04-08): Dexie v3 historical trades — real trade-flow data.
    # The bot's volatility scaling currently estimates volatility from
    # Tibet reserve drift snapshots. This endpoint gives us actual trade
    # prices with timestamps so we can compute REAL variance, fill rate,
    # and trade size distribution for the trading pair.
    # -------------------------------------------------------------------

    def fetch_v3_historical_trades(self, ticker_id: str,
                                   limit: int = 50,
                                   force: bool = False) -> Optional[List[Dict]]:
        """Fetch recent historical trades for a Dexie ticker pair.

        Cached for v3_trades_ttl_secs to avoid hammering the API.
        Use force=True to bypass the cache.

        Args:
            ticker_id: Dexie ticker (e.g. "MZ_XCH")
            limit: Max trades to return (Dexie caps this around 100)
            force: Skip the cache

        Returns: list of trade dicts, or None on error
        """
        if not ticker_id:
            return None
        if time.time() < self._rate_limited_until:
            return None

        # Cache check
        if not force:
            cached = self._v3_trades_cache.get(ticker_id)
            if cached and (time.time() - cached["fetched_at"]) < self._v3_trades_ttl_secs:
                return cached["trades"]

        url = f"{cfg.DEXIE_API_BASE.rstrip('/')}/v3/prices/historical_trades"
        params = {"ticker_id": ticker_id, "limit": int(limit)}

        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 429:
                self._rate_limited_until = time.time() + 60
                log_event("warning", "dexie_v3_rate_limited",
                          "Dexie v3 historical trades — rate limited")
                return None
            if r.status_code != 200:
                log_event("debug", "dexie_v3_trades_http_error",
                          f"v3 historical_trades returned HTTP {r.status_code}")
                return None
            data = r.json()
        except Exception as e:
            log_event("debug", "dexie_v3_trades_error",
                      f"v3 historical_trades fetch failed: {e}")
            return None

        # Dexie v3 returns either a list directly or a dict with "trades" key
        trades = data if isinstance(data, list) else data.get("trades", [])
        if not isinstance(trades, list):
            return None

        # Cache it
        self._v3_trades_cache[ticker_id] = {
            "trades": trades,
            "fetched_at": time.time(),
        }
        log_event("debug", "dexie_v3_trades_fetched",
                  f"Cached {len(trades)} v3 historical trade(s) for {ticker_id}")
        return trades

    def compute_v3_trade_metrics(self, ticker_id: str,
                                 hours: float = 24.0) -> Optional[Dict]:
        """Compute volatility / fill-rate metrics from cached v3 trades.

        Returns dict with:
            trades_in_window: int
            trades_per_hour: float
            mean_price: Decimal
            price_stdev_pct: float (relative volatility, %)
            min_price: Decimal
            max_price: Decimal
            high_low_pct: float (full range as %)
        """
        from decimal import Decimal as _D
        from statistics import stdev as _stdev

        trades = self.fetch_v3_historical_trades(ticker_id) or []
        if not trades:
            return None

        # Each trade has price + timestamp; tolerate slight schema variation
        cutoff = time.time() - (hours * 3600)
        prices: List[_D] = []
        for tr in trades:
            try:
                ts = float(tr.get("timestamp") or tr.get("time") or 0)
                if ts < cutoff:
                    continue
                price_raw = tr.get("price") or tr.get("price_xch") or tr.get("avg_price")
                if price_raw is None:
                    continue
                prices.append(_D(str(price_raw)))
            except Exception:
                continue

        if len(prices) < 2:
            return None

        try:
            mean_p = sum(prices) / _D(len(prices))
            std_p = _D(str(_stdev([float(p) for p in prices])))
            min_p = min(prices)
            max_p = max(prices)
            return {
                "trades_in_window": len(prices),
                "trades_per_hour": float(len(prices) / hours),
                "mean_price": mean_p,
                "price_stdev_pct": float(std_p / mean_p * _D("100")) if mean_p > 0 else 0.0,
                "min_price": min_p,
                "max_price": max_p,
                "high_low_pct": float((max_p - min_p) / mean_p * _D("100")) if mean_p > 0 else 0.0,
            }
        except Exception as e:
            log_event("debug", "v3_metrics_compute_error",
                      f"Failed to compute v3 trade metrics: {e}")
            return None

    # -------------------------------------------------------------------
    # F38 (2026-04-08): Dexie v3 pairs — pair selector enrichment.
    # Returns the active trading pairs with summary stats (volume, last
    # price, etc.) so the pair selector can show real activity instead
    # of just "wallet has this CAT".
    # -------------------------------------------------------------------

    def fetch_v3_pairs(self, force: bool = False) -> Optional[List[Dict]]:
        """Return cached Dexie v3 pairs list.

        Cached for v3_pairs_ttl_secs.
        Use force=True to bypass.
        """
        if not force and self._v3_pairs_cache is not None:
            if (time.time() - self._v3_pairs_fetched_at) < self._v3_pairs_ttl_secs:
                return self._v3_pairs_cache.get("pairs")

        if time.time() < self._rate_limited_until:
            return None

        url = f"{cfg.DEXIE_API_BASE.rstrip('/')}/v3/prices/pairs"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 429:
                self._rate_limited_until = time.time() + 60
                return None
            if r.status_code != 200:
                log_event("debug", "dexie_v3_pairs_http_error",
                          f"v3 pairs returned HTTP {r.status_code}")
                return None
            data = r.json()
        except Exception as e:
            log_event("debug", "dexie_v3_pairs_error",
                      f"v3 pairs fetch failed: {e}")
            return None

        pairs = data if isinstance(data, list) else data.get("pairs", [])
        if not isinstance(pairs, list):
            return None

        self._v3_pairs_cache = {"pairs": pairs}
        self._v3_pairs_fetched_at = time.time()
        log_event("debug", "dexie_v3_pairs_fetched",
                  f"Cached {len(pairs)} Dexie v3 pair(s)")
        return pairs

    def get_v3_pair_stats(self, ticker_id: str) -> Optional[Dict]:
        """Lookup a single pair's stats from the cached v3 pairs list."""
        pairs = self.fetch_v3_pairs() or []
        norm = (ticker_id or "").upper()
        for p in pairs:
            try:
                pid = str(p.get("ticker_id") or p.get("ticker") or "").upper()
                if pid == norm:
                    return p
            except Exception:
                continue
        return None

    # -------------------------------------------------------------------
    # Housekeeping
    # -------------------------------------------------------------------

    def prune_mappings(self, active_trade_ids: set):
        """Remove stale entries from tracking maps.

        Called periodically to prevent unbounded growth.
        """
        with self._lock:
            # Prune trade_dexie_map
            stale = set(self._trade_dexie_map.keys()) - active_trade_ids
            for tid in stale:
                del self._trade_dexie_map[tid]

            # Cap fingerprints at 400
            max_fps = 400
            if len(self._posted_fingerprints) > max_fps:
                # Clear all fingerprints — they'll get re-added on next post
                # (which will succeed as a force=True re-post anyway)
                old_len = len(self._posted_fingerprints)
                self._posted_fingerprints.clear()
                log_event("debug", "dexie_fingerprints_cleared",
                          f"Cleared {old_len} fingerprints (exceeded {max_fps} cap)")

        if stale:
            log_event("debug", "dexie_pruned",
                      f"Pruned {len(stale)} stale Dexie mappings")

        # Cap module-level offer detail cache
        global _offer_detail_cache, _offer_detail_cache_at
        if len(_offer_detail_cache) > 500:
            _offer_detail_cache.clear()
            _offer_detail_cache_at.clear()

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _fingerprint(offer_bech32: str) -> str:
        """SHA256 fingerprint of offer bech32 string."""
        return hashlib.sha256(offer_bech32.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_json(resp) -> dict:
        """Safely parse JSON response."""
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text[:500]}


def get_offer_detail(dexie_id: str, timeout: int = 8,
                     cache_ttl_secs: int = 15) -> Optional[Dict]:
    """Fetch Dexie detail for a specific posted offer."""
    if not dexie_id:
        return None

    dexie_id = str(dexie_id).strip()
    now = time.time()
    cached = _offer_detail_cache.get(dexie_id)
    cached_at = _offer_detail_cache_at.get(dexie_id, 0.0)
    if cached and (now - cached_at) < max(1, int(cache_ttl_secs)):
        return dict(cached)

    url = f"{cfg.DEXIE_API_BASE.rstrip('/')}/v1/offers/{dexie_id}"
    headers = {
        "accept": "application/json",
        "x-bot-tag": getattr(cfg, "BOT_TAG", "market-maker"),
    }

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            log_event("warning", "dexie_rate_limited",
                      f"Dexie GET 429 on offer detail {dexie_id[:16]}...")
            return None
        resp.raise_for_status()
        data = resp.json()
        offer = data.get("offer") if isinstance(data, dict) else None
        if isinstance(offer, dict):
            _offer_detail_cache[dexie_id] = dict(offer)
            _offer_detail_cache_at[dexie_id] = now
            return dict(offer)
    except Exception as e:
        log_event("debug", "dexie_offer_detail_failed",
                  f"Could not fetch Dexie detail for {dexie_id[:16]}...: {e}")

    return None

