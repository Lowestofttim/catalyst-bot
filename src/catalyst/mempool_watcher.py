"""Pre-emptive price intelligence from TibetSwap reserves and the mempool

`MempoolWatcher` runs two background pollers on a 5-second cadence: one
watches TibetSwap pool reserves to emit `price_move` signals when a swap
confirms, and one scans the Coinset mempool for spends of the known
Tibet pool coin to emit `imminent_swap` and `fill_imminent` warnings up
to ~18 s before block confirmation. Consumed by `bot_loop`, this lets
the bot react to arb activity far sooner than the main 30-second cycle
would allow. `compute_coin_id()` implements the Chia coin-ID hash
`sha256(parent_coin_info + puzzle_hash + amount_bytes)` used to match
mempool entries against the pool coin.

Key responsibilities:
    - Detect confirmed AMM swaps via reserve-delta polling
    - Detect pending AMM swaps and fills via mempool scanning
    - Emit typed signals with direction and magnitude for the bot loop
    - Provide the Chia coin-ID hash helper used for mempool matching
"""

from __future__ import annotations

import hashlib
import threading
import time
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from database import log_event

# ---------------------------------------------------------------------------
# Coin-ID computation (Chia CLVM encoding)
# ---------------------------------------------------------------------------

def _encode_amount(amount: int) -> bytes:
    """Encode a Chia coin amount to bytes (big-endian, minimal, unsigned)."""
    if amount == 0:
        return b"\x00"
    n = amount
    result = []
    while n:
        result.append(n & 0xFF)
        n >>= 8
    result.reverse()
    # Ensure unsigned (no high bit set) by prepending 0x00 if needed
    if result[0] & 0x80:
        result = [0x00] + result
    return bytes(result)


def compute_coin_id(parent_coin_info: str, puzzle_hash: str, amount: int) -> str:
    """Compute a Chia coin ID from its three components.

    coin_id = sha256(parent_coin_info_bytes + puzzle_hash_bytes + amount_bytes)
    """
    try:
        parent_bytes = bytes.fromhex(parent_coin_info.removeprefix("0x"))
        puzzle_bytes = bytes.fromhex(puzzle_hash.removeprefix("0x"))
        amount_bytes = _encode_amount(amount)
        return hashlib.sha256(parent_bytes + puzzle_bytes + amount_bytes).hexdigest()
    except Exception:
        return ""


def _coin_amount(coin: Dict) -> Optional[int]:
    try:
        raw = coin.get("amount")
        if isinstance(raw, str):
            raw = raw.strip()
            if raw.lower().startswith("0x"):
                return int(raw, 16)
        return int(raw)
    except Exception:
        return None


def _coin_puzzle_hash(coin: Dict) -> str:
    try:
        return str(coin.get("puzzle_hash") or "").removeprefix("0x").lower()
    except Exception:
        return ""


def _find_same_puzzle_amount_change(
    removals: List[Dict],
    additions: List[Dict],
    current_amount: Optional[int],
) -> Optional[Dict]:
    if current_amount is None:
        return None

    matching_removals = [
        coin for coin in removals
        if isinstance(coin, dict) and _coin_amount(coin) == current_amount
    ]
    if not matching_removals:
        return None

    best = None
    best_delta_abs = -1
    for removal in matching_removals:
        puzzle_hash = _coin_puzzle_hash(removal)
        if not puzzle_hash:
            continue
        for addition in additions:
            if not isinstance(addition, dict):
                continue
            if _coin_puzzle_hash(addition) != puzzle_hash:
                continue
            new_amount = _coin_amount(addition)
            if new_amount is None or new_amount == current_amount:
                continue
            delta = new_amount - current_amount
            delta_abs = abs(delta)
            if delta_abs > best_delta_abs:
                best_delta_abs = delta_abs
                best = {
                    "puzzle_hash": puzzle_hash,
                    "old_amount": current_amount,
                    "new_amount": new_amount,
                    "delta": delta,
                }
    return best


def infer_pending_pool_move(
    item: Dict,
    current_xch_reserve: Optional[int],
    current_tok_reserve: Optional[int],
) -> Optional[Dict]:
    """Infer pending Tibet price direction from reserve coin children.

    Tibet's pool singleton amount is 1 mojo, so the spent pool coin alone
    only tells us a swap is pending. In the same mempool item, the XCH
    reserve coin is also spent and recreated under the same puzzle hash with
    the projected new reserve amount. That delta tells us the side to protect
    before the block confirms.
    """
    if not isinstance(item, dict):
        return None
    try:
        old_xch = int(current_xch_reserve)
    except Exception:
        return None
    try:
        old_tok = int(current_tok_reserve) if current_tok_reserve is not None else None
    except Exception:
        old_tok = None

    removals = item.get("removals") or []
    additions = item.get("additions") or []
    if not isinstance(removals, list) or not isinstance(additions, list):
        return None

    xch_move = _find_same_puzzle_amount_change(removals, additions, old_xch)
    if not xch_move:
        return None

    new_xch = int(xch_move["new_amount"])
    delta_xch = int(xch_move["delta"])
    if delta_xch == 0:
        return None

    token_move = _find_same_puzzle_amount_change(removals, additions, old_tok)
    new_tok = int(token_move["new_amount"]) if token_move else None

    direction = "up" if delta_xch > 0 else "down"
    confidence = "xch_reserve_only"
    magnitude_source = "xch_reserve_pct"
    token_projection_rejected = False
    try:
        xch_signed_pct = (Decimal(delta_xch) / Decimal(old_xch)) * Decimal("100")
        signed_pct = xch_signed_pct
        if old_tok and new_tok and old_tok > 0 and new_tok > 0:
            old_price = Decimal(old_xch) / Decimal(old_tok)
            new_price = Decimal(new_xch) / Decimal(new_tok)
            projected_signed_pct = ((new_price - old_price) / old_price) * Decimal("100")
            projected_mag = abs(projected_signed_pct)
            xch_mag = abs(xch_signed_pct)
            plausible_ceiling = max(Decimal("0.5"), xch_mag * Decimal("6"))
            same_direction = (
                projected_signed_pct == 0
                or (projected_signed_pct > 0 and xch_signed_pct > 0)
                or (projected_signed_pct < 0 and xch_signed_pct < 0)
            )
            if same_direction and projected_mag <= plausible_ceiling:
                signed_pct = projected_signed_pct
                confidence = "xch_and_token_reserves"
                magnitude_source = "projected_price_pct"
            else:
                token_projection_rejected = True
        magnitude_pct = abs(signed_pct)
    except Exception:
        return None

    result = {
        "direction": direction,
        "magnitude_pct": round(float(magnitude_pct), 4),
        "signed_pct": round(float(signed_pct), 4),
        "source": "mempool_projected_reserves",
        "confidence": confidence,
        "magnitude_source": magnitude_source,
        "old_xch_reserve": old_xch,
        "new_xch_reserve": new_xch,
        "delta_xch": delta_xch,
    }
    if old_tok is not None:
        result["old_tok_reserve"] = old_tok
    if new_tok is not None:
        result["new_tok_reserve"] = new_tok
        result["delta_tok"] = new_tok - old_tok
    if token_projection_rejected:
        result["token_projection_rejected"] = True
    return result


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class MempoolWatcher:
    """Background watcher for TibetSwap pool reserve changes.

    Thread-safe. Designed to be started once at bot startup and queried
    each bot cycle via get_pending_signals().
    """

    TIBET_POLL_INTERVAL = 3      # seconds between Tibet reserve checks (tightened 2026-04-22 — confirmed-move defensive cancel is now our primary defense since imminent_swap no longer mass-cancels, so we want direction info fast)
    MEMPOOL_POLL_INTERVAL = 2    # seconds between Coinset mempool checks (tightened 2026-04-22 after a fill slipped between 5s polls)
    SIGNAL_MAX_AGE = 120         # drop stale signals older than 2 minutes
    MIN_SIGNAL_MAGNITUDE = 0.05  # ignore moves < 0.05% to suppress noise

    def __init__(
        self,
        pair_id: str,
        asset_id: str,
        cat_decimals: int = 3,
        coinset_url: str = "https://api.coinset.org",
        coinset_timeout: int = 5,
        tibet_url: str = "https://api.v2.tibetswap.io",
        wake_callback=None,
        full_node_url: str = "",
        full_node_cert_path: str = "",
        full_node_key_path: str = "",
        full_node_timeout: int = 5,
    ):
        self._pair_id = pair_id
        self._asset_id = asset_id
        self._cat_decimals = cat_decimals
        self._coinset_url = coinset_url.rstrip("/")
        self._coinset_timeout = coinset_timeout
        self._tibet_url = tibet_url.rstrip("/")
        self._wake_callback = wake_callback  # callable to wake bot loop immediately

        # Local Chia full-node config (optional). When a URL + cert + key
        # are all provided, the mempool watcher queries the local node
        # directly via its RPC instead of Coinset's indexed snapshot.
        # Zero indexer latency; also not subject to Coinset rate limits.
        self._full_node_url = (full_node_url or "").rstrip("/")
        self._full_node_cert_path = full_node_cert_path or ""
        self._full_node_key_path = full_node_key_path or ""
        self._full_node_timeout = int(full_node_timeout or 5)
        self._full_node_active: bool = bool(
            self._full_node_url
            and self._full_node_cert_path
            and self._full_node_key_path
        )

        self._lock = threading.Lock()
        self._signals: List[Dict] = []
        self._stop_event = threading.Event()

        # API call counters (session-scoped)
        self._coinset_api_calls: int = 0
        self._tibet_api_calls: int = 0
        # Count full-node RPC calls separately so diagnostics can show
        # which source served the mempool queries this session.
        self._full_node_api_calls: int = 0
        # Fill-miss counters: how often a confirmed fill was pre-warned in
        # mempool vs slipped through between polls. Updated by was_fill_warned().
        self._fill_warn_hits: int = 0
        self._fill_warn_misses: int = 0

        # Known state of the Tibet pool
        self._pool_coin_id: Optional[str] = None   # last_coin_id_on_chain
        self._xch_reserve: Optional[int] = None
        self._tok_reserve: Optional[int] = None

        # Debounce: don't emit repeated mempool signals for same pool coin
        self._mempool_warned_coin_id: Optional[str] = None

        # Offer coin watching — set of normalised coin IDs (no 0x prefix, lowercase)
        # Updated by bot_loop after every offer state change.
        # When any of these appear in mempool removals a fill_imminent signal fires.
        self._watched_offer_coins: set = set()
        # Debounce: track which offer coins have already fired this session
        # so we don't spam signals while the tx is pending confirmation.
        # F10 (2026-04-08): timestamped to allow TTL-based expiry. If the
        # bot misses the wake (e.g. cycle was already running) the entry
        # would otherwise stay until set_watched_offer_coins removes the
        # coin entirely. With the TTL, an unconsumed entry naturally
        # decays so a future re-emission can fire if the coin is still
        # being watched. Map: coin_id -> first_warned_timestamp.
        self._fill_warned_coin_ids: Dict[str, float] = {}
        self._fill_warn_ttl_secs: float = 300.0  # 5 minutes

        # Background threads
        self._reserve_thread: Optional[threading.Thread] = None
        self._mempool_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _wake_bot(self) -> None:
        """Fire the wake callback to interrupt the bot loop sleep."""
        cb = self._wake_callback
        if cb:
            try:
                cb()
            except Exception:
                pass

    def start(self) -> None:
        """Start background polling threads."""
        if self._reserve_thread and self._reserve_thread.is_alive():
            return  # already running

        self._stop_event.clear()
        self._reserve_thread = threading.Thread(
            target=self._reserve_poll_loop,
            name="MempoolWatcher-reserves",
            daemon=True,
        )
        self._mempool_thread = threading.Thread(
            target=self._mempool_poll_loop,
            name="MempoolWatcher-mempool",
            daemon=True,
        )
        self._reserve_thread.start()
        self._mempool_thread.start()
        log_event("info", "mempool_watcher_started",
                  f"MempoolWatcher started for pair {self._pair_id[:16]}...")

    def stop(self) -> None:
        """Signal background threads to stop."""
        self._stop_event.set()

    def is_running(self) -> bool:
        return bool(self._reserve_thread and self._reserve_thread.is_alive())

    def get_pending_signals(self) -> List[Dict]:
        """Return and clear accumulated price-movement signals.

        Call this once per bot cycle. Signals older than SIGNAL_MAX_AGE
        are automatically discarded.
        """
        now = time.time()
        with self._lock:
            fresh = [
                s for s in self._signals
                if (now - s["timestamp"]) < self.SIGNAL_MAX_AGE
            ]
            self._signals = []
            return fresh

    def get_current_reserves(self) -> Optional[Tuple[int, int]]:
        """Return the last known (xch_reserve, tok_reserve), or None."""
        with self._lock:
            if self._xch_reserve is None or self._tok_reserve is None:
                return None
            return self._xch_reserve, self._tok_reserve

    def was_fill_warned(self, coin_id: str) -> bool:
        """Return True if we fired a fill_imminent signal for this coin id
        before it was reported as confirmed. Also updates the hit/miss
        counters so effectiveness is visible via diagnostics.

        Called by the fill-detection path right before logging ``offer_filled``.
        The counters only reflect fills that passed through this method —
        Sage/cleanup-path recoveries skip it and are not counted either way.
        """
        if not coin_id:
            return False
        norm = coin_id.removeprefix("0x").lower()
        with self._lock:
            hit = norm in self._fill_warned_coin_ids
            if hit:
                self._fill_warn_hits += 1
            else:
                self._fill_warn_misses += 1
            return hit

    def set_watched_offer_coins(self, coin_ids: set) -> None:
        """Register the set of coin IDs locked by currently open offers.

        Call this after every offer state change (create, cancel, fill).
        coin_ids may include '0x' prefixes — they are normalised internally.

        When any of these coins appear as a removal in the Coinset mempool,
        a 'fill_imminent' signal is emitted so the bot can wake early and
        run fill detection before the 30-second cycle fires.

        Coins that are no longer in the new set are removed from the debounce
        cache so a fresh signal fires if they somehow reappear (shouldn't
        happen in practice, but keeps state clean).
        """
        normalised = {c.removeprefix("0x").lower() for c in coin_ids if c}
        with self._lock:
            self._watched_offer_coins = normalised
            # Remove debounce entries for coins no longer being watched
            # F10: dict-based debounce — keep only entries for coins still
            # in the watch set, AND drop entries that have aged past TTL.
            now = time.time()
            self._fill_warned_coin_ids = {
                cid: ts
                for cid, ts in self._fill_warned_coin_ids.items()
                if cid in normalised and (now - ts) < self._fill_warn_ttl_secs
            }

    # ------------------------------------------------------------------
    # Reserve polling thread
    # ------------------------------------------------------------------

    def _reserve_poll_loop(self) -> None:
        """Poll Tibet for reserve changes. Fires 'price_move' signals."""
        import requests as _req
        session = _req.Session()

        # First fetch to initialise baseline (no signal on first run)
        self._fetch_and_update_reserves(session, emit_signal=False)

        while not self._stop_event.wait(self.TIBET_POLL_INTERVAL):
            self._fetch_and_update_reserves(session, emit_signal=True)

    def _fetch_and_update_reserves(self, session, emit_signal: bool = True) -> None:
        """Fetch current reserves from Tibet API and emit a signal if changed."""
        try:
            self._tibet_api_calls += 1
            resp = session.get(
                f"{self._tibet_url}/pairs",
                params={"skip": 0, "limit": 200},
                timeout=(3, 8),
            )
            if resp.status_code != 200:
                return
            pairs = resp.json()
            if not isinstance(pairs, list):
                return

            for pair in pairs:
                if pair.get("pair_id") != self._pair_id:
                    continue
                new_coin = str(pair.get("last_coin_id_on_chain") or "")
                new_xch = int(pair.get("xch_reserve") or 0)
                new_tok = int(pair.get("token_reserve") or 0)

                with self._lock:
                    old_xch = self._xch_reserve
                    old_tok = self._tok_reserve
                    old_coin = self._pool_coin_id

                    # Always update known pool coin for mempool thread
                    self._pool_coin_id = new_coin
                    self._xch_reserve = new_xch
                    self._tok_reserve = new_tok

                # Emit a signal only when reserves actually changed
                if emit_signal and old_xch is not None and (
                    new_xch != old_xch or new_tok != old_tok
                ):
                    self._emit_price_move_signal(
                        old_xch, old_tok, new_xch, new_tok,
                        old_coin, new_coin,
                    )
                break

        except Exception as _e:
            # F81: surface errors at debug so persistent failures are visible
            # via the trace log without spamming the events feed.
            try:
                log_event("debug", "mempool_watcher_reserve_fetch_failed",
                          f"Tibet reserve fetch failed: {_e}")
            except Exception:
                pass

    def _emit_price_move_signal(
        self,
        old_xch: int, old_tok: int,
        new_xch: int, new_tok: int,
        old_coin: str, new_coin: str,
    ) -> None:
        """Calculate price direction + magnitude and push a price_move signal."""
        try:
            factor = 10 ** self._cat_decimals
            old_price = Decimal(old_xch) / Decimal(old_tok) / Decimal(factor)
            new_price = Decimal(new_xch) / Decimal(new_tok) / Decimal(factor)

            if old_price <= 0:
                return

            pct_change = float((new_price - old_price) / old_price * 100)
            if abs(pct_change) < self.MIN_SIGNAL_MAGNITUDE:
                return  # noise filter

            direction = "up" if pct_change > 0 else "down"
            signal = {
                "type": "price_move",
                "direction": direction,
                "magnitude_pct": round(abs(pct_change), 4),
                "signed_pct": round(pct_change, 4),
                "source": "confirmed_reserves",
                "timestamp": time.time(),
                "old_xch_reserve": old_xch,
                "old_tok_reserve": old_tok,
                "new_xch_reserve": new_xch,
                "new_tok_reserve": new_tok,
                "delta_xch": new_xch - old_xch,
                "delta_tok": new_tok - old_tok,
                "old_price_xch": float(old_price),
                "new_price_xch": float(new_price),
                "pair_id": self._pair_id,
                "pool_coin_changed": (old_coin != new_coin),
            }
            with self._lock:
                self._signals.append(signal)
            self._wake_bot()

            log_event("info", "mempool_price_move",
                      f"Pool reserve change: {direction} {abs(pct_change):.3f}% "
                      f"(XCH {old_xch}→{new_xch}, TOK {old_tok}→{new_tok})")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Mempool polling thread
    # ------------------------------------------------------------------

    def _mempool_poll_loop(self) -> None:
        """Poll Coinset mempool for pending spends of the pool coin."""
        import requests as _req
        session = _req.Session()

        # Stagger slightly behind reserve thread
        time.sleep(2)

        while not self._stop_event.wait(self.MEMPOOL_POLL_INTERVAL):
            self._check_mempool_for_pool_spend(session)

    def _check_mempool_for_pool_spend(self, session) -> None:
        """Scan Coinset mempool for pending spends of the Tibet pool coin
        and any of the bot's currently open offer coins.

        A single API call serves both checks — the full mempool is fetched
        once and scanned for all watched IDs in the same pass.
        """
        with self._lock:
            current_pool_coin = self._pool_coin_id
            already_warned = self._mempool_warned_coin_id
            watched_offers = set(self._watched_offer_coins)
            # F10: prune expired entries before reading. A coin warned >TTL ago
            # is no longer considered "already warned" — fresh signal can fire.
            now = time.time()
            ttl = self._fill_warn_ttl_secs
            expired = [cid for cid, ts in self._fill_warned_coin_ids.items()
                       if (now - ts) >= ttl]
            for cid in expired:
                self._fill_warned_coin_ids.pop(cid, None)
            already_warned_fills = set(self._fill_warned_coin_ids.keys())

        # Skip the fetch entirely if there's nothing new to watch
        pool_check_needed = bool(
            current_pool_coin and current_pool_coin != already_warned
        )
        offer_check_needed = bool(watched_offers - already_warned_fills)
        if not pool_check_needed and not offer_check_needed:
            return

        # Pick the source: local full node if configured, else Coinset.
        if self._full_node_active:
            target_url = f"{self._full_node_url}/get_all_mempool_items"
            cert = (self._full_node_cert_path, self._full_node_key_path)
            timeout = (3, self._full_node_timeout)
            source_name = "full_node"
        else:
            target_url = f"{self._coinset_url}/get_all_mempool_items"
            cert = None
            timeout = (3, self._coinset_timeout)
            source_name = "coinset"

        try:
            if source_name == "full_node":
                self._full_node_api_calls += 1
                resp = session.post(
                    target_url,
                    json={},
                    headers={
                        "content-type": "application/json",
                        "User-Agent": "CATalyst/2.0",
                    },
                    cert=cert,
                    verify=False,  # Chia uses self-signed certs for local RPC
                    timeout=timeout,
                )
            else:
                self._coinset_api_calls += 1
                resp = session.post(
                    target_url,
                    json={},
                    headers={
                        "content-type": "application/json",
                        "User-Agent": "CATalyst/2.0",
                    },
                    timeout=timeout,
                )
            if resp.status_code != 200:
                return
            data = resp.json()
            if not data.get("success"):
                return

            items = data.get("mempool_items") or {}
            if isinstance(items, dict):
                items = list(items.values())

        except Exception as _e:
            # F81: surface errors at debug so persistent failures are visible
            try:
                log_event("debug", "mempool_watcher_poll_failed",
                          f"Mempool poll ({source_name}) failed: {_e}")
            except Exception:
                pass
            return  # network error, retry next interval

        # Single-pass scan: compute each removal's coin ID and check against
        # the Tibet pool coin AND the set of watched offer coins.
        pool_found = False
        pool_item: Optional[Dict] = None
        fill_hits: List[str] = []  # offer coin IDs found in mempool

        unwatched_offers = watched_offers - already_warned_fills
        current_pool_coin_norm = str(current_pool_coin or "").removeprefix("0x").lower()

        for item in items:
            if not isinstance(item, dict):
                continue
            removals = item.get("removals") or []
            for removal in removals:
                if not isinstance(removal, dict):
                    continue
                parent = str(removal.get("parent_coin_info") or "")
                ph = str(removal.get("puzzle_hash") or "")
                amount = _coin_amount(removal)
                if not parent or not ph or amount is None:
                    continue
                computed_id = compute_coin_id(parent, ph, amount)

                if pool_check_needed and not pool_found:
                    if computed_id == current_pool_coin_norm:
                        pool_found = True
                        pool_item = item

                if unwatched_offers and computed_id in unwatched_offers:
                    fill_hits.append(computed_id)
                    unwatched_offers.discard(computed_id)

            # Early exit once all targets found
            if (not pool_check_needed or pool_found) and not unwatched_offers:
                break

        # --- Emit Tibet swap signal ---
        if pool_found:
            with self._lock:
                self._mempool_warned_coin_id = current_pool_coin
                xch_res = self._xch_reserve
                tok_res = self._tok_reserve

            signal = {
                "type": "imminent_swap",
                "direction": "unknown",
                "magnitude_pct": 0.0,
                "source": "mempool_detected",
                "timestamp": time.time(),
                "pool_coin_id": current_pool_coin,
                "current_xch_reserve": xch_res,
                "current_tok_reserve": tok_res,
            }
            projected = infer_pending_pool_move(pool_item or {}, xch_res, tok_res)
            if projected:
                signal.update(projected)
                signal["current_xch_reserve"] = xch_res
                signal["current_tok_reserve"] = tok_res
            with self._lock:
                self._signals.append(signal)
            self._wake_bot()

            if projected:
                log_event("info", "mempool_swap_detected",
                          f"PENDING swap detected in mempool for pool coin "
                          f"{current_pool_coin[:16]}... - projected "
                          f"{projected['direction']} {projected['magnitude_pct']:.3f}% "
                          f"(XCH {xch_res}->{projected['new_xch_reserve']}); "
                          "pre-confirm protection window open.")
            else:
                log_event("info", "mempool_swap_detected",
                          f"PENDING swap detected in mempool for pool coin "
                          f"{current_pool_coin[:16]}... — pre-emptive sniper window open.")

        # --- Emit fill_imminent signals for our offer coins ---
        if fill_hits:
            with self._lock:
                _now_ts = time.time()
                for cid in fill_hits:
                    self._fill_warned_coin_ids[cid] = _now_ts

            for coin_id in fill_hits:
                signal = {
                    "type": "fill_imminent",
                    "direction": "unknown",
                    "magnitude_pct": 0.0,
                    "source": "mempool_detected",
                    "timestamp": time.time(),
                    "coin_id": coin_id,
                }
                with self._lock:
                    self._signals.append(signal)
                self._wake_bot()

                log_event("info", "mempool_fill_detected",
                          f"Offer coin {coin_id[:16]}... appears in mempool — "
                          f"fill likely pending, waking bot early.")


# ---------------------------------------------------------------------------
# Singleton factory for bot_loop integration
# ---------------------------------------------------------------------------

_watcher_instance: Optional[MempoolWatcher] = None
_watcher_lock = threading.Lock()


def get_or_create_watcher(
    pair_id: str,
    asset_id: str,
    cat_decimals: int = 3,
    wake_callback=None,
) -> MempoolWatcher:
    """Return the singleton MempoolWatcher, creating it if needed.

    Called from bot_loop once the pair is known.
    """
    global _watcher_instance
    with _watcher_lock:
        if _watcher_instance is None:
            from config import cfg
            coinset_url = str(getattr(cfg, "COINSET_API_URL", "https://api.coinset.org") or "")
            coinset_timeout = int(getattr(cfg, "COINSET_TIMEOUT", 5) or 5)
            tibet_url = str(getattr(cfg, "TIBET_API_BASE", "https://api.v2.tibetswap.io") or "https://api.v2.tibetswap.io")
            fn_enabled = bool(getattr(cfg, "FULL_NODE_ENABLED", False))
            fn_url = str(getattr(cfg, "FULL_NODE_RPC_URL", "") or "") if fn_enabled else ""
            fn_cert = str(getattr(cfg, "FULL_NODE_CERT_PATH", "") or "") if fn_enabled else ""
            fn_key = str(getattr(cfg, "FULL_NODE_KEY_PATH", "") or "") if fn_enabled else ""
            fn_timeout = int(getattr(cfg, "FULL_NODE_TIMEOUT", 5) or 5)
            _watcher_instance = MempoolWatcher(
                pair_id=pair_id,
                asset_id=asset_id,
                cat_decimals=cat_decimals,
                coinset_url=coinset_url,
                coinset_timeout=coinset_timeout,
                tibet_url=tibet_url,
                wake_callback=wake_callback,
                full_node_url=fn_url,
                full_node_cert_path=fn_cert,
                full_node_key_path=fn_key,
                full_node_timeout=fn_timeout,
            )
            if _watcher_instance._full_node_active:
                try:
                    log_event("info", "mempool_watcher_full_node_source",
                              f"Mempool watcher using local full node at "
                              f"{fn_url} (cert configured). Zero-indexer-lag "
                              f"mempool poll active.")
                except Exception:
                    pass
        elif wake_callback and not _watcher_instance._wake_callback:
            # Attach callback if watcher already exists but had no callback
            _watcher_instance._wake_callback = wake_callback
        return _watcher_instance


def start_watcher(pair_id: str, asset_id: str, cat_decimals: int = 3,
                  wake_callback=None) -> MempoolWatcher:
    """Convenience: get-or-create and start the watcher."""
    w = get_or_create_watcher(pair_id, asset_id, cat_decimals,
                              wake_callback=wake_callback)
    if not w.is_running():
        w.start()
    return w


def stop_watcher() -> None:
    """Stop the singleton watcher (called on bot shutdown)."""
    global _watcher_instance
    with _watcher_lock:
        if _watcher_instance:
            _watcher_instance.stop()
