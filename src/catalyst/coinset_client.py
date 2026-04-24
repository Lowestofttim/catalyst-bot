"""Fast coin queries via the public Coinset API with wallet RPC fallback

Mirrors Chia's full-node RPC as a cloud service so coin lookups go from
seconds (local wallet RPC) down to roughly a hundred milliseconds. The
client caches the wallet's puzzle hashes on startup, then queries Coinset
by puzzle hash; if Coinset is unreachable it falls back transparently to
wallet RPC. Sage wallets skip the puzzle-hash cache and go straight to
Sage RPC.

Key responsibilities:
    - On-startup puzzle-hash discovery for non-Sage wallets
    - Spendable-coin / coin-record queries by puzzle hash
    - Fee-estimate helpers, block-record and additions-and-removals lookups
    - Fill-verification fallback when Spacescan is unavailable

Enabled via COINSET_ENABLED=true in .env. All write operations (signing,
cancelling) always go through the local wallet RPC.
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
    "User-Agent": "CATalyst/2.0",
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
        self._total_queries: int = 0  # legacy: only counted get_spendable_coins
        self._last_coinset_time_ms: float = 0
        # F53 (2026-04-09): a catch-all counter that fires on EVERY Coinset
        # HTTP request, not just get_spendable_coins. This is the counter
        # that reflects real usage for Sage wallets, where the puzzle-hash
        # code path is skipped but other Coinset APIs (coin lookups, block
        # additions, fill verification) are heavily used. Broken down by
        # method so the diagnostics panel shows where the traffic goes.
        self._api_calls_total: int = 0
        self._api_calls_by_method: dict = {}
        self._api_errors_total: int = 0

        # Health tracking
        self._coinset_healthy: bool = True
        self._consecutive_failures: int = 0

        # Rate limit cooldown (epoch time until which we skip Coinset calls)
        self._rate_limited_until: float = 0.0

    # -------------------------------------------------------------------
    # F53 (2026-04-09): Stats helpers
    # -------------------------------------------------------------------

    def _record_api_call(self, method: str) -> None:
        """Count a Coinset API call by method name. Call this at the start
        of every network-fronting method (before the requests.post) so the
        diagnostics panel reflects real usage regardless of whether the
        puzzle-hash code path was exercised."""
        try:
            with self._lock:
                self._api_calls_total += 1
                self._api_calls_by_method[method] = (
                    self._api_calls_by_method.get(method, 0) + 1
                )
        except Exception:
            pass  # stats must never break the caller

    def _record_api_error(self) -> None:
        """Count an exception or non-2xx response from Coinset."""
        try:
            with self._lock:
                self._api_errors_total += 1
        except Exception:
            pass

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

        self._record_api_call("get_coin_records_by_puzzle_hashes")
        r = requests.post(
            url, json=payload,
            headers=COINSET_HEADERS,
            timeout=timeout
        )

        if r.status_code == 429:
            try:
                retry_after = min(int(r.headers.get("Retry-After", "60")), 300)
            except (ValueError, TypeError):
                retry_after = 60
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

        self._record_api_call("get_coin_record_by_name")
        try:
            r = requests.post(
                url, json=payload,
                headers=COINSET_HEADERS,
                timeout=timeout
            )

            if r.status_code == 429:
                try:
                    retry_after = min(int(r.headers.get("Retry-After", "60")), 300)
                except (ValueError, TypeError):
                    retry_after = 60
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
    # F34 (2026-04-08): Fill verification fallback via Coinset.
    # When Spacescan is rate-limited, down, or out of budget, we still
    # need to confirm whether a disappeared offer's locked coin was
    # actually spent on-chain. Coinset mirrors the full Chia node so
    # /get_coin_record_by_name returns spent_block_index for any coin
    # at near-zero cost — strictly better than NO verification.
    # -------------------------------------------------------------------

    def verify_coin_spent_on_chain(self, coin_id: str) -> Optional[bool]:
        """Verify whether a coin has been spent on-chain via Coinset.

        Used as a fallback when Spacescan is unavailable. Spacescan
        provides richer info (recipient address, etc.) but costs budget;
        Coinset is free, fast, and always available — it just doesn't
        tell us WHO received the proceeds.

        Returns:
            True  = coin is spent on-chain (treat as fill candidate;
                    caller still uses bot_cancelled set + wallet status
                    gates to filter out cancellations and topups)
            False = coin is unspent (definitely NOT a fill — phantom)
            None  = could not determine (Coinset down or coin not found)
        """
        if not getattr(cfg, "COINSET_ENABLED", True):
            return None
        if not coin_id:
            return None

        # Strip 0x prefix — Coinset expects bare hex on /get_coin_record_by_name
        normalised = str(coin_id).lower()
        if normalised.startswith("0x"):
            normalised = normalised[2:]

        record = self.get_coin_by_name("0x" + normalised)
        if not record:
            return None

        # Coin record's spent_block_index > 0 means spent
        try:
            spent_idx = int(record.get("spent_block_index", 0) or 0)
        except (TypeError, ValueError):
            spent_idx = 0
        spent = spent_idx > 0
        log_event(
            "debug",
            "coinset_verify_spent",
            f"Coinset spent check for {normalised[:16]}...: spent={spent} "
            f"(spent_block_index={spent_idx})",
        )
        return spent

    # -------------------------------------------------------------------
    # F41 (2026-04-08): Block record by height — bridge helper used by
    # the post-fill enrichment path. Spacescan and Coinset both return
    # `spent_block_index` (a height) for spent coins, but
    # /get_additions_and_removals takes a `header_hash`. This helper
    # converts height → header_hash so the enrichment can chain calls.
    # -------------------------------------------------------------------

    def get_block_record_by_height(self, height: int) -> Optional[Dict]:
        """Return the Chia block record at a given height.

        Returns the full block record dict (or None on error). The most
        useful field is `header_hash`, which downstream calls like
        get_additions_and_removals require.
        """
        if not getattr(cfg, "COINSET_ENABLED", True):
            return None
        if height is None or int(height) <= 0:
            return None
        if time.time() < self._rate_limited_until:
            return None

        api_url = getattr(cfg, "COINSET_API_URL", "https://api.coinset.org")
        timeout = getattr(cfg, "COINSET_TIMEOUT", 5)
        url = f"{api_url.rstrip('/')}/get_block_record_by_height"

        self._record_api_call("get_block_record_by_height")
        try:
            r = requests.post(
                url, json={"height": int(height)},
                headers=COINSET_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                try:
                    retry_after = min(int(r.headers.get("Retry-After", "60")), 300)
                except (ValueError, TypeError):
                    retry_after = 60
                self._rate_limited_until = time.time() + retry_after
                return None
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    return data.get("block_record")
        except Exception as e:
            log_event("debug", "coinset_block_record_error",
                      f"get_block_record_by_height({height}) failed: {e}")
        return None

    # -------------------------------------------------------------------
    # F35 (2026-04-08): Block-level reconstruction.
    # /get_additions_and_removals returns every coin created and every
    # coin destroyed in a specific block — gives us the complete picture
    # of a fill block in one call. Use case: after a fill is detected,
    # query the spent block to find the new receive coin paired with
    # the spent locked coin, all without polling the wallet.
    # -------------------------------------------------------------------

    def get_additions_and_removals(self, header_hash: str) -> Optional[Dict]:
        """Return additions + removals for a specific block.

        Args:
            header_hash: The block's header hash (hex, may include 0x prefix)

        Returns dict with keys "additions" (list of new coin records)
        and "removals" (list of spent coin records), or None on error.
        """
        if not getattr(cfg, "COINSET_ENABLED", True):
            return None
        if not header_hash:
            return None
        if time.time() < self._rate_limited_until:
            return None

        api_url = getattr(cfg, "COINSET_API_URL", "https://api.coinset.org")
        timeout = getattr(cfg, "COINSET_TIMEOUT", 5)
        url = f"{api_url.rstrip('/')}/get_additions_and_removals"

        # Coinset expects header_hash WITHOUT 0x prefix in some calls,
        # WITH in others — pass as-is and let the upstream normalise.
        payload = {"header_hash": header_hash}

        self._record_api_call("get_additions_and_removals")
        try:
            r = requests.post(
                url, json=payload,
                headers=COINSET_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                try:
                    retry_after = min(int(r.headers.get("Retry-After", "60")), 300)
                except (ValueError, TypeError):
                    retry_after = 60
                self._rate_limited_until = time.time() + retry_after
                return None
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    return {
                        "additions": data.get("additions") or [],
                        "removals": data.get("removals") or [],
                    }
        except Exception as e:
            log_event("debug", "coinset_additions_removals_error",
                      f"get_additions_and_removals failed for {header_hash[:16]}...: {e}")
        return None

    # -------------------------------------------------------------------
    # F36 (2026-04-08): Hint-based coin lookup.
    # CAT memos (hints) are searchable. After a fill we know the hint
    # used on the receive coin, so /get_coin_records_by_hint finds the
    # new coin instantly without waiting for the wallet poll cycle.
    # -------------------------------------------------------------------

    def get_coin_records_by_hint(
        self,
        hint: str,
        include_spent_coins: bool = False,
        start_height: Optional[int] = None,
        end_height: Optional[int] = None,
    ) -> Optional[List[Dict]]:
        """Return coin records tagged with a specific hint.

        Args:
            hint: The hint hex string (with or without 0x prefix)
            include_spent_coins: If False, only return unspent coins
            start_height: Optional minimum block height filter
            end_height: Optional maximum block height filter

        Returns list of coin records or None on error.
        """
        if not getattr(cfg, "COINSET_ENABLED", True):
            return None
        if not hint:
            return None
        if time.time() < self._rate_limited_until:
            return None

        api_url = getattr(cfg, "COINSET_API_URL", "https://api.coinset.org")
        timeout = getattr(cfg, "COINSET_TIMEOUT", 5)
        url = f"{api_url.rstrip('/')}/get_coin_records_by_hint"

        # Coinset accepts hint with or without 0x prefix — be explicit
        normalised = str(hint).lower()
        if not normalised.startswith("0x"):
            normalised = "0x" + normalised

        payload = {
            "hint": normalised,
            "include_spent_coins": include_spent_coins,
        }
        if start_height is not None:
            payload["start_height"] = int(start_height)
        if end_height is not None:
            payload["end_height"] = int(end_height)

        self._record_api_call("get_coin_records_by_hint")
        try:
            r = requests.post(
                url, json=payload,
                headers=COINSET_HEADERS,
                timeout=timeout,
            )
            if r.status_code == 429:
                try:
                    retry_after = min(int(r.headers.get("Retry-After", "60")), 300)
                except (ValueError, TypeError):
                    retry_after = 60
                self._rate_limited_until = time.time() + retry_after
                return None
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    return data.get("coin_records") or []
        except Exception as e:
            log_event("debug", "coinset_records_by_hint_error",
                      f"get_coin_records_by_hint failed for {normalised[:16]}...: {e}")
        return None

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
        """Get query statistics for monitoring/GUI.

        F53 (2026-04-09): now returns `api_calls_total` and
        `api_calls_by_method`, which count EVERY Coinset HTTP request
        regardless of whether the puzzle-hash code path was used. For
        Sage wallets where puzzle-hash initialization is skipped, the
        legacy `total_queries` field stays at 0 but `api_calls_total`
        reflects real usage (block lookups, fill verification, hint
        searches, etc.).
        """
        # Determine operating mode for the diagnostics label. Sage users
        # don't initialize the puzzle-hash cache (see bot_loop.py:2830),
        # so `initialized=False` is expected and should not be reported
        # as an error.
        try:
            from config import cfg as _cfg
            wallet_type = str(getattr(_cfg, "WALLET_TYPE", "sage") or "sage").lower().strip()
        except Exception:
            wallet_type = "sage"
        if wallet_type == "sage":
            mode = "sage_compat"  # direct API use, no puzzle-hash cache
        elif self._initialized:
            mode = "initialized"  # full puzzle-hash cache active
        else:
            mode = "pending_init"  # Chia wallet, init not yet run

        return {
            "mode": mode,
            "initialized": self._initialized,
            "puzzle_hashes_cached": sum(len(v) for v in self._puzzle_hashes.values()),
            # Legacy counter — only fires on get_spendable_coins path
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
            # F53: real usage counters covering every Coinset HTTP call
            "api_calls_total": self._api_calls_total,
            "api_calls_by_method": dict(self._api_calls_by_method),
            "api_errors_total": self._api_errors_total,
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

