"""Background TibetSwap reserve polling and drift detection

`AMMMonitor` runs a background thread that polls the TibetSwap pair
endpoint on `AMM_POLL_INTERVAL_SECS` (default 30 s) for live
`xch_reserve` and `token_reserve` values. On each poll it compares the
derived AMM mid-price against the last quoted price, injects fresh
reserves into `price_engine._tibet_cache`, and invalidates that cache
when drift exceeds threshold so the next bot cycle requotes within one
poll interval. It also computes an arb-pressure score and exposes
proximity checks used by `offer_manager` to avoid posting inside the
TibetSwap arb range.

Key responsibilities:
    - Poll pool reserves on a configurable interval
    - Detect drift vs the last quoted price
    - Inject reserves into `price_engine` and invalidate its Tibet cache
    - Expose AMM state / price for the API and GUI
    - Provide buffer-proximity checks for offer placement
"""

import time
import threading
import requests
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict
from database import log_event


class AMMMonitor:
    """Background AMM reserve monitor with drift detection and cache invalidation.

    Thread-safe. All public methods can be called from any thread.
    The background polling thread only writes under _lock.
    """

    def __init__(self, price_engine=None):
        # Injected price_engine reference (for cache invalidation)
        self._price_engine = price_engine

        # Current cached AMM state (None until first successful poll)
        self._state: Optional[Dict] = None
        self._lock = threading.Lock()

        # Background thread control
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # HTTP session (reused across polls)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "CATalyst/4.0",
            "Accept": "application/json",
        })

        # Health tracking
        self._consecutive_failures = 0
        self._last_success_at: float = 0
        self._total_polls: int = 0
        self._failed_polls: int = 0

        # Last quoted price tracked for drift detection
        # Set by bot_loop via notify_quoted_price() each time it posts offers
        self._last_quoted_buy: Optional[Decimal] = None
        self._last_quoted_sell: Optional[Decimal] = None

        # F84 (2026-04-18): suppress repeated identical drift logs.
        # Without this the same 83bps drift event fires every 10s after a
        # requote because notify_quoted_price hasn't reset the baseline yet.
        # Track last logged drift bucket and only re-log when it CHANGES
        # by >5 bps OR drops below threshold (and re-emerges).
        self._last_drift_bucket: Optional[int] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start background polling thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="amm_monitor",
            daemon=True,
        )
        self._thread.start()
        log_event("info", "amm_monitor_start",
                  "AMM Monitor started — polling TibetSwap reserves")

    def stop(self) -> None:
        """Signal background thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------
    # Public accessors (called from main bot loop thread)
    # ------------------------------------------------------------------

    def get_amm_price(self) -> Optional[Decimal]:
        """Return cached AMM mid price (XCH per token). None if no data."""
        with self._lock:
            if self._state:
                return self._state.get("amm_price")
        return None

    def get_amm_state(self) -> Optional[Dict]:
        """Return full cached AMM state dict for API/GUI consumption."""
        with self._lock:
            return dict(self._state) if self._state else None

    def is_available(self) -> bool:
        """True if we have at least one successful poll."""
        with self._lock:
            return self._state is not None and self._state.get("available", False)

    def notify_quoted_price(self, buy_price: Optional[Decimal],
                            sell_price: Optional[Decimal]) -> None:
        """Called by bot_loop after posting offers — records the last quoted price
        so drift detection knows what we're comparing against."""
        with self._lock:
            if buy_price is not None:
                self._last_quoted_buy = buy_price
            if sell_price is not None:
                self._last_quoted_sell = sell_price

    def get_drift_bps(self) -> Optional[Decimal]:
        """Return how far the current AMM price has moved from the last quoted
        mid-price (average of buy/sell), in basis points. None if unknown.

        A large drift means our posted offers are now stale relative to the AMM
        and TibetSwap will sweep them unless we requote.
        """
        with self._lock:
            if not self._state:
                return None
            amm_price = self._state.get("amm_price")
            buy = self._last_quoted_buy
            sell = self._last_quoted_sell

        if not amm_price or amm_price <= 0:
            return None

        # Compute quoted mid from last buy/sell (average of the two sides)
        if buy and sell and buy > 0 and sell > 0:
            quoted_mid = (buy + sell) / Decimal("2")
        elif buy and buy > 0:
            quoted_mid = buy
        elif sell and sell > 0:
            quoted_mid = sell
        else:
            return None

        drift = abs(amm_price - quoted_mid) / quoted_mid * Decimal("10000")
        return drift

    def get_arb_pressure(self) -> float:
        """Return a 0.0–1.0 arb-pressure score based on price divergence and
        recent sweep activity.

        Interpretation:
            0.0–0.3  — low pressure (normal operation)
            0.3–0.6  — moderate pressure (consider widening spreads)
            0.6–0.9  — high pressure (arb window likely open)
            0.9–1.0  — critical (sweep imminent / just occurred)

        Score is composed of:
            • 60% — current drift from quoted price vs AMM_DRIFT_REQUOTE_BPS
            • 40% — recent sweep frequency from dynamic_amm_buffer
        """
        from config import cfg

        # ---- Component 1: price divergence (0.0–1.0) -------------------
        drift_score = 0.0
        try:
            drift = self.get_drift_bps()
            if drift is not None and drift >= 0:
                requote_bps = float(getattr(cfg, "AMM_DRIFT_REQUOTE_BPS", "40"))
                # Score saturates at 3× the requote threshold
                drift_score = min(1.0, float(drift) / (requote_bps * 3.0))
        except Exception:
            pass

        # ---- Component 2: sweep frequency (0.0–1.0) --------------------
        sweep_score = 0.0
        try:
            from dynamic_amm_buffer import _get_buffer_instance
            count = _get_buffer_instance().sweep_count_in_window()
            # Score: 0 sweeps→0, 1→0.33, 2→0.55, 3→0.70, 4→0.80, 5+→saturate
            sweep_score = min(1.0, 1.0 - (1.0 / (1.0 + count * 0.5)))
        except Exception:
            pass

        return round(0.6 * drift_score + 0.4 * sweep_score, 3)

    def get_arb_pressure_label(self) -> str:
        """Human-readable arb pressure level."""
        score = self.get_arb_pressure()
        if score < 0.3:  return "low"
        if score < 0.6:  return "moderate"
        if score < 0.9:  return "high"
        return "critical"

    def check_amm_buffer(self, offer_price: Decimal, side: str):
        """Check whether an offer price is safe to post (not inside arb range).

        Returns True  = safe to post (outside buffer).
        Returns False = unsafe (TibetSwap will likely arb this immediately).
        Returns None  = AMM data unavailable, cannot determine safety.

        Logic:
          BUY side:  if offer_price >= amm_price × (1 - buffer_bps/10000)
                     → we're willing to pay MORE than the AMM → instant arb
          SELL side: if offer_price <= amm_price × (1 + buffer_bps/10000)
                     → we're willing to accept LESS than the AMM → instant arb
        """
        from config import cfg
        if not getattr(cfg, "ENABLE_AMM_BUFFER", False):
            return True

        with self._lock:
            state = self._state

        if not state or not state.get("available"):
            return True  # No data — fail open (AMM guard inactive without data)

        amm_price = state.get("amm_price")
        if not amm_price or amm_price <= 0:
            return True  # No price — fail open

        try:
            base_bps = Decimal(str(getattr(cfg, "AMM_BUFFER_BPS", "30")))
            # Widen buffer after recent sweep activity (Tier 3: dynamic buffer)
            try:
                from dynamic_amm_buffer import get_buffer as _dyn_buf
                buffer_bps = _dyn_buf(base_bps)
            except Exception:
                buffer_bps = base_bps
            buffer_frac = buffer_bps / Decimal("10000")

            if side == "buy":
                # Our buy price must be BELOW amm × (1 - buffer)
                threshold = amm_price * (Decimal("1") - buffer_frac)
                within_buffer = offer_price >= threshold
            else:
                # Our sell price must be ABOVE amm × (1 + buffer)
                threshold = amm_price * (Decimal("1") + buffer_frac)
                within_buffer = offer_price <= threshold

            if within_buffer:
                distance_bps = abs(offer_price - amm_price) / amm_price * Decimal("10000")
                log_event("debug", "amm_buffer_guard",
                          f"AMM buffer: {side} offer {offer_price:.8f} is {distance_bps:.1f}bps "
                          f"from AMM {amm_price:.8f} (buffer={buffer_bps}bps) — SKIPPED",
                          data={"side": side, "offer_price": str(offer_price),
                                "amm_price": str(amm_price),
                                "distance_bps": str(distance_bps.quantize(Decimal("0.1")))})
                return False

            return True

        except (InvalidOperation, ZeroDivisionError):
            return True  # Calculation error — fail open

    def get_stats(self) -> Dict:
        """Return health/stats dict for monitoring."""
        with self._lock:
            state = dict(self._state) if self._state else {}

        drift = self.get_drift_bps()
        arb_pressure = self.get_arb_pressure()

        dyn_buffer_state: dict = {}
        try:
            from dynamic_amm_buffer import get_state as _dyn_state
            dyn_buffer_state = _dyn_state()
        except Exception:
            pass

        return {
            "available": state.get("available", False),
            "amm_price": str(state.get("amm_price", "")) if state.get("amm_price") else None,
            "xch_reserve": str(state.get("xch_reserve", "")) if state.get("xch_reserve") else None,
            "token_reserve": str(state.get("token_reserve", "")) if state.get("token_reserve") else None,
            "fetched_at": state.get("fetched_at", 0),
            "pair_id": state.get("pair_id", ""),
            "total_polls": self._total_polls,
            "failed_polls": self._failed_polls,
            "consecutive_failures": self._consecutive_failures,
            "last_success_ago_secs": round(time.time() - self._last_success_at, 1) if self._last_success_at else None,
            "drift_bps": str(drift.quantize(Decimal("0.1"))) if drift is not None else None,
            "arb_pressure": arb_pressure,
            "arb_pressure_label": self.get_arb_pressure_label(),
            "dynamic_buffer": dyn_buffer_state,
        }

    # ------------------------------------------------------------------
    # Background polling
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Background thread: poll AMM reserves every AMM_POLL_INTERVAL_SECS."""
        from config import cfg

        # Stagger startup slightly to avoid hammering APIs at bot start
        time.sleep(3)

        while not self._stop_event.is_set():
            try:
                self._do_poll()
            except Exception as e:
                log_event("debug", "amm_monitor_poll_error",
                          f"AMM Monitor poll error: {e}")

            interval = int(getattr(cfg, "AMM_POLL_INTERVAL_SECS", 30))
            self._stop_event.wait(timeout=interval)

    def _do_poll(self) -> None:
        """Single poll cycle: fetch reserves, update state, check drift."""
        from config import cfg

        pair_id = getattr(cfg, "TIBET_PAIR_ID", "").strip()
        if not pair_id:
            return  # Not configured — nothing to poll

        self._total_polls += 1

        state = self._fetch_pair(pair_id)
        if state is None:
            self._consecutive_failures += 1
            self._failed_polls += 1
            if self._consecutive_failures == 3:
                log_event("warning", "amm_monitor_unhealthy",
                          "AMM Monitor: 3 consecutive failures — TibetSwap API may be down")
            return

        # Success
        self._consecutive_failures = 0
        self._last_success_at = time.time()

        # Check drift before updating state (need old state for comparison)
        drift_bps = self._compute_drift_vs_old_state(state)

        with self._lock:
            self._state = state

        # Invalidate tibet price cache if drift is significant
        drift_threshold = Decimal(str(getattr(cfg, "AMM_DRIFT_REQUOTE_BPS", "40")))
        if drift_bps is not None and drift_bps >= drift_threshold:
            # F84: suppress repeated identical drift firings. The drift value
            # only changes when EITHER amm_price moves OR last_quoted_mid
            # moves (post-requote). When both stall (e.g. requote failed or
            # baseline reset wasn't called), the same value would fire every
            # 10s. Bucket to nearest 5 bps so small ticks don't all fire.
            current_bucket = int(round(float(drift_bps) / 5.0)) * 5
            should_log = (
                self._last_drift_bucket is None
                or abs(current_bucket - self._last_drift_bucket) >= 5
            )
            self._last_drift_bucket = current_bucket
            if should_log:
                log_event("info", "amm_drift_detected",
                          f"AMM price drifted {drift_bps:.1f}bps from last quoted mid "
                          f"— invalidating Tibet cache for fresh requote",
                          data={"drift_bps": str(drift_bps.quantize(Decimal("0.1"))),
                                "threshold_bps": str(drift_threshold),
                                "amm_price": str(state.get("amm_price", ""))})
            if self._price_engine is not None:
                try:
                    self._price_engine.invalidate_tibet_cache()
                except Exception:
                    pass
        elif drift_bps is not None and drift_bps < drift_threshold:
            # Drift dropped below threshold — clear bucket so next cross-up
            # logs again.
            self._last_drift_bucket = None

        # Also update the tibet_cache in price_engine with live reserve data
        # so the next get_price() call uses fresh values without a full /pairs fetch
        self._inject_into_tibet_cache(state, pair_id)

    def _fetch_pair(self, pair_id: str) -> Optional[Dict]:
        """Fetch single pair from TibetSwap API. Returns state dict or None.

        Implementation note: uses the `/pairs` list endpoint and filters
        for the target pair_id locally, rather than the `/pair/{id}`
        singular endpoint. TibetSwap's singular endpoint has been observed
        to time out even when the plural endpoint serves the same data
        fine; consolidating on `/pairs` (which is also what the rest of
        the codebase uses) gives us a consistent failure mode and fewer
        endpoints to worry about.
        """
        from config import cfg
        base = getattr(cfg, "TIBET_API_BASE", "https://api.v2.tibetswap.io")
        timeout = int(getattr(cfg, "TIBET_TIMEOUT", 10))
        decimals = int(getattr(cfg, "CAT_DECIMALS", 3))

        url = f"{base.rstrip('/')}/pairs"
        try:
            resp = self._session.get(url, params={"skip": 0, "limit": 2000},
                                     timeout=timeout)
            resp.raise_for_status()
            all_pairs = resp.json()
        except Exception as e:
            log_event("debug", "amm_monitor_fetch_error",
                      f"AMM Monitor fetch failed for pair {pair_id[:16]}...: {e}")
            return None

        if not isinstance(all_pairs, list):
            return None

        # Find the target pair in the list. Normalise both sides to plain
        # hex (strip 0x, lowercase) so an accidental prefix difference
        # doesn't cause a silent miss.
        def _norm(h: str) -> str:
            s = str(h or "").strip().lower()
            return s[2:] if s.startswith("0x") else s

        target = _norm(pair_id)
        data = None
        for entry in all_pairs:
            if isinstance(entry, dict) and _norm(entry.get("launcher_id") or entry.get("pair_id") or "") == target:
                data = entry
                break
        if not isinstance(data, dict):
            return None

        try:
            xch_reserve_mojos = Decimal(str(data.get("xch_reserve", 0)))
            token_reserve_mojos = Decimal(str(data.get("token_reserve", 0)))
            liquidity = Decimal(str(data.get("liquidity", 0)))

            if xch_reserve_mojos <= 0 or token_reserve_mojos <= 0:
                return None

            # Convert to human units
            xch_reserve = xch_reserve_mojos / Decimal("1000000000000")
            token_reserve = token_reserve_mojos / (Decimal(10) ** Decimal(str(decimals)))

            amm_price = xch_reserve / token_reserve

            return {
                "available": True,
                "pair_id": pair_id,
                "amm_price": amm_price,
                "xch_reserve": xch_reserve,
                "xch_reserve_mojos": xch_reserve_mojos,
                "token_reserve": token_reserve,
                "token_reserve_mojos": token_reserve_mojos,
                "liquidity": liquidity,
                "fetched_at": time.time(),
                # Keep raw mojos for injecting into tibet cache
                "_raw_xch_reserve": int(xch_reserve_mojos),
                "_raw_token_reserve": int(token_reserve_mojos),
            }
        except (InvalidOperation, ZeroDivisionError, TypeError) as e:
            log_event("debug", "amm_monitor_parse_error",
                      f"AMM Monitor failed to parse pair data: {e}")
            return None

    def _compute_drift_vs_old_state(self, new_state: Dict) -> Optional[Decimal]:
        """Compute price drift between new AMM state and last quoted mid.

        Returns drift in basis points, or None if comparison isn't possible.
        """
        new_price = new_state.get("amm_price")
        if not new_price or new_price <= 0:
            return None

        with self._lock:
            buy = self._last_quoted_buy
            sell = self._last_quoted_sell

        if buy and sell and buy > 0 and sell > 0:
            quoted_mid = (buy + sell) / Decimal("2")
        elif buy and buy > 0:
            quoted_mid = buy
        elif sell and sell > 0:
            quoted_mid = sell
        else:
            return None

        return abs(new_price - quoted_mid) / quoted_mid * Decimal("10000")

    def _inject_into_tibet_cache(self, state: Dict, pair_id: str) -> None:
        """Update the price_engine's Tibet pair cache with fresh reserve data.

        This means the next get_price() call will use live AMM reserves
        without needing to re-fetch the full /pairs list.
        """
        try:
            import price_engine as _pe
            raw_xch = state.get("_raw_xch_reserve")
            raw_token = state.get("_raw_token_reserve")
            if raw_xch is None or raw_token is None:
                return

            with _pe._tibet_lock:
                pairs = _pe._tibet_cache.get("pairs", [])
                updated = False
                for pair in pairs:
                    if str(pair.get("pair_id", "")) == pair_id:
                        pair["xch_reserve"] = raw_xch
                        pair["token_reserve"] = raw_token
                        updated = True
                        break

                if updated:
                    # Refresh the cache timestamp so it won't be re-fetched
                    # for another AMM_POLL_INTERVAL_SECS
                    _pe._tibet_cache["fetched_at"] = time.time()
                    log_event("debug", "amm_cache_injected",
                              f"Injected fresh AMM reserves into Tibet cache "
                              f"(xch={raw_xch}, token={raw_token})")
        except Exception as e:
            log_event("debug", "amm_cache_inject_error",
                      f"AMM cache injection failed (non-critical): {e}")

