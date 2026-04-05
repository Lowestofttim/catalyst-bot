"""
V2 Dexie Manager — Dexie Exchange Posting & Tracking

Handles posting offers to the Dexie.space orderbook, tracking which
offers have been posted, and preventing duplicate posts.

Replaces V1's dexie_post.py (file-based state) with database-backed
tracking via database.py.

Usage:
    from dexie_manager import DexieManager
    manager = DexieManager()
    manager.queue_post(offer_bech32, trade_id)
    manager.flush_queue()
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

        def _process_one(item):
            """Post a single item — used by both sequential and parallel paths."""
            offer_bech32 = item["offer"]
            trade_id = item.get("trade_id")
            force = item.get("force", False)
            return self._post_single(offer_bech32, trade_id, force)

        # Parallel posting for large batches (startup repost)
        if len(batch) > 10:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            workers = min(8, len(batch))  # Up to 8 concurrent posts
            log_event("info", "dexie_flush_parallel",
                      f"Parallel flush: {len(batch)} offers with {workers} workers")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_process_one, item): item for item in batch}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        if result.get("skipped"):
                            skipped += 1
                            self._total_skipped += 1
                        elif result.get("success"):
                            posted += 1
                            self._total_posted += 1
                        else:
                            failed += 1
                            self._total_failed += 1
                    except Exception as e:
                        log_event("warning", "dexie_parallel_error",
                                  f"Parallel Dexie post failed: {e}")
                        failed += 1
                        self._total_failed += 1
        else:
            # Sequential for small batches (normal cycle)
            for item in batch:
                result = _process_one(item)
                if result.get("skipped"):
                    skipped += 1
                    self._total_skipped += 1
                elif result.get("success"):
                    posted += 1
                    self._total_posted += 1
                else:
                    failed += 1
                    self._total_failed += 1

        summary = {"posted": posted, "failed": failed, "skipped": skipped}
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
                        log_event("warning", "dexie_no_id",
                                  f"Dexie returned 2xx but no offer ID found "
                                  f"(trade: {trade_id[:16]}..., response keys: {list(data.keys()) if isinstance(data, dict) else 'non-dict'})")

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
                    retry_after = min(int(r.headers.get("Retry-After", "30")), 120)
                    self._rate_limited_until = time.time() + retry_after
                    last_err = f"HTTP 429 rate limited (backing off {retry_after}s)"
                    log_event("warning", "dexie_rate_limited",
                              f"Dexie returned 429 — backing off {retry_after}s")
                    break  # Don't burn remaining retries

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

        log_event("error", "dexie_post_failed",
                  f"Failed to post to Dexie after {cfg.DEXIE_POST_RETRIES + 1} attempts: {last_err}")
        return {"success": False, "error": last_err}

    # -------------------------------------------------------------------
    # Repost active offers (recovery after outage)
    # -------------------------------------------------------------------

    def repost_active_offers(self, active_offers: List[Dict]):
        """Re-post all currently active offers to Dexie.

        Used after startup or Dexie outage to ensure all our offers
        are visible on the orderbook.

        Args:
            active_offers: List of offer dicts with 'trade_id' and 'offer_bech32'
        """
        count = 0
        for offer in active_offers:
            bech32 = offer.get("offer_bech32", "")
            trade_id = offer.get("trade_id", "")

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
        """Get posting statistics."""
        return {
            "total_posted": self._total_posted,
            "total_failed": self._total_failed,
            "total_skipped": self._total_skipped,
            "queue_size": len(self._queue),
            "tracked_mappings": len(self._trade_dexie_map),
            "fingerprints_cached": len(self._posted_fingerprints),
        }

    # -------------------------------------------------------------------
    # Housekeeping
    # -------------------------------------------------------------------

    def prune_mappings(self, active_trade_ids: set):
        """Remove stale entries from tracking maps.

        Called periodically to prevent unbounded growth.
        """
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
