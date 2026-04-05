"""
V3 Coinset Client — Fast Cloud Coin Queries via Coinset API

Coinset.org mirrors Chia's full node RPC as a fast cloud API.
Coin queries drop from 2-5 seconds (wallet RPC) to ~100ms.

How it works:
  1. On startup: ask wallet RPC for our coins → extract puzzle hashes
  2. After startup: query Coinset by puzzle hash instead of wallet RPC
  3. If Coinset is down: transparently fall back to wallet RPC
  4. Wallet RPC still handles all write operations (signing, cancelling)

This module is opt-in via COINSET_ENABLED=true in .env.

Usage:
    from coinset_client import CoinsetClient
    client = CoinsetClient()
    client.initialize_puzzle_hashes()  # Call once after wallet ready
    coins = client.get_spendable_coins(wallet_id)  # Fast query

Requires: Internet access to api.coinset.org
"""

import time
import requests
import threading
from typing import Dict, List, Optional, Set

from config import cfg
from database import log_event

# Coinset.org uses Cloudflare protection — requests without User-Agent get 403/1010
COINSET_HEADERS = {
    "content-type": "application/json",
    "User-Agent": "ChiaMarketMaker/2.0",
}


class CoinsetClient:
    """Fast coin queries via Coinset cloud API with wallet RPC fallback.

    Key responsibilities:
    - Cache puzzle hashes from wallet RPC (one-time startup cost)
    - Query Coinset by puzzle hash for coin records (fast)
    - Fall back to wallet RPC if Coinset is unreachable
    - Track hit/miss/fallback statistics for monitoring
    """

    def __init__(self):
        # Puzzle hash cache: wallet_id → set of puzzle hashes
        self._puzzle_hashes: Dict[int, Set[str]] = {}

        # Whether we've been initialized (puzzle hashes loaded)
        self._initialized: bool = False

        # Lock for thread safety
        self._lock = threading.Lock()

        # Stats
        self._coinset_hits: int = 0
        self._coinset_misses: int = 0
        self._fallback_count: int = 0
        self._total_queries: int = 0
        self._last_coinset_time_ms: float = 0

        # Health tracking
        self._coinset_healthy: bool = True
        self._consecutive_failures: int = 0
        self._last_success_time: float = 0

        # Rate limit cooldown (epoch time until which we skip Coinset calls)
        self._rate_limited_until: float = 0.0

    # -------------------------------------------------------------------
    # Initialization — extract puzzle hashes from wallet
    # -------------------------------------------------------------------

    def initialize_puzzle_hashes(self) -> bool:
        """Extract puzzle hashes from wallet RPC (one-time startup cost).

        Queries the wallet for all spendable coins in XCH and CAT wallets,
        then caches the unique puzzle hashes for Coinset queries.

        Returns True if at least one puzzle hash was found.
        """
        if not getattr(cfg, "COINSET_ENABLED", True):
            return False

        try:
            from wallet import get_spendable_coins_rpc

            puzzle_hashes = {}

            # XCH wallet
            xch_wallet_id = cfg.WALLET_ID_XCH
            xch_result = get_spendable_coins_rpc(xch_wallet_id)
            xch_phs = self._extract_puzzle_hashes(xch_result)
            if xch_phs:
                puzzle_hashes[xch_wallet_id] = xch_phs

            # CAT wallet
            cat_wallet_id = cfg.CAT_WALLET_ID
            cat_result = get_spendable_coins_rpc(cat_wallet_id)
            cat_phs = self._extract_puzzle_hashes(cat_result)
            if cat_phs:
                puzzle_hashes[cat_wallet_id] = cat_phs

            with self._lock:
                self._puzzle_hashes = puzzle_hashes
                self._initialized = bool(puzzle_hashes)

            total_phs = sum(len(v) for v in puzzle_hashes.values())
            log_event("info", "coinset_init",
                      f"Cached {total_phs} puzzle hashes from "
                      f"{len(puzzle_hashes)} wallets for Coinset queries")

            return self._initialized

        except Exception as e:
            log_event("warning", "coinset_init_failed",
                      f"Failed to initialize Coinset puzzle hashes: {e}")
            return False

    def refresh_puzzle_hashes(self) -> bool:
        """Re-scan wallet for new puzzle hashes.

        Called periodically (e.g., after fills) since new coins may
        arrive at addresses we haven't cached yet.
        """
        return self.initialize_puzzle_hashes()

    # -------------------------------------------------------------------
    # Core query: get spendable coins (Coinset with fallback)
    # -------------------------------------------------------------------

    def get_spendable_coins(self, wallet_id: int) -> Optional[Dict]:
        """Get spendable coins — tries Coinset first, falls back to wallet RPC.

        Returns the same format as wallet.get_spendable_coins_rpc() so
        it's a drop-in replacement.

        Args:
            wallet_id: Chia wallet ID (1 for XCH, N for CAT)

        Returns:
            dict with coin records in the same format as wallet RPC,
            or None on total failure.
        """
        self._total_queries += 1

        # If not initialized or no puzzle hashes for this wallet, fall back
        if not self._initialized or wallet_id not in self._puzzle_hashes:
            return self._fallback_wallet_rpc(wallet_id, "no_puzzle_hashes")

        # Try Coinset
        puzzle_hashes = self._puzzle_hashes.get(wallet_id, set())
        if not puzzle_hashes:
            return self._fallback_wallet_rpc(wallet_id, "empty_puzzle_hashes")

        try:
            start = time.time()
            coins = self._query_coinset(puzzle_hashes)
            elapsed_ms = (time.time() - start) * 1000

            if coins is not None:
                self._coinset_hits += 1
                self._last_coinset_time_ms = elapsed_ms
                self._last_success_time = time.time()

                # Reset health tracking on success
                if not self._coinset_healthy:
                    self._coinset_healthy = True
                    self._consecutive_failures = 0
                    log_event("info", "coinset_recovered",
                              "Coinset connection restored")

                log_event("debug", "coinset_query",
                          f"Coinset returned {len(coins)} coins "
                          f"in {elapsed_ms:.0f}ms (wallet {wallet_id})")

                # Format response like wallet RPC
                return self._format_as_wallet_response(coins)

        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures <= 3:
                log_event("warning", "coinset_error",
                          f"Coinset query failed: {e}")

            if self._coinset_healthy and self._consecutive_failures >= 3:
                self._coinset_healthy = False
                log_event("warning", "coinset_unhealthy",
                          "Coinset appears offline — falling back to wallet RPC")

        # Coinset failed — fall back to wallet RPC
        return self._fallback_wallet_rpc(wallet_id, "coinset_failed")

    # -------------------------------------------------------------------
    # Coinset API query
    # -------------------------------------------------------------------

    def _query_coinset(self, puzzle_hashes: Set[str]) -> Optional[List[Dict]]:
        """Query Coinset API for coin records by puzzle hash.

        Coinset mirrors Chia's full node RPC, so the endpoint and
        payload format are identical to get_coin_records_by_puzzle_hashes.

        Returns list of unspent coin records, or None on error.
        """
        # Respect 429 cooldown
        if time.time() < self._rate_limited_until:
            return None

        api_url = getattr(cfg, "COINSET_API_URL", "https://api.coinset.org")
        timeout = getattr(cfg, "COINSET_TIMEOUT", 5)

        # Use the batch endpoint for multiple puzzle hashes
        url = f"{api_url.rstrip('/')}/get_coin_records_by_puzzle_hashes"
        payload = {
            "puzzle_hashes": list(puzzle_hashes),
            "include_spent_coins": False,
        }

        r = requests.post(
            url, json=payload,
            headers=COINSET_HEADERS,
            timeout=timeout
        )

        if r.status_code == 429:
            retry_after = min(int(r.headers.get("Retry-After", "60")), 300)
            self._rate_limited_until = time.time() + retry_after
            log_event("warning", "coinset_rate_limited",
                      f"Coinset returned 429 — backing off {retry_after}s")
            return None

        if r.status_code != 200:
            log_event("debug", "coinset_http_error",
                      f"Coinset returned HTTP {r.status_code}")
            return None

        data = r.json()

        if not data.get("success"):
            log_event("debug", "coinset_api_error",
                      f"Coinset returned success=false: "
                      f"{data.get('error', 'unknown')}")
            return None

        return data.get("coin_records", [])

    def get_coin_by_name(self, coin_name: str) -> Optional[Dict]:
        """Look up a specific coin by its name/ID via Coinset.

        Args:
            coin_name: The coin ID (hex string)

        Returns coin record dict or None.
        """
        if not getattr(cfg, "COINSET_ENABLED", True):
            return None

        # Respect 429 cooldown
        if time.time() < self._rate_limited_until:
            return None

        api_url = getattr(cfg, "COINSET_API_URL", "https://api.coinset.org")
        timeout = getattr(cfg, "COINSET_TIMEOUT", 5)

        url = f"{api_url.rstrip('/')}/get_coin_record_by_name"
        payload = {"name": coin_name}

        try:
            r = requests.post(
                url, json=payload,
                headers=COINSET_HEADERS,
                timeout=timeout
            )

            if r.status_code == 429:
                retry_after = min(int(r.headers.get("Retry-After", "60")), 300)
                self._rate_limited_until = time.time() + retry_after
                log_event("warning", "coinset_rate_limited",
                          f"Coinset returned 429 on coin lookup — backing off {retry_after}s")
                return None

            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    return data.get("coin_record")

        except Exception as e:
            log_event("debug", "coinset_lookup_error",
                      f"Coinset coin lookup failed: {e}")

        return None

    # -------------------------------------------------------------------
    # Mempool watching — early fill detection
    # -------------------------------------------------------------------

    def watch_coins(self, coin_ids: List[str]) -> None:
        """Register coin IDs (offer locked coins) for mempool watching.

        Called by fill_tracker / bot_loop after posting offers.
        The background check_mempool_for_spends() will monitor these
        for pending spend transactions.

        Args:
            coin_ids: List of coin name hex strings (without 0x prefix)
        """
        if not getattr(cfg, "ENABLE_MEMPOOL_WATCH", False):
            return
        with self._lock:
            if not hasattr(self, "_watched_coins"):
                self._watched_coins: Set[str] = set()
            normalised = {
                cid.lower().lstrip("0x") for cid in coin_ids if cid
            }
            self._watched_coins.update(normalised)

    def unwatch_coins(self, coin_ids: List[str]) -> None:
        """Remove coin IDs from mempool watch list (after fill confirmed)."""
        with self._lock:
            if not hasattr(self, "_watched_coins"):
                return
            normalised = {cid.lower().lstrip("0x") for cid in coin_ids if cid}
            self._watched_coins -= normalised

    def clear_watched_coins(self) -> None:
        """Clear all watched coins (e.g., on bot restart or mass cancel)."""
        with self._lock:
            if hasattr(self, "_watched_coins"):
                self._watched_coins = set()

    def check_mempool_for_spends(self) -> List[str]:
        """Check mempool for pending spends on all watched coins.

        Queries Coinset's get_mempool_items_by_coin_name for each watched
        coin. Returns the list of coin IDs that have a pending spend in
        the mempool (i.e., about to be filled/swept).

        This gives ~30-50s earlier fill detection than waiting for block
        confirmation. The caller (fill_tracker / bot_loop) can use this to:
          - Set _force_requote early so replacement offers are ready faster
          - Log that a sweep is in progress
          - Distinguish arb sweeps (multiple coins hit simultaneously) from
            retail fills (single coin)

        Returns:
            List of coin_id strings that are currently in the mempool.
            Empty list if mempool watch is disabled, nothing is watched,
            or Coinset is unreachable.
        """
        if not getattr(cfg, "ENABLE_MEMPOOL_WATCH", False):
            return []

        # Respect 429 cooldown
        if time.time() < self._rate_limited_until:
            return []

        with self._lock:
            if not hasattr(self, "_watched_coins") or not self._watched_coins:
                return []
            coins_to_check = list(self._watched_coins)

        if not coins_to_check:
            return []

        api_url = getattr(cfg, "COINSET_API_URL", "https://api.coinset.org")
        timeout = getattr(cfg, "COINSET_TIMEOUT", 5)
        url = f"{api_url.rstrip('/')}/get_mempool_items_by_coin_name"

        pending: List[str] = []

        # Cap coins per check to prevent long stalls (rotate through watched set)
        MAX_PER_CHECK = 10
        if len(coins_to_check) > MAX_PER_CHECK:
            coins_to_check = coins_to_check[:MAX_PER_CHECK]

        def _check_one(coin_id: str) -> Optional[str]:
            try:
                r = requests.post(
                    url,
                    json={"coin_name": coin_id},
                    headers=COINSET_HEADERS,
                    timeout=min(timeout, 3),  # Short timeout for best-effort check
                )
                if r.status_code == 429:
                    retry_after = min(int(r.headers.get("Retry-After", "60")), 300)
                    self._rate_limited_until = time.time() + retry_after
                    log_event("warning", "coinset_rate_limited",
                              f"Coinset mempool 429 — backing off {retry_after}s")
                    return None
                if r.status_code != 200:
                    return None
                data = r.json()
                if not data.get("success"):
                    return None
                mempool_items = data.get("mempool_items", {})
                if mempool_items:
                    log_event("debug", "mempool_spend_detected",
                              f"Mempool: pending spend on coin {coin_id[:16]}... "
                              f"({len(mempool_items)} item(s))",
                              data={"coin_id": coin_id,
                                    "mempool_tx_count": len(mempool_items)})
                    return coin_id
            except requests.Timeout:
                pass
            except Exception as e:
                log_event("debug", "mempool_check_error",
                          f"Mempool check error for coin {coin_id[:16]}...: {e}")
            return None

        # Parallel checks to avoid O(N * timeout) blocking
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(5, len(coins_to_check))) as pool:
            futures = {pool.submit(_check_one, cid): cid for cid in coins_to_check}
            for future in as_completed(futures, timeout=15):
                try:
                    result = future.result()
                    if result:
                        pending.append(result)
                except Exception:
                    pass

        return pending

    def get_watched_coin_count(self) -> int:
        """Return number of coins currently being watched."""
        with self._lock:
            if not hasattr(self, "_watched_coins"):
                return 0
            return len(self._watched_coins)

    # -------------------------------------------------------------------
    # Wallet RPC fallback
    # -------------------------------------------------------------------

    def _fallback_wallet_rpc(self, wallet_id: int, reason: str) -> Optional[Dict]:
        """Fall back to wallet RPC for coin queries.

        This is the safety net — if Coinset is down or not initialized,
        we use the slower but reliable wallet RPC.
        """
        if not getattr(cfg, "COINSET_FALLBACK_WALLET", True):
            self._coinset_misses += 1
            return None

        self._fallback_count += 1

        try:
            from wallet import get_spendable_coins_rpc
            result = get_spendable_coins_rpc(wallet_id)
            log_event("debug", "coinset_fallback",
                      f"Used wallet RPC fallback (reason: {reason}, "
                      f"wallet: {wallet_id})")
            return result

        except Exception as e:
            self._coinset_misses += 1
            log_event("warning", "coinset_fallback_failed",
                      f"Both Coinset and wallet RPC failed: {e}")
            return None

    # -------------------------------------------------------------------
    # Health check
    # -------------------------------------------------------------------

    def check_health(self) -> Dict:
        """Quick health check — can we reach Coinset?

        Returns: {healthy: bool, url: str, latency_ms: float, error: str|None}
        """
        api_url = getattr(cfg, "COINSET_API_URL", "https://api.coinset.org")
        timeout = getattr(cfg, "COINSET_TIMEOUT", 5)

        try:
            start = time.time()
            # Use get_blockchain_state as a lightweight ping
            r = requests.post(
                f"{api_url.rstrip('/')}/get_blockchain_state",
                json={},
                headers=COINSET_HEADERS,
                timeout=timeout
            )
            latency = (time.time() - start) * 1000

            if r.status_code == 200 and r.json().get("success"):
                return {
                    "healthy": True,
                    "url": api_url,
                    "latency_ms": round(latency, 1),
                    "error": None,
                }

            return {
                "healthy": False,
                "url": api_url,
                "latency_ms": round(latency, 1),
                "error": f"HTTP {r.status_code}",
            }

        except requests.ConnectionError:
            return {"healthy": False, "url": api_url,
                    "latency_ms": 0, "error": "Connection refused"}
        except requests.Timeout:
            return {"healthy": False, "url": api_url,
                    "latency_ms": timeout * 1000, "error": "Timeout"}
        except Exception as e:
            return {"healthy": False, "url": api_url,
                    "latency_ms": 0, "error": str(e)}

    # -------------------------------------------------------------------
    # Stats
    # -------------------------------------------------------------------

    def get_stats(self) -> Dict:
        """Get query statistics for monitoring/GUI."""
        return {
            "initialized": self._initialized,
            "puzzle_hashes_cached": sum(len(v) for v in self._puzzle_hashes.values()),
            "total_queries": self._total_queries,
            "coinset_hits": self._coinset_hits,
            "coinset_misses": self._coinset_misses,
            "fallback_count": self._fallback_count,
            "last_coinset_time_ms": round(self._last_coinset_time_ms, 1),
            "healthy": self._coinset_healthy,
            "consecutive_failures": self._consecutive_failures,
            "hit_rate_pct": round(
                self._coinset_hits / max(self._total_queries, 1) * 100, 1
            ),
        }

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _extract_puzzle_hashes(rpc_result) -> Set[str]:
        """Extract unique puzzle hashes from a wallet RPC coin result.

        Works with the response from get_spendable_coins_rpc() which
        returns a dict with 'coin_records' (Chia format) or
        'confirmed_records' / 'unconfirmed_records' (some wallet versions).
        """
        puzzle_hashes = set()

        if not rpc_result or not isinstance(rpc_result, dict):
            return puzzle_hashes

        # Try standard format (coin_records list)
        records = rpc_result.get("coin_records", [])

        # Also try confirmed_records format
        if not records:
            records = rpc_result.get("confirmed_records", [])

        for record in records:
            coin = record.get("coin", record)  # Some formats nest under 'coin'
            ph = coin.get("puzzle_hash", "")
            if ph:
                puzzle_hashes.add(ph)

        return puzzle_hashes

    @staticmethod
    def _format_as_wallet_response(coinset_records: List[Dict]) -> Dict:
        """Convert Coinset response to wallet RPC format.

        Coinset returns full node format (coin_record with coin nested inside).
        We normalize it to match what get_spendable_coins_rpc() returns so
        the rest of the code doesn't need to know the difference.

        The key format from Coinset's full node mirror:
        {
            "coin_records": [
                {
                    "coin": {
                        "parent_coin_info": "0x...",
                        "puzzle_hash": "0x...",
                        "amount": 1000000000000
                    },
                    "confirmed_block_index": 12345,
                    "spent": false,
                    "spent_block_index": 0,
                    "timestamp": 1234567890
                }
            ]
        }

        We return this in a wrapper that matches the wallet RPC shape.
        """
        # Filter to only unspent coins
        unspent = [r for r in coinset_records if not r.get("spent", False)]

        return {
            "coin_records": unspent,
            "confirmed_records": unspent,
            "success": True,
            "_source": "coinset",
        }
