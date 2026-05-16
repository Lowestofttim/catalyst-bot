"""Central trading-loop orchestrator that wires all bot subsystems together

The `BotLoop` class owns instances of `PriceEngine`, `OfferManager`, `FillTracker`,
`DexieManager`, `SplashManager`, `CoinManager`, `RiskManager`, `Sniper`,
`BoostManager`, `MarketIntel`, `RuntimeMonitor`, `AMMMonitor`, and `MempoolWatcher`,
then completes cross-module wiring via attribute injection after construction.
The main entry point `_run_one_cycle()` runs on a background thread every
`cfg.LOOP_SECONDS` and drives the per-cycle pipeline: price fetch, risk checks,
fill detection, round-trip matching, requote, new-offer creation, Dexie posting,
coin management, and housekeeping.

Key responsibilities:
    - Construct and wire every trading subsystem into a single runnable loop
    - Drive the per-cycle trading pipeline on a background thread
    - Manage auxiliary watchers: health thread, price watcher, coin watcher,
      Splash-receive thread
    - Provide start/stop lifecycle hooks for the desktop app and API server

The loop is single-threaded per cycle; concurrent work (health, price, coin,
Splash) runs in dedicated watcher threads that signal back through shared state.
"""

import os
import time
import threading
import traceback
import requests
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

from config import cfg
from database import (
    log_event,
    get_stats,
    get_offer,
    update_offer_status,
    backfill_verified_fills_from_offers,
)

try:
    from database import get_offers_by_trade_ids
except ImportError:

    def get_offers_by_trade_ids(trade_ids):
        del trade_ids
        return []


try:
    from super_log import slog, log_thread_start
except ImportError:

    def slog(cat, msg, data=None):
        pass

    def log_thread_start(name=None):
        pass


from price_engine import PriceEngine
from offer_manager import OfferManager
from fill_tracker import FillTracker
from dexie_manager import DexieManager, get_offer_detail
from splash_manager import SplashManager
from splash_node import SplashNode
from coinset_client import CoinsetClient
from coin_manager import CoinManager
from risk_manager import RiskManager
from sniper import Sniper
from boost_manager import BoostManager
from market_intel import MarketIntel
from market_toxicity import MarketToxicityGuard, ToxicityContext
from runtime_monitor import RuntimeMonitor
from amm_monitor import AMMMonitor
from splash_receive import classify_offer_for_asset
from shock_protection import evaluate_tibet_shock
from wallet import get_all_offers, get_chia_health

try:
    import mempool_watcher as _mempool_watcher_mod
except ImportError:
    _mempool_watcher_mod = None


DEXIE_STATUS_ACTIVE = 0
DEXIE_STATUS_PENDING = 1
DEXIE_STATUS_CANCELLED = 3
DEXIE_STATUS_COMPLETED = 4
DEXIE_STATUS_EXPIRED = 6


def _bps_to_pct(val):
    """Convert a BPS value (int, float, str, Decimal) to a formatted % string."""
    try:
        n = float(val) / 100
        if n < 1:
            return f"{n:.2f}%"
        return f"{n:.1f}%"
    except (ValueError, TypeError):
        return str(val)


def _format_sage_cleanup_skip_summary(total: int, new: int, repeated: int) -> str:
    """Format the user-facing summary for Sage cleanup skips."""
    return (
        f"Skipped {total} historical Sage offer records during safe cleanup "
        f"({new} first seen this session, {repeated} already known). "
        "They were not DB-verified as cancelled/expired, so CATalyst left "
        "them visible in Sage instead of deleting them."
    )


def _find_cat_deposit_for_position_delta(delta_cat, scale, baseline_at=0):
    """Return a recent external CAT deposit coin matching a positive delta."""
    try:
        delta = Decimal(str(delta_cat or 0))
        unit_scale = Decimal(str(scale or 0))
    except Exception:
        return None
    if delta <= 0 or unit_scale <= 0:
        return None

    target_mojos = int((delta * unit_scale).copy_abs())
    if target_mojos <= 0:
        return None
    tolerance_mojos = max(
        int(unit_scale * Decimal("100")),
        int(Decimal(target_mojos) * Decimal("0.02")),
    )
    lower = max(1, target_mojos - tolerance_mojos)
    upper = target_mojos + tolerance_mojos

    try:
        from database import get_connection

        conn = get_connection()
        rows = conn.execute(
            "SELECT coin_id, amount_mojos, designation, first_seen "
            "FROM coins "
            "WHERE wallet_type='cat' "
            "  AND status IN ('free', 'gone') "
            "  AND COALESCE(designation, 'unknown') IN ('unknown', 'reserve') "
            "  AND amount_mojos BETWEEN ? AND ? "
            "ORDER BY first_seen DESC",
            (int(lower), int(upper)),
        ).fetchall()
    except Exception:
        return None

    try:
        baseline_ts = float(baseline_at or 0)
    except Exception:
        baseline_ts = 0

    for row in rows or []:
        try:
            coin_id = row["coin_id"]
            amount = int(row["amount_mojos"] or 0)
            designation = row["designation"]
            first_seen = row["first_seen"]
        except Exception:
            continue
        if not coin_id or amount <= 0:
            continue
        if baseline_ts > 0 and first_seen:
            try:
                if isinstance(first_seen, (int, float)):
                    first_seen_ts = float(first_seen)
                else:
                    first_seen_raw = str(first_seen).strip()
                    if first_seen_raw.endswith("Z"):
                        first_seen_raw = first_seen_raw[:-1] + "+00:00"
                    first_seen_dt = datetime.fromisoformat(
                        first_seen_raw.replace(" ", "T", 1)
                    )
                    if first_seen_dt.tzinfo is None:
                        first_seen_dt = first_seen_dt.replace(tzinfo=timezone.utc)
                    first_seen_ts = first_seen_dt.timestamp()
                if first_seen_ts < baseline_ts:
                    continue
            except Exception:
                pass
        return {
            "coin_id": coin_id,
            "amount_mojos": amount,
            "designation": designation or "unknown",
            "first_seen": first_seen,
        }
    return None


def map_sage_terminal_offer_status(
    status_val, sage_offer=None, local_offer=None, now_ts=None
):
    """Map Sage terminal offer states onto the local offer status enum.

    The local SQLite schema only supports:
      - open
      - filled
      - cancelled
      - expired

    Sage exposes richer states like PENDING_CANCEL / CONFIRMED / FAILED.
    Only terminal states collapse onto the local SQLite enum; transitional
    states return None so cleanup leaves them for reconciliation.
    """
    from datetime import datetime, timezone

    sage_offer = sage_offer or {}
    local_offer = local_offer or {}
    now_ts = int(now_ts or time.time())
    status_text = str(status_val).upper() if isinstance(status_val, str) else ""

    local_expires_at = local_offer.get("expires_at")
    if local_expires_at:
        try:
            expiry_dt = datetime.fromisoformat(
                str(local_expires_at).replace("Z", "+00:00")
            )
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            if expiry_dt.timestamp() <= time.time():
                return "expired"
        except Exception:
            pass

    valid_times = sage_offer.get("valid_times") or {}
    max_time = valid_times.get("max_time", 0) or sage_offer.get("max_time", 0) or 0
    if max_time:
        try:
            if int(max_time) <= now_ts:
                return "expired"
        except Exception:
            pass

    if isinstance(status_val, int):
        if status_val == 2:  # pending_cancel: not terminal; can still fill
            return None
        if status_val in {3, 5}:  # cancelled / failed
            return "cancelled"
        if status_val == 4:  # confirmed
            return "filled"
        # 0 = pending, 1 = active/open — both return None (not terminal).
        # Anything else is an unknown future status code from Sage.
        if status_val not in {0, 1}:
            log_event(
                "warning",
                "sage_unknown_status_code",
                f"Unrecognised Sage offer status code {status_val!r} — "
                f"treating as active (not terminal). Sage API may have added "
                f"a new status. Update map_sage_terminal_offer_status.",
            )
        return None

    if status_text in {"EXPIRED"}:
        return "expired"
    if status_text in {"PENDING_CANCEL"}:
        return None
    if status_text in {"CANCELLED", "CANCELED", "FAILED"}:
        return "cancelled"
    if status_text in {"CONFIRMED", "COMPLETED", "SUCCEEDED", "SUCCESS"}:
        return "filled"
    # Known active strings — return None silently
    if status_text in {"ACTIVE", "OPEN", "PENDING", "SUBMITTED", ""}:
        return None
    # Truly unknown string status — log so we know to handle it
    log_event(
        "warning",
        "sage_unknown_status_str",
        f"Unrecognised Sage offer status string {status_text!r} — "
        f"treating as active (not terminal). Update map_sage_terminal_offer_status.",
    )
    return None


def collect_locally_expired_stale_offer_ids(
    stale_ids, stale_db_map, already_resolved=None, now_ts=None
):
    """Return stale DB offers whose local max_time has already elapsed.

    Startup cleanup only sees offers missing from the wallet-open list. If
    Sage completed history also omits one of those offers, the local expiry
    timestamp is still enough to distinguish an expiry from a cancel.
    """
    resolved = set(already_resolved or set())
    expired = set()
    for tid in stale_ids:
        if tid in resolved:
            continue
        mapped = map_sage_terminal_offer_status(
            None,
            sage_offer={},
            local_offer=(stale_db_map or {}).get(tid, {}),
            now_ts=now_ts,
        )
        if mapped == "expired":
            expired.add(tid)
    return expired


class _ReserveCheckDeferred(Exception):
    """Raised inside the reserve-floor block when a balance read failed.

    Caught by the surrounding handler so the cycle continues normally. The
    reserve check will be retried on the next cycle once Sage recovers — we
    must never coerce a failed RPC into 'balance = 0' because that path
    triggers a mass cancel of every open offer.
    """


def _extract_wallet_balance_or_defer(raw):
    """Return the wallet_balance payload from a get_wallet_balance() result,
    or raise _ReserveCheckDeferred if the read failed.

    Treats any of these as failure (DEFER, do not coerce to zero):
      - None
      - non-dict result
      - dict with success == False
      - dict missing or with empty 'wallet_balance' key

    Returns the inner wallet_balance dict on success.
    """
    if not isinstance(raw, dict):
        raise _ReserveCheckDeferred("balance result was not a dict")
    if raw.get("success") is False:
        raise _ReserveCheckDeferred(
            f"balance RPC reported success=False: {raw.get('error')}"
        )
    bal = raw.get("wallet_balance")
    if not bal:
        raise _ReserveCheckDeferred("balance result missing wallet_balance payload")
    return bal


class BotLoop:
    """Main bot orchestrator — runs one trading cycle per LOOP_SECONDS.

    Architecture: Each module is instantiated here and called in sequence.
    The bot loop owns the coordination logic; modules own domain logic.
    """

    def __init__(self):
        # ---- Module instances ----
        self.price_engine = PriceEngine()
        self.market_intel = MarketIntel(price_engine=self.price_engine)
        self.offer_manager = OfferManager()
        self.fill_tracker = FillTracker(offer_manager=self.offer_manager)
        self.dexie_manager = DexieManager()
        self.splash_manager = SplashManager()
        self.splash_node = SplashNode()
        self.coinset_client = CoinsetClient()
        self.market_toxicity_guard = MarketToxicityGuard()
        # F34 (2026-04-08): wire coinset_client into offer_manager so
        # fill_tracker can reach it for the Coinset fill verification
        # fallback when Spacescan is unavailable.
        self.offer_manager._coinset_client = self.coinset_client
        self.coin_manager = CoinManager()
        self.coin_manager._price_engine = self.price_engine  # For CAT size derivation
        self.risk_manager = RiskManager(
            price_engine=self.price_engine, market_intel=self.market_intel
        )
        # F37 (2026-04-08): wire dexie_manager into risk_manager so the
        # volatility calculation can use real Dexie v3 historical_trades
        # instead of relying solely on price_engine snapshots.
        self.risk_manager._dexie_manager = self.dexie_manager
        self.risk_manager._bot_ref = self
        self.sniper = Sniper(
            offer_manager=self.offer_manager,
            risk_manager=self.risk_manager,
            dexie_manager=self.dexie_manager,
            splash_manager=self.splash_manager,
        )
        self.boost_manager = BoostManager(
            offer_manager=self.offer_manager,
            dexie_manager=self.dexie_manager,
            risk_manager=self.risk_manager,
            splash_manager=self.splash_manager,
        )
        self.runtime_monitor = RuntimeMonitor(self)
        # AMM monitor — live TibetSwap reserve polling and drift detection
        self.amm_monitor = AMMMonitor(price_engine=self.price_engine)
        # Wire amm_monitor into offer_manager so buffer guard has AMM data
        self.offer_manager.amm_monitor = self.amm_monitor
        # Wire fee coin pool so each create/cancel gets a dedicated fee coin
        # (prevents MEMPOOL_CONFLICT from concurrent Sage operations)
        self.offer_manager._fee_pool = self.coin_manager.fee_pool
        # Wire dexie_manager into offer_manager so cancel_offers can purge
        # cancelled trade IDs from public post queues, preventing invalid or
        # unsafe offers from being posted after a cancel wins the race.
        self.offer_manager.dexie_manager = self.dexie_manager
        self.offer_manager.splash_manager = self.splash_manager
        # Wire boost_manager into risk_manager for spread convergence
        self.risk_manager._boost_manager = self.boost_manager

        # ---- Loop state ----
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._loop_count: int = 0
        self._start_time: float = 0  # Set when bot starts, used for uptime
        self._last_loop_duration: float = 0
        self._last_live_offer_edges: Dict[str, str] = {
            "our_best_bid": "0",
            "our_best_ask": "0",
        }

        # ---- Ladder watchdog state ----
        # Fill-aware watchdog: skip violation checks for a window after any
        # fill so the natural refill cycle can restore ladder shape before
        # we warn. Persistence counter: same (side, code) must recur across
        # consecutive non-fill watchdog passes before we promote it to WARN.
        # One-shot spikes (e.g. mid-refill transient state) never fire an
        # operator-visible warning.
        self._watchdog_violation_streaks: Dict[tuple, int] = {}
        self._watchdog_post_fill_cooldown_secs: float = 120.0
        # Bumped 2 → 5 alongside the refill-price interpolator (see
        # OfferManager._interpolate_refill_price). Interpolation lets the
        # bot self-heal a tier-drift violation over the next few refill
        # cycles; the higher threshold means the dashboard alert only
        # appears after ~5 audit passes of persistent mismatch — i.e.
        # when self-heal has genuinely failed and user intervention
        # (force-requote) is needed. Watchdog runs every 10 cycles, so
        # this corresponds to ~50 cycles of sustained drift.
        self._watchdog_persistence_threshold: int = 5
        # 2026-04-22: if the same shape-drift has persisted this many passes,
        # stop waiting for an operator click and auto-dispatch the ShapeFix
        # flow. Set well above the warn threshold so self-heal and operator
        # intervention both get a fair chance first. 10 watchdog passes ≈
        # 100 cycles of unresolved drift — a stronger signal that the
        # problem needs the cancel+rebuild sequence, not more waiting.
        self._watchdog_auto_heal_threshold: int = 10
        # Remember which (side, code, alert_id) we've already auto-healed
        # this session so we don't fire the orchestrator repeatedly for the
        # same persistent alert if it somehow reappears mid-flow.
        self._watchdog_auto_healed: set = set()
        # After a persisted watchdog warning is visible, keep repeated
        # passes out of the warning feed unless we reach auto-heal or a
        # long reminder interval. The alert itself is still refreshed.
        self._watchdog_repeat_warning_interval: int = 12
        self._last_tier_drift_topup_time: float = 0.0
        self._last_topup_source_wait_log: Dict[str, Dict] = {}

        # ---- Ladder anchor state ----
        # Replacement offers must price against the same grid the original
        # ladder was built on, else new offers land interleaved with
        # surviving offers and the watchdog (correctly) flags drift.
        #
        # Two parallel anchors per side:
        #   _ladder_grid_mid      — the exact mid used to compute the
        #                           original ladder's prices (may include
        #                           a probe-anchor offset). Replacements
        #                           use THIS value so they align with the
        #                           existing offers. Invariant across the
        #                           ladder's lifetime — the grid doesn't
        #                           shift as probe state toggles.
        #   _ladder_anchor_plain_mid — the plain (non-probe-adjusted) mid
        #                           at stamp time. Used for drift detection
        #                           against the CURRENT plain mid. Probe
        #                           state changes don't affect plain mid,
        #                           so this yields clean drift signals.
        # Both are stamped together on full rebuild (empty book) and
        # re-stamped when drift exceeds threshold triggers a realign.
        self._ladder_grid_mid: Dict[str, Decimal] = {
            "buy": Decimal("0"),
            "sell": Decimal("0"),
        }
        self._ladder_anchor_plain_mid: Dict[str, Decimal] = {
            "buy": Decimal("0"),
            "sell": Decimal("0"),
        }
        # Drift threshold in percent. 1.5% ≈ 150 bps — generous enough to
        # absorb normal noise, tight enough that a directional move forces
        # a realign before the ladder looks ragged to takers.
        self._ladder_anchor_drift_pct: Decimal = Decimal("1.5")

        # ---- Price tracking ----
        self._last_quoted_price: Dict[str, Decimal] = {
            "buy": Decimal("0"),
            "sell": Decimal("0"),
        }
        # F67: Un-anchored (plain) mid captured alongside probe-anchored
        # _last_quoted_price at ladder creation. When probes expire and
        # _clear_probe_side fires, the baseline snaps to this value to prevent
        # the dead probe offset from triggering a spurious requote.
        self._last_quoted_plain_mid: Dict[str, Decimal] = {
            "buy": Decimal("0"),
            "sell": Decimal("0"),
        }
        self._force_requote: Dict[str, bool] = {"buy": False, "sell": False}
        self._requote_failure_backoff_until: Dict[str, float] = {
            "buy": 0.0,
            "sell": 0.0,
        }
        self._current_mid_price: Decimal = Decimal("0")
        self._requoted_this_cycle: set = set()  # sides requoted in current cycle

        # ---- Bot state (for GUI) ----
        self._bot_state: Dict = {
            "running": False,
            "loop_count": 0,
            "last_loop_time": 0,
            "mid_price": "0",
            "open_buys": 0,
            "open_sells": 0,
            "status": "stopped",
        }
        self._state_lock = threading.Lock()
        self._probe_lock = threading.Lock()  # Protects _probe_state multi-key updates
        self._ladder_threads: list = []  # Track active ladder-creation threads

        # ---- Event bus (set by api_server after creation) ----
        self._event_bus = None

        # ---- Spacescan context getter (injected by api_server after creation) ----
        # Callable matching _get_spacescan_market_context(asset_id, ticker_id, decimals)
        # Injected so bot_loop can augment SSE dashboard_update events with spacescan
        # metrics without creating a circular import.
        self._spacescan_context_getter = None

        # ---- Coin settling grace period ----
        # After bulk offer creation, the wallet needs time to settle.
        # Skip coin health checks during this window to avoid false topups.
        self._last_bulk_create_time: float = 0
        self._coin_settle_grace_secs: int = 90  # seconds to wait after bulk create
        self._startup_coin_recheck_done: bool = (
            False  # Re-check coins after wallet settles
        )

        # ---- Startup gate ----
        # Background threads wait on this before doing DB writes.
        # Set once _startup_sync() finishes, so cleanup + recovery
        # can run without lock contention from other threads.
        self._startup_complete = threading.Event()

        # ---- Housekeeping timer ----
        self._last_housekeeping: float = 0
        self._housekeeping_interval: int = 300  # 5 minutes

        # ---- F19/F22/F23 hardening watchdog state ----
        self._wal_oversize_streak: int = 0
        self._position_baseline_cat: Optional[Decimal] = None
        # F48 (2026-04-09): also snapshot the bot's own net_position_cat
        # at the moment the wallet baseline is taken, so the sanity check
        # compares deltas against deltas (since-session) rather than
        # current-wallet against all-time-fills (mismatched windows).
        self._position_baseline_net_cat: Optional[Decimal] = None
        self._position_baseline_at: float = 0
        self._last_daily_reconcile_at: float = 0
        self._wallet_stale_streak: int = 0
        self._wallet_stale_first_at: float = 0
        self._trim_streak: int = 0
        self._startup_self_test_results: Dict = {}

        # ---- F27 (2026-04-08): per-step SLA tracking ----
        # _current_cycle_step: name of the cycle step currently executing
        # _cycle_step_started_at: monotonic timestamp when step started
        # The housekeeping watchdog reads these to detect hung steps. We
        # don't try to abort mid-step (state could be corrupted) — we just
        # escalate alerts so the operator knows what's stuck.
        self._current_cycle_step: str = "idle"
        self._cycle_step_started_at: float = 0.0
        self._step_sla_secs: float = 60.0  # warn if a single step exceeds 60s
        self._step_sla_alerted_for: Optional[str] = None

        # ---- Sweep protection (Tier 3) ----
        # Maps side ("buy"/"sell") → wall-clock expiry time.
        # When active, offer creation on that side is paused for one cycle
        # to avoid immediately re-posting stale-priced offers into an arb window.
        self._sweep_protection: Dict[str, float] = {}
        self._recent_sweep_events: list = []

        # ---- Health monitor state (V1 parity) ----
        self._health_thread: Optional[threading.Thread] = None
        self._health_check_interval: int = 15  # seconds between checks
        self._consecutive_unhealthy: int = 0
        self._last_auto_restart_time: float = 0
        self._auto_restart_threshold: int = 300  # 5 min unhealthy → restart
        self._auto_restart_cooldown: int = 1800  # 30 min between restarts
        self._chia_health: Dict = {
            "status": "unknown",
            "wallet_sync_state": "unknown",
            "wallet_reachable": False,
            "wallet_synced": False,
            "node_synced": False,
            "consecutive_failures": 0,
            "last_check": 0,
        }
        self._chia_health_lock = threading.Lock()
        self._circuit_breaker_offer_safed: bool = False
        self._wallet_sync_stale_cycle: bool = False
        self._wallet_sync_was_stale: bool = False

        # ---- Price watcher state (V1 parity) ----
        self._watcher_thread: Optional[threading.Thread] = None
        self._watcher_event = threading.Event()  # Wakes main loop early
        self._watcher_lock = threading.Lock()
        self._watcher_interval: int = 12  # seconds between polls
        self._watcher_min_change_pct: float = 0.03  # 0.03% reserve change triggers
        self._watcher_data: Dict = {
            "last_xch_reserve": 0,
            "last_token_reserve": 0,
            "triggered": False,
            "change_pct": 0.0,
            "direction": "",
            "last_change_ts": 0,
            "last_confirmed_price_xch": "0",
            "polls": 0,
            "triggers": 0,
        }
        self._mempool_price_refresh_needed: bool = False

        # ---- Coin watcher state (lifecycle tracking) ----
        self._coin_watcher_thread: Optional[threading.Thread] = None
        self._coin_watcher_lock = threading.Lock()
        self._coin_watcher_interval: int = (
            30  # seconds between polls (reduced from 12s — read-only thread)
        )
        self._coin_snapshot: Dict[
            str, Dict
        ] = {}  # {coin_id: {amount, status, wallet_type}}
        self._coin_watcher_stale_log_last: float = 0

        # ---- Splash incoming watcher state ----
        self._splash_receive_thread: Optional[threading.Thread] = None
        self._splash_receive_interval: int = max(
            2, int(getattr(cfg, "SPLASH_RECEIVE_POLL_SECS", 5) or 5)
        )
        self._splash_receive_batch_size: int = max(
            1, int(getattr(cfg, "SPLASH_RECEIVE_BATCH_SIZE", 10) or 10)
        )
        self._splash_receive_parser_warned: bool = False

        # ---- Graceful migration state (V1 parity) ----
        self._graceful_migration: Dict = {
            "active": False,
            "phase": "idle",
            "protected_buy_ids": [],
            "protected_sell_ids": [],
            "started_at": 0,
        }

        # ---- Sniper probe state (price discovery) ----
        # The sniper fires both sides near Tibet price, then we wait one loop
        # to see which offers survive. Main offers only deploy once probes confirm.
        self._probe_state: Dict = {
            "active": False,  # True while probing
            "buy_tid": None,  # trade_id of buy probe
            "sell_tid": None,  # trade_id of sell probe
            "buy_price": Decimal("0"),
            "sell_price": Decimal("0"),
            "tibet_price": Decimal("0"),
            "attempt": 0,  # retry count (widens buffer each time)
            "max_attempts": 5,  # give up after 5 retries
            "confirmed_price": None,  # the mid price to use for main offers
            "confirmed_at": 0,
            "launched_at": 0,
            "last_wait_log_at": 0,
            "last_discovery_mid_price": Decimal("0"),
            "last_discovery_arb_gap_bps": Decimal("0"),
            "last_discovery_tibet_price": Decimal("0"),
            "last_discovery_reason": "",
            "last_discovery_at": 0,
        }

        # ---- Probe warning log-rate limits ----
        # probe_timeout_status / probe_max_retries / probe_edge_lost can fire
        # every cycle during sustained failure windows (e.g. coin exhaustion).
        # Each key maps event-name → last log timestamp; only emit once per
        # cooldown period so a 2-hour stall doesn't generate 150+ WARNING lines.
        self._probe_warn_ts: Dict[str, float] = {}
        self._probe_warn_cooldown: float = 600.0  # 10 minutes between repeats

        # ---- Startup repost tracking (V1 parity) ----
        self._startup_repost_done: bool = False
        self._startup_repost_thread: Optional[threading.Thread] = None
        self._startup_repost_lock = threading.Lock()

        # ---- Connectivity recovery tracking (V1 parity) ----
        self._last_pricing_success_ts: float = 0
        self._connectivity_gap_threshold: int = 1800  # 30 min gap → repost to Dexie

        # ---- Defensive-cancel coordination (post-mortem 2026-04-22) ----
        # When the mempool watcher fires a defensive cancel on a tier, record
        # which tiers were cleared so the next requote doesn't try to do the
        # same work a second time. Tier enters as a set of tier names; the
        # next requote consults `_defensive_cancel_active_tiers()` to know
        # which tiers to skip ("already in flight").
        self._last_defensive_cancel: Dict = {
            "at": 0.0,
            "tiers": set(),
            "reason": "",
        }
        self._last_tibet_shock: Dict = {
            "at": 0.0,
            "direction": "",
            "pct": 0.0,
            "sides": tuple(),
            "tiers": tuple(),
        }
        self._last_shock_requote: Dict[str, Dict] = {
            "buy": {},
            "sell": {},
        }
        # How long to consider a defensive cancel still "in flight" — long
        # enough for the cancel to confirm on-chain and the coins to return.
        self._defensive_cancel_grace_secs: float = 90.0
        self._pending_cancel_wallet_ids_by_side: Dict[str, set] = {
            "buy": set(),
            "sell": set(),
        }
        self._pending_cancel_dexie_terminal_seen: Dict[str, Dict] = {}
        self._pending_cancel_dexie_terminal_confirmations: int = 2
        self._pending_cancel_dexie_max_checks_per_cycle: int = 8
        self._pending_cancel_dexie_detail_timeout_secs: float = 2.0
        self._pending_cancel_settle_seen: Dict[str, Dict] = {}
        self._pending_cancel_settle_retry_secs: float = float(
            getattr(cfg, "PENDING_CANCEL_SETTLE_RETRY_SECS", 120) or 120
        )
        self._pending_cancel_settle_max_retries: int = int(
            getattr(cfg, "PENDING_CANCEL_SETTLE_MAX_RETRIES", 5) or 5
        )
        self._pending_cancel_notice_seen: Dict[str, Dict] = {}
        self._pending_cancel_notice_repeat_secs: float = 300.0

        # ---- Degraded-mode recovery tracking ----
        # When the bot stays materially under target or Sage visibility is
        # degraded across multiple cycles, pause the noisy extras and focus on
        # rebuilding the main ladder from a fresh wallet view.
        self._recovery_state: Dict = {
            "active": False,
            "phase": "idle",
            "reason": "",
            "started_at": 0.0,
            "last_transition_at": 0.0,
            "entered_loop": 0,
            "under_target_streak": 0,
            "wallet_stale_streak": 0,
            "probe_churn_streak": 0,
            "create_stall_streak": 0,
            "healthy_streak": 0,
            "buy_deficit": 0,
            "sell_deficit": 0,
            "cycle_probe_churn": False,
            "cycle_create_stalled": False,
            # Flips True the first time the book reaches its full target in
            # this session. Used to distinguish "initial deploy in progress"
            # (book under target because we're still creating offers) from
            # "real recovery" (book regressed after being healthy). Only
            # the latter deserves a WARN — the former is just throttled
            # ladder build.
            "book_ever_at_target": False,
        }
        self._recovery_under_target_cycles: int = 4
        self._recovery_wallet_stale_cycles: int = 2
        self._recovery_probe_churn_cycles: int = 3
        self._recovery_create_stall_cycles: int = 3
        self._recovery_exit_healthy_cycles: int = 2
        self._recovery_min_side_deficit: int = 2
        self._recovery_min_total_deficit: int = 2
        self._last_bot_health_anomaly_log: Dict[str, object] = {
            "signature": "",
            "at": 0.0,
        }
        self._bot_health_anomaly_log_cooldown: float = 3600.0
        self._last_create_disabled_log: Dict[str, Dict] = {
            "buy": {"at": 0.0, "signature": ""},
            "sell": {"at": 0.0, "signature": ""},
        }
        self._create_disabled_log_cooldown: float = 60.0
        self._adaptive_target_backoff_until: Dict[str, float] = {
            "buy": 0.0,
            "sell": 0.0,
        }
        self._last_adaptive_target_log: Dict[str, Dict] = {
            "buy": {"at": 0.0, "signature": ""},
            "sell": {"at": 0.0, "signature": ""},
        }
        self._last_adaptive_offer_targets: Dict[str, int] = {
            "buy": 0,
            "sell": 0,
        }
        self._last_zero_target_topup_defer_log_at: float = 0.0
        self._last_toxicity_live_cancel: Dict[str, Dict] = {
            "buy": {"at": 0.0, "signature": ""},
            "sell": {"at": 0.0, "signature": ""},
        }
        self._toxicity_live_cancel_cooldown: float = float(
            getattr(cfg, "MARKET_TOXICITY_CANCEL_COOLDOWN_SECS", 60) or 60
        )

        # Flag: __init__ complete — get_state() checks this to avoid
        # AttributeError when SSE connects before all attrs are set.
        try:
            from bot_health import set_dexie_requeue_handler

            set_dexie_requeue_handler(self._queue_stale_dexie_post)
        except Exception as e:
            log_event(
                "warning",
                "bot_health_dexie_requeue_hook_failed",
                f"Failed to register Dexie stale-post repair hook: {e}",
            )

        self._init_complete = True

    def _queue_stale_dexie_post(
        self, offer_bech32: str, trade_id: Optional[str] = None
    ) -> None:
        self.dexie_manager.queue_post(offer_bech32, trade_id, force=True)

    def _requote_backoff_remaining(self, side: str) -> float:
        try:
            until = float(self._requote_failure_backoff_until.get(side, 0) or 0)
        except Exception:
            return 0.0
        return max(0.0, until - time.time())

    def _set_requote_failure_backoff(
        self, side: str, reason: str, data: Optional[Dict] = None
    ) -> None:
        try:
            base = float(getattr(cfg, "REQUOTE_FAILURE_BACKOFF_SECS", 0) or 0)
        except Exception:
            base = 0.0
        if base <= 0:
            try:
                base = max(90.0, float(getattr(cfg, "REQUOTE_COOLDOWN_SECS", 60) or 60))
            except Exception:
                base = 90.0
        until = time.time() + base
        self._requote_failure_backoff_until[side] = until
        self._force_requote[side] = False
        payload = {"side": side, "reason": reason, "backoff_secs": round(base, 1)}
        if data:
            payload.update(data)
        log_event(
            "warning",
            "requote_failure_backoff",
            f"{side} requote made no progress ({reason}) — backing off for "
            f"{base:.0f}s so wallet cancels/coin reconciliation can settle",
            data=payload,
        )

    def _process_emergency_requote_result(
        self,
        side: str,
        requote_result,
        requote_mid: Decimal,
        plain_mid: Decimal,
    ) -> Dict:
        """Apply emergency-requote result semantics and return compact state."""
        side_norm = str(side or "").strip().lower()
        new_offers = []
        replaced = 0
        original_target = 0
        any_progress = False
        waiting_for_cancel_settle = False

        if isinstance(requote_result, dict):
            new_offers = requote_result.get("offers", []) or []
            replaced = int(requote_result.get("replaced_count", 0) or 0)
            original_target = int(
                requote_result.get(
                    "original_target_count",
                    requote_result.get("target_count", 0) or 0,
                )
                or 0
            )
            pending_cancel_count = int(
                requote_result.get("pending_cancel_count", 0) or 0
            )
            failed_cancel_count = int(requote_result.get("failed_cancel_count", 0) or 0)
            fully_replaced = bool(requote_result.get("fully_replaced"))
            any_progress = replaced > 0 or len(new_offers) > 0

            if fully_replaced:
                # Clean full replace: advance baseline and refresh ladder anchor
                # so Step 10 does not force another emergency next cycle.
                self._last_quoted_price[side_norm] = requote_mid
                self._last_quoted_plain_mid[side_norm] = plain_mid
                self._ladder_grid_mid[side_norm] = requote_mid
                self._ladder_anchor_plain_mid[side_norm] = plain_mid
            elif any_progress:
                log_event(
                    "warning",
                    "emergency_requote_partial",
                    f"Emergency {side_norm} requote replaced "
                    f"{replaced}/{original_target} offers; holding baseline "
                    f"so the next cycle re-detects remaining stale quotes",
                    data={
                        "side": side_norm,
                        "replaced": replaced,
                        "original_target": original_target,
                    },
                )
                # Do not advance _last_quoted_price; drift will re-fire next
                # cycle to finish the job.
            elif pending_cancel_count > 0 and failed_cancel_count == 0:
                waiting_for_cancel_settle = True
                log_event(
                    "info",
                    "emergency_requote_waiting_for_cancel_settle",
                    f"Emergency {side_norm} requote submitted "
                    f"{pending_cancel_count} cancel(s); waiting for wallet "
                    f"settlement before creating replacements",
                    data={
                        "side": side_norm,
                        "original_target": original_target,
                        "pending_cancel_count": pending_cancel_count,
                        "failed_cancel_count": failed_cancel_count,
                    },
                )
            else:
                log_event(
                    "error",
                    "emergency_requote_no_progress",
                    f"Emergency {side_norm} requote replaced 0/"
                    f"{original_target} offers despite severe price shock; "
                    f"leaving stale offers exposed until wallet state settles",
                    data={
                        "side": side_norm,
                        "original_target": original_target,
                        "pending_cancel_count": pending_cancel_count,
                        "failed_cancel_count": failed_cancel_count,
                    },
                )
                self._set_requote_failure_backoff(
                    side_norm,
                    "emergency_no_progress",
                    data={
                        "original_target": original_target,
                        "pending_cancel_count": pending_cancel_count,
                        "failed_cancel_count": failed_cancel_count,
                    },
                )
        else:
            # Legacy return format (list)
            new_offers = requote_result if isinstance(requote_result, list) else []
            any_progress = bool(new_offers)
            replaced = len(new_offers)
            original_target = len(new_offers)
            if new_offers:
                self._last_quoted_price[side_norm] = requote_mid
                self._last_quoted_plain_mid[side_norm] = plain_mid
                self._ladder_grid_mid[side_norm] = requote_mid
                self._ladder_anchor_plain_mid[side_norm] = plain_mid
            # No-offers legacy path: hold baseline for retry.

        return {
            "new_offers": new_offers,
            "any_progress": any_progress,
            "replaced": replaced,
            "original_target": original_target,
            "waiting_for_cancel_settle": waiting_for_cancel_settle,
        }

    def _set_state(self, **updates):
        """Update _bot_state under the state lock (GUI-visible state).

        All bot_state writes must go through this method so the read side
        in get_state() sees a consistent snapshot.
        """
        with self._state_lock:
            self._bot_state.update(updates)

    # -------------------------------------------------------------------
    # Start / Stop
    # -------------------------------------------------------------------

    def _emit(self, event_type: str, data: dict):
        """Push an event to the SSE event bus (if connected)."""
        if self._event_bus:
            try:
                self._event_bus.emit(event_type, data)
            except Exception:
                pass  # Don't let event bus errors crash the bot

    def _augment_health_with_spacescan(self, health_data: dict) -> dict:
        """Inject spacescan metrics into a market_health dict before SSE emit.

        The api_server HTTP dashboard endpoint adds these fields manually after
        calling _get_spacescan_market_context(). SSE emits skip that path and
        deliver raw get_market_health() output, which has no spacescan keys.
        This method bridges the gap by calling the injected getter (if present)
        and merging results into health_data["metrics"].

        Safe to call even when the getter is None — returns health_data unchanged.
        """
        if not callable(self._spacescan_context_getter):
            return health_data
        try:
            asset_id = str(getattr(cfg, "CAT_ASSET_ID", "") or "").strip().lower()
            ticker_id = (
                str(
                    getattr(cfg, "CAT_NAME", "")
                    or getattr(cfg, "CAT_TICKER_ID", "")
                    or ""
                )
                .strip()
                .upper()
            )
            decimals = int(getattr(cfg, "CAT_DECIMALS", 3) or 3)
            mid_price = float(self._current_mid_price or 0)
            spacescan = self._spacescan_context_getter(
                asset_id,
                ticker_id,
                decimals,
                executable_mid_price=mid_price,
            )
            metrics = health_data.setdefault("metrics", {})
            metrics["spacescan_enabled"] = spacescan.get("enabled", False)
            metrics["spacescan_has_data"] = spacescan.get("has_data", False)
            metrics["spacescan_holder_count"] = spacescan.get("holder_count", 0)
            metrics["spacescan_activity_level"] = spacescan.get(
                "activity_level", "unknown"
            )
            metrics["spacescan_risk_level"] = spacescan.get("risk_level", "unknown")
            metrics["spacescan_price_gap_bps"] = str(spacescan.get("price_gap_bps", 0))
            # F40 (2026-04-08): expose supply + market-cap metrics so the
            # Advisor can warn about low-cap risk relative to trade size.
            metrics["spacescan_circulating_supply"] = spacescan.get(
                "circulating_supply", 0
            )
            metrics["spacescan_total_supply"] = spacescan.get("total_supply", 0)
            metrics["spacescan_activity_count"] = spacescan.get("activity_count", 0)
            # Compute market cap in XCH = circulating_supply × current_mid_price
            try:
                cs = float(spacescan.get("circulating_supply", 0) or 0)
                if cs > 0 and mid_price > 0:
                    metrics["market_cap_xch"] = round(cs * mid_price, 4)
                else:
                    metrics["market_cap_xch"] = 0
            except Exception:
                metrics["market_cap_xch"] = 0
        except Exception as e:
            log_event(
                "debug",
                "spacescan_augment_failed",
                f"Spacescan health augment failed (non-critical): {e}",
            )
        return health_data

    def _emit_alert(
        self,
        alert_id: str,
        severity: str,
        title: str,
        message: str,
        action: str = None,
        action_label: str = None,
    ):
        """Emit a persistent alert to the GUI.

        If the event bus itself throws (misconfigured alert_store, disk
        full during persistence, etc.) we previously swallowed the
        exception silently — a real fault could then happen with no
        banner AND no evidence that alerting itself had failed. Now we
        log a fallback warning so the operator at least sees via the
        log that an alert was dropped.
        """
        if self._event_bus:
            try:
                self._event_bus.alert(
                    alert_id, severity, title, message, action, action_label
                )
            except Exception as _alert_err:
                try:
                    log_event(
                        "warning",
                        "alert_emit_failed",
                        f"Alert '{alert_id}' (sev={severity}) failed to emit: "
                        f"{_alert_err}. Title was: {title!r}",
                        data={
                            "alert_id": alert_id,
                            "severity": severity,
                            "title": title,
                        },
                    )
                except Exception:
                    pass  # don't cascade a logging failure

    def _clear_alert(self, alert_id: str):
        """Clear a resolved alert."""
        if self._event_bus:
            try:
                if hasattr(self._event_bus, "_alert_store"):
                    self._event_bus._alert_store.clear(alert_id)
            except Exception as _clear_err:
                try:
                    log_event(
                        "debug",
                        "alert_clear_failed",
                        f"Alert '{alert_id}' failed to clear: {_clear_err}",
                        data={"alert_id": alert_id},
                    )
                except Exception:
                    pass

    def _probe_warn(self, event: str, message: str) -> bool:
        """Emit a probe-related warning at most once per _probe_warn_cooldown seconds.

        Returns True if the message was emitted (caller may want to know).
        Use this for events that fire every cycle during sustained failure
        windows (probe_max_retries, probe_timeout_status, probe_edge_lost).
        """
        now = time.time()
        last = self._probe_warn_ts.get(event, 0.0)
        if (now - last) < self._probe_warn_cooldown:
            return False
        self._probe_warn_ts[event] = now
        log_event("warning", event, message)
        return True

    def _recovery_is_active(self) -> bool:
        return bool((self._recovery_state or {}).get("active"))

    def _mark_recovery_probe_churn(self):
        self._recovery_state["cycle_probe_churn"] = True

    def _mark_recovery_create_stall(self):
        self._recovery_state["cycle_create_stalled"] = True

    def _bot_health_anomaly_signature(self, health) -> str:
        parts = []
        for check in getattr(health, "checks", []) or []:
            try:
                count = int(getattr(check, "anomaly_count", 0) or 0)
            except Exception:
                count = 0
            if count <= 0:
                continue
            name = str(getattr(check, "name", "") or "?")
            message = str(getattr(check, "message", "") or "")
            parts.append(f"{name}:{count}:{message}")
        if parts:
            return "|".join(sorted(parts))
        names = [str(name) for name in getattr(health, "anomaly_check_names", []) or []]
        return "|".join(sorted(names))

    def _should_log_bot_health_anomalies(self, health) -> bool:
        signature = self._bot_health_anomaly_signature(health)
        if not signature:
            return False
        now = time.time()
        try:
            cooldown = float(
                getattr(cfg, "BOT_HEALTH_ANOMALY_LOG_COOLDOWN_SECS", 0)
                or self._bot_health_anomaly_log_cooldown
            )
        except Exception:
            cooldown = self._bot_health_anomaly_log_cooldown
        last = self._last_bot_health_anomaly_log
        last_signature = str(last.get("signature", "") or "")
        try:
            last_at = float(last.get("at", 0.0) or 0.0)
        except Exception:
            last_at = 0.0
        if signature == last_signature and cooldown > 0 and (now - last_at) < cooldown:
            return False
        self._last_bot_health_anomaly_log = {"signature": signature, "at": now}
        return True

    def _get_expected_offer_targets(self, mid_price: Decimal) -> Dict[str, int]:
        """Return the intended live-book targets for the current market state."""
        targets = {"buy": 0, "sell": 0}
        for side in ("buy", "sell"):
            if side == "buy":
                enabled = bool(getattr(cfg, "ENABLE_BUY", True))
                max_offers = int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 0) or 0)
            else:
                enabled = bool(getattr(cfg, "ENABLE_SELL", True))
                max_offers = int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 0) or 0)
            if not enabled or max_offers <= 0:
                continue
            try:
                if not self.risk_manager.should_enable_side(side, mid_price):
                    continue
            except Exception:
                pass
            targets[side] = max_offers
        return targets

    @staticmethod
    def _config_enables_side(side: str) -> bool:
        """Return whether config currently permits creating/requoting a side."""
        side = str(side or "").lower()
        if side == "buy":
            return bool(getattr(cfg, "ENABLE_BUY", True))
        if side == "sell":
            return bool(getattr(cfg, "ENABLE_SELL", True))
        return False

    def _wallet_type_for_offer_side(self, side: str) -> str:
        return "xch" if str(side or "").lower() == "buy" else "cat"

    def _available_tier_spares_for_side(self, side: str) -> Optional[int]:
        """Return free tier-spare count for the asset that funds a side."""
        wallet_type = self._wallet_type_for_offer_side(side)
        tier_names = ("inner", "mid", "outer", "extreme")
        spares = {}
        known = False
        try:
            all_spares = getattr(self.coin_manager, "_tier_spares", None)
            if isinstance(all_spares, dict):
                if wallet_type not in all_spares:
                    return None
                spares = dict((all_spares.get(wallet_type, {}) or {}))
                known = True
        except Exception:
            spares = {}
        if not known:
            try:
                from database import get_tier_spare_counts

                spares = dict(get_tier_spare_counts(wallet_type) or {})
                known = True
            except Exception:
                spares = {}
        if not known:
            return None
        return sum(max(0, int(spares.get(tier, 0) or 0)) for tier in tier_names)

    def _position_guard_pauses(self) -> Dict[str, Dict]:
        getter = getattr(self.offer_manager, "get_position_guard_pause", None)
        if not callable(getter):
            return {}
        try:
            pauses = getter() or {}
        except Exception:
            return {}
        return {
            str(side).lower(): dict(info or {})
            for side, info in pauses.items()
            if str(side).lower() in {"buy", "sell"} and info
        }

    def _emit_position_guard_pause_alert(self, side: str, pause: Dict) -> None:
        side_norm = str(side or "").lower()
        if side_norm not in {"buy", "sell"}:
            return
        opposite = str(
            (pause or {}).get("opposite_side")
            or ("sell" if side_norm == "buy" else "buy")
        )
        current = str((pause or {}).get("current_position_xch") or "?")
        limit = str((pause or {}).get("hard_limit_xch") or "?")
        self._emit_alert(
            f"{side_norm}_position_paused",
            "info",
            f"{side_norm.capitalize()}s paused by position limit",
            f"{side_norm.capitalize()} offers are paused because adding more would "
            f"push position risk above the {limit} XCH hard limit. "
            f"{opposite.capitalize()} offers remain live to rebalance the wallet "
            f"(current exposure about {current} XCH).",
            action="view_position",
            action_label="View Position",
        )

    def _offer_rebuild_deficits(
        self,
        active_buy_count: int,
        active_sell_count: int,
    ) -> Dict[str, Dict[str, int]]:
        try:
            mid_price = Decimal(str(self._current_mid_price or "0"))
        except Exception:
            mid_price = Decimal("0")
        try:
            targets = self._get_expected_offer_targets(mid_price)
        except Exception:
            targets = {
                "buy": int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 0) or 0),
                "sell": int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 0) or 0),
            }

        deficits: Dict[str, Dict[str, int]] = {}
        for side, active_count in (
            ("buy", active_buy_count),
            ("sell", active_sell_count),
        ):
            target = max(0, int(targets.get(side, 0) or 0))
            active = max(0, int(active_count or 0))
            deficit = max(0, target - active)
            if deficit > 0:
                deficits[side] = {
                    "active": active,
                    "target": target,
                    "deficit": deficit,
                }
        return deficits

    def _defer_drip_topup_for_offer_rebuild(
        self,
        active_buy_count: int,
        active_sell_count: int,
    ) -> bool:
        """Let missing offer creation run before non-critical spare refills."""
        deficits = self._offer_rebuild_deficits(active_buy_count, active_sell_count)
        for side, info in deficits.items():
            deficit = int(info.get("deficit", 0) or 0)
            spare_count = self._available_tier_spares_for_side(side)
            if spare_count is None or int(spare_count) < deficit:
                return False
            info["spares"] = int(spare_count)

        if not deficits:
            return False

        summary = ", ".join(
            f"{side} {info['active']}/{info['target']} with {info['spares']} spare"
            for side, info in deficits.items()
        )
        log_event(
            "info",
            "topup_deferred_offer_rebuild_priority",
            f"Deferring proactive spare top-up; missing offers can be rebuilt "
            f"from existing tier spares ({summary}).",
            data={"deficits": deficits},
        )
        return True

    def _defer_spare_topup_until_source_available(
        self,
        active_buy_count: int,
        active_sell_count: int,
    ) -> bool:
        """Suppress spare-only topup when there is nothing useful to split."""
        if self._offer_rebuild_deficits(active_buy_count, active_sell_count):
            return False

        raw_wallet_types = getattr(
            self.coin_manager, "_topup_needed_wallet_types", None
        )
        if not raw_wallet_types:
            return False
        wallet_types = {
            str(wallet_type or "").lower()
            for wallet_type in raw_wallet_types
            if str(wallet_type or "").lower() in {"xch", "cat"}
        }
        if not wallet_types:
            return False

        source_available = getattr(
            self.coin_manager,
            "_optional_topup_source_available",
            None,
        )
        if not callable(source_available):
            return False

        waiting = []
        for wallet_type in sorted(wallet_types):
            try:
                if source_available(wallet_type):
                    continue
            except Exception:
                return False
            waiting.append(wallet_type)
        if len(waiting) != len(wallet_types):
            return False

        signature = ",".join(waiting)
        try:
            now = time.time()
            last = self._last_topup_source_wait_log.get(signature, {})
            cooldown = float(
                getattr(cfg, "TOPUP_SOURCE_WAIT_LOG_COOLDOWN_SECS", 300) or 300
            )
            if (
                str(last.get("signature") or "") == signature
                and now - float(last.get("at", 0.0) or 0.0) < cooldown
            ):
                return True
            self._last_topup_source_wait_log[signature] = {
                "at": now,
                "signature": signature,
            }
        except Exception:
            pass
        sides = "/".join(waiting).upper()
        log_event(
            "info",
            "topup_waiting_for_source",
            f"Spare coin top-up waiting: {sides} buffer is low, but the live "
            "book is full and there is no reserve, top-up pool, or useful "
            "small coin source to split yet.",
            data={"wallet_types": waiting},
        )
        return True

    def _defer_topup_for_zero_offer_targets(
        self,
        active_buy_count: int,
        active_sell_count: int,
    ) -> bool:
        """Avoid splitting coins while risk logic intentionally disables quoting."""
        try:
            mid_price = Decimal(str(self._current_mid_price or "0"))
            targets = self._get_expected_offer_targets(mid_price)
        except Exception:
            return False

        buy_target = max(0, int((targets or {}).get("buy", 0) or 0))
        sell_target = max(0, int((targets or {}).get("sell", 0) or 0))
        if buy_target > 0 or sell_target > 0:
            return False

        try:
            now = time.time()
            cooldown = float(
                getattr(cfg, "TOPUP_ZERO_TARGET_LOG_COOLDOWN_SECS", 120) or 120
            )
            if now - float(self._last_zero_target_topup_defer_log_at or 0.0) < cooldown:
                return True
            self._last_zero_target_topup_defer_log_at = now
        except Exception as e:
            log_event(
                "debug",
                "topup_zero_target_log_cooldown_error",
                "Could not update zero-target top-up deferral log cooldown; "
                "continuing with the deferral log.",
                data={"error": str(e)},
            )
        log_event(
            "info",
            "topup_deferred_zero_offer_targets",
            "Skipping coin top-up because risk controls have reduced both "
            "offer targets to zero; coin shaping will resume when quoting resumes.",
            data={
                "active_buy": int(active_buy_count or 0),
                "active_sell": int(active_sell_count or 0),
                "buy_target": buy_target,
                "sell_target": sell_target,
            },
        )
        return True

    def _log_adaptive_target_reduction(
        self,
        side: str,
        target: int,
        full_target: int,
        current_count: int,
        spare_count: int,
        backoff_remaining: float,
    ) -> None:
        side_norm = str(side or "").lower()
        if target >= full_target:
            return
        signature = f"{target}/{full_target}:{current_count}:{spare_count}:{int(backoff_remaining > 0)}"
        try:
            now = time.time()
            last = self._last_adaptive_target_log.get(side_norm, {})
            cooldown = float(
                getattr(cfg, "ADAPTIVE_LADDER_TARGET_LOG_COOLDOWN_SECS", 120) or 120
            )
            if (
                str(last.get("signature") or "") == signature
                and now - float(last.get("at") or 0) < cooldown
            ):
                return
            self._last_adaptive_target_log[side_norm] = {
                "at": now,
                "signature": signature,
            }
        except Exception:
            pass
        reason = (
            f"retry backoff {backoff_remaining:.0f}s"
            if backoff_remaining > 0
            else f"{spare_count} usable spare coin(s)"
        )
        log_event(
            "info",
            "adaptive_ladder_target_reduced",
            f"{side_norm} ladder target reduced to {target}/{full_target} "
            f"while coin inventory catches up ({reason})",
            data={
                "side": side_norm,
                "target": int(target),
                "full_target": int(full_target),
                "current_count": int(current_count),
                "spare_count": int(spare_count),
                "backoff_remaining_secs": round(float(backoff_remaining), 1),
            },
        )

    def _get_adaptive_offer_targets(
        self,
        mid_price: Decimal,
        current_buy_count: int = 0,
        current_sell_count: int = 0,
    ) -> Dict[str, int]:
        """Return active offer targets capped by verified depth and free spares.

        This does not cancel healthy live offers when inventory is low. It only
        limits how many missing slots Step 10 tries to replace until tier-spare
        coins or wallet confirmation catch up.
        """
        targets = self._get_expected_offer_targets(mid_price)
        try:
            targets["buy"] = max(
                0,
                int(targets["buy"])
                - int(self.offer_manager.get_suspended_slot_count("buy") or 0),
            )
        except Exception:
            targets["buy"] = max(0, int(targets.get("buy", 0) or 0))
        try:
            targets["sell"] = max(
                0,
                int(targets["sell"])
                - int(self.offer_manager.get_suspended_slot_count("sell") or 0),
            )
        except Exception:
            targets["sell"] = max(0, int(targets.get("sell", 0) or 0))

        if not bool(getattr(cfg, "ADAPTIVE_LADDER_TARGETS", True)):
            self._last_adaptive_offer_targets = dict(targets)
            return targets

        now = time.time()
        current = {
            "buy": max(0, int(current_buy_count or 0)),
            "sell": max(0, int(current_sell_count or 0)),
        }
        for side in ("buy", "sell"):
            full_target = max(0, int(targets.get(side, 0) or 0))
            if full_target <= 0:
                targets[side] = 0
                continue
            live_count = min(current[side], full_target)
            spare_count = self._available_tier_spares_for_side(side)
            if spare_count is None:
                targets[side] = full_target
                continue
            affordable_target = min(full_target, live_count + spare_count)
            backoff_until = float(
                self._adaptive_target_backoff_until.get(side, 0.0) or 0.0
            )
            backoff_remaining = max(0.0, backoff_until - now)
            if backoff_remaining > 0:
                affordable_target = min(affordable_target, live_count)
            target = max(live_count, affordable_target)
            targets[side] = int(target)
            self._log_adaptive_target_reduction(
                side,
                target=int(target),
                full_target=full_target,
                current_count=live_count,
                spare_count=spare_count,
                backoff_remaining=backoff_remaining,
            )

        for side, pause in self._position_guard_pauses().items():
            live_count = max(0, int(current.get(side, 0) or 0))
            if int(targets.get(side, 0) or 0) > live_count:
                targets[side] = live_count

        self._last_adaptive_offer_targets = dict(targets)
        return targets

    def _offer_age_seconds(self, offer: Dict, now_ts: float) -> float:
        created_at = str((offer or {}).get("created_at") or "").strip()
        if not created_at:
            return 0.0
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, float(now_ts) - dt.timestamp())
        except Exception:
            return 0.0

    def _retire_wallet_missing_db_offers(
        self,
        db_buy_offers,
        db_sell_offers,
        wallet_buy_ids,
        wallet_sell_ids,
        wallet_sync_fresh: bool,
        now_ts: Optional[float] = None,
    ) -> Dict[str, set]:
        """Expire DB-only offers once a fresh wallet view proves they never landed."""
        retired = {"buy": set(), "sell": set()}
        if not wallet_sync_fresh:
            return retired
        now = float(now_ts if now_ts is not None else time.time())
        grace = max(
            15.0, float(getattr(cfg, "DB_ONLY_OFFER_CONFIRM_GRACE_SECS", 90) or 90)
        )
        retry_backoff = max(
            0.0, float(getattr(cfg, "DB_ONLY_OFFER_RETRY_BACKOFF_SECS", 300) or 300)
        )
        side_rows = {
            "buy": (list(db_buy_offers or []), set(wallet_buy_ids or set())),
            "sell": (list(db_sell_offers or []), set(wallet_sell_ids or set())),
        }
        for side, (rows, wallet_ids) in side_rows.items():
            for offer in rows:
                tid = str((offer or {}).get("trade_id") or "")
                if not tid or tid in wallet_ids:
                    continue
                if str((offer or {}).get("tier") or "").lower() in ("sniper", "boost"):
                    continue
                age_secs = self._offer_age_seconds(offer, now)
                if age_secs < grace:
                    continue
                if not update_offer_status(tid, "not_submitted"):
                    continue
                retired[side].add(tid)
                try:
                    self.offer_manager._recently_created.pop(tid, None)
                    self.offer_manager._offer_details_cache.pop(tid, None)
                except Exception:
                    pass
                log_event(
                    "info",
                    "db_only_offer_not_submitted",
                    f"{side} offer {tid[:16]}... was still absent from a fresh "
                    f"wallet view after {age_secs:.0f}s; retiring DB row and "
                    "freeing its locked coin",
                    data={
                        "side": side,
                        "trade_id": tid,
                        "age_secs": round(age_secs, 1),
                        "grace_secs": round(grace, 1),
                    },
                )
            if retired[side] and retry_backoff > 0:
                current_until = float(
                    self._adaptive_target_backoff_until.get(side, 0.0) or 0.0
                )
                self._adaptive_target_backoff_until[side] = max(
                    current_until, now + retry_backoff
                )
        return retired

    def _enter_recovery_mode(self, reason: str, buy_deficit: int, sell_deficit: int):
        state = self._recovery_state
        if state.get("active"):
            state["reason"] = reason
            state["buy_deficit"] = int(buy_deficit)
            state["sell_deficit"] = int(sell_deficit)
            self._set_state(status="recovering")
            return

        now = time.time()
        state.update(
            {
                "active": True,
                "phase": "rebuilding",
                "reason": reason,
                "started_at": now,
                "last_transition_at": now,
                "entered_loop": int(self._loop_count),
                "healthy_streak": 0,
                "buy_deficit": int(buy_deficit),
                "sell_deficit": int(sell_deficit),
            }
        )
        self._set_state(status="recovering")
        # Recovery mode is a controlled rebuild state. The alert still makes
        # the state visible; the log line should read as progress, not failure.
        _is_initial_deploy = not bool(state.get("book_ever_at_target"))
        log_event(
            "info",
            "recovery_mode_enter",
            f"{'Initial deploy in progress' if _is_initial_deploy else 'Recovery mode active'} - "
            f"{reason}. Current deficit: {buy_deficit} buy, {sell_deficit} sell.",
        )
        self._emit_alert(
            "bot_recovery",
            "warning",
            "Bot Recovering",
            "Persistent book drift detected. Probing, requotes, and top-ups are "
            "paused while the bot rebuilds missing offers from a fresh wallet view.",
            action="stop_bot",
            action_label="Stop Bot",
        )

    def _exit_recovery_mode(self):
        state = self._recovery_state
        if not state.get("active"):
            return

        duration = max(0.0, time.time() - float(state.get("started_at") or time.time()))
        log_event(
            "info",
            "recovery_mode_exit",
            f"Recovery mode cleared after {duration:.0f}s — main book is back on target.",
        )
        state.update(
            {
                "active": False,
                "phase": "idle",
                "reason": "",
                "started_at": 0.0,
                "last_transition_at": time.time(),
                "entered_loop": 0,
                "under_target_streak": 0,
                "wallet_stale_streak": 0,
                "probe_churn_streak": 0,
                "create_stall_streak": 0,
                "healthy_streak": 0,
                "buy_deficit": 0,
                "sell_deficit": 0,
                "cycle_probe_churn": False,
                "cycle_create_stalled": False,
            }
        )
        if self._running:
            self._set_state(status="running")
        self._clear_alert("bot_recovery")

    def _evaluate_recovery_mode(
        self, mid_price: Decimal, current_buy_count: int, current_sell_count: int
    ):
        """Enter or clear recovery mode based on persistent degraded state."""
        state = self._recovery_state
        targets = self._get_adaptive_offer_targets(
            mid_price,
            current_buy_count=current_buy_count,
            current_sell_count=current_sell_count,
        )
        buy_effective = int(current_buy_count) + int(
            self.offer_manager.get_recently_created_count("buy")
        )
        sell_effective = int(current_sell_count) + int(
            self.offer_manager.get_recently_created_count("sell")
        )
        buy_deficit = max(0, int(targets["buy"]) - buy_effective)
        sell_deficit = max(0, int(targets["sell"]) - sell_effective)
        total_deficit = buy_deficit + sell_deficit
        under_target = (
            buy_deficit >= self._recovery_min_side_deficit
            or sell_deficit >= self._recovery_min_side_deficit
            or total_deficit >= self._recovery_min_total_deficit
        )

        state["buy_deficit"] = int(buy_deficit)
        state["sell_deficit"] = int(sell_deficit)
        # Mark "book has been at target at least once this session" the first
        # time both sides reach their target. Used by _enter_recovery_mode to
        # distinguish initial-deploy under-target (expected) from real recovery
        # (book regressed after being healthy).
        if not under_target and not state.get("book_ever_at_target"):
            state["book_ever_at_target"] = True
        state["wallet_stale_streak"] = (
            int(state.get("wallet_stale_streak", 0)) + 1
            if self._wallet_sync_stale_cycle
            else 0
        )
        state["under_target_streak"] = (
            int(state.get("under_target_streak", 0)) + 1 if under_target else 0
        )
        state["probe_churn_streak"] = (
            int(state.get("probe_churn_streak", 0)) + 1
            if state.get("cycle_probe_churn")
            else 0
        )
        state["create_stall_streak"] = (
            int(state.get("create_stall_streak", 0)) + 1
            if state.get("cycle_create_stalled")
            else 0
        )

        # Escalate if create-stall streak is very long — bot may be stuck.
        # Use exponential back-off (fire at cycles 10, 20, 40, 80, 160, 320)
        # to avoid spamming CRITICAL entries every 7.5 min during multi-hour stalls.
        _stall_streak = int(state.get("create_stall_streak", 0))
        if _stall_streak in (10, 20, 40, 80, 160, 320):
            log_event(
                "critical",
                "recovery_create_stall_escalation",
                f"Offer creation has been stalled for {_stall_streak} consecutive "
                f"recovery cycles — bot may be unable to rebuild the book. "
                f"Check wallet connectivity and coin inventory.",
            )

        reasons = []
        wallet_stale_recovery_relevant = under_target or state.get("active")
        if (
            state["wallet_stale_streak"] >= self._recovery_wallet_stale_cycles
            and wallet_stale_recovery_relevant
        ):
            reasons.append(
                f"wallet offer sync has been stale for {state['wallet_stale_streak']} cycles"
            )
        if (
            under_target
            and state["under_target_streak"] >= self._recovery_under_target_cycles
        ):
            reasons.append(
                f"book is still under target ({buy_effective}/{targets['buy']} buys, "
                f"{sell_effective}/{targets['sell']} sells)"
            )
        if (
            under_target
            and state["probe_churn_streak"] >= self._recovery_probe_churn_cycles
        ):
            reasons.append(
                "probe retries keep churning while the main ladder is under target"
            )
        if (
            under_target
            and state["create_stall_streak"] >= self._recovery_create_stall_cycles
        ):
            reasons.append(
                "offer creation is stalling while the book is trying to recover"
            )

        if reasons:
            self._enter_recovery_mode("; ".join(reasons), buy_deficit, sell_deficit)
        elif state.get("active"):
            # Exit recovery only when both sides are fully filled (deficit == 0).
            # The entry threshold is deficit >= 2, so allowing exit at deficit <= 1
            # creates a gap where the book stays degraded without recovery re-engaging.
            # Tightening exit to deficit <= 0 closes that gap. Timing jitter that
            # leaves one slot unfilled will simply extend the healthy streak delay
            # by one cycle rather than trapping recovery indefinitely.
            healthy = (
                not self._wallet_sync_stale_cycle
                and buy_deficit <= 0
                and sell_deficit <= 0
                and not state.get("cycle_probe_churn")
                and not state.get("cycle_create_stalled")
            )
            if healthy:
                state["healthy_streak"] = int(state.get("healthy_streak", 0)) + 1
                if state["healthy_streak"] >= self._recovery_exit_healthy_cycles:
                    self._exit_recovery_mode()
            else:
                state["healthy_streak"] = 0
                self._set_state(status="recovering")
                self._emit_alert(
                    "bot_recovery",
                    "warning",
                    "Bot Recovering",
                    "The bot is still rebuilding the main ladder from a fresh wallet "
                    "view. Non-essential churn remains paused until the book is "
                    "healthy again.",
                    action="stop_bot",
                    action_label="Stop Bot",
                )

        state["cycle_probe_churn"] = False
        state["cycle_create_stalled"] = False

    def _safeguard_offers_for_circuit_breaker(self):
        """Cancel the appropriate side's live offers when the circuit breaker trips.

        PRICE CB (full halt): cancel all offers on both sides — price is outside
        safe range so no position should be held.

        POSITION CB (partial halt): cancel only the ACCUMULATING side's offers.
        The correcting side's offers stay live (they reduce position) and new
        correcting-side offers will be placed on the next cycle.
        """
        if self._circuit_breaker_offer_safed:
            return

        # Don't set _safed until cancel actually succeeds — if it fails,
        # the next cycle will retry the safety cancel.
        reason = (
            getattr(self.risk_manager, "_circuit_breaker_reason", "")
            or "circuit breaker active"
        )

        if not getattr(self, "offer_manager", None):
            log_event(
                "warning",
                "circuit_breaker_cancel_skipped",
                f"Circuit breaker active but offer manager unavailable ({reason})",
            )
            return

        # Determine scope: partial (position CB) or full (price CB)
        blocked_side = self.risk_manager.get_circuit_breaker_blocked_side()
        is_full = self.risk_manager.is_full_halt()

        try:
            if is_full:
                # Price CB — cancel everything
                log_event(
                    "warning",
                    "circuit_breaker_cancel_start",
                    f"PRICE circuit breaker — cancelling ALL offers ({reason})",
                )
                result = self.offer_manager.cancel_all(cat_asset_id=cfg.CAT_ASSET_ID)
            else:
                # Position CB — cancel only the accumulating side
                log_event(
                    "warning",
                    "circuit_breaker_cancel_start",
                    f"POSITION circuit breaker — cancelling '{blocked_side}' offers only. "
                    f"Correcting side stays live to reduce position. ({reason})",
                )
                result = self.offer_manager.cancel_all(
                    cat_asset_id=cfg.CAT_ASSET_ID,
                    side_filter=blocked_side,
                )

            cancelled = sum(
                1 for item in result.values() if item and item.get("success")
            )
            failed = sum(
                1 for item in result.values() if item and not item.get("success")
            )
            log_event(
                "warning",
                "circuit_breaker_cancel_done",
                f"CB safety cancel complete — cancelled {cancelled}, failed {failed}",
            )
            # Only mark safed once cancel actually succeeded (or partially succeeded).
            # If all cancels failed, leave _safed=False so next cycle retries.
            if cancelled > 0 or failed == 0:
                self._circuit_breaker_offer_safed = True
            try:
                self.coin_manager.snapshot_coins("circuit_breaker_cancel")
                self._emit_coin_update("circuit_breaker_cancel")
            except Exception as e:
                log_event(
                    "warning",
                    "circuit_breaker_coin_snapshot_failed",
                    f"Coin snapshot after circuit breaker cancel failed: {e}",
                )
        except Exception as e:
            # Cancel threw — don't mark as safed, retry on next cycle
            log_event(
                "error",
                "circuit_breaker_cancel_failed",
                f"Circuit breaker could not cancel live offers: {e} — will retry next cycle",
            )

    def _get_probe_anchored_mid(self, side: str, fallback_mid: Decimal) -> Decimal:
        """Back-solve a side-specific ladder mid from the surviving probe edge."""
        try:
            probe = self._probe_state or {}
            if probe.get("active", False):
                return fallback_mid
            if self._probe_cleanup_seconds_remaining(probe, time.time()) <= 0:
                return fallback_mid

            confirmed_price = Decimal(str(probe.get("confirmed_price") or 0))
            if confirmed_price <= 0 or fallback_mid <= 0:
                return fallback_mid

            drift_bps = (
                abs(fallback_mid - confirmed_price) / confirmed_price * Decimal("10000")
            )
            if drift_bps > cfg.ARB_ALERT_THRESHOLD_BPS:
                return fallback_mid

            edge_price = Decimal(str(probe.get(f"{side}_price") or 0))
            if edge_price <= 0:
                return fallback_mid

            inner_edge = cfg.MIN_EDGE_BPS / Decimal("10000")
            if inner_edge <= 0 or inner_edge >= Decimal("1"):
                return fallback_mid

            if side == "buy":
                anchored_mid = edge_price / (Decimal("1") - inner_edge)
            else:
                anchored_mid = edge_price / (Decimal("1") + inner_edge)

            if anchored_mid > 0:
                return anchored_mid
        except Exception as e:
            log_event(
                "debug",
                "probe_anchor_failed",
                f"Probe-anchored mid calculation failed (falling back to Tibet mid): {e}",
            )
        return fallback_mid

    def _clear_probe_side(self, side: str, trade_id: Optional[str] = None):
        """Forget a probe edge once that side has been cancelled, filled, or expired."""
        probe = self._probe_state or {}
        tid_key = f"{side}_tid"
        price_key = f"{side}_price"
        current_tid = probe.get(tid_key)
        if trade_id and current_tid and current_tid != trade_id:
            return

        probe[tid_key] = None
        probe[price_key] = Decimal("0")

        # Snap requote baseline from probe-anchored mid → plain mid.
        # At ladder creation we stored the probe-offset mid in
        # _last_quoted_price and the un-anchored mid in _last_quoted_plain_mid.
        # Once the probe is gone, the main ladder is still priced against
        # the probe-anchored mid. Previously (F67) we force-requoted the
        # inner tier on probe clear to avoid a subsequent fill-triggered
        # replacement landing at plain-mid prices and interleaving with
        # the surviving probe-anchored offers. That was a workaround for
        # the refill-pricing interleave, not a market signal.
        #
        # The refill-price interpolator (see
        # OfferManager._interpolate_refill_price) fills gaps into the
        # existing tier band regardless of where the current mid is, so
        # any subsequent refill stays in the ladder's original grid. The
        # forced requote is no longer needed — dropping it saves the
        # batched ~6+6 cancel/recreate churn that fired ~10 minutes into
        # every bot session and repeated after every probe cycle.
        #
        # We still snap the baseline so the drift-detection code has a
        # meaningful reference for real market moves going forward.
        try:
            plain_mid = self._last_quoted_plain_mid.get(side, Decimal("0"))
            current_baseline = self._last_quoted_price.get(side, Decimal("0"))
            if plain_mid > 0 and current_baseline != plain_mid:
                self._last_quoted_price[side] = plain_mid
                self._last_quoted_plain_mid[side] = Decimal("0")
                log_event(
                    "info",
                    "probe_baseline_snap",
                    f"{side} baseline snapped to plain mid "
                    f"({current_baseline:.8f} -> {plain_mid:.8f}) on probe clear; "
                    f"ladder kept in place — refills will interpolate into the "
                    f"existing tier band on next fill/expiry turnover",
                )
        except Exception as _e:
            log_event(
                "debug",
                "probe_baseline_snap_failed",
                f"Could not snap {side} baseline on probe clear: {_e}",
            )

    def _remember_probe_market_snapshot(
        self,
        mid_price: Decimal,
        arb_gap: Decimal,
        tibet_price: Decimal = Decimal("0"),
        reason: str = "",
    ):
        """Persist the market snapshot that justified the latest probe discovery."""
        probe = self._probe_state or {}
        probe["last_discovery_mid_price"] = Decimal(str(mid_price or 0))
        probe["last_discovery_arb_gap_bps"] = Decimal(str(arb_gap or 0))
        probe["last_discovery_tibet_price"] = Decimal(str(tibet_price or 0))
        probe["last_discovery_reason"] = reason or ""
        probe["last_discovery_at"] = time.time()

    def _get_sniper_launch_reason(
        self,
        mid_price: Decimal,
        arb_gap: Decimal,
        current_buy_ids=None,
        current_sell_ids=None,
    ) -> Optional[str]:
        """Return why a new sniper discovery probe is justified, or None."""
        current_buy_ids = set(current_buy_ids or set())
        current_sell_ids = set(current_sell_ids or set())

        if not current_buy_ids and not current_sell_ids:
            return "startup_empty_book"

        probe = self._probe_state or {}
        last_mid = Decimal(str(probe.get("last_discovery_mid_price") or 0))
        last_gap = Decimal(str(probe.get("last_discovery_arb_gap_bps") or 0))

        price_threshold = max(
            Decimal("0"),
            Decimal(str(getattr(cfg, "SNIPER_REARM_PRICE_MOVE_BPS", "0") or 0)),
        )
        gap_threshold = max(
            Decimal("0"),
            Decimal(str(getattr(cfg, "SNIPER_REARM_GAP_MOVE_BPS", "0") or 0)),
        )

        if last_mid > 0 and mid_price > 0 and price_threshold > 0:
            price_move_bps = abs(mid_price - last_mid) / last_mid * Decimal("10000")
            if price_move_bps >= price_threshold:
                return (
                    f"price_move ({_bps_to_pct(price_move_bps)} >= "
                    f"{_bps_to_pct(price_threshold)})"
                )

        if gap_threshold > 0:
            gap_move_bps = abs(Decimal(str(arb_gap or 0)) - last_gap)
            if gap_move_bps >= gap_threshold:
                return (
                    f"arb_gap_shift ({_bps_to_pct(gap_move_bps)} >= "
                    f"{_bps_to_pct(gap_threshold)})"
                )

        # Iterative floor tightening: after a successful probe, push tighter
        # each cycle until one side gets taken. The first probe survives at
        # SNIPER_BUFFER_BPS (a comfortable margin); subsequent rounds shrink
        # the buffer in SNIPER_FLOOR_TIGHTEN_STEP_BPS steps until we hit the
        # AMM-arb fee point or get arbed — that's the empirical floor.
        if (
            getattr(cfg, "SNIPER_FLOOR_TIGHTEN_ENABLED", True)
            and probe.get("confirmed_at", 0) > 0
            and not probe.get("floor_converged", False)
        ):
            cooldown = float(getattr(cfg, "SNIPER_FLOOR_TIGHTEN_COOLDOWN_SECS", 60))
            since_confirmed = time.time() - float(probe.get("confirmed_at", 0))
            if since_confirmed >= cooldown:
                tibet_fee = int(getattr(cfg, "TIBETSWAP_FEE_BPS", 70))
                safety = int(getattr(cfg, "SNIPER_FLOOR_SAFETY_BPS", 5))
                min_buffer = max(1, tibet_fee - safety)
                step = max(1, int(getattr(cfg, "SNIPER_FLOOR_TIGHTEN_STEP_BPS", 15)))
                current_buffer = int(
                    probe.get("tightening_buffer_bps")
                    or getattr(cfg, "SNIPER_BUFFER_BPS", 50)
                )
                next_buffer = current_buffer - step
                if next_buffer >= min_buffer:
                    return (
                        f"floor_tighten ({_bps_to_pct(current_buffer)} → "
                        f"{_bps_to_pct(next_buffer)}; target ≥ "
                        f"{_bps_to_pct(min_buffer)} from Tibet)"
                    )
                # Within safety margin of the AMM-arb floor — stop tightening
                # so we don't keep re-evaluating every cycle.
                with self._probe_lock:
                    self._probe_state["floor_converged"] = True

        return None

    def _get_market_aware_probe_prices(
        self,
        tibet_price: Decimal,
        buffer_bps: Decimal,
        offer_edges: Optional[Dict] = None,
    ) -> Dict[str, Decimal]:
        """Derive probe prices from Tibet while nudging onto the live Dexie book."""
        result = {
            "buy_price": Decimal("0"),
            "sell_price": Decimal("0"),
            "overall_best_bid": Decimal("0"),
            "overall_best_ask": Decimal("0"),
        }
        if tibet_price <= 0:
            return result

        buffer_bps = max(Decimal("0"), Decimal(str(buffer_bps or 0)))
        buffer_mult = Decimal("1") + buffer_bps / Decimal("10000")
        buy_price = tibet_price / buffer_mult
        sell_price = tibet_price * buffer_mult
        amm_safe_buy_ceiling = buy_price
        amm_safe_sell_floor = sell_price

        orderbook = {}
        if getattr(self, "market_intel", None) and hasattr(
            self.market_intel, "refresh_orderbook"
        ):
            try:
                orderbook = self.market_intel.refresh_orderbook(force=True) or {}
            except Exception:
                orderbook = {}

        overall_best_bid = Decimal(str(orderbook.get("overall_best_bid") or 0))
        overall_best_ask = Decimal(str(orderbook.get("overall_best_ask") or 0))
        offer_edges = offer_edges or {}

        def _edge_decimal(source, key):
            try:
                return Decimal(str((source or {}).get(key) or 0))
            except Exception:
                return Decimal("0")

        our_best_bid = _edge_decimal(offer_edges, "our_best_bid")
        our_best_ask = _edge_decimal(offer_edges, "our_best_ask")
        if our_best_bid <= 0:
            our_best_bid = _edge_decimal(orderbook, "our_best_bid")
        if our_best_ask <= 0:
            our_best_ask = _edge_decimal(orderbook, "our_best_ask")

        if our_best_bid > overall_best_bid:
            overall_best_bid = our_best_bid
        if our_best_ask > 0:
            if overall_best_ask <= 0:
                overall_best_ask = our_best_ask
            else:
                overall_best_ask = min(overall_best_ask, our_best_ask)

        improve_bps = max(
            Decimal("0"),
            Decimal(str(getattr(cfg, "SNIPER_TOP_BOOK_BPS", "1") or 0)),
        )
        improve_mult = Decimal("1") + improve_bps / Decimal("10000")

        if overall_best_bid > 0:
            buy_price = max(buy_price, overall_best_bid * improve_mult)
        if overall_best_ask > 0:
            best_ask_probe = overall_best_ask / improve_mult
            if best_ask_probe > 0:
                sell_price = min(sell_price, best_ask_probe)

        # Dexie can be crossed, stale, or already arbable during a shock.  The
        # probe is meant to discover the AMM-safe edge, so top-book nudging must
        # never move a buy above the Tibet-safe ceiling or a sell below the
        # Tibet-safe floor.
        buy_price = min(buy_price, amm_safe_buy_ceiling)
        sell_price = max(sell_price, amm_safe_sell_floor)

        result.update(
            {
                "buy_price": buy_price,
                "sell_price": sell_price,
                "overall_best_bid": overall_best_bid,
                "overall_best_ask": overall_best_ask,
            }
        )
        return result

    def _get_probe_price_boundary(self, side: str) -> Optional[Decimal]:
        """Return a strict main-book boundary relative to the live probe edge."""
        try:
            probe = self._probe_state or {}
            if probe.get("active", False):
                return None
            if self._probe_cleanup_seconds_remaining(probe, time.time()) <= 0:
                return None

            edge_price = Decimal(str(probe.get(f"{side}_price") or 0))
            if edge_price <= 0:
                return None

            guard_bps = max(
                Decimal("0"),
                Decimal(str(getattr(cfg, "SNIPER_MAIN_BOOK_GUARD_BPS", "1") or 0)),
            )
            guard_mult = Decimal("1") + guard_bps / Decimal("10000")
            if side == "buy":
                boundary = edge_price / guard_mult
            else:
                boundary = edge_price * guard_mult
            return boundary if boundary > 0 else edge_price
        except Exception:
            return None

    def _apply_probe_retry_backoff(
        self, side: str, candidate_price: Decimal, previous_price: Decimal
    ) -> Decimal:
        """Step a retried probe away from the previous edge after it gets taken."""
        try:
            candidate = Decimal(str(candidate_price or 0))
            previous = Decimal(str(previous_price or 0))
            if candidate <= 0 or previous <= 0:
                return candidate

            backoff_bps = max(
                Decimal("0"),
                Decimal(str(getattr(cfg, "SNIPER_RETRY_BACKOFF_BPS", "50") or 0)),
            )
            backoff_mult = Decimal("1") + backoff_bps / Decimal("10000")
            if backoff_mult <= 0:
                return candidate

            if side == "buy":
                return min(candidate, previous / backoff_mult)
            return max(candidate, previous * backoff_mult)
        except Exception:
            return Decimal(str(candidate_price or 0))

    def _probe_hold_seconds_remaining(
        self, probe: Optional[Dict] = None, now_ts: Optional[float] = None
    ) -> float:
        """Return how much longer an active probe should sit before confirmation."""
        probe = probe or self._probe_state or {}
        confirm_secs = max(0, int(getattr(cfg, "SNIPER_CONFIRM_SECS", 30) or 0))
        if confirm_secs <= 0:
            return 0.0

        launched_at = float(probe.get("launched_at") or 0)
        if launched_at <= 0:
            return float(confirm_secs)

        now_ts = now_ts if now_ts is not None else time.time()
        age = max(0.0, now_ts - launched_at)
        return max(0.0, float(confirm_secs) - age)

    @staticmethod
    def _get_live_offer_edges(open_buys, open_sells) -> Dict[str, str]:
        """Return our current best live bid/ask from the wallet-open offer lists."""
        best_bid = Decimal("0")
        best_ask = Decimal("0")

        def _trade_id(offer: Dict) -> str:
            try:
                return str(
                    (offer or {}).get("trade_id") or (offer or {}).get("offer_id") or ""
                )
            except Exception:
                return ""

        live_trade_ids = [
            tid
            for tid in (
                _trade_id(o) for o in list(open_buys or []) + list(open_sells or [])
            )
            if tid
        ]
        db_by_trade_id = {}
        if live_trade_ids:
            try:
                db_by_trade_id = {
                    str(row.get("trade_id") or ""): row
                    for row in get_offers_by_trade_ids(live_trade_ids)
                    if row.get("trade_id")
                }
            except Exception:
                db_by_trade_id = {}

        def _price_candidates(offers, side: str):
            prices = []
            for offer in offers or []:
                raw_values = []
                tid = _trade_id(offer)
                db_offer = db_by_trade_id.get(tid) if tid else None
                # Sage can briefly omit price_xch on a subset of open offers
                # during startup/sync.  Use DB prices only for trade IDs that
                # the wallet just confirmed live so stale DB rows cannot widen
                # the displayed inner spread by themselves.
                if db_offer and str(db_offer.get("side") or "").lower() == side:
                    raw_values.append(
                        db_offer.get("price_xch") or db_offer.get("price")
                    )
                raw_values.append(
                    (offer or {}).get("price_xch") or (offer or {}).get("price")
                )
                for raw in raw_values:
                    try:
                        price = Decimal(str(raw or 0))
                    except Exception:
                        price = Decimal("0")
                    if price > 0:
                        prices.append(price)
                        break
            return prices

        try:
            buy_prices = _price_candidates(open_buys, "buy")
            if buy_prices:
                best_bid = max(buy_prices)
        except Exception:
            best_bid = Decimal("0")

        try:
            sell_prices = _price_candidates(open_sells, "sell")
            positive_sell_prices = [p for p in sell_prices if p > 0]
            if positive_sell_prices:
                best_ask = min(positive_sell_prices)
        except Exception:
            best_ask = Decimal("0")

        return {
            "our_best_bid": str(best_bid),
            "our_best_ask": str(best_ask),
        }

    def _record_live_offer_edges(self, offer_edges: Dict[str, str]) -> None:
        """Cache live bid/ask edges for dashboard market-health calculations."""
        edges = dict(offer_edges or {})
        edges.setdefault("our_best_bid", "0")
        edges.setdefault("our_best_ask", "0")
        self._last_live_offer_edges = edges
        self._set_state(
            our_best_bid=edges.get("our_best_bid", "0"),
            our_best_ask=edges.get("our_best_ask", "0"),
        )

    def _apply_immediate_sweep_protection(self, buy_fills, sell_fills):
        """Pause same-side creation immediately when one cycle sees a fill burst."""
        try:
            min_fills = max(2, int(getattr(cfg, "SWEEP_MIN_FILLS", 3) or 3))
        except Exception:
            min_fills = 3

        try:
            protection_secs = float(getattr(cfg, "SWEEP_PROTECTION_SECS", 90) or 90)
        except Exception:
            protection_secs = 90.0

        if protection_secs <= 0:
            return set()

        now_ts = time.time()
        protected = set()
        for side, fills in (("buy", buy_fills or []), ("sell", sell_fills or [])):
            if len(fills) < min_fills:
                continue
            expiry = now_ts + protection_secs
            current_expiry = float(self._sweep_protection.get(side, 0) or 0)
            self._sweep_protection[side] = max(current_expiry, expiry)
            protected.add(side)

        if protected:
            log_event(
                "info",
                "sweep_protection_immediate",
                f"Same-cycle sweep protection: pausing {sorted(protected)} "
                f"offer creation after fill burst",
                data={
                    "protected_sides": sorted(protected),
                    "buy_fills": len(buy_fills or []),
                    "sell_fills": len(sell_fills or []),
                    "protection_secs": round(protection_secs, 1),
                },
            )

        return protected

    def _toxicity_offer_exposure_xch(
        self, offer: Dict, side: str, mid_price: Decimal
    ) -> Decimal:
        """Return offer notional in XCH without making wallet/API calls."""
        for key in ("size_xch", "xch_amount"):
            raw = offer.get(key)
            if raw not in (None, ""):
                try:
                    return Decimal(str(raw))
                except Exception:
                    pass

        summary = offer.get("summary") or {}
        offered = summary.get("offered") or {}
        requested = summary.get("requested") or {}

        def _mojos_to_xch(value) -> Decimal:
            try:
                return Decimal(str(value or 0)) / Decimal("1000000000000")
            except Exception:
                return Decimal("0")

        if side == "buy":
            return _mojos_to_xch(offered.get("xch") or offered.get("XCH"))
        if side == "sell":
            xch = _mojos_to_xch(requested.get("xch") or requested.get("XCH"))
            if xch > 0:
                return xch
            cat_mojos = Decimal("0")
            try:
                for key, val in offered.items():
                    if str(key).lower().replace("0x", "") == str(
                        cfg.CAT_ASSET_ID
                    ).lower().replace("0x", ""):
                        cat_mojos = Decimal(str(val or 0))
                        break
            except Exception:
                cat_mojos = Decimal("0")
            if cat_mojos > 0 and mid_price > 0:
                return (
                    cat_mojos / (Decimal(10) ** int(getattr(cfg, "CAT_DECIMALS", 3)))
                ) * mid_price
        return Decimal("0")

    def _toxicity_coin_bucket_mojos(self, wallet: str) -> int:
        """Sum in-memory spendable coin buckets without hitting Sage."""
        inv = (
            getattr(
                self.coin_manager,
                "_xch_inventory" if wallet == "xch" else "_cat_inventory",
                {},
            )
            or {}
        )
        total = 0
        for coins in inv.values():
            if not isinstance(coins, list):
                continue
            for coin in coins:
                try:
                    if isinstance(coin, dict):
                        total += int(
                            coin.get("amount") or coin.get("amount_mojos") or 0
                        )
                    else:
                        total += int(getattr(coin, "amount", 0) or 0)
                except Exception:
                    continue
        return total

    def _build_toxicity_inventory_state(self, mid_price: Decimal) -> Dict:
        """Build the small-balance view used by MarketToxicityGuard."""
        cat_scale = Decimal(10) ** int(getattr(cfg, "CAT_DECIMALS", 3))
        xch_spendable = Decimal(self._toxicity_coin_bucket_mojos("xch")) / Decimal(
            "1000000000000"
        )
        cat_spendable = Decimal(self._toxicity_coin_bucket_mojos("cat")) / cat_scale
        cat_spendable_xch = cat_spendable * mid_price if mid_price > 0 else Decimal("0")

        pos_xch = Decimal("0")
        position_pct = Decimal("0")
        pressure_side = ""
        try:
            pos_xch = (
                Decimal(
                    str(getattr(self.risk_manager, "_net_position_cat", "0") or "0")
                )
                * mid_price
            )
            max_pos = Decimal(str(getattr(cfg, "MAX_POSITION_XCH", "0") or "0"))
            if max_pos > 0:
                position_pct = abs(pos_xch) / max_pos * Decimal("100")
            if pos_xch > 0:
                pressure_side = "buy"
            elif pos_xch < 0:
                pressure_side = "sell"
        except Exception as e:
            log_event(
                "debug",
                "toxicity_inventory_state_failed",
                f"Could not derive toxicity inventory position; using neutral defaults: {e}",
            )

        return {
            "xch_spendable": xch_spendable,
            "cat_spendable_xch": cat_spendable_xch,
            "position_xch": pos_xch,
            "position_pct": position_pct,
            "pressure_side": pressure_side,
        }

    def _update_market_toxicity(
        self,
        price_data: Dict,
        mid_price: Decimal,
        arb_gap: Decimal,
        open_buys,
        open_sells,
        buy_fills,
        sell_fills,
    ):
        """Score adverse-selection risk and hand the snapshot to risk_manager."""
        try:
            now_ts = time.time()
            recent_created = getattr(self.offer_manager, "_recently_created", {}) or {}

            def _fill_payload(fill: Dict, side: str) -> Dict:
                tid = fill.get("trade_id", "")
                created_at = recent_created.get(tid)
                age = fill.get("age_secs")
                if age is None and created_at:
                    age = max(0, int(now_ts - float(created_at)))
                payload = dict(fill)
                payload["side"] = side
                if age is not None:
                    payload["age_secs"] = age
                return payload

            recent_fills = [
                _fill_payload(fill, "buy") for fill in (buy_fills or [])
            ] + [_fill_payload(fill, "sell") for fill in (sell_fills or [])]

            open_offers = []
            for side, offers in (("buy", open_buys or []), ("sell", open_sells or [])):
                for offer in offers:
                    size_xch = self._toxicity_offer_exposure_xch(offer, side, mid_price)
                    open_offers.append(
                        {
                            "side": side,
                            "trade_id": offer.get("trade_id", ""),
                            "size_xch": size_xch,
                        }
                    )

            market_summary = {}
            orderbook_snapshot = {}
            try:
                market_summary = self.market_intel.get_market_summary() or {}
            except Exception:
                market_summary = {}
            try:
                orderbook_snapshot = self.market_intel.get_orderbook_snapshot() or {}
            except Exception:
                orderbook_snapshot = {}

            recent_sweeps = []
            cutoff = now_ts - 600
            for event in list(getattr(self, "_recent_sweep_events", []) or []):
                if float(event.get("timestamp", 0) or 0) >= cutoff:
                    recent_sweeps.append(event)
            self._recent_sweep_events = recent_sweeps[-20:]

            context = ToxicityContext(
                now=now_ts,
                loop_count=int(getattr(self, "_loop_count", 0) or 0),
                mid_price=mid_price,
                tibet_price=Decimal(str((price_data or {}).get("tibet_price", 0) or 0)),
                dexie_price=Decimal(str((price_data or {}).get("dexie_price", 0) or 0)),
                arb_gap_bps=arb_gap,
                open_offers=open_offers,
                recent_fills=recent_fills,
                recent_created_offers=[
                    {"trade_id": tid, "created_at": ts}
                    for tid, ts in list(recent_created.items())[:50]
                ],
                market_intel=market_summary,
                orderbook_snapshot=orderbook_snapshot,
                inventory_state=self._build_toxicity_inventory_state(mid_price),
                wallet_health=dict(getattr(self, "_chia_health", {}) or {}),
                recent_sweep_events=recent_sweeps,
                liquidity_mode=str(
                    getattr(cfg, "LIQUIDITY_MODE", "two_sided") or "two_sided"
                ),
            )
            snapshot = self.market_toxicity_guard.update(context)
            self.risk_manager.set_market_toxicity(snapshot)
            self._set_state(market_toxicity=snapshot.to_dict())
            if snapshot.level in ("high", "extreme") or snapshot.throttled_sides:
                log_event(
                    "warning",
                    "market_toxicity_guard",
                    f"Market toxicity {snapshot.level} ({snapshot.score}/100): "
                    f"{snapshot.suggested_action}",
                    data=snapshot.to_dict(),
                )
            return snapshot
        except Exception as e:
            log_event(
                "warning", "toxicity_guard_error", f"Market toxicity guard failed: {e}"
            )
            return None

    def _cancel_toxicity_throttled_offers(
        self,
        snapshot,
        current_buy_ids=None,
        current_sell_ids=None,
    ) -> Dict[str, set]:
        """Cancel live offers on sides the toxicity guard has risked off."""
        cancelled_by_side = {"buy": set(), "sell": set()}
        if snapshot is None:
            return cancelled_by_side
        if not bool(getattr(cfg, "TOXICITY_CANCEL_ENABLED", False)):
            return cancelled_by_side
        if not bool(getattr(cfg, "MARKET_TOXICITY_CANCEL_LIVE_OFFERS", True)):
            return cancelled_by_side

        now_ts = time.time()
        side_ids = {
            "buy": set(current_buy_ids or set()),
            "sell": set(current_sell_ids or set()),
        }

        try:
            snapshot_data = snapshot.to_dict()
        except Exception:
            snapshot_data = {}

        for side, current_ids in side_ids.items():
            try:
                throttled = bool(snapshot.is_side_throttled(side, now_ts))
            except Exception:
                throttled = side in set(getattr(snapshot, "throttled_sides", []) or [])
            if not throttled:
                continue

            pending_cancel_ids = self._pending_cancel_wallet_ids(side)
            live_ids = {tid for tid in current_ids if tid} - pending_cancel_ids
            if not live_ids:
                continue

            signature = ",".join(sorted(live_ids))
            last = (self._last_toxicity_live_cancel or {}).get(side, {}) or {}
            cooldown = float(
                getattr(
                    cfg,
                    "MARKET_TOXICITY_CANCEL_COOLDOWN_SECS",
                    self._toxicity_live_cancel_cooldown,
                )
                or self._toxicity_live_cancel_cooldown
            )
            if (
                signature == str(last.get("signature") or "")
                and now_ts - float(last.get("at") or 0) < cooldown
            ):
                continue

            log_event(
                "warning",
                "market_toxicity_live_cancel_start",
                f"Market toxicity is throttling {side}; cancelling "
                f"{len(live_ids)} live {side} offer(s) instead of leaving "
                "exposure on-book.",
                data={
                    "side": side,
                    "offer_count": len(live_ids),
                    "pending_cancel_count": len(pending_cancel_ids),
                    "toxicity": snapshot_data,
                },
            )

            try:
                results = self.offer_manager.cancel_offers(
                    sorted(live_ids),
                    reason="market_toxicity_guard",
                    force_storm=True,
                    skip_confirmation=True,
                )
            except Exception as e:
                log_event(
                    "warning",
                    "market_toxicity_live_cancel_failed",
                    f"Could not cancel {side} offers for toxicity guard: {e}",
                    data={"side": side, "offer_count": len(live_ids)},
                )
                continue

            success_ids = {
                tid
                for tid, result in (results or {}).items()
                if result and result.get("success")
            }
            failed_ids = live_ids - success_ids
            cancelled_by_side[side] = success_ids
            self._last_toxicity_live_cancel[side] = {
                "at": now_ts,
                "signature": signature,
            }

            log_event(
                "warning" if failed_ids else "info",
                "market_toxicity_live_cancel_result",
                f"Market toxicity cancel for {side}: "
                f"{len(success_ids)} submitted, {len(failed_ids)} failed.",
                data={
                    "side": side,
                    "submitted_count": len(success_ids),
                    "failed_count": len(failed_ids),
                    "failed_trade_ids": sorted(failed_ids)[:10],
                },
            )

        return cancelled_by_side

    def _clear_adaptive_target_backoff_for_confirmed_fills(
        self, buy_fills, sell_fills
    ) -> None:
        """Let verified fills rebuild after sweep protection instead of DB-only retry backoff."""
        cleared = []
        now_ts = time.time()
        for side, fills in (("buy", buy_fills or []), ("sell", sell_fills or [])):
            if not fills:
                continue
            backoff_until = float(
                self._adaptive_target_backoff_until.get(side, 0.0) or 0.0
            )
            if backoff_until <= 0:
                continue
            self._adaptive_target_backoff_until[side] = 0.0
            cleared.append((side, max(0.0, backoff_until - now_ts), len(fills)))

        if cleared:
            summary = ", ".join(
                f"{side} ({count} fill{'s' if count != 1 else ''}, "
                f"{remaining:.0f}s backoff cleared)"
                for side, remaining, count in cleared
            )
            log_event(
                "info",
                "adaptive_target_backoff_cleared_for_fill",
                f"Confirmed fill cleared DB-only retry backoff for {summary}",
                data={
                    "cleared": [
                        {
                            "side": side,
                            "fill_count": count,
                            "remaining_secs": round(float(remaining), 1),
                        }
                        for side, remaining, count in cleared
                    ]
                },
            )

    def _probe_has_matured(
        self, probe: Optional[Dict] = None, now_ts: Optional[float] = None
    ) -> bool:
        """True once a probe pair has survived for the minimum hold time."""
        return self._probe_hold_seconds_remaining(probe, now_ts) <= 0

    def _probe_cleanup_seconds_remaining(
        self, probe: Optional[Dict] = None, now_ts: Optional[float] = None
    ) -> float:
        """Return how much longer confirmed probes should linger before cleanup."""
        probe = probe or self._probe_state or {}
        linger_secs = max(0, int(getattr(cfg, "SNIPER_LINGER_SECS", 600) or 0))
        if linger_secs <= 0:
            return 0.0

        now_ts = now_ts if now_ts is not None else time.time()
        confirmed_at = float(probe.get("confirmed_at") or 0)
        launched_at = float(probe.get("launched_at") or 0)
        anchor_at = max(confirmed_at, launched_at)
        if anchor_at <= 0:
            return float(linger_secs)

        age = max(0.0, now_ts - anchor_at)
        return max(0.0, float(linger_secs) - age)

    def _classify_probe_offer(
        self, side: str, trade_id: Optional[str], open_ids
    ) -> Dict:
        """Classify a probe using both wallet visibility and Dexie state."""
        open_ids = set(open_ids or set())
        wallet_alive = bool(trade_id and trade_id in open_ids)
        state = {
            "side": side,
            "trade_id": trade_id or "",
            "wallet_alive": wallet_alive,
            "confirmable": wallet_alive,
            "taken": False,
            "unverified": False,
            "reason": "wallet_open" if wallet_alive else "wallet_missing",
            "dexie_id": "",
            "dexie_status": None,
        }
        if not trade_id:
            state.update(
                {
                    "confirmable": False,
                    "reason": "not_placed",
                }
            )
            return state

        if not bool(getattr(cfg, "DEXIE_AUTO_POST", True)):
            return state

        dexie_id = ""
        try:
            local_offer = get_offer(trade_id) or {}
            dexie_id = str(local_offer.get("dexie_id") or "").strip()
        except Exception as e:
            state["dexie_error"] = str(e)

        if not dexie_id:
            if wallet_alive:
                state.update(
                    {
                        "confirmable": False,
                        "unverified": True,
                        "reason": "dexie_id_missing",
                    }
                )
            return state

        state["dexie_id"] = dexie_id
        try:
            try:
                dexie_offer = get_offer_detail(
                    dexie_id,
                    timeout=4,
                    cache_ttl_secs=2,
                )
            except TypeError:
                dexie_offer = get_offer_detail(dexie_id)
        except Exception as e:
            dexie_offer = None
            state["dexie_error"] = str(e)

        if not isinstance(dexie_offer, dict):
            if wallet_alive:
                state.update(
                    {
                        "confirmable": False,
                        "unverified": True,
                        "reason": "dexie_unreachable",
                    }
                )
            return state

        raw_status = dexie_offer.get("status")
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            status = raw_status
        state["dexie_status"] = status

        if status == DEXIE_STATUS_ACTIVE:
            state.update(
                {
                    "confirmable": wallet_alive,
                    "unverified": not wallet_alive,
                    "reason": "dexie_active"
                    if wallet_alive
                    else "wallet_missing_dexie_active",
                }
            )
        elif status in (DEXIE_STATUS_PENDING, DEXIE_STATUS_COMPLETED):
            state.update(
                {
                    "confirmable": False,
                    "taken": True,
                    "unverified": False,
                    "reason": "dexie_pending"
                    if status == DEXIE_STATUS_PENDING
                    else "dexie_completed",
                }
            )
        elif status in (DEXIE_STATUS_CANCELLED, DEXIE_STATUS_EXPIRED):
            state.update(
                {
                    "confirmable": False,
                    "unverified": False,
                    "reason": "dexie_cancelled"
                    if status == DEXIE_STATUS_CANCELLED
                    else "dexie_expired",
                }
            )
        else:
            state.update(
                {
                    "confirmable": False,
                    "unverified": wallet_alive,
                    "reason": f"dexie_status_{status}",
                }
            )
        return state

    def _confirmed_probe_slot_offsets(
        self, current_buy_ids=None, current_sell_ids=None
    ) -> Dict[str, int]:
        """Exclude lingering confirmed probes from the main ladder slot count."""
        probe = self._probe_state or {}
        if probe.get("active", False):
            return {"buy": 0, "sell": 0}

        offsets = {"buy": 0, "sell": 0}
        if current_buy_ids is not None:
            buy_tid = probe.get("buy_tid")
            if buy_tid and buy_tid in current_buy_ids:
                offsets["buy"] = 1
        if current_sell_ids is not None:
            sell_tid = probe.get("sell_tid")
            if sell_tid and sell_tid in current_sell_ids:
                offsets["sell"] = 1
        return offsets

    def _extract_open_offer_ids(self, offers_list):
        """Build open buy/sell trade-id sets from an already-open wallet snapshot."""
        buy_ids = set()
        sell_ids = set()
        asset_id = str(cfg.CAT_ASSET_ID or "").lower()

        for offer in offers_list or []:
            if not isinstance(offer, dict):
                continue
            trade_id = offer.get("trade_id") or offer.get("offer_id") or ""
            if not trade_id:
                continue

            summary = offer.get("summary") or {}
            offered = {str(k).lower() for k in (summary.get("offered") or {}).keys()}
            requested = {
                str(k).lower() for k in (summary.get("requested") or {}).keys()
            }

            if "xch" in offered and asset_id in requested:
                buy_ids.add(trade_id)
            elif asset_id in offered and "xch" in requested:
                sell_ids.add(trade_id)

        return buy_ids, sell_ids

    def _refresh_live_offer_ids_from_wallet(
        self, current_buy_ids=None, current_sell_ids=None
    ):
        """Lightweight wallet refresh for probe monitoring without a full cycle sync."""
        current_buy_ids = set(current_buy_ids or set())
        current_sell_ids = set(current_sell_ids or set())

        try:
            offers = get_all_offers(include_completed=False, start=0, end=500)
            if offers is None:
                return current_buy_ids, current_sell_ids

            current_buy_ids, current_sell_ids = self._extract_open_offer_ids(offers)
            all_open_ids = current_buy_ids | current_sell_ids
            self.offer_manager.clean_visible_recently_created(all_open_ids)
            self.sniper.prune_active_snipes(all_open_ids)
            self.boost_manager.prune_active_boosts(all_open_ids)
            self._set_state(
                open_buys=len(current_buy_ids), open_sells=len(current_sell_ids)
            )
        except Exception as e:
            log_event(
                "debug",
                "probe_wallet_poll_failed",
                f"Fast probe wallet poll failed: {e}",
            )

        return current_buy_ids, current_sell_ids

    def _watch_active_probe_window(
        self, current_buy_ids=None, current_sell_ids=None, force_refresh: bool = False
    ):
        """Poll wallet offers quickly while a probe is active so confirmation is real-time-ish."""
        current_buy_ids = set(current_buy_ids or set())
        current_sell_ids = set(current_sell_ids or set())
        if not self._probe_state.get("active", False):
            return current_buy_ids, current_sell_ids

        poll_secs = max(1, int(getattr(cfg, "SNIPER_POLL_SECS", 5) or 5))
        visibility_grace = max(float(poll_secs), 10.0)
        first_pass = True

        while self._running and self._probe_state.get("active", False):
            if force_refresh or not first_pass:
                current_buy_ids, current_sell_ids = (
                    self._refresh_live_offer_ids_from_wallet(
                        current_buy_ids,
                        current_sell_ids,
                    )
                )
                force_refresh = False

            probe = self._probe_state or {}
            open_ids = current_buy_ids | current_sell_ids
            buy_tid = probe.get("buy_tid")
            sell_tid = probe.get("sell_tid")
            buy_state = self._classify_probe_offer("buy", buy_tid, open_ids)
            sell_state = self._classify_probe_offer("sell", sell_tid, open_ids)
            buy_alive = bool(buy_state.get("confirmable"))
            sell_alive = bool(sell_state.get("confirmable"))
            probe_taken = bool(buy_state.get("taken") or sell_state.get("taken"))
            remaining_hold = self._probe_hold_seconds_remaining(probe, time.time())

            # For a buy-only probe (sell_tid=None), treat buy-alive as "all expected alive"
            sell_required = bool(sell_tid)
            all_expected_alive = buy_alive and (sell_alive or not sell_required)

            if all_expected_alive and remaining_hold > 0:
                time.sleep(min(float(poll_secs), float(remaining_hold)))
                first_pass = False
                continue
            if probe_taken:
                break

            probe_age = max(
                0.0, time.time() - float(probe.get("launched_at") or time.time())
            )
            # Only wait for visibility if at least one expected probe isn't visible yet
            if (
                (buy_tid or sell_tid)
                and probe_age < visibility_grace
                and not all_expected_alive
            ):
                time.sleep(
                    min(float(poll_secs), max(0.0, visibility_grace - probe_age))
                )
                first_pass = False
                continue

            break

        return current_buy_ids, current_sell_ids

    def _revalidate_confirmed_probe_edges(
        self,
        current_buy_ids=None,
        current_sell_ids=None,
        arb_gap: Decimal = Decimal("0"),
    ):
        """Before building the main ladder, make sure confirmed probe edges still exist."""
        current_buy_ids = set(current_buy_ids or set())
        current_sell_ids = set(current_sell_ids or set())
        probe = self._probe_state or {}

        if probe.get("active", False):
            return current_buy_ids, current_sell_ids, False

        if self._probe_cleanup_seconds_remaining(probe, time.time()) <= 0:
            return current_buy_ids, current_sell_ids, False

        buy_tid = probe.get("buy_tid")
        sell_tid = probe.get("sell_tid")
        if not buy_tid and not sell_tid:
            return current_buy_ids, current_sell_ids, False

        if self._running:
            current_buy_ids, current_sell_ids = (
                self._refresh_live_offer_ids_from_wallet(
                    current_buy_ids,
                    current_sell_ids,
                )
            )
        open_ids = current_buy_ids | current_sell_ids
        buy_state = self._classify_probe_offer("buy", buy_tid, open_ids)
        sell_state = self._classify_probe_offer("sell", sell_tid, open_ids)
        buy_alive = bool(buy_state.get("confirmable"))
        sell_alive = bool(sell_state.get("confirmable"))

        # If sell was never placed (no CAT sniper coins), treat buy-only as sufficient
        sell_required = bool(sell_tid)
        if buy_alive and (sell_alive or not sell_required):
            return current_buy_ids, current_sell_ids, False

        missing_sides = []
        if buy_tid and not buy_alive:
            missing_sides.append(f"buy:{buy_state.get('reason', 'not_confirmable')}")
        if sell_tid and not sell_alive:
            missing_sides.append(f"sell:{sell_state.get('reason', 'not_confirmable')}")
        if not missing_sides:
            return current_buy_ids, current_sell_ids, False

        self._mark_recovery_probe_churn()
        self._probe_warn(
            "probe_edge_lost",
            f"Confirmed probe edge became unsafe before ladder creation: "
            f"{'+'.join(missing_sides)}. Re-arming probe first.",
        )
        log_event(
            "info",
            "probe_retry_status",
            "Probe edge moved before ladder deploy - re-testing missing side",
        )

        probe["active"] = True
        probe["confirmed_at"] = 0
        probe["last_wait_log_at"] = 0

        probe_result = self._process_active_probe(
            current_buy_ids,
            current_sell_ids,
            arb_gap,
            force_refresh=True,
        )
        return (
            probe_result["buy_ids"],
            probe_result["sell_ids"],
            True,
        )

    def _process_active_probe(
        self,
        current_buy_ids=None,
        current_sell_ids=None,
        arb_gap: Decimal = Decimal("0"),
        force_refresh: bool = False,
    ):
        """Handle active-probe confirmation/retry logic using fast wallet polling."""
        current_buy_ids = set(current_buy_ids or set())
        current_sell_ids = set(current_sell_ids or set())
        sniper_fired = False

        if not self._probe_state.get("active", False):
            return {
                "buy_ids": current_buy_ids,
                "sell_ids": current_sell_ids,
                "sniper_fired": False,
            }

        current_buy_ids, current_sell_ids = self._watch_active_probe_window(
            current_buy_ids,
            current_sell_ids,
            force_refresh=force_refresh,
        )

        probe = self._probe_state
        buy_tid = probe["buy_tid"]
        sell_tid = probe["sell_tid"]
        now_ts = time.time()
        open_ids = current_buy_ids | current_sell_ids

        buy_state = self._classify_probe_offer("buy", buy_tid, open_ids)
        sell_state = self._classify_probe_offer("sell", sell_tid, open_ids)
        buy_alive = bool(buy_state.get("confirmable"))
        sell_alive = bool(sell_state.get("confirmable"))

        # If sell was never placed (no CAT sniper coins), treat buy-only as sufficient
        sell_required = bool(sell_tid)
        if buy_alive and (sell_alive or not sell_required):
            remaining_hold = self._probe_hold_seconds_remaining(probe, now_ts)
            if remaining_hold > 0:
                last_notice_at = float(probe.get("last_wait_log_at") or 0)
                if (now_ts - last_notice_at) >= 5:
                    probe_age = max(
                        0.0, now_ts - float(probe.get("launched_at") or now_ts)
                    )
                    log_event(
                        "info",
                        "probe_hold_wait",
                        f"Buy probe alive for {probe_age:.1f}s - holding "
                        f"{remaining_hold:.1f}s more before confirming",
                    )
                    probe["last_wait_log_at"] = now_ts
                log_event(
                    "debug",
                    "probe_hold_status",
                    "Probe pair still aging before main ladder deploy",
                )
            else:
                probe["confirmed_price"] = probe["tibet_price"]
                probe["confirmed_at"] = now_ts
                probe["active"] = False
                self._current_mid_price = probe["tibet_price"]
                self._set_state(mid_price=str(probe["tibet_price"]))
                linger_secs = int(getattr(cfg, "SNIPER_LINGER_SECS", 600) or 0)
                _probe_desc = (
                    "Both probes survived"
                    if sell_required
                    else "Buy probe survived (sell probe not placed — sniper single-sided or no CAT sniper coins)"
                )
                log_event(
                    "info",
                    "probe_confirmed",
                    f"{_probe_desc} - price confirmed at Tibet "
                    f"{probe['tibet_price']:.8f}. Keeping probes live for "
                    f"{linger_secs}s while building main offers behind them.",
                )
                log_event(
                    "info",
                    "probe_confirmed_status",
                    "Price confirmed - deploying main offers behind live probes",
                )
                self._clear_alert("probe_status")
        else:
            active_states = [buy_state]
            if sell_required:
                active_states.append(sell_state)
            taken_state_sides = [s.get("side") for s in active_states if s.get("taken")]
            unverified_sides = [
                f"{s.get('side')}:{s.get('reason', 'unverified')}"
                for s in active_states
                if s.get("unverified") and not s.get("taken")
            ]
            if unverified_sides and not taken_state_sides:
                last_notice_at = float(probe.get("last_wait_log_at") or 0)
                if (now_ts - last_notice_at) >= 5:
                    log_event(
                        "info",
                        "probe_state_wait",
                        "Probe edge not yet verified on Dexie "
                        f"({'+'.join(unverified_sides)}) - waiting before "
                        "main ladder deploy",
                    )
                    probe["last_wait_log_at"] = now_ts
                log_event(
                    "debug",
                    "probe_hold_status",
                    "Probe pair still awaiting Dexie verification",
                )
                return {
                    "buy_ids": current_buy_ids,
                    "sell_ids": current_sell_ids,
                    "sniper_fired": sniper_fired,
                }

            poll_secs = max(1, int(getattr(cfg, "SNIPER_POLL_SECS", 5) or 5))
            visibility_grace = max(float(poll_secs), 10.0)
            probe_age = max(0.0, now_ts - float(probe.get("launched_at") or now_ts))
            if (
                not taken_state_sides
                and probe_age < visibility_grace
                and (buy_tid or sell_tid)
            ):
                log_event(
                    "debug",
                    "probe_visibility_wait",
                    f"Probe offers not fully visible yet ({probe_age:.1f}s old) - waiting",
                )
            else:
                # ----------------------------------------------------------
                # PROBE-FILL = BOUNDARY NOT FOUND — widen and retry
                #
                # A probe disappearing after the visibility grace window means
                # it was FILLED — someone took it. This means the price was
                # too aggressive: the safe boundary hasn't been found yet.
                #
                # The probe's purpose is to find the price where offers
                # SURVIVE on the book. A fill means we overshot — widen the
                # spread and try again. Only when a probe survives for
                # SNIPER_CONFIRM_SECS do we know the edge is safe, and only
                # then does the main ladder deploy behind it.
                #
                # This prevents the ladder from being built at prices that
                # immediately get taken (feeding arb bots or losing spread).
                # ----------------------------------------------------------

                taken_sides = []
                if buy_tid and (
                    buy_state.get("taken")
                    or (not buy_alive and not buy_state.get("unverified"))
                ):
                    taken_sides.append("buy")
                if sell_tid and (
                    sell_state.get("taken")
                    or (not sell_alive and not sell_state.get("unverified"))
                ):
                    taken_sides.append("sell")

                arb_gap_bps_float = float(arb_gap or 0)

                if taken_sides or not (buy_alive or sell_alive):
                    # Probe was taken — the edge is further out than we tested.
                    # Widen the buffer and retry to find the safe boundary.
                    if probe["attempt"] >= probe["max_attempts"]:
                        self._mark_recovery_probe_churn()
                        self._probe_warn(
                            "probe_max_retries",
                            f"Probe still unsafe after {probe['attempt']} attempts. "
                            "Continuing to back off; main ladder remains held.",
                        )
                        self._probe_warn(
                            "probe_timeout_status",
                            "Probe still unsafe - main ladder held until an edge survives",
                        )
                        probe["max_attempts"] = int(probe["attempt"]) + 5
                    attempt = probe["attempt"] + 1
                    base_buffer = getattr(cfg, "SNIPER_BUFFER_BPS", Decimal("50"))
                    adjusted_buffer = base_buffer + Decimal(str(attempt * 50))
                    tibet_p = probe["tibet_price"]
                    probe_prices = self._get_market_aware_probe_prices(
                        tibet_p,
                        adjusted_buffer,
                        offer_edges=self._last_live_offer_edges,
                    )

                    log_event(
                        "info",
                        "probe_retry",
                        f"Probe attempt {attempt}: {'+'.join(taken_sides) or 'none visible'} "
                        f"taken (arb gap {_bps_to_pct(arb_gap_bps_float)}) — safe edge "
                        f"not found, widening buffer to {_bps_to_pct(adjusted_buffer)}",
                    )
                    log_event(
                        "info",
                        "probe_retry_status",
                        f"Probe retry {attempt} - widening spread to find safe edge",
                    )
                    self._clear_alert("probe_status")

                    new_sell_price = self._apply_probe_retry_backoff(
                        "sell",
                        probe_prices["sell_price"],
                        probe.get("sell_price"),
                    )
                    new_buy_price = self._apply_probe_retry_backoff(
                        "buy",
                        probe_prices["buy_price"],
                        probe.get("buy_price"),
                    )

                    sell_results = None
                    buy_results = None
                    # SYMMETRY (2026-04-08): both sides retry on either
                    # "originally placed and now gone" OR "never placed".
                    # The previous asymmetry — sell only retried if already
                    # placed — biased discovery toward the buy side under
                    # CAT shortage and could permanently stall sell-side
                    # widening once a sell probe failed to place. The
                    # sniper's own cooldown / per-side cap / coin checks
                    # already bound retry frequency, so symmetric retries
                    # don't hammer the wallet beyond what the sniper allows.
                    if (sell_tid and not sell_alive) or not sell_tid:
                        self.sniper._last_snipe_time = 0
                        sell_results = self.sniper.try_snipe_single(
                            "sell", new_sell_price, arb_gap
                        )
                    if (buy_tid and not buy_alive) or not buy_tid:
                        self.sniper._last_snipe_time = 0
                        buy_results = self.sniper.try_snipe_single(
                            "buy", new_buy_price, arb_gap
                        )

                    probe["attempt"] = attempt
                    if sell_results:
                        probe["sell_tid"] = sell_results[0].get("trade_id", "")
                        probe["sell_price"] = new_sell_price
                        sniper_fired = True
                    if buy_results:
                        probe["buy_tid"] = buy_results[0].get("trade_id", "")
                        probe["buy_price"] = new_buy_price
                        sniper_fired = True

                    if sniper_fired:
                        probe["launched_at"] = time.time()
                        probe["last_wait_log_at"] = 0
                        self._emit("sniper", {"count": 1})

        return {
            "buy_ids": current_buy_ids,
            "sell_ids": current_sell_ids,
            "sniper_fired": sniper_fired,
        }

    def _emit_coin_update(self, reason: str = ""):
        """Push a coin_update SSE event so the GUI sees free/locked transitions instantly.

        Called after every coin state change: offer created, filled, cancelled,
        requoted, expired, and after each update_coin_counts() call.
        """
        try:
            from database import get_live_tier_group_counts

            status = self.coin_manager.get_status()
            inv = status.get("inventory") or {}
            tier_counts = get_live_tier_group_counts()
            tier_counts["enabled"] = bool(inv.get("tier_enabled"))
            self._emit(
                "coin_update",
                {
                    "reason": reason,
                    "xch_free": status.get("xch_coins", 0),
                    "xch_locked": status.get("xch_locked_coins", 0),
                    "xch_total": status.get("xch_total_coins", 0),
                    "cat_free": status.get("cat_coins", 0),
                    "cat_locked": status.get("cat_locked_coins", 0),
                    "cat_total": status.get("cat_total_coins", 0),
                    "xch_locked_amount": inv.get("xch_locked_amount", "0"),
                    "cat_locked_amount": inv.get("cat_locked_amount", "0"),
                    "xch_topup_pool_amount": inv.get("xch_reserve_total", "0"),
                    "cat_topup_pool_amount": inv.get("cat_reserve_total", "0"),
                    "tier_counts": tier_counts,
                },
            )
        except Exception as e:
            # Non-critical GUI update — log at debug so it's findable but doesn't alarm
            log_event(
                "debug",
                "coin_update_event_failed",
                f"Coin update GUI event failed (non-critical): {e}",
            )

    def _try_start_mempool_watcher(self, *, log_skip: bool = False) -> bool:
        """F78 (2026-04-17): attempt to start the mempool watcher.

        Split out from :meth:`start` so the main loop can call it each
        cycle when the initial attempt at boot failed (usually because
        the TibetSwap pair_id hadn't been resolved yet).

        Returns True on successful start or when already running;
        False when it couldn't start (caller should retry next cycle).
        """
        if not (
            _mempool_watcher_mod
            and getattr(cfg, "COINSET_ENABLED", True)
            and cfg.CAT_ASSET_ID
        ):
            return False
        # Already running?
        try:
            if getattr(_mempool_watcher_mod, "_watcher_instance", None) is not None:
                return True
        except Exception:
            pass

        try:
            pair_id = getattr(cfg, "_cached_tibet_pair_id", "") or ""
            if not pair_id:
                from price_engine import PriceEngine as _PE

                _tmp_pe = _PE()
                _tmp_pair = _tmp_pe._find_tibet_pair(cfg.CAT_ASSET_ID) or {}
                pair_id = _tmp_pair.get("pair_id", "")
            if not pair_id:
                if log_skip:
                    log_event(
                        "info",
                        "mempool_watcher_deferred",
                        "Mempool watcher deferred — TibetSwap pair_id "
                        "not resolved yet; will retry on next cycle",
                    )
                return False
            _mempool_watcher_mod.start_watcher(
                pair_id=pair_id,
                asset_id=cfg.CAT_ASSET_ID,
                cat_decimals=int(getattr(cfg, "CAT_DECIMALS", 3) or 3),
                wake_callback=self._watcher_event.set,
            )
            log_event(
                "info",
                "mempool_watcher_init",
                f"Mempool watcher started (pair {pair_id[:16]}...)",
            )
            return True
        except Exception as _mw_err:
            if log_skip:
                log_event(
                    "warning",
                    "mempool_watcher_skip",
                    f"Mempool watcher could not start: {_mw_err}",
                )
            return False

    def _run_ladder_watchdog(self) -> None:
        """F72: Periodic ladder + coin-accounting integrity audit.

        Detects drift without fixing it. The scheduled overnight monitors
        (catalyst-log-healthcheck etc.) pick up violations from the log
        and apply corrective actions. Running this inline keeps the loop
        lean and avoids cascading "fix triggers more fixes" storms.

        Called every 10 cycles. Any exception here is swallowed by the
        caller (trading is never blocked by watchdog failure).
        """
        try:
            from ladder_watchdog import run_periodic_audit, Severity
            from database import get_open_offers, get_locked_coins
        except Exception as _imp_err:
            log_event(
                "debug",
                "watchdog_import_failed",
                f"Watchdog import failed (non-fatal): {_imp_err}",
            )
            return

        # Pull current open offers from DB (authoritative for live book).
        try:
            db_buys = get_open_offers(side="buy", cat_asset_id=cfg.CAT_ASSET_ID)
            db_sells = get_open_offers(side="sell", cat_asset_id=cfg.CAT_ASSET_ID)
        except Exception:
            return

        # Map DB rows into the {price, size_xch, trade_id} shape the
        # watchdog expects. trade_id is carried through so the watchdog
        # can attribute violations to specific offers — the dashboard
        # "Cancel mismatched offers" button cancels exactly those.
        def _offer_rows(rows):
            out = []
            for r in rows or []:
                try:
                    # Skip sniper offers — they sit closest to mid but use
                    # sniper-tier coins (0.01 XCH), not main-ladder coins.
                    # Feeding them to the watchdog makes it count the sniper
                    # as "slot 0 inner", pushing every main-tier slot down
                    # by one and producing a cascade of bogus taper-drift
                    # warnings on every cycle.
                    tier = (r.get("tier") or "").strip().lower()
                    if tier == "sniper":
                        continue
                    p = r.get("price_xch") or r.get("price")
                    s = r.get("size_xch")
                    if p is None or s is None:
                        continue
                    out.append(
                        {
                            "price": p,
                            "size_xch": Decimal(str(s)),
                            "trade_id": r.get("trade_id") or "",
                            "tier": tier,
                        }
                    )
                except Exception:
                    continue
            return out

        offers_buy = _offer_rows(db_buys)
        offers_sell = _offer_rows(db_sells)

        # Build per-side tier config from cfg.
        try:
            from config import (
                get_buy_tier_size_xch,
                get_sell_tier_size_xch,
            )
        except Exception:
            return

        def _tier_sizes(side: str):
            getter = get_sell_tier_size_xch if side == "sell" else get_buy_tier_size_xch
            return {
                t: Decimal(str(getter(t)))
                for t in ("inner", "mid", "outer", "extreme")
                if Decimal(str(getter(t))) > 0
            }

        buy_sizes = _tier_sizes("buy")
        sell_sizes = _tier_sizes("sell")
        buy_counts = {
            "inner": int(getattr(cfg, "BUY_INNER_TIER_COUNT", 0) or 0),
            "mid": int(getattr(cfg, "BUY_MID_TIER_COUNT", 0) or 0),
            "outer": int(getattr(cfg, "BUY_OUTER_TIER_COUNT", 0) or 0),
            "extreme": int(getattr(cfg, "BUY_EXTREME_TIER_COUNT", 0) or 0),
        }
        sell_counts = {
            "inner": int(getattr(cfg, "SELL_INNER_TIER_COUNT", 0) or 0),
            "mid": int(getattr(cfg, "SELL_MID_TIER_COUNT", 0) or 0),
            "outer": int(getattr(cfg, "SELL_OUTER_TIER_COUNT", 0) or 0),
            "extreme": int(getattr(cfg, "SELL_EXTREME_TIER_COUNT", 0) or 0),
        }
        # Fall back to legacy shared counts if per-side counts are all 0.
        if sum(buy_counts.values()) == 0:
            buy_counts = {
                "inner": int(getattr(cfg, "INNER_TIER_COUNT", 10) or 0),
                "mid": int(getattr(cfg, "MID_TIER_COUNT", 5) or 0),
                "outer": int(getattr(cfg, "OUTER_TIER_COUNT", 3) or 0),
                "extreme": int(getattr(cfg, "EXTREME_TIER_COUNT", 2) or 0),
            }
            sell_counts = dict(buy_counts)

        # Wallet + inventory totals (for the invariant checks).
        # Snapshot under the coin_manager lock to avoid torn reads while
        # coin_manager is mid-update on another thread.
        try:
            inv = self.coin_manager
            with inv._lock:
                wallet_totals = {
                    "xch_total": int(inv._xch_total_coins or 0),
                    "cat_total": int(inv._cat_total_coins or 0),
                }
                inventory_dict = {
                    "xch": {
                        "free": int(inv._xch_coins or 0),
                        "locked": int(inv._xch_locked_coins or 0),
                    },
                    "cat": {
                        "free": int(inv._cat_coins or 0),
                        "locked": int(inv._cat_locked_coins or 0),
                    },
                }
        except Exception:
            return

        # Count DB-locked coins per wallet type.
        try:
            xch_locked = len(get_locked_coins("xch") or [])
            cat_locked = len(get_locked_coins("cat") or [])
        except Exception:
            xch_locked = cat_locked = 0

        # Per-side layout detection. The watchdog's "reversed" mode expects
        # inner to be the LARGEST tier; "standard" expects inner SMALLEST.
        # Smart Defaults + config don't always produce symmetric layouts:
        #   - Sell is typically natural (inner LARGEST, extreme SMALLEST) →
        #     watchdog-"reversed".
        #   - Buy is typically standard (inner SMALLEST, extreme LARGEST) →
        #     watchdog-"standard" — unless the user flipped BUY_LADDER_REVERSED.
        # Using one flag for both fires spurious inversion ERRORs on sell.
        # Derive per-side orientation from the actual configured sizes.
        def _is_reversed(tier_sizes: Dict[str, Decimal]) -> bool:
            """True when inner tier is bigger than extreme (watchdog 'reversed')."""
            try:
                inner = tier_sizes.get("inner", Decimal("0")) or Decimal("0")
                extreme = tier_sizes.get("extreme", Decimal("0")) or Decimal("0")
                if inner <= 0 or extreme <= 0:
                    # Missing data — fall back to the legacy config flag.
                    return bool(getattr(cfg, "BUY_LADDER_REVERSED", False))
                return inner > extreme
            except Exception:
                return bool(getattr(cfg, "BUY_LADDER_REVERSED", False))

        buy_reversed = _is_reversed(buy_sizes)
        sell_reversed = _is_reversed(sell_sizes)

        # Fill-awareness gate. Right after a fill, the remaining offers
        # shift in price-position, the next-cycle refill hasn't landed
        # yet, and the watchdog would see "wrong-size at slot N" for
        # several slots — all transient. Suppress noise by skipping the
        # audit entirely for a cooldown window after any fill, and by
        # requiring the same violation to recur across consecutive
        # passes before we promote it to WARN.
        _now = time.time()
        try:
            last_fill_buy = float(self.fill_tracker._last_fill_time.get("buy", 0) or 0)
            last_fill_sell = float(
                self.fill_tracker._last_fill_time.get("sell", 0) or 0
            )
        except Exception:
            last_fill_buy = last_fill_sell = 0.0
        _last_fill = max(last_fill_buy, last_fill_sell)
        _in_post_fill_window = (
            _last_fill > 0
            and (_now - _last_fill) < self._watchdog_post_fill_cooldown_secs
        )
        if _in_post_fill_window:
            log_event(
                "debug",
                "watchdog_post_fill_skip",
                f"Watchdog skipped: in post-fill refill window "
                f"({int(_now - _last_fill)}s since last fill, "
                f"cooldown {int(self._watchdog_post_fill_cooldown_secs)}s). "
                f"Refill cycle will realign the ladder.",
            )
            return

        issues = run_periodic_audit(
            offers_buy=offers_buy,
            offers_sell=offers_sell,
            buy_tier_sizes_xch=buy_sizes,
            sell_tier_sizes_xch=sell_sizes,
            buy_tier_counts=buy_counts,
            sell_tier_counts=sell_counts,
            buy_reversed=buy_reversed,
            sell_reversed=sell_reversed,
            wallet_totals=wallet_totals,
            inventory=inventory_dict,
            db_locked_count={"xch": xch_locked, "cat": cat_locked},
        )

        # Persistence: track (side, code) → consecutive-pass streak.
        # A violation only warrants an operator-visible warning when it
        # survives the refill cycle AND is still present on the next
        # watchdog pass. This kills one-shot transient noise from requote
        # churn, cancel-all reconciliation lag, and mid-refill states
        # without suppressing genuine sustained drift.
        seen_now: set = set()
        for issue in issues:
            _side = str((issue.details or {}).get("side") or "ladder").lower()
            seen_now.add((_side, issue.code))
        # Reset streaks for codes that did NOT recur
        stale_keys = [k for k in self._watchdog_violation_streaks if k not in seen_now]
        for k in stale_keys:
            self._watchdog_violation_streaks.pop(k, None)

        # Log each issue at the appropriate severity. The overnight
        # monitors grep for these codes and follow up.
        #
        # Additionally, for "actionable" codes we raise a persistent
        # Recommendation in the dashboard with a Cancel button pre-loaded
        # with the offender trade_ids. This closes the loop: watchdog
        # *sees* drift → Recommendations panel lets the user *fix* it
        # with one click.
        ACTIONABLE = {
            "ladder_size_taper_violated",
            "ladder_inversion_reverse",
            "ladder_inversion_standard",
        }
        # Alerts we raised this pass — used to clear stale ones.
        raised_alert_ids: set = set()
        has_event_bus = self._event_bus is not None

        for issue in issues:
            _side = str((issue.details or {}).get("side") or "ladder").lower()
            _streak_key = (_side, issue.code)
            prev_streak = int(self._watchdog_violation_streaks.get(_streak_key, 0) or 0)
            new_streak = prev_streak + 1
            self._watchdog_violation_streaks[_streak_key] = new_streak

            if new_streak < self._watchdog_persistence_threshold:
                # First observation — likely a transient refill artefact.
                # Log at debug level and do NOT raise an operator alert.
                log_event(
                    "debug",
                    f"watchdog_{issue.code}_pending",
                    f"{issue.message} (first observation, streak "
                    f"{new_streak}/{self._watchdog_persistence_threshold} "
                    f"— will warn if it persists)",
                    data=issue.details,
                )
                continue

            is_error = issue.severity == Severity.ERROR
            sev = "error" if is_error else "warning"
            if self._should_log_watchdog_operator_warning(
                is_error=is_error, streak=new_streak
            ):
                log_event(
                    sev,
                    f"watchdog_{issue.code}",
                    f"{issue.message} — {issue.suggested_action} "
                    f"(persisted for {new_streak} watchdog passes)",
                    data=issue.details,
                )
            else:
                log_event(
                    "debug",
                    f"watchdog_{issue.code}_persistent",
                    f"{issue.message} still present "
                    f"(streak {new_streak}; operator alert already active)",
                    data=issue.details,
                )

            if issue.code in ACTIONABLE and has_event_bus:
                det = issue.details or {}
                side = str(det.get("side") or "ladder")
                tids = [str(t) for t in (det.get("trade_ids") or []) if t]
                alert_id = f"watchdog_{issue.code}_{side}"
                raised_alert_ids.add(alert_id)
                # Only offer the button when we actually have trade_ids
                # to cancel. Without them the button would cancel nothing.
                has_targets = len(tids) > 0
                title_map = {
                    "ladder_size_taper_violated": f"Ladder size drift — {side} side",
                    "ladder_inversion_reverse": f"Ladder inversion (reverse layout) — {side} side",
                    "ladder_inversion_standard": f"Ladder inversion (standard layout) — {side} side",
                }
                title = title_map.get(issue.code, f"Ladder issue — {side} side")
                try:
                    self._event_bus.alert(
                        alert_id,
                        sev,
                        title,
                        f"{issue.message} {issue.suggested_action}".strip(),
                        action=("cancel_mismatched_offers" if has_targets else None),
                        action_label=(
                            f"Cancel {len(tids)} mismatched offer(s)"
                            if has_targets
                            else None
                        ),
                        action_value=(",".join(tids) if has_targets else None),
                    )
                except Exception as _alert_err:
                    log_event(
                        "debug",
                        "watchdog_alert_failed",
                        f"Watchdog alert dispatch failed (non-fatal): {_alert_err}",
                    )

                # 2026-04-22: auto-dispatch the ShapeFix flow once the same
                # drift has persisted past the auto-heal threshold. Gates:
                #   * the issue has trade_ids we can act on
                #   * the ShapeFixOrchestrator exists and isn't already busy
                #   * we haven't already auto-healed this alert this session
                if (
                    has_targets
                    and new_streak >= self._watchdog_auto_heal_threshold
                    and alert_id not in self._watchdog_auto_healed
                ):
                    orchestrator = getattr(self, "shape_fix_orchestrator", None)
                    if orchestrator is not None:
                        try:
                            outcome = orchestrator.start_flow(
                                side=side,
                                trade_ids=tids,
                                alert_id=alert_id,
                            )
                            if outcome.get("accepted"):
                                self._watchdog_auto_healed.add(alert_id)
                                log_event(
                                    "info",
                                    "watchdog_auto_heal_dispatched",
                                    f"Auto-heal: {issue.code} on {side} "
                                    f"persisted {new_streak} passes — "
                                    f"ShapeFix flow started for "
                                    f"{len(tids)} offer(s)",
                                    data={
                                        "alert_id": alert_id,
                                        "flow_id": outcome.get("flow_id"),
                                        "side": side,
                                        "trade_id_count": len(tids),
                                        "streak": new_streak,
                                    },
                                )
                            else:
                                log_event(
                                    "debug",
                                    "watchdog_auto_heal_busy",
                                    f"Auto-heal deferred: orchestrator "
                                    f"busy ({outcome.get('error')})",
                                )
                        except Exception as _ah_err:
                            log_event(
                                "debug",
                                "watchdog_auto_heal_failed",
                                f"Auto-heal dispatch failed (non-fatal): {_ah_err}",
                            )

        # Clear any previously-raised watchdog alerts that did NOT fire
        # this pass — those conditions have resolved.
        #
        # Seed previously_raised from the live alert store on the first
        # run after startup. Otherwise stale watchdog alerts raised by the
        # previous process instance (before a restart) would sit in the
        # alert panel forever, because our in-memory tracker starts empty
        # and has no record of them. With the seed, the first clean pass
        # clears any pre-existing watchdog_* alerts automatically.
        previously_raised = getattr(self, "_watchdog_active_alert_ids", None)
        if previously_raised is None and has_event_bus:
            try:
                store = getattr(self._event_bus, "_alert_store", None)
                existing = set()
                # AlertStore exposes its live state via the `_alerts` dict
                # (keyed by alert_id). Walk it and collect any watchdog_*
                # entries that aren't already dismissed.
                raw = getattr(store, "_alerts", None) if store else None
                if isinstance(raw, dict):
                    for aid, payload in raw.items():
                        if not aid or not aid.startswith("watchdog_"):
                            continue
                        if isinstance(payload, dict) and payload.get("dismissed"):
                            continue
                        existing.add(aid)
                previously_raised = existing
            except Exception:
                previously_raised = set()
        if previously_raised is None:
            previously_raised = set()
        to_clear = previously_raised - raised_alert_ids
        if has_event_bus and to_clear:
            try:
                store = getattr(self._event_bus, "_alert_store", None)
                if store is not None:
                    for aid in to_clear:
                        try:
                            store.clear(aid)
                        except Exception:
                            pass
            except Exception:
                pass
        self._watchdog_active_alert_ids = raised_alert_ids
        # When an alert clears (no longer firing), also forget that we
        # auto-healed it — so if the same drift recurs later, we'll
        # dispatch ShapeFix again rather than silently ignoring.
        if to_clear:
            self._watchdog_auto_healed -= to_clear

        if not issues:
            # All clear — log at debug so we can confirm the watchdog is
            # running, without spamming info-level logs every 10 cycles.
            log_event(
                "debug",
                "watchdog_clean",
                f"Watchdog audit clean "
                f"(cycle={self._loop_count}, "
                f"buys={len(offers_buy)}, sells={len(offers_sell)})",
            )

    def _should_log_watchdog_operator_warning(
        self,
        *,
        is_error: bool,
        streak: int,
    ) -> bool:
        """Return true when a persisted watchdog issue should hit WARN/ERROR."""
        if is_error:
            return True
        if streak == self._watchdog_persistence_threshold:
            return True
        if streak == self._watchdog_auto_heal_threshold:
            return True
        interval = max(1, int(self._watchdog_repeat_warning_interval or 1))
        return (
            streak > self._watchdog_auto_heal_threshold
            and (streak - self._watchdog_auto_heal_threshold) % interval == 0
        )

    def _tier_size_drift_waiting_sides(self, findings: list[dict]) -> set[str]:
        """Return drift sides that cannot be reshaped by a drip topup now."""
        source_available = getattr(
            self.coin_manager,
            "_optional_topup_source_available",
            None,
        )
        if not callable(source_available):
            return set()

        targets: dict[str, int] = {}
        for finding in findings:
            side = str(finding.get("side") or "").lower()
            if side not in {"xch", "cat"}:
                continue
            try:
                target = int(finding.get("live_size_mojos") or 0)
            except Exception:
                target = 0
            targets[side] = max(targets.get(side, 0), target)

        waiting: set[str] = set()
        for side, target in targets.items():
            try:
                if not source_available(side, target):
                    waiting.add(side)
            except Exception as err:
                log_event(
                    "debug",
                    "tier_size_drift_source_check_failed",
                    f"Tier-size drift source check failed for {side}: {err}",
                )
        return waiting

    def _log_tier_size_drift_waiting_for_source(
        self,
        summary: str,
        waiting_sides: set[str],
        findings: list[dict],
    ) -> None:
        """Rate-limit no-source drift notices so the dashboard stays calm."""
        now = time.time()
        sides_key = ",".join(sorted(waiting_sides))
        last_logs = getattr(self, "_last_tier_drift_source_wait_log", None)
        if not isinstance(last_logs, dict):
            last_logs = {}
            self._last_tier_drift_source_wait_log = last_logs
        if now - float(last_logs.get(sides_key, 0.0) or 0.0) < 1800:
            return
        last_logs[sides_key] = now

        side_label = "/".join(side.upper() for side in sorted(waiting_sides))
        log_event(
            "info",
            "tier_size_drift_waiting_for_source",
            f"Prepared {side_label} tier coins need reshaping: {summary}. "
            f"Waiting for {side_label} reserve, top-up pool, or useful small "
            f"coins before live topup can rebuild those spares.",
            data={"waiting_sides": sorted(waiting_sides), "findings": findings[:8]},
        )

    def _check_tier_size_drift(self) -> None:
        """Periodic check: do prepared coin sizes still fit live tier targets?

        Prep coins are sized for the price at prep time. After enough price
        drift the F70 selector has nothing it can use for some tier and the
        ladder runs short. Detection only — surfaces a persistent banner
        and a log line. The fix is "re-run Smart Settings", which the user
        triggers from the alert.

        Self-healing: every run, reclassify tier_spare coins against the
        CURRENT cfg before measuring. Without this, a coin labeled
        "tier_spare/inner" by a previous prep against an OLDER tier-size
        config keeps that label even after Smart Settings has rewritten the
        tier sizes — and the median of stale-labeled coins drifts against
        the new target by exactly the cfg delta, firing a false positive
        on the very first cycle after a clean prep + Smart Settings re-run.
        Reclassification re-stamps the coin against the current cfg so the
        drift measurement reflects only coins that genuinely belong to that
        tier under the live targets. Genuine drift (coin sizes don't fit
        any tier) still surfaces as findings.
        """
        try:
            from coin_manager import reclassify_tier_spare_coins

            reclassify_tier_spare_coins()
        except Exception as _reclass_err:
            log_event(
                "debug",
                "tier_size_drift_reclassify_failed",
                f"Pre-drift reclassification failed (non-fatal): {_reclass_err}",
            )

        try:
            findings = self.coin_manager.check_tier_size_drift()
        except Exception as _err:
            log_event(
                "debug",
                "tier_size_drift_unavailable",
                f"Drift check raised (non-fatal): {_err}",
            )
            return

        alert_id = "tier_size_drift"
        if not findings:
            # Clear any previously-raised alert so the banner disappears
            # once the user has re-prepped (or price has come back).
            self._clear_alert(alert_id)
            return

        # Build a compact summary for the log line and the banner.
        # Sort worst drift first so the user sees the most affected tier.
        findings.sort(key=lambda f: abs(f.get("ratio", 1.0) - 1.0), reverse=True)
        parts = []
        for f in findings:
            side = (f.get("side") or "").upper()
            tier = (f.get("tier") or "").lower()
            ratio = float(f.get("ratio") or 0.0)
            n = int(f.get("coin_count") or 0)
            parts.append(f"{side}/{tier}={ratio:.2f}× (n={n})")
        summary = ", ".join(parts)

        topup_note = "Live topup will retry on the next eligible cycle."
        waiting_sides = self._tier_size_drift_waiting_sides(findings)
        actionable_findings = [
            f for f in findings if str(f.get("side") or "").lower() not in waiting_sides
        ]
        waiting_for_source_only = bool(waiting_sides) and not actionable_findings
        if waiting_for_source_only:
            waiting_label = "/".join(side.upper() for side in sorted(waiting_sides))
            topup_note = (
                f"Waiting for {waiting_label} source coins before live topup "
                f"can reshape those spares."
            )
            self._log_tier_size_drift_waiting_for_source(
                summary,
                waiting_sides,
                findings,
            )
        else:
            log_event(
                "info",
                "tier_size_drift",
                f"Prepared coin sizes need live reshaping: {summary}. "
                f"The bot will use live topup/rebuild before asking for Coin Prep.",
            )

        try:
            _now = time.time()
            _cooldown = max(
                120,
                int(getattr(cfg, "TIER_DRIFT_TOPUP_COOLDOWN_SECS", 600) or 600),
            )
            _last_topup = float(
                getattr(self, "_last_tier_drift_topup_time", 0.0) or 0.0
            )
            if waiting_for_source_only:
                pass
            elif _now - _last_topup >= _cooldown:
                from database import get_open_offers

                _open = get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
                _active_buy = sum(1 for o in _open if o.get("side") == "buy")
                _active_sell = sum(1 for o in _open if o.get("side") == "sell")
                if self.coin_manager.start_topup(
                    _active_buy, _active_sell, is_drip=True
                ):
                    self._last_tier_drift_topup_time = _now
                    topup_note = "Live topup has been queued to reshape the coin pools."
                    log_event(
                        "info",
                        "tier_size_drift_topup_started",
                        f"Queued live topup for tier-size drift: {summary}",
                        data={
                            "active_buy_count": _active_buy,
                            "active_sell_count": _active_sell,
                            "findings": findings[:8],
                        },
                    )
                else:
                    log_event(
                        "info",
                        "tier_size_drift_topup_deferred",
                        "Tier-size drift found, but live topup is already busy "
                        "or unavailable this cycle",
                        data={"findings": findings[:8]},
                    )
            else:
                _remaining = int(_cooldown - (_now - _last_topup))
                topup_note = (
                    f"Live topup recently ran; retry window opens in {_remaining}s."
                )
                log_event(
                    "debug",
                    "tier_size_drift_topup_cooldown",
                    topup_note,
                    data={"findings": findings[:8]},
                )
        except Exception as _topup_err:
            log_event(
                "debug",
                "tier_size_drift_topup_failed",
                f"Tier-size drift topup trigger failed (non-fatal): {_topup_err}",
            )

        self._emit_alert(
            alert_id,
            "info",
            "Coin pools reshaping",
            f"Some spare tier coins are outside the current live ladder sizes "
            f"({summary}). {topup_note} Coin Prep is only needed if this "
            f"stays unresolved after topup has had time to finish.",
        )

    def _reset_runtime_state(self) -> None:
        """Reset all per-session runtime state before (re)starting.

        Called at the top of start() to ensure a clean slate on every
        start — whether first run or a stop/start within the same process.
        Without this, stale _probe_state, _last_quoted_price, etc. from
        the previous session cause incorrect behaviour on the second start.
        """

        def note_reset_failure(component: str, exc: Exception) -> None:
            log_event(
                "debug",
                "runtime_state_reset_skipped",
                f"Runtime reset skipped {component}: {exc}",
            )

        # Reset probe state to empty initial value (lock for thread safety)
        with self._probe_lock:
            self._probe_state = {
                "active": False,
                "buy_tid": None,
                "sell_tid": None,
                "buy_price": Decimal("0"),
                "sell_price": Decimal("0"),
                "tibet_price": Decimal("0"),
                "attempt": 0,
                "max_attempts": 5,
                "confirmed_price": None,
                "confirmed_at": 0,
                "launched_at": 0,
                "last_wait_log_at": 0,
                "last_discovery_mid_price": Decimal("0"),
                "last_discovery_arb_gap_bps": Decimal("0"),
                "last_discovery_tibet_price": Decimal("0"),
                "last_discovery_reason": "",
                "last_discovery_at": 0,
                # Iterative floor-tightening (option 2). After a probe is
                # confirmed, we keep launching tighter probes until one gets
                # taken — that empirically locates the true safe floor instead
                # of stopping at the first comfortable buffer (50 bps default).
                # tightening_buffer_bps starts at SNIPER_BUFFER_BPS and shrinks
                # by SNIPER_FLOOR_TIGHTEN_STEP_BPS each successful round; once
                # a probe gets taken we restore safe_buffer_bps and stop.
                "tightening_buffer_bps": 0,
                "safe_buffer_bps": 0,
                "floor_converged": False,
            }
        self._last_quoted_price = {"buy": Decimal("0"), "sell": Decimal("0")}
        self._last_quoted_plain_mid = {"buy": Decimal("0"), "sell": Decimal("0")}
        self._watcher_data = {
            "last_xch_reserve": 0,
            "last_token_reserve": 0,
            "triggered": False,
            "change_pct": 0.0,
            "direction": "",
            "last_change_ts": 0,
            "last_confirmed_price_xch": "0",
            "polls": 0,
            "triggers": 0,
        }
        self._mempool_price_refresh_needed = False
        self._last_tibet_shock = {
            "at": 0.0,
            "direction": "",
            "pct": 0.0,
            "sides": tuple(),
            "tiers": tuple(),
        }
        self._last_bulk_create_time = 0
        self._force_requote = {"buy": False, "sell": False}
        self._loop_count = 0
        # Reset startup flags
        if hasattr(self, "_startup_coin_recheck_done"):
            self._startup_coin_recheck_done = False
        if hasattr(self, "_startup_repost_done"):
            self._startup_repost_done = False
        self._graceful_migration = {
            "active": False,
            "phase": "idle",
            "protected_buy_ids": [],
            "protected_sell_ids": [],
            "started_at": 0,
        }
        self._wallet_sync_stale_cycle = False
        self._wallet_sync_was_stale = False
        self._consecutive_unhealthy = 0
        self._sweep_protection = {}
        self._recent_sweep_events = []
        self._last_toxicity_live_cancel = {
            "buy": {"at": 0.0, "signature": ""},
            "sell": {"at": 0.0, "signature": ""},
        }
        try:
            self.market_toxicity_guard.reset()
        except Exception as exc:
            note_reset_failure("market_toxicity_guard", exc)
        try:
            from sweep_coordinator import reset_coordinator as _reset_sweeps

            _reset_sweeps()
        except Exception as exc:
            note_reset_failure("sweep_coordinator", exc)
        try:
            from dynamic_amm_buffer import reset_buffer as _reset_dynamic_buffer

            _reset_dynamic_buffer()
        except Exception as exc:
            note_reset_failure("dynamic_amm_buffer", exc)
        self._adaptive_target_backoff_until = {"buy": 0.0, "sell": 0.0}
        self._last_adaptive_offer_targets = {"buy": 0, "sell": 0}
        self._last_pricing_success_ts = 0
        # Reset position baselines so stale wallet comparisons from the
        # previous CAT/session don't trigger false drift alarms.
        self._position_baseline_cat = None
        self._position_baseline_net_cat = None
        self._position_baseline_at = 0
        # Full risk-manager session reset — clears inventory, circuit
        # breaker, volatility, and all market data caches so nothing from
        # the previous CAT/session leaks into the new one.
        if self.risk_manager:
            self.risk_manager.reset_session()
        # Clear startup gate so background threads re-wait on next start
        self._startup_complete.clear()
        log_event("debug", "runtime_state_reset", "Runtime state reset for new session")

    def start(self) -> bool:
        """Start the bot loop in a background thread.

        Also launches health monitor and price watcher threads (V1 parity).
        Returns True if started, False if already running.
        """
        if self._running:
            return False

        self._reset_runtime_state()

        # Database already initialised at app startup (api_server.py).
        # Just reload config to pick up any .env changes from GUI.
        cfg.reload()
        self.runtime_monitor.reset_session()
        self._recovery_state.update(
            {
                "active": False,
                "phase": "idle",
                "reason": "",
                "started_at": 0.0,
                "last_transition_at": 0.0,
                "entered_loop": 0,
                "under_target_streak": 0,
                "wallet_stale_streak": 0,
                "probe_churn_streak": 0,
                "create_stall_streak": 0,
                "healthy_streak": 0,
                "buy_deficit": 0,
                "sell_deficit": 0,
                "cycle_probe_churn": False,
                "cycle_create_stalled": False,
            }
        )
        self._clear_alert("bot_recovery")

        # ---- Preflight / Doctor checks ----
        # Run structured readiness checks before starting.
        # If any check fails, block startup with a detailed report.
        try:
            from doctor import run_preflight

            preflight = run_preflight(force=True)
            log_event(
                "info",
                "preflight_run",
                preflight.summary,
                data={
                    "can_start": preflight.can_start,
                    "duration_ms": round(preflight.duration_ms, 1),
                },
            )
            if not preflight.can_start:
                failures = [c for c in preflight.checks if c.status == "fail"]
                fail_msgs = "; ".join(f"{c.name}: {c.message}" for c in failures[:3])
                log_event(
                    "error",
                    "preflight_blocked",
                    f"Bot start blocked by preflight: {fail_msgs}",
                )
                self._emit_alert(
                    "preflight_blocked",
                    "error",
                    "Preflight Failed",
                    f"Cannot start: {fail_msgs}",
                    action="run_doctor",
                    action_label="View Report",
                )
                self._set_state(
                    running=False, status="blocked", preflight=preflight.to_dict()
                )
                return False
        except Exception as e:
            # Preflight failure should not block startup — fall through
            # to the legacy watch-only check below.
            log_event(
                "warning", "preflight_error", f"Preflight could not run: {str(e)[:160]}"
            )

        # Legacy watch-only guard — kept as fallback in case preflight
        # import fails or is incomplete. The preflight system checks this
        # via check_wallet_can_sign() above, but we keep this as defense.
        try:
            from wallet import get_wallet_type

            if get_wallet_type() == "sage":
                from wallet_sage import get_current_key

                key = get_current_key() or {}
                has_secrets = key.get("has_secrets", False)
                if not has_secrets:
                    fp = key.get("fingerprint")
                    msg = "Active Sage wallet is watch-only and cannot sign offers"
                    if fp:
                        msg += f" (fingerprint {fp})"
                    log_event("error", "bot_start_blocked_watch_only", msg)
                    self._emit_alert(
                        "wallet_signing",
                        "error",
                        "Wallet Cannot Sign",
                        msg
                        + ". Log in to a Sage wallet with secrets before starting or resuming the bot.",
                        action="open_wallet_picker",
                        action_label="Change Wallet",
                    )
                    self._set_state(running=False, status="blocked")
                    return False
        except Exception as e:
            log_event(
                "warning",
                "bot_start_signing_check_failed",
                f"Could not verify Sage signing capability before start: {str(e)[:160]}",
            )

        self._running = True
        self._start_time = time.time()
        self._set_state(running=True, status="starting")
        self._circuit_breaker_offer_safed = False
        self._clear_alert("circuit_breaker")
        self._clear_alert("preflight_blocked")
        self._clear_alert("wallet_signing")
        self._clear_alert("buy_disabled")
        self._clear_alert("sell_disabled")
        self._clear_alert("cancel_retries")
        self._clear_alert("bot_recovery")

        # Clear any previous stop signal so ladder creation works
        self.offer_manager._stop_requested = False

        # Emit immediately (on the request thread) so the console and
        # system log have something to show before the background thread
        # finishes its slower startup sync.
        log_event(
            "info", "bot_starting", "Bot starting — syncing with wallet, please wait..."
        )

        slog("STARTUP", "Launching bot-loop thread")
        # Main trading loop
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="bot-loop"
        )
        self._thread.start()

        # Health monitor thread (V1 parity)
        self._health_thread = threading.Thread(
            target=self._health_monitor_thread, daemon=True, name="health-monitor"
        )
        self._health_thread.start()

        # Price watcher thread (V1 parity)
        self._watcher_thread = threading.Thread(
            target=self._price_watcher_thread, daemon=True, name="price-watcher"
        )
        self._watcher_thread.start()

        # Mempool watcher — pre-emptive price intelligence
        # Polls Coinset mempool every 5s for pending Tibet pool spends,
        # giving up to one block (~18-54s) early warning of incoming swaps.
        # Only starts if COINSET_ENABLED is True.
        #
        # F78 (2026-04-17): if pair_id isn't cached yet (startup hasn't
        # resolved it), we schedule a retry on each main-loop tick via
        # self._mempool_watcher_needs_start. Previously this block
        # failed silently and the watcher never started for the session.
        self._mempool_watcher_needs_start = False
        if (
            _mempool_watcher_mod
            and getattr(cfg, "COINSET_ENABLED", True)
            and cfg.CAT_ASSET_ID
        ):
            started = self._try_start_mempool_watcher(log_skip=True)
            if not started:
                self._mempool_watcher_needs_start = True

        # Coin watcher thread (lifecycle tracking)
        self._coin_watcher_thread = threading.Thread(
            target=self._coin_watcher_thread_run, daemon=True, name="coin-watcher"
        )
        self._coin_watcher_thread.start()
        # If outbound Splash broadcasting is enabled, keep inbound listening on
        # as well so the listener stats and pair-specific intake stay live.
        if getattr(cfg, "SPLASH_ENABLED", False) and not getattr(
            cfg, "SPLASH_RECEIVE_ENABLED", False
        ):
            cfg.SPLASH_RECEIVE_ENABLED = True
            log_event(
                "info",
                "splash_receive_auto",
                "Splash incoming listener auto-enabled alongside outbound broadcast",
            )

        # Splash incoming watcher thread (classifies inbound P2P offers).
        # Extracted into _start_splash_receive() so the liveness watchdog
        # can restart this daemon if it dies mid-session — without that,
        # a crashed Splash receive thread silently stopped classifying
        # inbound P2P offers while the bot happily kept quoting.
        self._start_splash_receive()

        # V3: Auto-start Splash P2P node if enabled
        if getattr(cfg, "SPLASH_ENABLED", False) and getattr(
            cfg, "SPLASH_AUTO_START", True
        ):
            try:
                started = self.splash_node.start()
                if started:
                    log_event(
                        "info",
                        "splash_node_auto",
                        "Splash P2P node auto-started with bot",
                    )
                # If binary not found, splash_node.start() already logged the message
            except Exception as e:
                log_event(
                    "warning",
                    "splash_node_auto_failed",
                    f"Failed to auto-start Splash node: {e}",
                )

        # AMM monitor — starts background polling thread for live reserve data
        if getattr(cfg, "TIBET_PAIR_ID", "").strip():
            try:
                self.amm_monitor.start()
            except Exception as _amm_err:
                log_event(
                    "warning",
                    "amm_monitor_start_failed",
                    f"AMM Monitor could not start: {_amm_err}",
                )

        # Runtime monitor — tracks fill activity, conditions, diagnostics
        try:
            self.runtime_monitor.start()
        except Exception as _rm_err:
            log_event(
                "warning",
                "runtime_monitor_start_failed",
                f"Runtime monitor could not start: {_rm_err}",
            )

        log_event(
            "info",
            "bot_started",
            "Bot loop started (with health, price, coin, AMM, and runtime monitors)",
        )
        return True

    def stop(self) -> bool:
        """Stop the bot loop gracefully.

        Returns True if stopped, False if not running.
        """
        if not self._running:
            return False

        self._running = False
        self._set_state(running=False, status="stopping")

        # Signal offer_manager to interrupt any in-progress ladder creation.
        # Without this, a 50-offer create_ladder loop keeps running for
        # minutes after stop() returns, because the 10s join timeout
        # expires but the thread is still alive creating offers.
        self.offer_manager._stop_requested = True
        try:
            self.coin_manager.stop_topup(wait_secs=10)
        except Exception as e:
            log_event(
                "debug", "stop_topup_failed", f"stop_topup raised during shutdown: {e}"
            )

        # Wake the watcher event so price watcher thread exits promptly
        self._watcher_event.set()

        # Stop AMM monitor
        try:
            self.amm_monitor.stop()
        except Exception as e:
            log_event(
                "debug",
                "amm_monitor_stop_failed",
                f"AMM Monitor stop raised during shutdown: {e}",
            )

        # Stop mempool watcher
        if _mempool_watcher_mod:
            try:
                _mempool_watcher_mod.stop_watcher()
            except Exception as e:
                log_event(
                    "debug",
                    "mempool_watcher_stop_failed",
                    f"Mempool watcher stop raised during shutdown: {e}",
                )

        # Wait for current cycle to finish (max 30 seconds — ladder
        # creation needs time to bail out gracefully)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=30)

        if self._splash_receive_thread and self._splash_receive_thread.is_alive():
            self._splash_receive_thread.join(timeout=5)

        # Join all background watcher threads
        for _t_name, _t_ref in [
            ("health_monitor", self._health_thread),
            ("price_watcher", self._watcher_thread),
            ("coin_watcher", self._coin_watcher_thread),
            ("dexie_repost", self._startup_repost_thread),
        ]:
            if _t_ref and _t_ref.is_alive():
                _t_ref.join(timeout=10)
                if _t_ref.is_alive():
                    log_event(
                        "warning",
                        "thread_join_timeout",
                        f"{_t_name} thread did not exit within 10s",
                    )

        # V3: Stop Splash node
        if self.splash_node.is_running():
            try:
                self.splash_node.stop()
            except Exception as e:
                log_event(
                    "debug",
                    "splash_node_stop_failed",
                    f"Splash node stop raised during shutdown: {e}",
                )

        self._set_state(status="stopped")

        # Clear all operational alerts so they don't linger on the GUI after stop
        for alert_id in (
            "circuit_breaker",
            "bot_recovery",
            "cancel_retries",
            "buy_disabled",
            "sell_disabled",
        ):
            self._clear_alert(alert_id)

        log_event("info", "bot_stopped", "Bot loop stopped")
        return True

    def is_running(self) -> bool:
        """Check if the bot loop is running."""
        return self._running

    def _current_splash_pair_label(self) -> str:
        ticker = (
            str(getattr(cfg, "CAT_TICKER_ID", "") or getattr(cfg, "CAT_NAME", "CAT"))
            .strip()
            .upper()
        )
        if ticker.endswith("_XCH"):
            return ticker.replace("_XCH", "/XCH")
        return f"{ticker}/XCH"

    def get_splash_receive_stats(self) -> Dict:
        """Return inbound Splash listening state + DB-backed counts."""
        asset_id = str(getattr(cfg, "CAT_ASSET_ID", "") or "").strip().lower()
        try:
            from database import get_splash_incoming_stats

            stats = get_splash_incoming_stats(asset_id=asset_id)
        except Exception:
            stats = {
                "total": 0,
                "new": 0,
                "processed": 0,
                "ignored": 0,
                "expired": 0,
                "relevant": 0,
                "last_received_at": None,
                "last_relevant_at": None,
            }

        stats["enabled"] = bool(getattr(cfg, "SPLASH_RECEIVE_ENABLED", False))
        stats["pair_asset_id"] = asset_id
        stats["pair_label"] = self._current_splash_pair_label()
        stats["poll_secs"] = getattr(self, "_splash_receive_interval", 5)
        stats["batch_size"] = getattr(self, "_splash_receive_batch_size", 10)

        # Daemon-side metrics (peer count, offers_received/broadcasted,
        # reachability) — scraped from splash.exe's --listen-metrics
        # endpoint by the SplashNode poller. Empty dict when the node
        # isn't running or the endpoint isn't configured. The GUI uses
        # these to distinguish "splash silent because no peers" from
        # "splash has peers but offer-hook broken".
        try:
            if getattr(self, "splash_node", None) is not None:
                stats["node_metrics"] = self.splash_node.get_metrics() or {}
            else:
                stats["node_metrics"] = {}
        except Exception:
            stats["node_metrics"] = {}

        return stats

    def _resolve_splash_view_offer(self):
        """Return a wallet-native view_offer callable when available."""
        try:
            from wallet import get_wallet_type

            wallet_type = str(get_wallet_type() or "").strip().lower()
        except Exception:
            wallet_type = (
                str(getattr(cfg, "WALLET_TYPE", "sage") or "sage").strip().lower()
            )

        if wallet_type != "sage":
            return wallet_type, None

        try:
            from wallet_sage import view_offer as sage_view_offer

            return wallet_type, sage_view_offer
        except Exception as e:
            if not self._splash_receive_parser_warned:
                self._splash_receive_parser_warned = True
                log_event(
                    "warning",
                    "splash_receive_parser",
                    f"Incoming Splash parsing unavailable: {e}",
                )
            return wallet_type, None

    def _process_splash_incoming_batch(self):
        """Classify newly received Splash offers for the active CAT/XCH pair."""
        if not getattr(cfg, "SPLASH_RECEIVE_ENABLED", False):
            return

        from database import get_splash_incoming_offers, update_splash_incoming_status

        pending = get_splash_incoming_offers(
            status="new", limit=self._splash_receive_batch_size
        )
        if not pending:
            return

        asset_id = str(getattr(cfg, "CAT_ASSET_ID", "") or "").strip().lower()
        pair_label = self._current_splash_pair_label()
        wallet_type, view_offer = self._resolve_splash_view_offer()

        if view_offer is None:
            if wallet_type != "sage":
                for offer in pending:
                    update_splash_incoming_status(
                        offer["id"], "ignored", pair_hint="unsupported"
                    )
                self._emit("splash_incoming", self.get_splash_receive_stats())
            return

        relevant_found = 0
        processed_any = False

        for offer in reversed(pending):
            offer_id = int(offer.get("id", 0) or 0)
            bech32 = str(offer.get("offer_bech32") or "").strip()
            if not offer_id or not bech32:
                if offer_id:
                    update_splash_incoming_status(
                        offer_id, "ignored", pair_hint="invalid"
                    )
                    processed_any = True
                continue

            try:
                viewed = view_offer(bech32)
            except Exception as e:
                log_event(
                    "debug",
                    "splash_receive_view_error",
                    f"Could not inspect inbound Splash offer {offer_id}: {e}",
                )
                continue

            if not viewed:
                continue

            classified = classify_offer_for_asset(viewed, asset_id)
            pair_hint = classified.get("pair_hint") or "unknown"

            if classified.get("relevant"):
                update_splash_incoming_status(
                    offer_id, "processed", pair_hint=pair_hint
                )
                relevant_found += 1
                processed_any = True
                log_event(
                    "info",
                    "splash_incoming_relevant",
                    f"Relevant inbound Splash offer ({classified.get('side', 'unknown')} {pair_label})",
                )
            else:
                update_splash_incoming_status(offer_id, "ignored", pair_hint=pair_hint)
                processed_any = True

        if processed_any:
            payload = self.get_splash_receive_stats()
            payload["relevant_found"] = relevant_found
            self._emit("splash_incoming", payload)

    def _start_splash_receive(self):
        """Start (or restart) the Splash incoming watcher thread.

        Extracted so the liveness watchdog (_restart_dead_threads) can
        re-invoke this if the daemon dies. Safe to call multiple times:
        if the current thread is still alive it is left alone.
        """
        existing = getattr(self, "_splash_receive_thread", None)
        if existing is not None and existing.is_alive():
            return
        self._splash_receive_thread = threading.Thread(
            target=self._splash_receive_thread_run,
            daemon=True,
            name="splash-receive",
        )
        self._splash_receive_thread.start()

    def _splash_receive_thread_run(self):
        """Background classifier for inbound Splash offers."""
        slog("THREAD", "splash-receive waiting for startup_complete gate...")
        self._startup_complete.wait()
        if not self._running:
            return

        slog("THREAD", "splash-receive gate released — starting work")
        log_thread_start("splash-receive")
        log_event(
            "info",
            "splash_receive_started",
            f"Splash receive watcher active (polling every {self._splash_receive_interval}s)",
        )

        while self._running:
            try:
                self._process_splash_incoming_batch()
            except Exception as e:
                log_event(
                    "debug",
                    "splash_receive_error",
                    f"Splash receive watcher error: {e}",
                )

            for _ in range(self._splash_receive_interval):
                if not self._running:
                    break
                time.sleep(1)

        log_event("info", "splash_receive_exit", "Splash receive watcher stopped")

    # -------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------

    def _run_loop(self):
        """The main trading loop — runs forever until stopped."""
        log_event("info", "bot_loop_init", "Initialising bot loop...")

        # Startup: sync state from wallet
        # Background threads wait for this to finish before writing to DB.
        self._startup_sync()
        slog(
            "STARTUP",
            "========== _startup_sync COMPLETE — releasing thread gates ==========",
        )
        self._startup_complete.set()  # Ungate background threads

        self._set_state(status="running")

        while self._running:
            loop_start = time.time()

            try:
                self._run_one_cycle()
            except Exception as e:
                log_event(
                    "error",
                    "loop_error",
                    f"Error in bot loop cycle: {e}\n{traceback.format_exc()}",
                )

            # Update timing
            self._last_loop_duration = time.time() - loop_start

            # Slow-iteration watchdog: warn if a single cycle took > 5 minutes.
            # This usually means a wallet RPC call is hanging. The cycle already
            # completed (or errored out) — this is purely diagnostic logging.
            if self._last_loop_duration > 300:
                log_event(
                    "warning",
                    "slow_iteration",
                    f"Slow cycle: {self._last_loop_duration:.0f}s — possible RPC hang. "
                    f"Normal cycles should complete in < {cfg.LOOP_SECONDS}s.",
                )
            self._loop_count += 1
            self._set_state(loop_count=self._loop_count)

            # Sleep until next cycle — OR wake early if price watcher detects a swap
            sleep_time = max(1, cfg.LOOP_SECONDS - self._last_loop_duration)
            watcher_triggered = self._watcher_event.wait(timeout=sleep_time)
            self._watcher_event.clear()

            if watcher_triggered and self._running:
                with self._watcher_lock:
                    watcher_info = (
                        f"{self._watcher_data['direction']} "
                        f"({self._watcher_data['change_pct']:.3f}%)"
                    )
                    self._watcher_data["triggered"] = False
                log_event(
                    "info",
                    "watcher_wake",
                    f"Fast wake — Tibet swap detected: {watcher_info}",
                )

            # Process mempool watcher signals (pre-emptive price intelligence)
            self._drain_mempool_signals(in_cycle=False)

        log_event("info", "bot_loop_exit", "Bot loop exited cleanly")

    def _defensive_cancel_active_tiers(self, side: str = None) -> set:
        """Return the set of tiers currently in-flight from a recent defensive
        cancel. Used by the requote path so it doesn't duplicate cancels the
        mempool watcher already submitted. Entries expire after the
        defensive-cancel grace window (enough for on-chain confirmation +
        coin return).

        ``side`` (2026-04-22) optionally narrows the result to only the tiers
        in-flight for that side. Without it (legacy callers), any tier with
        at least one side in-flight is returned. Tracked entries may be
        plain tier strings (legacy, both-sides implied) or ``(tier, side)``
        tuples produced by side-aware cancels.
        """
        info = getattr(self, "_last_defensive_cancel", None)
        if not info:
            return set()
        tiers = info.get("tiers")
        if not tiers:
            return set()
        try:
            at = float(info.get("at", 0) or 0)
        except Exception:
            return set()
        if at <= 0:
            return set()
        if time.time() - at > self._defensive_cancel_grace_secs:
            return set()
        if not isinstance(tiers, (set, list, tuple)):
            return set()
        result = set()
        side_lc = side.lower() if isinstance(side, str) else None
        for entry in tiers:
            if isinstance(entry, tuple) and len(entry) == 2:
                tier_name, tier_side = entry
                if side_lc is None or tier_side == side_lc:
                    result.add(tier_name)
            else:
                # Legacy entry: plain tier string implies both sides in-flight.
                result.add(entry)
        return result

    def _defensive_cancel_inner_offers(self, reason: str) -> int:
        """Backwards-compat wrapper — cancel inner tier only."""
        return self._defensive_cancel_tiers(("inner",), reason)

    def _pending_cancel_wallet_ids(self, side: str) -> set:
        """Return wallet-visible offers excluded from normal DB-open views.

        These are usually lifecycle=pending-cancel rows. They should not count
        as deployable ladder slots, but Sage can still fill them until the
        cancel confirms, so create-first replacement paths must wait.
        """
        try:
            pending = getattr(self, "_pending_cancel_wallet_ids_by_side", {}) or {}
            ids = pending.get((side or "").lower(), set()) or set()
            return set(ids)
        except Exception:
            return set()

    def _pending_cancel_settle_counts(
        self, sides: tuple = ("buy", "sell")
    ) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for side in sides or ("buy", "sell"):
            side_norm = str(side or "").strip().lower()
            if side_norm not in ("buy", "sell"):
                continue
            pending = self._pending_cancel_wallet_ids(side_norm)
            if pending:
                counts[side_norm] = len(pending)
        return counts

    def _confirmed_reserves_for_watcher(self, xch_reserve, token_reserve) -> tuple:
        """Return confirmed reserves in the same units as _fetch_tibet_reserves."""
        raw_hint = False
        xch_out = None
        token_out = None
        try:
            xch_dec = Decimal(str(xch_reserve))
            raw_hint = abs(xch_dec) > Decimal("1000000")
            xch_out = xch_dec / Decimal("1000000000000") if raw_hint else xch_dec
        except Exception:
            xch_out = None

        try:
            token_dec = Decimal(str(token_reserve))
            if raw_hint:
                decimals = int(getattr(cfg, "CAT_DECIMALS", 3) or 3)
                token_dec = token_dec / (Decimal(10) ** Decimal(decimals))
            token_out = token_dec
        except Exception:
            token_out = None

        return (
            float(xch_out) if xch_out is not None else None,
            float(token_out) if token_out is not None else None,
        )

    def _log_pending_cancel_notice(
        self,
        severity: str,
        event_type: str,
        message: str,
        *,
        side: str = "",
        pending_count: int = 0,
        data: Optional[Dict] = None,
        key: Optional[str] = None,
        repeat_secs: Optional[float] = None,
    ) -> bool:
        """Log pending-cancel waits once per unchanged state.

        Wallet-active pending cancels can last several cycles. Repeating the
        same warning every loop makes the dashboard look worse than the bot's
        actual state, so unchanged counts are suppressed for a short window.
        """
        side_norm = str(side or (data or {}).get("side") or "").strip().lower()
        try:
            count = int(pending_count or (data or {}).get("pending_count") or 0)
        except Exception:
            count = 0
        notice_key = key or f"{event_type}:{side_norm or 'all'}"
        now = time.time()
        repeat_window = float(
            repeat_secs
            if repeat_secs is not None
            else getattr(self, "_pending_cancel_notice_repeat_secs", 300.0)
        )
        state = (getattr(self, "_pending_cancel_notice_seen", {}) or {}).get(
            notice_key
        ) or {}
        last_at = float(state.get("last_at") or 0.0)
        last_count = state.get("pending_count")
        try:
            unchanged = int(last_count) == count
        except Exception:
            unchanged = False

        if last_at > 0 and unchanged and (now - last_at) < repeat_window:
            state["suppressed_repeats"] = int(state.get("suppressed_repeats") or 0) + 1
            self._pending_cancel_notice_seen[notice_key] = state
            return False

        payload = dict(data or {})
        if side_norm:
            payload.setdefault("side", side_norm)
        payload.setdefault("pending_count", count)
        suppressed = int(state.get("suppressed_repeats") or 0)
        if suppressed:
            payload["suppressed_repeats"] = suppressed
        log_event(severity, event_type, message, data=payload)
        self._pending_cancel_notice_seen[notice_key] = {
            "last_at": now,
            "pending_count": count,
            "side": side_norm,
            "suppressed_repeats": 0,
        }
        return True

    def _defer_probe_launch_for_pending_cancel(
        self, launch_reason: Optional[str]
    ) -> Optional[str]:
        if not launch_reason:
            return launch_reason

        pending_counts = self._pending_cancel_settle_counts()
        pending_total = sum(pending_counts.values())
        if pending_total <= 0:
            return launch_reason

        log_event(
            "info",
            "probe_launch_deferred_pending_cancel_settle",
            f"Probe launch deferred - {pending_total} wallet-active pending "
            f"cancel(s) must settle before {launch_reason}",
            data={
                "launch_reason": launch_reason,
                "pending_cancel_counts": pending_counts,
                "pending_cancel_total": pending_total,
            },
        )
        return None

    def _filter_pending_cancel_wallet_ids_by_dexie(
        self, pending_by_side: Dict[str, set]
    ) -> Dict[str, set]:
        """Drop Sage-lagged IDs once Dexie repeatedly reports terminal cancel.

        Sage can keep cancelled offers in the non-completed offer list after
        Dexie has already removed them from the live book.  We still block on
        Sage by default, but if Dexie's offer-detail endpoint reports the same
        wallet-visible ID as CANCELLED/EXPIRED for a small number of consecutive
        samples, allow replacement creation for that slot.  ACTIVE, PENDING,
        COMPLETED, missing, and unreachable Dexie states remain blocking.
        """
        filtered = {
            "buy": set((pending_by_side or {}).get("buy") or set()),
            "sell": set((pending_by_side or {}).get("sell") or set()),
        }
        active_ids = set(filtered["buy"]) | set(filtered["sell"])
        seen = getattr(self, "_pending_cancel_dexie_terminal_seen", {}) or {}
        for tid in list(seen.keys()):
            if tid not in active_ids:
                seen.pop(tid, None)

        min_hits = max(
            1,
            int(getattr(self, "_pending_cancel_dexie_terminal_confirmations", 2) or 2),
        )
        max_checks = max(
            0,
            int(getattr(self, "_pending_cancel_dexie_max_checks_per_cycle", 8) or 8),
        )
        detail_timeout = max(
            0.5,
            float(
                getattr(self, "_pending_cancel_dexie_detail_timeout_secs", 2.0) or 2.0
            ),
        )
        now = time.time()
        checks = 0

        for side in ("buy", "sell"):
            ordered_ids = sorted(
                filtered[side],
                key=lambda tid: (0 if tid in seen else 1, tid),
            )
            for tid in ordered_ids:
                entry = seen.get(tid) or {}
                if int(entry.get("hits") or 0) >= min_hits and entry.get("status") in (
                    DEXIE_STATUS_CANCELLED,
                    DEXIE_STATUS_EXPIRED,
                ):
                    filtered[side].discard(tid)
                    continue
                if checks >= max_checks:
                    continue
                try:
                    local_offer = get_offer(tid) or {}
                    dexie_id = str(local_offer.get("dexie_id") or "").strip()
                except Exception as e:
                    dexie_id = ""
                    seen.pop(tid, None)
                    log_event(
                        "debug",
                        "pending_cancel_dexie_lookup_failed",
                        f"Could not load local offer {tid[:16]}... for "
                        f"Dexie cancel-settle check: {e}",
                    )

                if not dexie_id:
                    continue

                try:
                    checks += 1
                    dexie_offer = get_offer_detail(
                        dexie_id,
                        timeout=detail_timeout,
                        cache_ttl_secs=3,
                    )
                except Exception as e:
                    seen.pop(tid, None)
                    log_event(
                        "debug",
                        "pending_cancel_dexie_lookup_failed",
                        f"Could not fetch Dexie detail for {tid[:16]}...: {e}",
                    )
                    continue

                if not isinstance(dexie_offer, dict):
                    seen.pop(tid, None)
                    continue

                raw_status = dexie_offer.get("status")
                try:
                    status = int(raw_status)
                except Exception:
                    status = raw_status

                if status not in (DEXIE_STATUS_CANCELLED, DEXIE_STATUS_EXPIRED):
                    seen.pop(tid, None)
                    continue

                entry = seen.get(tid) or {}
                if entry.get("status") == status:
                    entry["hits"] = int(entry.get("hits") or 0) + 1
                else:
                    entry["hits"] = 1
                entry.update(
                    {
                        "side": side,
                        "status": status,
                        "dexie_id": dexie_id,
                        "last_seen": now,
                    }
                )
                seen[tid] = entry

                if int(entry.get("hits") or 0) < min_hits:
                    continue

                filtered[side].discard(tid)
                if not entry.get("released_logged"):
                    label = (
                        "cancelled" if status == DEXIE_STATUS_CANCELLED else "expired"
                    )
                    log_event(
                        "info",
                        "pending_cancel_dexie_terminal_release",
                        f"{side} offer {tid[:16]}... still appears active in "
                        f"Sage, but Dexie confirmed {label} across "
                        f"{entry['hits']} sample(s); allowing replacement "
                        f"creation for that slot",
                        data={
                            "trade_id": tid,
                            "side": side,
                            "dexie_id": dexie_id,
                            "dexie_status": status,
                            "confirmations": int(entry["hits"]),
                        },
                    )
                    entry["released_logged"] = True

        self._pending_cancel_dexie_terminal_seen = seen
        return filtered

    def _pending_cancel_settle_first_seen(self, trade_id: str, now: float) -> float:
        """Prefer DB cancel-attempt age when starting a settle-watch entry."""
        try:
            offer = get_offer(trade_id) or {}
        except Exception:
            offer = {}
        for key in ("cancel_last_attempt_at", "updated_at", "last_seen", "created_at"):
            raw = (offer or {}).get(key)
            if not raw:
                continue
            try:
                from datetime import datetime, timezone

                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = float(dt.timestamp())
                if ts > 0:
                    return ts
            except Exception:
                continue
        return now

    def _track_pending_cancel_settle_watchdog(
        self, pending_by_side: Dict[str, set]
    ) -> None:
        """Queue stale wallet-active pending cancels back into cancel retries.

        The anti-stack guard treats wallet-visible / DB-cancelled offers as
        still fillable, which is correct. But if the DB already moved them out
        of the open pending-cancel lifecycle, bot_health will not re-drive them.
        This watchdog keeps creation blocked while nudging stale IDs back
        through OfferManager's retry path after a conservative settle window.
        """
        now = time.time()
        retry_secs = float(
            getattr(self, "_pending_cancel_settle_retry_secs", 300.0) or 300.0
        )
        max_retries = int(getattr(self, "_pending_cancel_settle_max_retries", 5) or 5)

        active: Dict[str, str] = {}
        for side, ids in (pending_by_side or {}).items():
            side_norm = str(side or "").strip().lower()
            if side_norm not in ("buy", "sell"):
                continue
            for raw_tid in ids or set():
                tid = str(raw_tid or "").strip()
                if tid:
                    active[tid] = side_norm

        seen = getattr(self, "_pending_cancel_settle_seen", {}) or {}
        for tid in list(seen.keys()):
            if tid not in active:
                seen.pop(tid, None)

        retry_map = getattr(
            getattr(self, "offer_manager", None), "_pending_cancel_retries", None
        )
        exhausted_by_side: Dict[str, list] = {"buy": [], "sell": []}
        queued_by_side: Dict[str, list] = {"buy": [], "sell": []}
        for tid, side in active.items():
            if tid not in seen:
                seen[tid] = {
                    "side": side,
                    "first_seen": self._pending_cancel_settle_first_seen(tid, now),
                    "last_retry": 0.0,
                    "retries": 0,
                }
            info = seen[tid]
            info["side"] = side
            first_seen = float(info.get("first_seen") or now)
            age = now - first_seen
            if age < retry_secs:
                continue

            retries = int(info.get("retries") or 0)
            if retries >= max_retries:
                if not info.get("exhausted_logged"):
                    exhausted_by_side.setdefault(side, []).append(
                        {
                            "trade_id": tid,
                            "age_secs": int(age),
                            "retries": retries,
                        }
                    )
                    info["exhausted_logged"] = True
                continue

            last_retry = float(info.get("last_retry") or 0.0)
            if last_retry > 0 and now - last_retry < retry_secs:
                continue
            if isinstance(retry_map, dict) and tid not in retry_map:
                retry_map[tid] = {
                    "attempts": 0,
                    "first_failed": first_seen,
                    "reason": "wallet_active_pending_cancel_settle",
                }
                info["last_retry"] = now
                info["retries"] = retries + 1
                queued_by_side.setdefault(side, []).append(
                    {
                        "trade_id": tid,
                        "age_secs": int(age),
                        "retry_count": info["retries"],
                    }
                )

        for side, entries in queued_by_side.items():
            if not entries:
                continue
            ages = [int(entry.get("age_secs") or 0) for entry in entries]
            min_age = min(ages) if ages else 0
            max_age = max(ages) if ages else 0
            age_phrase = (
                f"{min_age}s" if min_age == max_age else f"{min_age}-{max_age}s"
            )
            log_event(
                "info",
                "pending_cancel_settle_retry_queued",
                f"{len(entries)} {side} cancel(s) stayed wallet-active for "
                f"{age_phrase} after DB cancel; queued cancel retries and "
                f"kept new {side} creation blocked",
                data={
                    "side": side,
                    "queued_count": len(entries),
                    "age_secs_min": min_age,
                    "age_secs_max": max_age,
                    "trade_ids": [entry["trade_id"] for entry in entries],
                    "retry_counts": [entry["retry_count"] for entry in entries],
                },
            )

        for side, entries in exhausted_by_side.items():
            if not entries:
                continue
            ages = [int(entry.get("age_secs") or 0) for entry in entries]
            retries = [int(entry.get("retries") or 0) for entry in entries]
            log_event(
                "warning",
                "pending_cancel_settle_retry_exhausted",
                f"{len(entries)} {side} cancel(s) still wallet-active after "
                f"{max(retries) if retries else max_retries} retry nudge(s); "
                "keeping creation blocked until Sage stops reporting them active",
                data={
                    "side": side,
                    "exhausted_count": len(entries),
                    "age_secs_min": min(ages) if ages else 0,
                    "age_secs_max": max(ages) if ages else 0,
                    "trade_ids": [entry["trade_id"] for entry in entries],
                    "retries": retries,
                },
            )

        self._pending_cancel_settle_seen = seen

    def _remember_tibet_shock(
        self,
        *,
        direction: str,
        pct: float,
        sides: tuple,
        tiers: tuple,
        reason: str,
        at: Optional[float] = None,
    ) -> None:
        try:
            self._last_tibet_shock = {
                "at": float(at if at is not None else time.time()),
                "direction": str(direction or ""),
                "pct": float(pct or 0),
                "sides": tuple(sides or ()),
                "tiers": tuple(tiers or ()),
                "reason": str(reason or ""),
            }
        except Exception:
            pass

    def _record_shock_requote(
        self,
        side: str,
        mid_price: Decimal,
        source: str,
        severity: str,
        new_count: int = 0,
        replaced_count: int = 0,
    ) -> None:
        side_norm = str(side or "").strip().lower()
        if side_norm not in ("buy", "sell"):
            return
        try:
            records = getattr(self, "_last_shock_requote", None)
            if not isinstance(records, dict):
                records = {"buy": {}, "sell": {}}
                self._last_shock_requote = records
            records[side_norm] = {
                "at": time.time(),
                "mid_price": str(mid_price),
                "source": str(source or ""),
                "severity": str(severity or ""),
                "new_count": int(new_count or 0),
                "replaced_count": int(replaced_count or 0),
            }
        except Exception:
            pass

    def _recent_shock_requote_guard(
        self, side: str, direction: str, new_price_xch
    ) -> Optional[Dict]:
        """Return recent reprice details when it already covers a shock."""
        side_norm = str(side or "").strip().lower()
        if side_norm not in ("buy", "sell"):
            return None
        try:
            record = (getattr(self, "_last_shock_requote", {}) or {}).get(
                side_norm
            ) or {}
            at = float(record.get("at") or 0)
            if at <= 0:
                return None
            guard_secs = max(
                float(getattr(self, "_defensive_cancel_grace_secs", 90.0) or 90.0),
                float(getattr(cfg, "LOOP_SECONDS", 60) or 60) * 3.0,
            )
            age = time.time() - at
            if age < 0 or age > guard_secs:
                return None

            remaining_bps = Decimal("0")
            quoted_mid = Decimal(str(record.get("mid_price") or "0"))
            confirmed_price = Decimal(str(new_price_xch or "0"))
            if quoted_mid > 0 and confirmed_price > 0:
                direction_norm = str(direction or "").strip().lower()
                if side_norm == "buy" and direction_norm == "down":
                    remaining_bps = max(
                        Decimal("0"),
                        (quoted_mid - confirmed_price) / quoted_mid * Decimal("10000"),
                    )
                elif side_norm == "sell" and direction_norm == "up":
                    remaining_bps = max(
                        Decimal("0"),
                        (confirmed_price - quoted_mid) / quoted_mid * Decimal("10000"),
                    )
            else:
                if age > float(getattr(cfg, "LOOP_SECONDS", 60) or 60):
                    return None

            try:
                allowed_bps = Decimal(str(getattr(cfg, "MIN_EDGE_BPS", 100) or 100))
            except Exception:
                allowed_bps = Decimal("100")
            allowed_bps = max(Decimal("50"), allowed_bps)
            if remaining_bps > allowed_bps:
                return None

            details = dict(record)
            details["age_secs"] = age
            details["guard_secs"] = guard_secs
            details["remaining_bps"] = str(remaining_bps)
            details["allowed_bps"] = str(allowed_bps)
            return details
        except Exception:
            return None

    def _recent_same_price_shock_requote_guard(
        self, side: str, requote_mid: Decimal
    ) -> Optional[Dict]:
        """Return recent shock reprice details when Step 9 would repeat it."""
        side_norm = str(side or "").strip().lower()
        if side_norm not in ("buy", "sell"):
            return None
        try:
            record = (getattr(self, "_last_shock_requote", {}) or {}).get(
                side_norm
            ) or {}
            at = float(record.get("at") or 0)
            if at <= 0:
                return None

            guard_secs = max(
                float(getattr(self, "_defensive_cancel_grace_secs", 90.0) or 90.0),
                float(getattr(cfg, "LOOP_SECONDS", 60) or 60) * 3.0,
            )
            age = time.time() - at
            if age < 0 or age > guard_secs:
                return None

            if (
                max(
                    int(record.get("new_count") or 0),
                    int(record.get("replaced_count") or 0),
                )
                <= 0
            ):
                return None

            quoted_mid = Decimal(str(record.get("mid_price") or "0"))
            current_mid = Decimal(str(requote_mid or "0"))
            if quoted_mid <= 0 or current_mid <= 0:
                return None

            drift_bps = abs(current_mid - quoted_mid) / quoted_mid * Decimal("10000")
            try:
                inner_threshold = Decimal(
                    str(
                        getattr(cfg, "REQUOTE_DRIFT_INNER", Decimal("0.003"))
                        or Decimal("0.003")
                    )
                ) * Decimal("10000")
            except Exception:
                inner_threshold = Decimal("30")
            allowed_bps = max(Decimal("5"), inner_threshold)
            if drift_bps > allowed_bps:
                return None

            details = dict(record)
            details["age_secs"] = age
            details["guard_secs"] = guard_secs
            details["drift_bps"] = str(drift_bps)
            details["allowed_bps"] = str(allowed_bps)
            return details
        except Exception:
            return None

    def _recent_tibet_shock_guard(self, side: str) -> Optional[Dict]:
        """Return recent shock details when side should avoid create-first requote.

        A confirmed Tibet shock only makes one side urgent: price down exposes
        buys, price up exposes sells. The other side can wait because
        ``requote_side`` creates replacements before old cancels settle, which
        temporarily stacks fillable offers in Sage.
        """
        try:
            shock = getattr(self, "_last_tibet_shock", {}) or {}
            at = float(shock.get("at") or 0)
            if at <= 0:
                return None

            guard_secs = max(
                float(getattr(self, "_defensive_cancel_grace_secs", 90.0) or 90.0),
                float(getattr(cfg, "LOOP_SECONDS", 60) or 60) * 3.0,
            )
            age = time.time() - at
            if age > guard_secs:
                return None

            protected_sides = {
                str(s or "").strip().lower()
                for s in (shock.get("sides") or ())
                if str(s or "").strip()
            }
            side_norm = str(side or "").strip().lower()
            if not protected_sides or side_norm in protected_sides:
                return None

            details = dict(shock)
            details["age_secs"] = age
            details["guard_secs"] = guard_secs
            details["protected_sides"] = tuple(sorted(protected_sides))
            return details
        except Exception:
            return None

    def _defensive_cancel_tiers(
        self, tiers: tuple, reason: str, sides: tuple = ("buy", "sell")
    ) -> int:
        """F83: bulk-cancel offers in the given tiers.

        Triggered when the mempool watcher detects a pool spend. The TIER
        SET should grow with the magnitude of the move:

          * small move (<= 5%):   ('inner',)            — minimum exposure
          * medium move (5-10%):  ('inner', 'mid')      — inner+mid exposed
          * large move (> 10%):   ('inner', 'mid', 'outer')

        The SIDES tuple filters which side's offers are at risk from the
        move (2026-04-22):

          * direction "up"   (CAT price rising) → our SELL offers are now
            underpriced vs market → ("sell",)
          * direction "down" (CAT price falling) → our BUY offers are now
            overpriced vs market → ("buy",)
          * unknown direction → both sides (default), caller's choice

        For confirmed price_move signals we know the magnitude AND the
        direction, so both can be narrowed. Prior behaviour cancelled both
        sides unconditionally, which wasted 50% of the cancels and the
        associated fee coins on offers that were now MORE attractive to
        hold, not less.

        Live evidence (test 3, 2026-04-18 ~13.1% Tibet drop):
          - F82 cancelled inner-tier on imminent_swap → 0 inner fills ✓
          - But 3 MID-TIER buy fills happened ~7 minutes later, when
            Tibet was at 0.000115 and our mid bids at 0.000118-0.000119
            were still profitable arbs. F82 didn't catch them because it
            only cancels inner.

        Returns number of trade_ids submitted for cancel.
        """
        tiers_lc = tuple(t.lower() for t in tiers)
        sides_lc = tuple(s.lower() for s in sides)
        try:
            from database import get_open_offers
            from config import cfg as _cfg

            asset_id = getattr(_cfg, "CAT_ASSET_ID", "") or ""
            buys = []
            sells = []
            if "buy" in sides_lc:
                buys = [
                    o
                    for o in (get_open_offers(side="buy", cat_asset_id=asset_id) or [])
                    if (o.get("tier") or "").lower() in tiers_lc
                ]
            if "sell" in sides_lc:
                sells = [
                    o
                    for o in (get_open_offers(side="sell", cat_asset_id=asset_id) or [])
                    if (o.get("tier") or "").lower() in tiers_lc
                ]
            tids = [o.get("trade_id") for o in (buys + sells) if o.get("trade_id")]
            tier_label = "+".join(tiers_lc)
            side_label = "+".join(sides_lc)
            if not tids:
                log_event(
                    "info",
                    "defensive_cancel_skip",
                    f"Defensive cancel: no {tier_label} {side_label} offers "
                    f"to cancel (reason={reason})",
                )
                return 0
            log_event(
                "info",
                "defensive_cancel_start",
                f"Defensive cancel: firing on {len(tids)} {tier_label} {side_label} offers "
                f"({len(buys)} buy + {len(sells)} sell, reason={reason})",
            )
            try:
                results = self.offer_manager.cancel_offers(
                    tids,
                    reason=reason,
                    force_storm=True,
                    skip_confirmation=True,
                )
            except Exception as e:
                log_event(
                    "warning",
                    "defensive_cancel_call_failed",
                    f"cancel_offers raised: {e}",
                )
                return 0
            if isinstance(results, dict):
                successes = sum(
                    1
                    for result in results.values()
                    if isinstance(result, dict) and result.get("success")
                )
                failures = len(results) - successes
                if failures > 0:
                    log_event(
                        "warning",
                        "defensive_cancel_failed",
                        f"Defensive cancel submitted {successes}/{len(tids)} "
                        f"{tier_label} {side_label} offers (reason={reason}); "
                        f"{failures} failed or remain queued for retry.",
                    )
                return successes
            return len(tids)
        except Exception as e:
            log_event(
                "warning",
                "defensive_cancel_failed",
                f"_defensive_cancel_tiers exception: {e}",
            )
            return 0

    def _record_confirmed_price_move(self, sig: Dict, in_cycle: bool) -> None:
        """Publish confirmed reserve moves to pricing and recent-swap state."""
        try:
            new_xch = sig.get("new_xch_reserve")
            new_tok = sig.get("new_tok_reserve")
            old_xch = sig.get("old_xch_reserve")
            old_tok = sig.get("old_tok_reserve")
            now_ts = time.time()
            watcher_xch, watcher_tok = self._confirmed_reserves_for_watcher(
                new_xch if new_xch is not None else old_xch,
                new_tok if new_tok is not None else old_tok,
            )

            with self._watcher_lock:
                self._watcher_data["triggered"] = True
                self._watcher_data["change_pct"] = abs(
                    float(sig.get("magnitude_pct", 0) or 0)
                )
                self._watcher_data["direction"] = str(sig.get("direction", "") or "")
                self._watcher_data["last_change_ts"] = now_ts
                self._watcher_data["triggers"] = (
                    int(self._watcher_data.get("triggers", 0) or 0) + 1
                )
                if watcher_xch is not None:
                    self._watcher_data["last_xch_reserve"] = watcher_xch
                if watcher_tok is not None:
                    self._watcher_data["last_token_reserve"] = watcher_tok
                if sig.get("new_price_xch") is not None:
                    self._watcher_data["last_confirmed_price_xch"] = str(
                        sig.get("new_price_xch")
                    )

            injected = False
            if new_xch is not None and new_tok is not None:
                inject = getattr(self.price_engine, "inject_tibet_reserves", None)
                if callable(inject):
                    injected = bool(
                        inject(
                            pair_id=sig.get("pair_id"),
                            asset_id=getattr(cfg, "CAT_ASSET_ID", None),
                            xch_reserve=new_xch,
                            token_reserve=new_tok,
                            fetched_at=now_ts,
                        )
                    )
            if not injected:
                invalidate = getattr(self.price_engine, "invalidate_tibet_cache", None)
                if callable(invalidate):
                    invalidate()

            if in_cycle:
                self._mempool_price_refresh_needed = True
        except Exception as e:
            log_event(
                "debug",
                "mempool_price_cache_refresh_failed",
                f"Could not refresh Tibet cache from confirmed reserves: {e}",
            )

    def _refresh_price_if_mempool_move_pending(
        self, mid_price: Decimal, arb_gap: Decimal
    ):
        """Refresh the current cycle's price after an in-cycle reserve move."""
        if not getattr(self, "_mempool_price_refresh_needed", False):
            return mid_price, arb_gap, None

        self._mempool_price_refresh_needed = False
        try:
            fresh_price = self.price_engine.get_price(
                cfg.CAT_ASSET_ID, cfg.CAT_DECIMALS, cfg.CAT_TICKER_ID
            )
            if not fresh_price:
                return mid_price, arb_gap, None

            fresh_mid = Decimal(str(fresh_price.get("mid_price", 0) or 0))
            if fresh_mid <= 0:
                return mid_price, arb_gap, fresh_price

            fresh_arb = Decimal(str(fresh_price.get("arb_gap_bps", arb_gap) or 0))
            old_mid = mid_price
            self._current_mid_price = fresh_mid
            self._set_state(mid_price=str(fresh_mid), arb_gap_bps=str(fresh_arb))
            self.risk_manager.update_arb_gap(fresh_arb)
            log_event(
                "info",
                "mempool_reprice_applied",
                f"Applied confirmed reserve price before trading decisions: "
                f"{old_mid:.8f} -> {fresh_mid:.8f}",
                data={
                    "old_mid": str(old_mid),
                    "new_mid": str(fresh_mid),
                    "arb_gap_bps": str(fresh_arb),
                    "tibet_price": str(fresh_price.get("tibet_price", "")),
                },
            )
            self._emit(
                "price_update",
                {
                    "mid_price": str(fresh_mid),
                    "dexie_price": str(fresh_price.get("dexie_price", "")),
                    "tibet_price": str(fresh_price.get("tibet_price", "")),
                    "arb_gap_bps": str(fresh_arb),
                    "spread_bps": self._bot_state.get("spread_bps", "0"),
                },
            )
            return fresh_mid, fresh_arb, fresh_price
        except Exception as e:
            log_event(
                "warning",
                "mempool_reprice_failed",
                f"Could not refresh price after confirmed reserve move: {e}",
            )
            return mid_price, arb_gap, None

    def _refresh_cycle_price_after_mempool_move(
        self, price_data: Dict, mid_price: Decimal, arb_gap: Decimal
    ):
        """Refresh both cycle scalars and the source price_data dict."""
        refreshed_mid, refreshed_arb, fresh_price = (
            self._refresh_price_if_mempool_move_pending(mid_price, arb_gap)
        )
        if fresh_price:
            return fresh_price, refreshed_mid, refreshed_arb
        return price_data, refreshed_mid, refreshed_arb

    def _handle_tibet_shock_defensive_cancel(
        self,
        *,
        pct: float,
        direction: str,
        sig: Dict,
        reason_prefix: str,
        done_event: str,
        deferred_event: str,
        below_event: str,
        suppressed_event: str,
        source_label: str,
    ) -> None:
        try:
            _min_edge = float(getattr(cfg, "MIN_EDGE_BPS", 100) or 100)
        except Exception:
            _min_edge = 100.0

        action = evaluate_tibet_shock(pct, direction, cfg)
        trigger_pct = action.trigger_pct
        if not action.cancel:
            log_event(
                "info",
                below_event,
                f"{source_label} moved {pct:.3f}% - below defensive-cancel "
                f"trigger of {trigger_pct:.2f}% (scales with "
                f"MIN_EDGE_BPS={_min_edge:.0f}). Holding offers.",
            )
            return

        tiers = action.tiers
        at_risk_sides = action.sides
        reason = f"{reason_prefix}_{pct:.2f}pct_{direction}"
        pending_counts = self._pending_cancel_settle_counts()
        pending_total = sum(pending_counts.values())
        if pending_total > 0:
            for _side in at_risk_sides:
                _side_norm = str(_side or "").strip().lower()
                if _side_norm in self._force_requote:
                    self._force_requote[_side_norm] = True
            log_event(
                "info",
                deferred_event,
                f"Defensive cancel deferred for {'+'.join(at_risk_sides)} - "
                f"{pending_total} wallet-active pending cancel(s) must "
                "settle before another shock cancel wave",
                data={
                    "direction": str(direction or ""),
                    "pct": pct,
                    "tiers": tuple(tiers),
                    "at_risk_sides": tuple(at_risk_sides),
                    "pending_cancel_counts": pending_counts,
                    "pending_cancel_total": pending_total,
                    "source": source_label,
                },
            )
            self._remember_tibet_shock(
                direction=direction,
                pct=pct,
                sides=at_risk_sides,
                tiers=tiers,
                reason=reason,
            )
            return

        guarded_sides = []
        cancel_sides = []
        for _side in at_risk_sides:
            _guard = self._recent_shock_requote_guard(
                _side,
                direction,
                sig.get("new_price_xch"),
            )
            if _guard:
                guarded_sides.append((_side, _guard))
            else:
                cancel_sides.append(_side)
        if guarded_sides:
            try:
                guarded_details = [
                    {
                        "side": _side,
                        "age_secs": round(float(_guard.get("age_secs") or 0), 1),
                        "remaining_bps": str(_guard.get("remaining_bps") or "0"),
                        "allowed_bps": str(_guard.get("allowed_bps") or "0"),
                        "source": _guard.get("source", ""),
                    }
                    for _side, _guard in guarded_sides
                ]
            except Exception:
                guarded_details = []
            guarded_label = "+".join(_side for _side, _guard in guarded_sides)
            log_event(
                "info",
                suppressed_event,
                f"Defensive cancel suppressed for {guarded_label}: "
                "recent shock requote already repriced the at-risk side "
                "and the residual move is still inside edge",
                data={
                    "direction": str(direction or ""),
                    "pct": pct,
                    "tiers": tuple(tiers),
                    "guarded": guarded_details,
                    "source": source_label,
                },
            )
            self._remember_tibet_shock(
                direction=direction,
                pct=pct,
                sides=at_risk_sides,
                tiers=tiers,
                reason=reason,
            )
        if not cancel_sides:
            return

        try:
            n = self._defensive_cancel_tiers(
                tiers=tiers, sides=tuple(cancel_sides), reason=reason
            )
            log_event(
                "info",
                done_event,
                f"{source_label} {pct:.2f}% {direction} - "
                f"cancelled {n} {'+'.join(cancel_sides)} offers "
                f"across {'+'.join(tiers)}",
            )
            try:
                now_ts = time.time()
                self._last_defensive_cancel = {
                    "at": now_ts,
                    "tiers": {(t, s) for t in tiers for s in cancel_sides},
                    "reason": reason,
                }
                self._remember_tibet_shock(
                    direction=direction,
                    pct=pct,
                    sides=at_risk_sides,
                    tiers=tiers,
                    reason=reason,
                    at=now_ts,
                )
            except Exception:
                pass
        except Exception as _dc_err:
            log_event(
                "warning",
                "mempool_defensive_cancel_failed",
                f"{source_label} defensive cancel failed: {_dc_err}",
            )

    def _drain_mempool_signals(self, in_cycle: bool = False) -> None:
        """Drain pending mempool watcher signals and act on them.

        Called from two places:
          1. Between cycles (in_cycle=False) — original behaviour. Sets
             _watcher_event on fill_imminent so the loop wakes early.
          2. Mid-cycle, just before fill detection (in_cycle=True) — F11
             fix. The wake is no longer needed because we're already running
             the work; we still drain the signal so it doesn't pile up.
        """
        if not (_mempool_watcher_mod and self._running):
            return
        try:
            w = _mempool_watcher_mod._watcher_instance
            if not w:
                return
            for sig in w.get_pending_signals():
                sig_type = sig.get("type")
                if sig_type == "imminent_swap":
                    direction = (
                        str(sig.get("direction", "unknown") or "unknown")
                        .strip()
                        .lower()
                    )
                    pct = abs(float(sig.get("magnitude_pct", 0) or 0))
                    projected = (
                        sig.get("source") == "mempool_projected_reserves"
                        and direction in ("up", "down")
                        and pct > 0
                    )
                    if projected:
                        self._handle_tibet_shock_defensive_cancel(
                            pct=pct,
                            direction=direction,
                            sig=sig,
                            reason_prefix="mempool_preconfirm_price_move",
                            done_event="mempool_preconfirm_defensive_cancel_done",
                            deferred_event=(
                                "mempool_preconfirm_cancel_deferred_pending_cancel_settle"
                            ),
                            below_event="mempool_preconfirm_cancel_below_trigger",
                            suppressed_event=(
                                "mempool_defensive_cancel_suppressed_recent_requote"
                            ),
                            source_label="preconfirm_price_move",
                        )
                    wake_msg = (
                        f"Mempool: pending pool-coin spend projected {direction} "
                        f"{pct:.3f}% - pre-confirm protection evaluated"
                        if projected
                        else "Mempool: pending pool-coin spend detected - "
                        "waking bot; will evaluate magnitude once reserves confirm"
                    )
                    log_event(
                        "info",
                        "mempool_imminent_wake",
                        wake_msg,
                        data={
                            "pool_coin_id": (sig.get("pool_coin_id") or "")[:24],
                            "direction": direction,
                            "pct": pct,
                            "source": sig.get("source", ""),
                            "confidence": sig.get("confidence", ""),
                        },
                    )
                    # 2026-05-01: direction-aware mempool signals can cancel
                    # the vulnerable side before confirmation. Unknown
                    # signals stay wake-only because they have no magnitude
                    # or side direction. The prior
                    # inner-tier blanket cancel fired on every tiny TibetSwap
                    # trade (20 cancels per trigger) even when inner offers
                    # were still well inside the edge. On Chia, cancels cost
                    # real money (one spend + fee coin each, ~19s confirm),
                    # and nothing lands faster than the next block anyway,
                    # so the previous behaviour paid to race a race we can
                    # never win. The confirmed price_move handler below still
                    # backs this up when pre-confirm direction is unavailable,
                    # using magnitude-aware tier scaling. The wake
                    # below is still useful: it lets fill detection run
                    # immediately if the spend was a taker hitting us.
                    if not in_cycle:
                        self._watcher_event.set()
                elif sig_type == "fill_imminent":
                    coin_id = sig.get("coin_id", "?")[:16]
                    log_event(
                        "info",
                        "mempool_fill_wake",
                        f"Mempool: offer coin {coin_id}... spent — "
                        + (
                            "processing fill detection now"
                            if in_cycle
                            else "waking early for fill detection"
                        ),
                    )
                    if not in_cycle:
                        # Wake the bot immediately so fill_tracker can
                        # confirm via wallet RPC and post a replacement
                        # this cycle rather than waiting up to LOOP_SECONDS.
                        self._watcher_event.set()
                elif sig_type == "price_move":
                    direction = sig.get("direction", "?")
                    pct_raw = sig.get("magnitude_pct", 0)
                    pct = abs(float(pct_raw or 0))
                    log_event(
                        "info",
                        "mempool_price_confirmed",
                        f"Pool reserves confirmed: {direction} {pct:.3f}% "
                        f"(XCH {sig.get('delta_xch', 0):+d} mojos)",
                    )
                    self._record_confirmed_price_move(sig, in_cycle=in_cycle)
                    # F82 (2026-04-18): defensive cancel on confirmed pool
                    # move. F81 only fired on imminent_swap which missed
                    # swaps that confirmed before the 5s mempool poll caught
                    # them. Live test 2026-04-18 had two sell fills 29-37
                    # SECONDS AFTER price_move was logged — plenty of
                    # warning, but no defensive action was taken. This
                    # closes that gap. Bot's normal emergency requote still
                    # runs after; defensive cancel is just faster.
                    #
                    # Threshold (2026-04-22 post-mortem): scale by the
                    # ladder's inner edge. The inner ring sits MIN_EDGE_BPS
                    # away from mid, so a pool move only threatens inner
                    # offers once it approaches a meaningful fraction of
                    # that edge. Firing at a flat 50 bps caused pointless
                    # churn on 6.5%-spread books (post-mortem: 0.53% move
                    # triggered 20 cancels + a full ladder rebuild when
                    # inner offers were still 177 bps in-edge and
                    # profitable). Floor at 50 bps so we still react on
                    # very tight books where min-edge is small.
                    # Operators can override the auto threshold with
                    # TIBET_SHOCK_CANCEL_TRIGGER_PCT when a pair needs a
                    # more explicit AMM-shock trigger.
                    try:
                        _min_edge = float(getattr(cfg, "MIN_EDGE_BPS", 100) or 100)
                    except Exception:
                        _min_edge = 100.0
                    action = evaluate_tibet_shock(pct, direction, cfg)
                    trigger_pct = action.trigger_pct
                    if action.cancel:
                        # F83 (2026-04-18): graduated tier set based on
                        # magnitude. Big AMM moves expose more than just
                        # the inner tier. Test 3 saw 3 mid-tier buy fills
                        # 7 min after a -13.1% Tibet drop because F82
                        # only cancelled inner. The mid offers were still
                        # profitable arbs against the new mid.
                        tiers = action.tiers
                        # 2026-04-22: narrow cancels to the at-risk side.
                        # direction "up"   → CAT price rising → our SELL
                        #                    offers now cheap → cancel sells.
                        # direction "down" → CAT price falling → our BUY
                        #                    offers now expensive → cancel buys.
                        # Unknown direction → fall back to both sides.
                        at_risk_sides = action.sides
                        reason = f"mempool_price_move_{pct:.2f}pct_{direction}"
                        pending_counts = self._pending_cancel_settle_counts()
                        pending_total = sum(pending_counts.values())
                        if pending_total > 0:
                            for _side in at_risk_sides:
                                _side_norm = str(_side or "").strip().lower()
                                if _side_norm in self._force_requote:
                                    self._force_requote[_side_norm] = True
                            log_event(
                                "info",
                                "mempool_defensive_cancel_deferred_pending_cancel_settle",
                                f"Defensive cancel deferred for {'+'.join(at_risk_sides)} - "
                                f"{pending_total} wallet-active pending cancel(s) must "
                                "settle before another shock cancel wave",
                                data={
                                    "direction": str(direction or ""),
                                    "pct": pct,
                                    "tiers": tuple(tiers),
                                    "at_risk_sides": tuple(at_risk_sides),
                                    "pending_cancel_counts": pending_counts,
                                    "pending_cancel_total": pending_total,
                                },
                            )
                            self._remember_tibet_shock(
                                direction=direction,
                                pct=pct,
                                sides=at_risk_sides,
                                tiers=tiers,
                                reason=reason,
                            )
                            continue
                        guarded_sides = []
                        cancel_sides = []
                        for _side in at_risk_sides:
                            _guard = self._recent_shock_requote_guard(
                                _side,
                                direction,
                                sig.get("new_price_xch"),
                            )
                            if _guard:
                                guarded_sides.append((_side, _guard))
                            else:
                                cancel_sides.append(_side)
                        if guarded_sides:
                            try:
                                guarded_details = [
                                    {
                                        "side": _side,
                                        "age_secs": round(
                                            float(_guard.get("age_secs") or 0), 1
                                        ),
                                        "remaining_bps": str(
                                            _guard.get("remaining_bps") or "0"
                                        ),
                                        "allowed_bps": str(
                                            _guard.get("allowed_bps") or "0"
                                        ),
                                        "source": _guard.get("source", ""),
                                    }
                                    for _side, _guard in guarded_sides
                                ]
                            except Exception:
                                guarded_details = []
                            guarded_label = "+".join(
                                _side for _side, _guard in guarded_sides
                            )
                            log_event(
                                "info",
                                "mempool_defensive_cancel_suppressed_recent_requote",
                                f"Defensive cancel suppressed for {guarded_label}: "
                                "recent shock requote already repriced the at-risk side "
                                "and the confirmed residual move is still inside edge",
                                data={
                                    "direction": str(direction or ""),
                                    "pct": pct,
                                    "tiers": tuple(tiers),
                                    "guarded": guarded_details,
                                },
                            )
                            self._remember_tibet_shock(
                                direction=direction,
                                pct=pct,
                                sides=at_risk_sides,
                                tiers=tiers,
                                reason=reason,
                            )
                        if not cancel_sides:
                            continue
                        try:
                            n = self._defensive_cancel_tiers(
                                tiers=tiers, sides=tuple(cancel_sides), reason=reason
                            )
                            log_event(
                                "info",
                                "mempool_defensive_cancel_done",
                                f"price_move {pct:.2f}% {direction} — "
                                f"cancelled {n} {'+'.join(cancel_sides)} offers "
                                f"across {'+'.join(tiers)}",
                            )
                            # Record the cancel so the next cycle's requote
                            # knows the tier was just cleared and defers to
                            # ladder-fill instead of treating it as cold-start.
                            # 2026-04-22: record (tier, side) tuples so the
                            # opposite side (not at risk, not cancelled) isn't
                            # wrongly deferred by the requote path.
                            try:
                                now_ts = time.time()
                                self._last_defensive_cancel = {
                                    "at": now_ts,
                                    "tiers": {
                                        (t, s) for t in tiers for s in cancel_sides
                                    },
                                    "reason": reason,
                                }
                                self._remember_tibet_shock(
                                    direction=direction,
                                    pct=pct,
                                    sides=at_risk_sides,
                                    tiers=tiers,
                                    reason=reason,
                                    at=now_ts,
                                )
                            except Exception:
                                pass
                        except Exception as _dc_err:
                            log_event(
                                "warning",
                                "mempool_defensive_cancel_failed",
                                f"price_move defensive cancel failed: {_dc_err}",
                            )
                    else:
                        log_event(
                            "info",
                            "mempool_price_move_below_trigger",
                            f"Pool moved {pct:.3f}% — below defensive-cancel "
                            f"trigger of {trigger_pct:.2f}% (scales with "
                            f"MIN_EDGE_BPS={_min_edge:.0f}). Holding offers.",
                        )
        except Exception as _drain_err:
            # F81: previously silent — meant we couldn't tell if the watcher
            # signal flow was broken. Now logged at warning so anomalies
            # surface in the events feed.
            log_event(
                "warning",
                "mempool_drain_failed",
                f"Failed to drain mempool signals: {_drain_err}",
            )

    # -------------------------------------------------------------------
    # Startup sync
    # -------------------------------------------------------------------

    def _initialize_coinset_for_wallet_type(self, wallet_type: str) -> None:
        """Wire Coinset where it is safe for the active wallet backend.

        Sage cannot use Coinset as an inventory source because the light wallet
        owns address discovery, but point lookups by coin id are still useful
        chain truth for top-up submit verification.
        """
        if not getattr(cfg, "COINSET_ENABLED", True):
            return

        wallet_type = str(wallet_type or "").lower().strip()
        if wallet_type != "sage":
            return

        try:
            self.coin_manager._coinset_client = self.coinset_client
            log_event(
                "info",
                "coinset_ready",
                "Coinset point lookups enabled for Sage chain-truth checks; "
                "wallet RPC remains the inventory source",
            )
        except Exception as e:
            log_event(
                "info",
                "coinset_init_error",
                f"Coinset Sage chain-truth wiring failed: {e} - using wallet RPC only",
            )

    def _startup_sync(self):
        """Sync state from the Chia wallet on startup.

        Establishes baseline for fill detection, counts coins, etc.
        """
        slog("STARTUP", "========== _startup_sync BEGIN ==========")
        log_event("info", "startup_sync", "Syncing state from wallet...")

        # ── Config validation — log all warnings/errors before doing anything ──
        try:
            validation = cfg.validate()
            for warn in validation.get("warnings", []):
                log_event("warning", "config_validation", f"CONFIG WARNING: {warn}")
                slog("STARTUP", f"[WARN] CONFIG WARNING: {warn}")
            for err in validation.get("errors", []):
                log_event("error", "config_validation", f"CONFIG ERROR: {err}")
                slog("STARTUP", f"[ERROR] CONFIG ERROR: {err}")
            if validation.get("errors"):
                print(
                    f"[STARTUP] {len(validation['errors'])} config error(s) — check settings"
                )
            if not validation["warnings"] and not validation["errors"]:
                log_event("info", "config_validation", "Config validation OK")
        except Exception as e:
            log_event(
                "warning",
                "config_validation_failed",
                f"Config validation raised an exception: {e}",
            )

        # F18 (2026-04-08): startup self-test of external services.
        # Pings every external dependency the bot relies on (Sage,
        # Coinset, TibetSwap, Dexie, Spacescan) and emits a warning
        # alert for each one that's down. NEVER blocks startup —
        # the user is informed about what will be missing and chooses
        # whether to start the bot.
        try:
            self._run_startup_self_test()
        except Exception as _self_test_err:
            log_event(
                "warning",
                "startup_self_test_failed",
                f"Startup self-test raised: {_self_test_err}",
            )

        # ── Clear stale reservations from previous runtime ──────────
        try:
            from reservation_manager import ReservationManager

            cleared = ReservationManager().expire_all()
            if cleared:
                slog(
                    "STARTUP",
                    f"Cleared {cleared} stale reservation(s) from previous runtime",
                )
        except Exception as e:
            log_event(
                "warning",
                "reservation_clear_failed",
                f"Could not clear stale reservations on startup: {e}",
            )

        # ── SAFETY: Verify CAT wallet_id → asset_id mapping ──────────
        # Prevents the bot from trading the WRONG token if get_wallets()
        # mapped a different CAT to our configured wallet_id.
        # This is a hard stop — the bot refuses to start if the mapping
        # doesn't match, preventing costly wrong-token trades.
        try:
            from wallet import get_wallet_type

            if get_wallet_type() == "sage":
                from wallet_sage import _resolve_asset_id, _get_cat_asset_id

                resolved = _resolve_asset_id(cfg.CAT_WALLET_ID)
                configured = _get_cat_asset_id()
                if resolved and configured:
                    r_norm = resolved.lower().replace("0x", "").strip()
                    c_norm = configured.lower().replace("0x", "").strip()
                    if r_norm != c_norm:
                        msg = (
                            f"[SAFETY STOP] wallet_id {cfg.CAT_WALLET_ID} resolves to "
                            f"asset {resolved[:20]}... but .env has {configured[:20]}... "
                            f"— these are DIFFERENT tokens! Refusing to start."
                        )
                        log_event("error", "cat_mapping_mismatch", msg)
                        slog("STARTUP", msg)
                        self._running = False
                        self._set_state(
                            status="error",
                            error="CAT asset_id mismatch — wrong token detected",
                        )
                        return
                    else:
                        slog(
                            "STARTUP",
                            f"[OK] CAT mapping verified: wallet_id {cfg.CAT_WALLET_ID} "
                            f"-> {resolved[:20]}... matches .env config",
                        )
        except Exception as e:
            slog("STARTUP", f"[WARN] CAT mapping check skipped: {e}")

        try:
            # ---- Auto-detect wallet address for Spacescan verification ----
            # Pulls the receive address from the connected wallet (Sage or Chia).
            # This means it works for any wallet without needing .env config.
            try:
                from wallet import get_next_address, get_wallet_type

                addr_result = get_next_address(new_address=False)
                if addr_result and addr_result.get("success"):
                    cfg.WALLET_ADDRESS = addr_result["address"]
                    log_event(
                        "info",
                        "wallet_address_detected",
                        f"Wallet address: {cfg.WALLET_ADDRESS[:20]}...",
                    )

                    if get_wallet_type() == "sage" and getattr(
                        cfg, "SAGE_SET_CHANGE_ADDRESS", False
                    ):
                        try:
                            from wallet_sage import (
                                set_change_address as _sage_set_change_address,
                            )

                            change_result = _sage_set_change_address(cfg.WALLET_ADDRESS)
                            if change_result and change_result.get("success"):
                                log_event(
                                    "success",
                                    "sage_change_address_set",
                                    f"Sage change address set to "
                                    f"{cfg.WALLET_ADDRESS[:20]}... "
                                    f"for fingerprint {change_result.get('fingerprint')}",
                                )
                            else:
                                log_event(
                                    "warning",
                                    "sage_change_address_failed",
                                    f"Could not set Sage change address: "
                                    f"{(change_result or {}).get('error', 'unknown_error')}",
                                )
                        except Exception as e:
                            log_event(
                                "warning",
                                "sage_change_address_failed",
                                f"Error setting Sage change address: {e}",
                            )
                else:
                    log_event(
                        "warning",
                        "wallet_address_failed",
                        "Could not auto-detect wallet address — "
                        "Spacescan self-spend detection will be limited",
                    )
            except Exception as e:
                log_event(
                    "warning",
                    "wallet_address_error",
                    f"Wallet address detection failed: {e}",
                )

            # ---- Auto-resolve CAT metadata (TIBET_PAIR_ID, CAT_TICKER_ID, CAT_NAME) ----
            # Given only CAT_ASSET_ID, queries TibetSwap to fill any empty derived fields.
            # Only fills fields that are unset in .env — never overwrites explicit config.
            try:
                from cat_resolver import resolve_and_apply as _resolve_cat

                _cat_meta = _resolve_cat(cfg)
                if _cat_meta.get("pair_id"):
                    slog(
                        "STARTUP",
                        f"CAT metadata resolved — "
                        f"pair_id={_cat_meta['pair_id'][:20]}... "
                        f"ticker={_cat_meta.get('ticker_id')} "
                        f"name={_cat_meta.get('name')}",
                    )
                else:
                    slog(
                        "STARTUP",
                        "CAT metadata: token not found on TibetSwap "
                        "(no pair/ticker auto-resolved — check CAT_TICKER_ID in .env)",
                    )
            except Exception as e:
                log_event(
                    "warning",
                    "cat_resolver_failed",
                    f"CAT metadata auto-resolution failed (non-critical): {e}",
                )

            # Sync offers from wallet
            log_event(
                "info", "startup_wallet_sync", "Fetching offers from wallet RPC..."
            )
            open_buys, open_sells, closed = self.offer_manager.sync_from_wallet()
            log_event(
                "info",
                "startup_wallet_result",
                f"Wallet returned: {len(open_buys)} open buys, "
                f"{len(open_sells)} open sells, {len(closed)} closed",
            )

            # Set fill tracker baseline
            buy_ids = {o.get("trade_id", "") for o in open_buys if o.get("trade_id")}
            sell_ids = {o.get("trade_id", "") for o in open_sells if o.get("trade_id")}

            # Pre-seed fill tracker with pre-cleanup offer state.
            # This lets the first detect_fills() after startup detect any
            # offers that filled while the bot was offline (offline fills),
            # rather than silently treating the current state as the baseline.
            try:
                self.fill_tracker.set_baseline(buy_ids, sell_ids)
            except AttributeError:
                pass  # set_baseline not yet available — harmless

            # ---- Clean up stale database offers ----
            # The database may have offers marked 'open' from a previous session
            # that are no longer open in the wallet (cancelled, filled, expired).
            # Mark them as cancelled so the GUI doesn't show phantom offers.
            wallet_open_ids = buy_ids | sell_ids
            slog(
                "STARTUP",
                f"DB cleanup: wallet has {len(wallet_open_ids)} open offer IDs",
            )
            try:
                from database import get_open_offers, batch_cancel_stale_offers

                db_open = get_open_offers()
                log_event(
                    "info",
                    "db_cleanup_start",
                    f"DB has {len(db_open)} offers marked 'open', "
                    f"wallet has {len(wallet_open_ids)} truly open",
                )

                # Collect stale trade_ids (in DB but not in wallet)
                stale_ids = []
                stale_db_map = {}
                for db_offer in db_open:
                    tid = db_offer.get("trade_id", "")
                    if tid and tid not in wallet_open_ids:
                        stale_ids.append(tid)
                        stale_db_map[tid] = db_offer

                if stale_ids:
                    slog(
                        "STARTUP",
                        f"Found {len(stale_ids)} stale offers — checking status",
                    )
                    # Check completed offers from wallet to distinguish fills from cancels.
                    # Offers that filled while the bot was down must be marked "filled"
                    # not "cancelled" — otherwise PnL is permanently wrong.
                    fill_ids = set()
                    expire_ids = set()
                    pending_cancel_ids = set()
                    deferred_stale_ids = set()
                    try:
                        from wallet_sage import get_all_offers as sage_get_all

                        now_ts_cleanup = int(time.time())
                        remaining_stale = set(stale_ids)
                        completed_map: Dict[str, Dict] = {}
                        # Previously this call capped at the first 500 Sage
                        # completed offers; on a long live run, any stale
                        # offer that drifted past that window was silently
                        # classified as cancelled, erasing its P&L even
                        # when it had actually filled. Paginate until we've
                        # either resolved every stale_id or Sage returns an
                        # empty page (authoritative end-of-history). Hard
                        # cap at 40 pages / 20k offers so a pathological
                        # Sage response can't stall startup forever.
                        page_size = 500
                        # Hard cap at 100 pages (50k completed offers).
                        # Very busy wallets plus a long outage can in
                        # theory exceed this; when we hit it we log
                        # CRITICAL (persists to DB) so the operator sees
                        # the mismatch and can reconcile P&L manually.
                        max_pages = 100
                        page = 0
                        while page < max_pages:
                            start_idx = page * page_size
                            end_idx = start_idx + page_size
                            try:
                                page_offers = (
                                    sage_get_all(
                                        include_completed=True,
                                        start=start_idx,
                                        end=end_idx,
                                    )
                                    or []
                                )
                            except Exception as _page_err:
                                log_event(
                                    "warning",
                                    "db_cleanup_page_failed",
                                    f"Sage completed page {page} failed: {_page_err}",
                                )
                                break
                            if not page_offers:
                                break
                            for o in page_offers:
                                if not isinstance(o, dict):
                                    continue
                                tid = o.get("trade_id") or o.get("offer_id", "")
                                if tid:
                                    completed_map[tid] = o
                                    remaining_stale.discard(tid)
                            page += 1
                            if not remaining_stale:
                                break  # all stale_ids resolved, done paginating
                        if page >= max_pages and remaining_stale:
                            # Escalate to CRITICAL so the event persists
                            # via the storage-layer remap (post-Cat6 fix)
                            # and the operator sees it in post-startup
                            # review — misclassifying offline fills as
                            # cancelled understates P&L and can rebuild
                            # inventory against the wrong baseline.
                            log_event(
                                "critical",
                                "db_cleanup_page_cap_hit",
                                f"Stale-offer pagination hit the "
                                f"{max_pages * page_size}-offer safety cap "
                                f"with {len(remaining_stale)} ids still "
                                f"unchecked after startup. The remainder "
                                f"will be treated as cancelled. MANUAL "
                                f"P&L RECONCILIATION RECOMMENDED — some "
                                f"of these may actually have filled while "
                                f"the bot was offline.",
                                data={
                                    "unresolved_count": len(remaining_stale),
                                    "scanned_offers": max_pages * page_size,
                                    "action": "manual_reconcile_pnl",
                                },
                            )
                        for tid in stale_ids:
                            sage_offer = completed_map.get(tid)
                            if sage_offer:
                                status_val = sage_offer.get("status")
                                status_text = (
                                    str(status_val).upper()
                                    if isinstance(status_val, str)
                                    else ""
                                )
                                if status_val == 2 or status_text == "PENDING_CANCEL":
                                    pending_cancel_ids.add(tid)
                                    continue
                                mapped = map_sage_terminal_offer_status(
                                    status_val,
                                    sage_offer=sage_offer,
                                    local_offer=stale_db_map.get(tid, {}),
                                    now_ts=now_ts_cleanup,
                                )
                                if mapped == "filled":
                                    fill_ids.add(tid)
                                elif mapped == "expired":
                                    expire_ids.add(tid)
                                elif mapped is None:
                                    deferred_stale_ids.add(tid)
                    except Exception as e:
                        log_event(
                            "warning",
                            "db_cleanup_status_check_failed",
                            f"Could not check completed offers at startup: {e}",
                        )

                    local_expire_ids = collect_locally_expired_stale_offer_ids(
                        stale_ids,
                        stale_db_map,
                        already_resolved=(
                            fill_ids
                            | expire_ids
                            | pending_cancel_ids
                            | deferred_stale_ids
                        ),
                        now_ts=now_ts_cleanup,
                    )
                    if local_expire_ids:
                        expire_ids.update(local_expire_ids)
                        log_event(
                            "info",
                            "db_cleanup_local_expired",
                            f"Classified {len(local_expire_ids)} stale DB offers "
                            f"as expired from local offer max_time",
                        )

                    cancel_ids = [
                        t
                        for t in stale_ids
                        if t not in fill_ids
                        and t not in expire_ids
                        and t not in pending_cancel_ids
                        and t not in deferred_stale_ids
                    ]

                    if fill_ids:
                        from database import update_offer_status

                        for tid in fill_ids:
                            update_offer_status(tid, "filled")
                        log_event(
                            "info",
                            "db_cleanup_fills_recovered",
                            f"Marked {len(fill_ids)} offers as filled "
                            f"(filled while bot was offline)",
                        )

                    # Emit SSE fill events for offline fills + create fill rows
                    # so PnL matching works correctly without waiting for the
                    # housekeeping backfill cycle.
                    if fill_ids:
                        try:
                            from database import backfill_verified_fills_from_offers

                            _offline_fills = backfill_verified_fills_from_offers(
                                limit=len(fill_ids) + 10
                            )
                            for _off_fill in _offline_fills or []:
                                self._emit(
                                    "fill",
                                    {
                                        "side": _off_fill.get("side", ""),
                                        "price": str(
                                            _off_fill.get("price_xch")
                                            or _off_fill.get("price")
                                            or ""
                                        ),
                                        "size_xch": str(
                                            _off_fill.get("xch_amount") or ""
                                        ),
                                        "tier": _off_fill.get("tier", ""),
                                        "offline": True,
                                    },
                                )
                            if _offline_fills:
                                log_event(
                                    "info",
                                    "offline_fill_sse_emitted",
                                    f"Emitted SSE events for "
                                    f"{len(_offline_fills)} offline fill(s)",
                                )
                        except Exception as _sse_err:
                            log_event(
                                "warning",
                                "offline_fill_sse_failed",
                                f"Offline fill SSE emit failed (non-critical): "
                                f"{_sse_err}",
                            )

                    if expire_ids:
                        from database import update_offer_status as _uos

                        for tid in expire_ids:
                            _uos(tid, "expired")
                        log_event(
                            "info",
                            "db_cleanup_expired",
                            f"Marked {len(expire_ids)} offers as expired",
                        )

                    if pending_cancel_ids:
                        try:
                            from database import (
                                transition_offer as _to,
                                mark_cancel_attempted as _mca,
                            )
                        except Exception:
                            _to = None
                            _mca = None
                        for tid in pending_cancel_ids:
                            if _to:
                                try:
                                    _to(tid, "cancel_sent")
                                except Exception:
                                    pass
                            if _mca:
                                try:
                                    _mca(tid)
                                except Exception:
                                    pass
                        log_event(
                            "info",
                            "db_cleanup_pending_cancel_deferred",
                            f"Deferred {len(pending_cancel_ids)} stale offers still "
                            f"PENDING_CANCEL in Sage; pending-cancel verifier will reconcile",
                        )

                    if deferred_stale_ids:
                        log_event(
                            "warning",
                            "db_cleanup_stale_deferred",
                            f"Deferred {len(deferred_stale_ids)} stale offers with "
                            f"non-terminal Sage status; leaving open for next wallet sync",
                        )

                    cleaned = batch_cancel_stale_offers(cancel_ids) if cancel_ids else 0
                    total_cleaned = cleaned + len(fill_ids) + len(expire_ids)
                    total_deferred = len(pending_cancel_ids) + len(deferred_stale_ids)
                    slog(
                        "STARTUP",
                        f"Cleanup result: {cleaned} cancelled, "
                        f"{len(fill_ids)} filled, {len(expire_ids)} expired, "
                        f"{total_deferred} deferred",
                    )
                    if total_cleaned == len(stale_ids):
                        log_event(
                            "info",
                            "db_cleanup_done",
                            f"Cleaned {total_cleaned} stale DB offers "
                            f"({cleaned} cancelled, {len(fill_ids)} filled, "
                            f"{len(expire_ids)} expired)",
                        )
                    elif total_deferred:
                        log_event(
                            "info",
                            "db_cleanup_done",
                            f"Cleaned {total_cleaned}/{len(stale_ids)} stale DB offers "
                            f"({cleaned} cancelled, {len(fill_ids)} filled, "
                            f"{len(expire_ids)} expired, {total_deferred} deferred)",
                        )
                    else:
                        log_event(
                            "info",
                            "db_cleanup_done",
                            f"Cleaned {total_cleaned}/{len(stale_ids)} stale DB offers "
                            f"(some failed due to DB lock)",
                        )
                else:
                    log_event("info", "db_cleanup_done", "No stale DB offers found")
            except Exception as e:
                log_event(
                    "warning", "db_cleanup_failed", f"DB offer cleanup failed: {e}"
                )

            recovered_startup_trade_ids = set()

            # ---- Recover unknown offers ----
            # If the bot created offers on-chain but couldn't write to the DB
            # (e.g., DB was locked, bot crashed), import them now.
            try:
                from database import recover_unknown_offers

                all_wallet_offers = open_buys + open_sells
                recovery = recover_unknown_offers(all_wallet_offers, cfg.CAT_ASSET_ID)
                recovered_startup_trade_ids = {
                    str(tid) for tid in recovery.get("trade_ids", []) if tid
                }
                if recovery.get("recovered", 0) > 0:
                    log_event(
                        "info",
                        "startup_offer_recovery",
                        f"Recovered {recovery['recovered']} unknown offers from wallet "
                        f"(skipped {recovery['skipped']}, errors {recovery['errors']})",
                    )
                    # Update wallet_open_ids with the newly recovered offers
                    for offer in all_wallet_offers:
                        tid = offer.get("trade_id", "")
                        if tid:
                            wallet_open_ids.add(tid)
                else:
                    log_event(
                        "info",
                        "startup_offer_recovery",
                        "No unknown offers — DB and wallet in sync",
                    )
            except Exception as e:
                log_event(
                    "warning", "startup_recovery_failed", f"Offer recovery failed: {e}"
                )

            def _push_startup_market_snapshot():
                """Warm market intel and push a first dashboard snapshot early.

                On resume runs with many existing offers, the Dexie repost step can
                take a while. If we wait until after that, Market Health and Advisor
                sit in a placeholder state for too long even though the pair is
                already known and the bot is otherwise healthy.
                """
                try:
                    if self.market_intel:
                        self.market_intel.refresh_orderbook(force=True)
                except Exception as e:
                    log_event(
                        "debug",
                        "startup_orderbook_refresh_failed",
                        f"Startup orderbook refresh failed (non-critical): {e}",
                    )

                try:
                    offer_edges = self._get_live_offer_edges(open_buys, open_sells)
                    self._record_live_offer_edges(offer_edges)
                    health_data = self._augment_health_with_spacescan(
                        self.risk_manager.get_market_health(loop_count=self._loop_count)
                    )
                    cached_intel = {}
                    try:
                        cached_intel = (
                            self.market_intel.get_cached_data()
                            if self.market_intel
                            else {}
                        )
                    except Exception as e:
                        log_event(
                            "debug",
                            "startup_cached_intel_failed",
                            f"Startup cached intel fetch failed (non-critical): {e}",
                        )
                        cached_intel = {}

                    self._emit(
                        "dashboard_update",
                        {
                            "market_health": health_data,
                            "loop_count": self._loop_count,
                            "open_buys": len(buy_ids),
                            "open_sells": len(sell_ids),
                            "mid_price": str(self._current_mid_price or Decimal("0")),
                            "our_best_bid": offer_edges.get("our_best_bid", "0"),
                            "our_best_ask": offer_edges.get("our_best_ask", "0"),
                            "best_bid": str(cached_intel.get("best_bid", "0")),
                            "best_ask": str(cached_intel.get("best_ask", "0")),
                        },
                    )
                except Exception as e:
                    log_event(
                        "debug",
                        "startup_dashboard_update_failed",
                        f"Startup dashboard update failed (non-critical): {e}",
                    )

            # ---- Reload Dexie mappings + repost missing ----
            # The in-memory dexie_manager map is empty on startup.
            # 1) Load persisted trade_id→dexie_id mappings from DB
            # 2) For offers WITHOUT dexie_id, repost to Dexie to get links
            try:
                from database import get_trade_dexie_map, get_open_offers

                db_dexie_map = get_trade_dexie_map(cfg.CAT_ASSET_ID)
                if db_dexie_map:
                    self.dexie_manager._trade_dexie_map.update(db_dexie_map)
                    log_event(
                        "info",
                        "dexie_recovery",
                        f"Loaded {len(db_dexie_map)} Dexie mappings from database",
                    )

                # Check for offers missing dexie_id — repost them
                db_open = get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
                missing_dexie = [o for o in db_open if not o.get("dexie_id")]
                if missing_dexie and cfg.DEXIE_AUTO_POST:
                    # Build a lookup from trade_id → bech32 from wallet data
                    all_wallet = open_buys + open_sells
                    bech32_map = {}
                    for w in all_wallet:
                        tid = w.get("trade_id", "")
                        bech = w.get("offer", "") or w.get("offer_bech32", "")
                        if tid and bech:
                            bech32_map[tid] = bech

                    repost_count = 0
                    deferred_recovered = 0
                    for db_offer in missing_dexie:
                        tid = db_offer.get("trade_id", "")
                        if tid in recovered_startup_trade_ids:
                            deferred_recovered += 1
                            continue
                        bech = bech32_map.get(tid, "")
                        if bech and tid:
                            self.dexie_manager.queue_post(bech, tid, force=True)
                            repost_count += 1

                    if deferred_recovered > 0:
                        log_event(
                            "info",
                            "dexie_repost_deferred_recovered",
                            f"Deferred {deferred_recovered} recovered offer(s) until "
                            f"coin-linking and locked-coin reclaim finish",
                        )

                    if repost_count > 0:
                        log_event(
                            "info",
                            "dexie_repost",
                            f"Queued {repost_count} offers for Dexie posting "
                            f"(will post during loop cycles, "
                            f"{cfg.MAX_POSTS_PER_LOOP} per cycle)",
                        )
                        # DON'T flush here — let the normal loop cycle handle
                        # posting gradually via step 11. Flushing 80 offers
                        # during startup causes DB locking with concurrent reads.
                    elif not db_dexie_map:
                        log_event(
                            "info",
                            "dexie_recovery",
                            "No Dexie mappings found — will post on first cycle",
                        )
            except Exception as e:
                log_event(
                    "warning",
                    "dexie_recovery_failed",
                    f"Failed to recover Dexie links: {e}",
                )

            # Warm orderbook + advisor context before the slower resume repost path.
            # This keeps Market Health / Advisor responsive during startup.
            _push_startup_market_snapshot()

            # Count and classify all coins — first snapshot of the session
            self.coin_manager.snapshot_coins("startup")
            self._emit_coin_update("startup")

            # Coin readiness report — shows per-tier availability vs requirements
            # so we know exactly what's available before creating offers
            readiness = self.coin_manager.coin_readiness_report()
            resumed_live_book = len(wallet_open_ids) > 0
            if not readiness.get("overall_ready", True):
                status = readiness.get("overall_status", "UNKNOWN")
                if status == "CRITICAL":
                    # Suppress the warning if coin prep is running or about
                    # to run — the bot is actively building those tiers, so
                    # CRITICAL is a snapshot of an in-flight state, not a
                    # condition the user needs to act on. Only warn if the
                    # user genuinely has no path to coins (prep not running,
                    # not pending).
                    _prep_active = (
                        self.coin_manager.is_busy()
                        or getattr(self.coin_manager, "_topup_running", False)
                        or self.coin_manager.needs_coin_prep(
                            len(wallet_open_ids), len(wallet_open_ids)
                        )
                    )
                    if _prep_active:
                        log_event(
                            "info",
                            "startup_coins_critical_prep_pending",
                            "Coin tiers below target — coin prep is "
                            "in flight or queued, will replenish.",
                        )
                    elif resumed_live_book:
                        log_event(
                            "warning",
                            "startup_spares_low",
                            "Spare coin buffers below target — active offers remain live; "
                            "topup and normal fills will replenish specific tiers over time.",
                        )
                    else:
                        log_event(
                            "warning",
                            "startup_coins_unavailable",
                            "Coin pools missing required tier coins — run coin prep "
                            "before starting offers.",
                        )

            # Per-tier spare summary — fires on EVERY start (cold or resume).
            # Shows exactly which tiers are below their spare_target so the user
            # can see upfront what topup will address this session.
            # On resume with active offers, depletion from prior fills is normal.
            # If a previous bad topup cascade drained other tiers, this makes it
            # visible rather than silently deferring to "topup when needed."
            try:
                _tier_low_msgs = []
                for _tn, _ti in readiness.get("tiers", {}).items():
                    _xch_rem = _ti.get("xch_spare_remaining", 0)
                    _cat_rem = _ti.get("cat_spare_remaining", 0)
                    _xch_status = _ti.get("xch_status", "READY")
                    _cat_status = _ti.get("cat_status", "READY")
                    if _xch_status in ("LOW", "EMPTY") or _cat_status in (
                        "LOW",
                        "EMPTY",
                    ):
                        _parts = []
                        if _xch_status in ("LOW", "EMPTY"):
                            _parts.append(
                                f"XCH {_xch_rem} spare "
                                f"({'EMPTY' if _xch_status == 'EMPTY' else 'BUFFER LOW'})"
                            )
                        if _cat_status in ("LOW", "EMPTY"):
                            _parts.append(
                                f"CAT {_cat_rem} spare "
                                f"({'EMPTY' if _cat_status == 'EMPTY' else 'BUFFER LOW'})"
                            )
                        _tier_low_msgs.append(f"{_tn}: {', '.join(_parts)}")
                if _tier_low_msgs:
                    _context = "resumed session" if resumed_live_book else "cold start"
                    log_event(
                        "info",
                        "startup_spare_deficit",
                        f"Startup spare buffer below target ({_context}) — tiers below their "
                        "spare target: "
                        + "; ".join(_tier_low_msgs)
                        + ". Proactive drip/topup will replenish toward the "
                        "buffer while active offer slots stay usable.",
                    )
            except Exception as _spare_check_err:
                log_event(
                    "debug",
                    "startup_spare_check_failed",
                    f"Startup spare check error (non-critical): {_spare_check_err}",
                )

            # V3: Startup collateral advisory — tells user how XCH is allocated
            try:
                advisory = self.coin_manager.get_startup_advisory()
                assessment = advisory.get("assessment", "UNKNOWN")
                msg = advisory.get("message", "")
                if assessment in ("CRITICAL", "LOW"):
                    log_event(
                        "warning", "collateral_advisory", f"COLLATERAL ADVISORY: {msg}"
                    )
                else:
                    log_event("info", "collateral_advisory", f"Collateral: {msg}")
            except Exception as e:
                log_event("warning", "advisory_failed", f"Startup advisory failed: {e}")

            # ---- Old-style coin reconciliation — DISABLED (V4 fix) ----
            # This old approach was WRONG for Sage wallet: it used _xch_inventory
            # which includes owned-only coins (from the Sage "selectable" bug
            # workaround). Owned-only coins ARE locked by offers, but this code
            # treated them as "spendable" and freed them — undoing the correct
            # status set by reconcile_coins_with_wallet().
            #
            # The full reconcile below (owned-selectable=locked) handles this
            # correctly, so this block is no longer needed.
            log_event(
                "info",
                "coin_reconciliation",
                "Skipping old-style reconcile (replaced by full owned-selectable reconcile)",
            )

            # ---- Full coin reconcile + offer-to-coin linking ----
            # If offers were recovered above, we need to run the full
            # reconcile (owned + selectable) and link offers to locked coins.
            wallet_confirmed_locked = set()
            try:
                from database import (
                    reconcile_coins_with_wallet,
                    link_offers_to_locked_coins,
                )
                from wallet import get_owned_coins, get_selectable_coins_map

                xch_wid = (
                    cfg.WALLET_ID_XCH
                )  # int — don't str(), Sage _is_cat_wallet needs int
                cat_wid = cfg.CAT_WALLET_ID  # int

                # V5 FIX: Try detailed endpoint first (has offer_id for direct linking)
                _startup_detailed = False
                try:
                    from wallet import get_wallet_type as _s_gwt

                    if _s_gwt() == "sage":
                        from wallet_sage import get_owned_coins_detailed

                        xch_detail = get_owned_coins_detailed(xch_wid)
                        cat_detail = get_owned_coins_detailed(cat_wid)
                        if xch_detail is not None and cat_detail is not None:
                            _startup_detailed = True
                            # Build owned/selectable from detailed
                            xch_owned, xch_selectable = {}, {}
                            for cid, info in xch_detail.items():
                                xch_owned[cid] = info["amount"]
                                if info.get("offer_id"):
                                    wallet_confirmed_locked.add(
                                        cid if cid.startswith("0x") else "0x" + cid
                                    )
                                else:
                                    xch_selectable[cid] = info["amount"]
                            cat_owned, cat_selectable = {}, {}
                            for cid, info in cat_detail.items():
                                cat_owned[cid] = info["amount"]
                                if info.get("offer_id"):
                                    wallet_confirmed_locked.add(
                                        cid if cid.startswith("0x") else "0x" + cid
                                    )
                                else:
                                    cat_selectable[cid] = info["amount"]
                except Exception as e:
                    log_event(
                        "warning",
                        "detailed_coin_fetch_failed",
                        f"Detailed Sage coin fetch failed, falling back to basic mode: {e}",
                    )

                if not _startup_detailed:
                    xch_owned = get_owned_coins(xch_wid) or {}
                    xch_selectable = get_selectable_coins_map(xch_wid) or {}
                    cat_owned = get_owned_coins(cat_wid) or {}
                    cat_selectable = get_selectable_coins_map(cat_wid) or {}

                # Reconcile XCH coins
                xch_stats = reconcile_coins_with_wallet(
                    wallet_selectable=xch_selectable,
                    wallet_owned=xch_owned,
                    wallet_type="xch",
                )
                # Reconcile CAT coins
                cat_stats = reconcile_coins_with_wallet(
                    wallet_selectable=cat_selectable,
                    wallet_owned=cat_owned,
                    wallet_type="cat",
                )
                log_event(
                    "info",
                    "startup_full_reconcile",
                    f"Full reconcile: XCH({xch_stats}) CAT({cat_stats})",
                )

                # Link offers to locked coins (both sides per offer)
                all_wallet_open = open_buys + open_sells
                link_stats = link_offers_to_locked_coins(
                    all_wallet_open, cfg.CAT_ASSET_ID
                )
                log_event(
                    "info", "startup_offer_linking", f"Offer-coin linking: {link_stats}"
                )

            except Exception as e:
                log_event(
                    "warning",
                    "startup_reconcile_link_failed",
                    f"Full reconcile + link failed: {e}",
                )

            # ---- Orphaned locked coin cleanup (V5 FIX) ----
            # Free any locked coins whose offers no longer exist.
            # This catches: coins locked by offers that were cancelled outside the
            # bot, coins from failed offer creation, coins whose trade_ids are stale.
            # V5: For Sage, build wallet_confirmed_locked set to prevent tug-of-war.
            try:
                from database import cleanup_orphaned_locked_coins

                # Build wallet_confirmed_locked for Sage
                wallet_confirmed_locked = wallet_confirmed_locked or set()
                try:
                    from wallet import get_wallet_type

                    if get_wallet_type() == "sage" and not wallet_confirmed_locked:
                        from wallet_sage import get_owned_coins_detailed

                        for _wid in [cfg.WALLET_ID_XCH, cfg.CAT_WALLET_ID]:
                            _detail = get_owned_coins_detailed(_wid)
                            if _detail:
                                for _cid, _info in _detail.items():
                                    if _info.get("offer_id"):
                                        store_id = (
                                            _cid
                                            if _cid.startswith("0x")
                                            else "0x" + _cid
                                        )
                                        wallet_confirmed_locked.add(store_id)
                except Exception as e:
                    log_event(
                        "debug",
                        "sage_locked_coin_fetch_failed",
                        f"Sage locked coin set build failed (non-critical, proceeding without protection): {e}",
                    )
                orphan_stats = cleanup_orphaned_locked_coins(
                    wallet_open_ids, wallet_confirmed_locked=wallet_confirmed_locked
                )
                if orphan_stats["total_freed"] > 0:
                    log_event(
                        "info",
                        "startup_orphan_cleanup",
                        f"Freed {orphan_stats['total_freed']} orphaned locked coins "
                        f"({orphan_stats['freed_no_trade']} no trade_id, "
                        f"{orphan_stats['freed_stale_trade']} stale trade_id, "
                        f"{orphan_stats.get('skipped_wallet_locked', 0)} wallet-protected)",
                    )
                else:
                    log_event(
                        "info",
                        "startup_orphan_cleanup",
                        "No orphaned locked coins found",
                    )
            except Exception as e:
                log_event(
                    "warning",
                    "startup_orphan_cleanup_failed",
                    f"Orphaned locked coin cleanup failed: {e}",
                )

            # ---- Sniper recovery ----
            # Restore sniper active IDs from database (they're memory-only in sniper.py)
            try:
                from database import get_open_offers as _get_open_offers

                sniper_offers = _get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
                sniper_offers = [o for o in sniper_offers if o.get("tier") == "sniper"]
                sniper_ids = [o["trade_id"] for o in sniper_offers if o.get("trade_id")]
                if sniper_ids:
                    with self.sniper._snipe_lock:
                        self.sniper._active_snipe_ids = sniper_ids
                        self.sniper._active_snipe_sides = {
                            o["trade_id"]: o.get("side", "")
                            for o in sniper_offers
                            if o.get("trade_id")
                        }
                    latest_buy = next(
                        (o for o in sniper_offers if o.get("side") == "buy"),
                        None,
                    )
                    latest_sell = next(
                        (o for o in sniper_offers if o.get("side") == "sell"),
                        None,
                    )
                    anchor_ts = time.time()
                    try:
                        candidates = [
                            o.get("created_at")
                            for o in (latest_buy, latest_sell)
                            if o and o.get("created_at")
                        ]
                        if candidates:
                            from datetime import datetime

                            anchor_ts = max(
                                datetime.fromisoformat(
                                    ts.replace("Z", "+00:00")
                                ).timestamp()
                                for ts in candidates
                            )
                    except Exception as e:
                        log_event(
                            "debug",
                            "sniper_anchor_ts_failed",
                            f"Sniper anchor timestamp parse failed (using now): {e}",
                        )
                    with self._probe_lock:
                        self._probe_state.update(
                            {
                                "active": False,
                                "buy_tid": latest_buy.get("trade_id")
                                if latest_buy
                                else None,
                                "sell_tid": latest_sell.get("trade_id")
                                if latest_sell
                                else None,
                                "buy_price": Decimal(
                                    str(latest_buy.get("price_xch") or 0)
                                )
                                if latest_buy
                                else Decimal("0"),
                                "sell_price": Decimal(
                                    str(latest_sell.get("price_xch") or 0)
                                )
                                if latest_sell
                                else Decimal("0"),
                                "confirmed_at": anchor_ts,
                                "launched_at": anchor_ts,
                            }
                        )
                    log_event(
                        "info",
                        "sniper_recovery",
                        f"Recovered {len(sniper_ids)} active sniper offer IDs from DB",
                    )
            except Exception as e:
                log_event(
                    "warning",
                    "sniper_recovery_failed",
                    f"Sniper ID recovery failed: {e}",
                )

            # ---- Gap closer recovery ----
            # Restore gap-closer IDs from database (memory-only in boost_manager)
            # IMPORTANT: Don't just look for status='open' — during gap closer
            # stepping, old offers are marked 'cancelled' in DB when replaced.
            # If bot restarts mid-step, wallet may still have offers the DB
            # says are cancelled. Cross-reference DB boost tier with wallet.
            try:
                # Strategy 1: DB open boost offers (ideal case)
                boost_offers = _get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
                boost_ids = [
                    o["trade_id"] for o in boost_offers if o.get("tier") == "boost"
                ]

                # Strategy 2: If no open boost offers in DB, check ALL recent
                # boost offers (any status) and cross-reference with wallet
                if not boost_ids:
                    try:
                        from database import get_connection as _gc_conn

                        _db = _gc_conn()
                        # Find any boost-tier offers from last 4 hours
                        all_boost_rows = _db.execute(
                            "SELECT trade_id, status FROM offers "
                            "WHERE tier = 'boost' "
                            "AND created_at > datetime('now', '-4 hours') "
                            "ORDER BY created_at DESC"
                        ).fetchall()
                        # Keep only those that are actually in the wallet right now
                        for row in all_boost_rows:
                            tid = row[0]
                            if tid in wallet_open_ids:
                                boost_ids.append(tid)
                        if boost_ids:
                            print(
                                f"   [BOOST] Found {len(boost_ids)} boost offers in wallet "
                                f"(DB status was not 'open' — recovered via cross-reference)",
                                flush=True,
                            )
                    except Exception as e2:
                        log_event("debug", "boost_crossref_failed", str(e2))

                if boost_ids:
                    self.boost_manager._active_boost_ids = boost_ids
                    self.boost_manager._boost_active = True

                    # Try to recover last known spread from DB events
                    recovered_spread = 0
                    recovered_floor = 0
                    recovered_steps = 0
                    try:
                        import json as _json
                        from database import get_connection

                        db = get_connection()
                        row = db.execute(
                            "SELECT data FROM events "
                            "WHERE event_type IN ('gap_closer_step', 'gap_closer_arbed', 'gap_closer_activated') "
                            "AND data IS NOT NULL "
                            "ORDER BY timestamp DESC LIMIT 1"
                        ).fetchone()
                        if row and row[0]:
                            evt_data = (
                                _json.loads(row[0])
                                if isinstance(row[0], str)
                                else row[0]
                            )
                            recovered_spread = int(evt_data.get("spread_bps", 0))
                            recovered_floor = int(evt_data.get("arb_floor_bps", 0))
                            recovered_steps = int(evt_data.get("steps_taken", 0))
                    except Exception as e:
                        log_event(
                            "debug",
                            "gap_closer_recovery_db_failed",
                            f"Gap closer spread recovery from DB failed (non-critical): {e}",
                        )

                    if recovered_spread > 0:
                        self.boost_manager._gap_spread_bps = recovered_spread
                        self.boost_manager._arb_floor_bps = recovered_floor
                        self.boost_manager._steps_taken = recovered_steps
                        self.boost_manager._start_spread_bps = int(
                            getattr(cfg, "BOOST_SPREAD_BPS", recovered_spread)
                        )
                        log_event(
                            "info",
                            "gap_closer_recovery",
                            f"Recovered {len(boost_ids)} gap-closer offers — "
                            f"resuming at {_bps_to_pct(recovered_spread)} (step {recovered_steps}, "
                            f"floor {_bps_to_pct(recovered_floor)})",
                        )
                        print(
                            f"[BOOST] Gap closer recovery: {len(boost_ids)} offers restored "
                            f"at {_bps_to_pct(recovered_spread)} (step {recovered_steps})",
                            flush=True,
                        )
                    else:
                        # Fallback: no event data → use config default
                        self.boost_manager._gap_spread_bps = getattr(
                            cfg, "BOOST_SPREAD_BPS", 200
                        )
                        self.boost_manager._start_spread_bps = (
                            self.boost_manager._gap_spread_bps
                        )
                        log_event(
                            "info",
                            "gap_closer_recovery",
                            f"Recovered {len(boost_ids)} gap-closer offers from DB "
                            f"(spread data lost — starting fresh at {_bps_to_pct(self.boost_manager._gap_spread_bps)})",
                        )
                        print(
                            f"[BOOST] Gap closer recovery: {len(boost_ids)} offers restored "
                            f"(will resume probing from default)",
                            flush=True,
                        )
                # Pre-register recovered boost IDs so fill tracker ignores
                # them if they disappear during gap closer stepping
                for tid in boost_ids:
                    self.offer_manager._bot_cancelled_ids.add(tid)
            except Exception as e:
                log_event(
                    "warning",
                    "gap_closer_recovery_failed",
                    f"Gap closer ID recovery failed: {e}",
                )

            # Safety net: ALL boost-tier offers from recent history should be
            # pre-cancelled so fill tracker ignores them if they disappear.
            # This catches offers in ANY status (open, cancelled, etc.)
            try:
                from database import get_connection as _gc_conn2

                _db2 = _gc_conn2()
                all_boost_rows = _db2.execute(
                    "SELECT trade_id FROM offers "
                    "WHERE tier = 'boost' "
                    "AND created_at > datetime('now', '-4 hours')"
                ).fetchall()
                stale_boost_ids = [
                    r[0]
                    for r in all_boost_rows
                    if r[0] not in self.boost_manager._active_boost_ids
                ]
                for tid in stale_boost_ids:
                    self.offer_manager._bot_cancelled_ids.add(tid)
                if stale_boost_ids:
                    log_event(
                        "info",
                        "boost_stale_protection",
                        f"Pre-cancelled {len(stale_boost_ids)} stale boost offers "
                        f"from fill tracker (any status)",
                    )
                    print(
                        f"   🛡️ Pre-cancelled {len(stale_boost_ids)} stale boost IDs "
                        f"from fill detection",
                        flush=True,
                    )
            except Exception as e:
                log_event(
                    "debug",
                    "boost_stale_protection_failed",
                    f"Stale boost ID pre-cancel protection failed (non-critical): {e}",
                )

            # Load worker cancelled IDs (from any previous coin prep)
            worker_ids = self.coin_manager.get_worker_cancelled_ids()
            for tid in worker_ids:
                self.offer_manager._bot_cancelled_ids.add(tid)

            # Update inventory
            self.risk_manager.update_inventory()

            # ---- Startup Dexie/Splash visibility re-check ----
            # After restart, offers may have vanished from Dexie's orderbook.
            # Queue a background visibility/repost pass so startup can finish
            # promptly and the live loop can resume while Dexie/Splash catches up.
            total_offers = len(buy_ids) + len(sell_ids)
            if (
                total_offers > 0
                and cfg.DEXIE_AUTO_POST
                and not self._startup_repost_done
            ):
                self._startup_repost_done = True
                self._schedule_repost_active_offers_to_dexie(
                    reason="startup_resume",
                    total_offers=total_offers,
                )
            elif total_offers == 0:
                self._startup_repost_done = True  # Nothing to repost

            # ---- Set requote baseline price ----
            # Critical: without this, _last_quoted_price stays at 0 after restart
            # and ALL requoting (normal + emergency) is disabled because the
            # "if last_price <= 0: continue" check skips both sides.
            try:
                startup_price = self.price_engine.get_price(
                    cfg.CAT_ASSET_ID, cfg.CAT_DECIMALS, cfg.CAT_TICKER_ID
                )
                startup_mid = Decimal(str(startup_price.get("mid_price", 0)))
                startup_arb_gap = Decimal(str(startup_price.get("arb_gap_bps", 0) or 0))
                startup_tibet = Decimal(str(startup_price.get("tibet_price", 0) or 0))
                if startup_mid > 0:
                    self._last_quoted_price["buy"] = startup_mid
                    self._last_quoted_price["sell"] = startup_mid
                    # F67: plain mid == startup mid at this point (no probe yet)
                    self._last_quoted_plain_mid["buy"] = startup_mid
                    self._last_quoted_plain_mid["sell"] = startup_mid
                    self.amm_monitor.notify_quoted_price(startup_mid, startup_mid)
                    self._current_mid_price = startup_mid
                    with self._probe_lock:
                        if self._probe_state.get("confirmed_price") in (
                            None,
                            Decimal("0"),
                        ):
                            self._probe_state["confirmed_price"] = startup_mid
                    self._remember_probe_market_snapshot(
                        startup_mid,
                        startup_arb_gap,
                        startup_tibet,
                        "startup_baseline",
                    )
                    baseline_msg = (
                        f"📌 Requote baseline set: {startup_mid:.8f} XCH "
                        f"(enables requoting + emergency requote)"
                    )
                    print(baseline_msg, flush=True)
                    log_event("info", "startup_baseline_price", baseline_msg)
                else:
                    print(
                        "[WARN] Could not set requote baseline -- mid_price is 0!",
                        flush=True,
                    )
                    log_event(
                        "warning",
                        "startup_baseline_zero",
                        "mid_price was 0 — requoting will be disabled until offers are created",
                    )
            except Exception as e:
                err_msg = f"[WARN] Could not set baseline price: {e}"
                print(err_msg, flush=True)
                log_event("warning", "startup_baseline_failed", err_msg)

            # ---- V3: Initialize Coinset puzzle hash cache ----
            # Coinset requires a full node for puzzle hashes — skip for Sage light wallet
            wallet_type = (
                getattr(cfg, "WALLET_TYPE", "sage").lower().strip()
                if hasattr(cfg, "WALLET_TYPE")
                else os.getenv("WALLET_TYPE", "sage").lower().strip()
            )
            if getattr(cfg, "COINSET_ENABLED", True) and wallet_type == "sage":
                self._initialize_coinset_for_wallet_type(wallet_type)
            if getattr(cfg, "COINSET_ENABLED", True) and wallet_type != "sage":
                try:
                    ok = self.coinset_client.initialize_puzzle_hashes()
                    if ok:
                        log_event(
                            "info",
                            "coinset_ready",
                            "Coinset puzzle hash cache initialized — fast coin queries enabled",
                        )
                        # Pass coinset client to coin_manager for use in queries
                        self.coin_manager._coinset_client = self.coinset_client
                    else:
                        log_event(
                            "info",
                            "coinset_init_skipped",
                            "Coinset initialization returned no puzzle hashes — "
                            "using wallet RPC for coin queries",
                        )
                except Exception as e:
                    log_event(
                        "info",
                        "coinset_init_error",
                        f"Coinset initialization failed: {e} — "
                        f"using wallet RPC for coin queries",
                    )

            inv = self.coin_manager.get_inventory_summary()
            log_event(
                "info",
                "startup_sync_done",
                f"Synced: {len(buy_ids)} buys, {len(sell_ids)} sells | "
                f"Coins — XCH: {inv['xch_trading']} trading, "
                f"{inv['xch_reserve']} reserve ({inv['xch_reserve_total']}), "
                f"{inv['xch_small']} small | "
                f"CAT: {inv['cat_trading']} trading, "
                f"{inv['cat_reserve']} reserve ({inv['cat_reserve_total']}), "
                f"{inv['cat_small']} small",
            )

            # Push early dashboard update so Command Centre populates immediately
            # instead of waiting for the first full cycle to complete (~90s).
            _push_startup_market_snapshot()

        except Exception as e:
            log_event("error", "startup_sync_failed", f"Startup sync failed: {e}")
            # Critical: do NOT continue trading without a proper baseline.
            # Flag the bot as stopped so the main loop exits immediately.
            self._running = False
            self._set_state(status="error", error=f"Startup sync failed: {e}")
            return

    # -------------------------------------------------------------------
    # One trading cycle
    # -------------------------------------------------------------------

    def _run_one_cycle(self):
        """Execute one complete trading cycle."""
        self._recovery_state["cycle_probe_churn"] = False
        self._recovery_state["cycle_create_stalled"] = False
        self._requoted_this_cycle: set = set()  # sides requoted in step 9
        self._set_cycle_step("cycle_start")

        # ---- Step 0pre: Clear per-cycle coin exclusion set ----
        # Coins from the previous cycle's pending offers may now be confirmed
        # on-chain, so reset the exclusion set.  Within this new cycle,
        # successfully used coins will be added back to prevent MEMPOOL_CONFLICT.
        try:
            self.offer_manager.clear_cycle_coins()
        except Exception:
            pass  # non-critical — _select_coin_for_offer still has used_coins guard

        # ---- Step 0: Expire stale reservations ----
        try:
            from reservation_manager import ReservationManager

            ReservationManager().expire_stale()
        except Exception as e:
            log_event(
                "warning",
                "reservation_expire_failed",
                f"Could not expire stale reservations: {e}",
            )

        # ---- Step 0b: Tick sweep coordinator (expire pending groups) ----
        try:
            from sweep_coordinator import get_coordinator as _get_sc
            from dynamic_amm_buffer import record_sweep as _record_sweep
            from fill_classifier import FillType as _FT

            _sc = _get_sc()
            _sc.tick()
            for _sweep_evt in _sc.drain_sweep_events():
                # Record sweep for dynamic buffer widening
                _record_sweep(fill_count=_sweep_evt.fill_count)

                # Determine which side(s) were swept to apply protection.
                # Priority for direction:
                #   1. ARB_SWEEP_BUY/SELL classification (taker wallet known)
                #   2. entry.side (fill side stamped by fill_tracker — always available)
                #   3. Fallback: protect both, but for a shorter window
                _prot_secs_known = float(getattr(cfg, "SWEEP_PROTECTION_SECS", 90))
                _prot_secs_unknown = float(
                    getattr(cfg, "SWEEP_PROTECTION_UNKNOWN_SECS", 30)
                )
                _protected_sides: dict = {}  # side → expiry timestamp
                _side_fill_counts = {"buy": 0, "sell": 0}

                def _protect_sweep_side(_side: str, _secs: float) -> None:
                    if _side not in ("buy", "sell"):
                        return
                    _protected_sides[_side] = time.time() + _secs
                    _side_fill_counts[_side] += 1

                for _entry in _sweep_evt.fills:
                    if _entry.classification == _FT.ARB_SWEEP_BUY:
                        # Arb bought from us → our SELL offers swept
                        _protect_sweep_side("sell", _prot_secs_known)
                    elif _entry.classification == _FT.ARB_SWEEP_SELL:
                        # Arb sold to us → our BUY offers swept
                        _protect_sweep_side("buy", _prot_secs_known)
                    elif _entry.side in ("buy", "sell"):
                        # Direction from fill side: the offer that got swept
                        _protect_sweep_side(_entry.side, _prot_secs_known)
                    else:
                        # No direction data — protect both but only briefly
                        for _s in ("buy", "sell"):
                            _protect_sweep_side(_s, _prot_secs_unknown)
                self._sweep_protection.update(_protected_sides)
                _sweep_now = time.time()
                for _side in _protected_sides.keys() or [""]:
                    _side_fill_count = (
                        _side_fill_counts.get(_side) or _sweep_evt.fill_count
                    )
                    self._recent_sweep_events.append(
                        {
                            "side": _side,
                            "fill_count": _side_fill_count,
                            "side_fill_count": _side_fill_count,
                            "total_fill_count": _sweep_evt.fill_count,
                            "sweep_group_id": _sweep_evt.sweep_group_id,
                            "spent_block_index": _sweep_evt.spent_block_index,
                            "timestamp": _sweep_now,
                        }
                    )
                self._recent_sweep_events = self._recent_sweep_events[-20:]

                _prot_summary = {
                    s: round(e - time.time()) for s, e in _protected_sides.items()
                }
                log_event(
                    "info",
                    "sweep_detected",
                    f"Sweep detected: {_sweep_evt.fill_count} fills in block "
                    f"{_sweep_evt.spent_block_index} — group {_sweep_evt.sweep_group_id}"
                    + (f" — protecting {_prot_summary}" if _protected_sides else ""),
                    data={
                        "sweep_group_id": _sweep_evt.sweep_group_id,
                        "spent_block_index": _sweep_evt.spent_block_index,
                        "fill_count": _sweep_evt.fill_count,
                        "trade_ids": _sweep_evt.trade_ids,
                        "protected_sides": _prot_summary,
                    },
                )
        except Exception:
            pass  # Sweep coordinator is additive — never block main cycle

        # ---- Step 1: Fetch prices ----
        self._set_cycle_step("step1_price_fetch")
        price_data = self.price_engine.get_price(
            cfg.CAT_ASSET_ID, cfg.CAT_DECIMALS, cfg.CAT_TICKER_ID
        )

        if price_data is None:
            # get_price() returns None for two different failure modes:
            #   (a) a safety-guard rail breach (handled below via CB), or
            #   (b) both oracles (Dexie + TibetSwap) failed to produce a
            #       usable number. Case (b) is an oracle OUTAGE — if we
            #       don't track how long it has lasted, stale offers sit
            #       on the book at the old mid while the market may have
            #       moved. The stale-age counter below escalates the
            #       response from soft warn (>60s) to hard pause (>120s)
            #       so the book cannot stay exposed indefinitely.
            now_outage = time.time()
            prev_success = float(self._last_pricing_success_ts or 0)
            outage_age = (now_outage - prev_success) if prev_success > 0 else 0.0
            stale_threshold = int(getattr(cfg, "PRICE_STALE_ALERT_SECS", 60))
            hard_pause_threshold = int(getattr(cfg, "PRICE_HARD_PAUSE_SECS", 120))

            rail_dir = getattr(self.price_engine, "_last_rail_breach", None)
            rail_kind = getattr(self.price_engine, "_last_rail_breach_kind", None)
            rail_price = getattr(self.price_engine, "_last_rail_breach_price", None)
            if rail_dir in ("above", "below"):
                _direction_note = {
                    "above": "rail breach ABOVE — price spike past upper rail",
                    "below": "rail breach BELOW — price drop past lower rail",
                }.get(rail_dir, f"rail breach ({rail_dir})")
                _kind_note = f" [{rail_kind}]" if rail_kind else ""
                _price_note = f" at {rail_price}" if rail_price is not None else ""
                _reason = f"{_direction_note}{_kind_note}{_price_note}"
                try:
                    self.risk_manager.trip_price_rail_breach(_reason)
                    self._set_state(status="circuit_breaker")
                    self._emit_alert(
                        "circuit_breaker",
                        "error",
                        "Price Rail Breach",
                        _reason,
                        action="stop_bot",
                        action_label="Stop Bot",
                    )
                    log_event(
                        "critical",
                        "rail_breach",
                        f"Price rail breach detected — tripping price CB and "
                        f"cancelling stale offers ({_reason})",
                        data={
                            "direction": rail_dir,
                            "kind": rail_kind,
                            "rejected_price": str(rail_price)
                            if rail_price is not None
                            else None,
                        },
                    )
                    # _safeguard cancels ALL offers for a price CB. Without
                    # this call, stale offers stay on the book at the old
                    # mid until the CB clears or the operator intervenes.
                    self._safeguard_offers_for_circuit_breaker()
                except Exception as _e:
                    log_event(
                        "error",
                        "rail_breach_safeguard_failed",
                        f"Rail breach safeguard failed: {_e}",
                    )
            # Oracle outage escalation (runs regardless of rail-breach path).
            # Only escalate once we have a prior success timestamp to measure
            # against — the first post-startup failure is not yet an outage.
            if prev_success > 0 and rail_dir not in ("above", "below"):
                if outage_age >= hard_pause_threshold:
                    log_event(
                        "critical",
                        "price_outage_hard_pause",
                        f"Oracle outage has lasted {outage_age:.0f}s "
                        f"(>= {hard_pause_threshold}s hard-pause threshold). "
                        f"Cancelling all offers and tripping the price CB "
                        f"so the book does not stay exposed on a stale mid.",
                        data={
                            "outage_age_secs": outage_age,
                            "hard_pause_threshold": hard_pause_threshold,
                        },
                    )
                    try:
                        self.risk_manager.trip_price_rail_breach(
                            f"oracle outage >= {hard_pause_threshold}s"
                        )
                        self._set_state(status="circuit_breaker")
                        self._emit_alert(
                            "circuit_breaker",
                            "error",
                            "Oracle Outage — Hard Pause",
                            f"Price oracle has been unavailable for "
                            f"{outage_age:.0f}s. All offers cancelled.",
                            action="stop_bot",
                            action_label="Stop Bot",
                        )
                        self._safeguard_offers_for_circuit_breaker()
                    except Exception as _e:
                        log_event(
                            "error",
                            "price_outage_safeguard_failed",
                            f"Oracle-outage safeguard failed: {_e}",
                        )
                elif outage_age >= stale_threshold:
                    log_event(
                        "warning",
                        "price_outage_stale",
                        f"Oracle stale for {outage_age:.0f}s "
                        f"(alert threshold {stale_threshold}s; hard pause "
                        f"at {hard_pause_threshold}s). Offers still live "
                        f"on last-known mid.",
                        data={
                            "outage_age_secs": outage_age,
                            "stale_threshold": stale_threshold,
                        },
                    )
                    try:
                        self._emit_alert(
                            "price_stale",
                            "warning",
                            "Oracle Stale",
                            f"Price sources have not responded for "
                            f"{outage_age:.0f}s — offers still live. If this "
                            f"reaches {hard_pause_threshold}s the bot will "
                            f"hard-pause automatically.",
                        )
                    except Exception as _alert_err:
                        log_event(
                            "debug",
                            "price_outage_alert_failed",
                            f"Price-stale alert emit failed: {_alert_err}",
                        )

            # Run self-heal / reconcile passes even when we can't trade.
            # These are read/repair-only and do not create or requote offers,
            # so they are safe during an oracle outage and — critically —
            # prevent stuck pending-cancel rows, orphan coin locks, and
            # phantom fills from piling up while pricing is unavailable.
            # Each check has its own internal throttle (see bot_health.py)
            # so calling it here does not create extra load.
            try:
                from bot_health import run_runtime_checks as _no_price_health

                _no_price_health(auto_repair=True)
            except Exception as _hc_err:
                log_event(
                    "debug",
                    "no_price_self_heal_failed",
                    f"Self-heal during oracle outage failed: {_hc_err}",
                )

            log_event(
                "warning",
                "no_price",
                "Price rejected by safety guard — skipping cycle. "
                "Check HARD_MAX_PRICE_XCH / HARD_MIN_PRICE_XCH or dynamic band settings.",
            )
            return

        mid_price = Decimal(str(price_data.get("mid_price", 0)))
        if mid_price <= 0:
            log_event("warning", "no_price", "Could not fetch price — skipping cycle")
            return

        self._current_mid_price = mid_price
        self._set_state(mid_price=str(mid_price))

        # Pricing strategy is logged but not emitted as an alert (not actionable)

        # Clear the oracle-stale / hard-pause alerts as soon as we have a
        # fresh usable mid. Previously the "Oracle Stale" and "Oracle
        # Outage — Hard Pause" banners never cleared once raised, so the
        # GUI kept shouting at the operator even after pricing had been
        # healthy for many cycles. If pricing just recovered we ALSO
        # emit a one-shot recovery notification so the operator sees
        # the transition, not just the banner disappearance.
        try:
            self._clear_alert("price_stale")
            self._clear_alert("circuit_breaker")
        except Exception:
            pass

        # ---- Connectivity recovery check (V1 parity) ----
        # If pricing was down for >30 min and just recovered, repost all offers
        pricing_now = time.time()
        if self._last_pricing_success_ts > 0:
            gap = pricing_now - self._last_pricing_success_ts
            if gap > self._connectivity_gap_threshold:
                log_event(
                    "info",
                    "connectivity_recovery",
                    f"Pricing recovered after {gap:.0f}s gap — rechecking Dexie visibility in the background",
                )
                self._schedule_repost_active_offers_to_dexie(
                    reason="connectivity_recovery",
                )
        self._last_pricing_success_ts = pricing_now

        # Pass arb gap to risk manager (if available from price engine)
        arb_gap = Decimal(str(price_data.get("arb_gap_bps", 0)))
        self.risk_manager.update_arb_gap(arb_gap)
        self._set_state(arb_gap_bps=str(arb_gap))

        # Store main book spread for GUI (used by Close the Gap modal)
        # get_adjusted_spread returns a fraction (e.g. 0.08), multiply by 10000 for BPS
        try:
            buy_spread = self.risk_manager.get_adjusted_spread("buy")
            sell_spread = self.risk_manager.get_adjusted_spread("sell")
            avg_spread_bps = (buy_spread + sell_spread) / 2 * Decimal("10000")
            self._set_state(spread_bps=str(int(avg_spread_bps)))
        except Exception as e:
            print(f"   [WARN] Spread calc failed: {e}", flush=True)
            # Fallback: use config base spread so modal isn't stuck on zero
            try:
                fallback = int(
                    getattr(cfg, "BASE_SPREAD_BPS", 0) or getattr(cfg, "SPREAD_BPS", 0)
                )
                if fallback > 0:
                    self._set_state(spread_bps=str(fallback))
            except Exception as e2:
                log_event(
                    "debug",
                    "spread_fallback_failed",
                    f"Spread fallback also failed: {e2}",
                )

        # Push price update to GUI (after spread calc so it's included)
        self._emit(
            "price_update",
            {
                "mid_price": str(mid_price),
                "dexie_price": str(price_data.get("dexie_price", "")),
                "tibet_price": str(price_data.get("tibet_price", "")),
                "arb_gap_bps": str(arb_gap),
                "spread_bps": self._bot_state.get("spread_bps", "0"),
            },
        )

        # Terminal heartbeat — every loop (terminal is dev-only, GUI is for users)
        dexie_p = price_data.get("dexie_price", "")
        tibet_p = price_data.get("tibet_price", "")
        print(f"\n{'=' * 70}", flush=True)
        print(
            f"💓 Loop {self._loop_count} | mid: {mid_price:.8f} | "
            f"arb gap: {_bps_to_pct(arb_gap)} | "
            f"spread: {_bps_to_pct(self._bot_state.get('spread_bps', '0'))}",
            flush=True,
        )

        # Console heartbeat — user-visible cycle start
        log_event(
            "info",
            "cycle_start",
            f"Cycle #{self._loop_count} — mid price: {mid_price:.8f} XCH, "
            f"arb gap: {_bps_to_pct(arb_gap)}, spread: {_bps_to_pct(self._bot_state.get('spread_bps', '0'))}",
        )
        baseline_val = self._last_quoted_price.get("sell", Decimal("0"))
        baseline_str = f"{baseline_val:.8f}" if baseline_val > 0 else "pending requote"
        print(
            f"   Dexie: {dexie_p} | Tibet: {tibet_p} | baseline: {baseline_str}",
            flush=True,
        )

        # ---- Step 1b: Refresh market intelligence (NEW — ecosystem) ----
        print("   [1b] Market intel...", end="", flush=True)
        # Step-by-step debug logs removed — console print provides same info
        # without cluttering the system log panel during price adjustments.
        try:
            intel_data = self.market_intel.refresh_orderbook()
            if intel_data:
                print(
                    f" competitor spread: {_bps_to_pct(intel_data.get('competitor_spread_bps', '0'))}, "
                    f"thin side: {intel_data.get('thin_side', 'none')}",
                    flush=True,
                )
                payload = {
                    "competitor_spread_bps": str(
                        intel_data.get("competitor_spread_bps", "0")
                    ),
                    "best_bid": str(intel_data.get("best_bid", "0")),
                    "best_ask": str(intel_data.get("best_ask", "0")),
                    "overall_best_bid": str(intel_data.get("overall_best_bid", "0")),
                    "overall_best_ask": str(intel_data.get("overall_best_ask", "0")),
                    "buy_depth_xch": str(intel_data.get("buy_depth_xch", "0")),
                    "sell_depth_xch": str(intel_data.get("sell_depth_xch", "0")),
                    "num_buy_offers": intel_data.get("num_buy_offers", 0),
                    "num_sell_offers": intel_data.get("num_sell_offers", 0),
                    "thin_side": intel_data.get("thin_side", ""),
                }
                try:
                    payload["splash"] = {
                        **self.splash_manager.get_stats(),
                        "health": self.splash_manager.check_health(),
                    }
                except Exception as e:
                    log_event(
                        "debug",
                        "splash_stats_failed",
                        f"Splash stats fetch failed (non-critical): {e}",
                    )
                try:
                    payload["splash_node"] = self.splash_node.get_status()
                except Exception as e:
                    log_event(
                        "debug",
                        "splash_node_status_failed",
                        f"Splash node status fetch failed (non-critical): {e}",
                    )
                try:
                    payload["splash_receive"] = self.get_splash_receive_stats()
                except Exception as e:
                    log_event(
                        "debug",
                        "splash_receive_stats_failed",
                        f"Splash receive stats failed (non-critical): {e}",
                    )
                self._emit("market_intel", payload)
            else:
                print(" no data", flush=True)
        except Exception as e:
            print(f" error: {e}", flush=True)
            log_event("debug", "intel_error", f"Market intel refresh failed: {e}")

        # ---- Step 2: Check circuit breakers ----
        print("   [2] Circuit breakers...", end="", flush=True)
        if self.risk_manager.check_circuit_breakers(mid_price):
            self._set_state(status="circuit_breaker")
            reason = (
                getattr(self.risk_manager, "_circuit_breaker_reason", "")
                or "Trading halted by circuit breaker"
            )
            self._emit_alert(
                "circuit_breaker",
                "error",
                "Circuit Breaker Tripped",
                reason,
                action="stop_bot",
                action_label="Stop Bot",
            )
            self._safeguard_offers_for_circuit_breaker()

            if self.risk_manager.is_full_halt():
                # Price CB: price is outside safe range — skip cycle entirely.
                # No position should be built when price validity is in question.
                print(" [CB] PRICE CB -- skipping cycle", flush=True)
                log_event(
                    "warning",
                    "circuit_breaker",
                    "PRICE circuit breaker — skipping cycle (both sides halted)",
                )
                return
            else:
                # Position CB: position is too large on one side.
                # Cancel the accumulating side (done above) but CONTINUE the cycle
                # so the correcting side can place new offers to reduce position.
                blocked = self.risk_manager.get_circuit_breaker_blocked_side()
                print(
                    f" [CB] POSITION CB ({blocked} blocked) -- continuing correcting side",
                    flush=True,
                )
                log_event(
                    "warning",
                    "circuit_breaker_partial",
                    f"POSITION circuit breaker — '{blocked}' side halted, "
                    f"correcting side continues to reduce position",
                )
                # Fall through — cycle continues below

        else:
            print(" OK", flush=True)
            pass  # Console print covers this — no system log needed
            self._set_state(status="running")
            self._clear_alert("circuit_breaker")
            self._circuit_breaker_offer_safed = False

        # ---- Step 3: Get current offers from wallet ----
        self._set_cycle_step("step3_wallet_sync")
        print("   [3] Syncing offers from wallet...", end="", flush=True)
        # step3_sync log removed — console print covers this
        open_buys, open_sells, closed = self.offer_manager.sync_from_wallet()
        wallet_sync_meta = self.offer_manager.get_wallet_sync_meta()
        self._wallet_sync_stale_cycle = not bool(wallet_sync_meta.get("fresh", True))
        self._last_live_offer_edges = self._get_live_offer_edges(open_buys, open_sells)

        current_buy_ids = {
            o.get("trade_id", "") for o in open_buys if o.get("trade_id")
        }
        current_sell_ids = {
            o.get("trade_id", "") for o in open_sells if o.get("trade_id")
        }

        # Compute DB-open subsets for cap check (wallet set may include zombie
        # offers: cancelled in DB but still active in Sage after a failed cancel
        # attempt).  Fill detection and requote still use the full wallet sets.
        # Sniper-tier and boost-tier offers are excluded from cap counts
        # (mirroring trim_excess_offers), so neither inflates the main ladder
        # count and triggers a false trim-create cycle. Boost = Close the Gap
        # probes, sniper = arb snipes — both live in their own pools.
        _db_buy_all = []
        _db_sell_all = []
        try:
            from database import get_open_offers as _db_get_open

            _db_buy_all = [o for o in _db_get_open(side="buy") if o.get("trade_id")]
            _db_sell_all = [o for o in _db_get_open(side="sell") if o.get("trade_id")]
            _db_open_buy_ids = {
                o["trade_id"]
                for o in _db_buy_all
                if (o.get("tier") or "").lower() not in ("sniper", "boost")
            }
            _db_open_sell_ids = {
                o["trade_id"]
                for o in _db_sell_all
                if (o.get("tier") or "").lower() not in ("sniper", "boost")
            }
            # Sniper / boost offer IDs (DB-tracked) — exclude from wallet sets
            # so they don't appear as zombies and don't consume main-ladder
            # cap slots.
            _db_sniper_ids = {
                o["trade_id"]
                for o in _db_buy_all + _db_sell_all
                if (o.get("tier") or "").lower() in ("sniper", "boost")
            }
            _main_wallet_buy_ids = current_buy_ids - _db_sniper_ids
            _main_wallet_sell_ids = current_sell_ids - _db_sniper_ids
            _retired_db_only = self._retire_wallet_missing_db_offers(
                _db_buy_all,
                _db_sell_all,
                _main_wallet_buy_ids,
                _main_wallet_sell_ids,
                wallet_sync_fresh=(
                    bool(wallet_sync_meta.get("fresh", True))
                    and not bool(wallet_sync_meta.get("using_cache", False))
                ),
            )
            if _retired_db_only["buy"]:
                _db_buy_all = [
                    o
                    for o in _db_buy_all
                    if o.get("trade_id") not in _retired_db_only["buy"]
                ]
            if _retired_db_only["sell"]:
                _db_sell_all = [
                    o
                    for o in _db_sell_all
                    if o.get("trade_id") not in _retired_db_only["sell"]
                ]
            if _retired_db_only["buy"] or _retired_db_only["sell"]:
                _db_open_buy_ids = {
                    o["trade_id"]
                    for o in _db_buy_all
                    if (o.get("tier") or "").lower() not in ("sniper", "boost")
                }
                _db_open_sell_ids = {
                    o["trade_id"]
                    for o in _db_sell_all
                    if (o.get("tier") or "").lower() not in ("sniper", "boost")
                }
            _zombie_buys = len(_main_wallet_buy_ids) - len(
                _db_open_buy_ids & _main_wallet_buy_ids
            )
            _zombie_sells = len(_main_wallet_sell_ids) - len(
                _db_open_sell_ids & _main_wallet_sell_ids
            )
            _pending_cancel_wallet_ids_by_side = {
                "buy": _main_wallet_buy_ids - _db_open_buy_ids,
                "sell": _main_wallet_sell_ids - _db_open_sell_ids,
            }
            self._pending_cancel_wallet_ids_by_side = (
                self._filter_pending_cancel_wallet_ids_by_dexie(
                    _pending_cancel_wallet_ids_by_side
                )
            )
            self._track_pending_cancel_settle_watchdog(
                self._pending_cancel_wallet_ids_by_side
            )
            if _zombie_buys > 0 or _zombie_sells > 0:
                log_event(
                    "info",
                    "zombie_wallet_offers",
                    f"Wallet has {_zombie_buys} zombie buy / {_zombie_sells} zombie sell "
                    f"offers (cancelled in DB, still active in Sage) — excluded from cap",
                )
        except Exception as _dbo_err:
            _db_open_buy_ids = current_buy_ids
            _db_open_sell_ids = current_sell_ids
            self._pending_cancel_wallet_ids_by_side = {
                "buy": set(),
                "sell": set(),
            }
            log_event(
                "debug",
                "db_open_ids_fallback",
                f"DB-open cap filter failed (non-critical): {_dbo_err}",
            )

        # Remove recently-created offers now visible in wallet (prevents double-counting)
        self.offer_manager.clean_visible_recently_created(
            current_buy_ids | current_sell_ids
        )

        # Prune closed sniper/boost offers so caps stay accurate
        all_open_ids = current_buy_ids | current_sell_ids
        self.sniper.prune_active_snipes(all_open_ids)
        self.boost_manager.prune_active_boosts(all_open_ids)

        # Safety sweep: any sniper trade_id that is STILL open but not
        # tracked by _probe_state is an orphan from an abandoned probe
        # cycle (e.g. a thread that landed its offer after being
        # abandoned at 30s). These must be cancelled because they
        # occupy a sniper slot and sit on the book untracked.
        try:
            with self._probe_lock:
                _probe_known = set()
                for _key in ("buy_tid", "sell_tid"):
                    _tid = self._probe_state.get(_key)
                    if _tid:
                        _probe_known.add(_tid)
            with self.sniper._snipe_lock:
                _sniper_tracked = set(self.sniper._active_snipe_ids)
            _orphan_snipes = (_sniper_tracked & all_open_ids) - _probe_known
            if _orphan_snipes:
                log_event(
                    "info",
                    "sniper_orphan_sweep",
                    f"Sweep found {len(_orphan_snipes)} orphan sniper "
                    f"offer(s) not tracked by probe state — cancelling",
                )
                try:
                    self.offer_manager.cancel_offers(
                        list(_orphan_snipes),
                        reason="sniper_orphan_sweep",
                        skip_confirmation=True,
                    )
                    with self.sniper._snipe_lock:
                        for _tid in _orphan_snipes:
                            if _tid in self.sniper._active_snipe_ids:
                                self.sniper._active_snipe_ids.remove(_tid)
                            self.sniper._active_snipe_sides.pop(_tid, None)
                except Exception as _sweep_err:
                    log_event(
                        "error",
                        "sniper_orphan_sweep_failed",
                        f"Failed to cancel orphan snipers: {_sweep_err}",
                    )
        except Exception as _sweep_outer:
            log_event(
                "debug",
                "sniper_orphan_sweep_skipped",
                f"Orphan sweep skipped: {_sweep_outer}",
            )

        self._set_state(
            open_buys=len(current_buy_ids), open_sells=len(current_sell_ids)
        )

        # ---- Update mempool watcher with current offer coin IDs ----
        # The watcher scans Coinset mempool every 5s for these specific coins.
        # When one appears as a removal (taker spending our locked coin), a
        # fill_imminent signal fires and the bot wakes early for fill detection.
        if _mempool_watcher_mod:
            try:
                w = _mempool_watcher_mod._watcher_instance
                if w:
                    # Sage wallet offer records do not reliably include the
                    # locked coin_id. CATalyst's DB rows do, so include the
                    # DB-open book when feeding the mempool watcher.
                    all_open_offers = (
                        list(open_buys)
                        + list(open_sells)
                        + list(_db_buy_all)
                        + list(_db_sell_all)
                    )
                    offer_coin_ids = {
                        o.get("coin_id", "")
                        for o in all_open_offers
                        if o.get("coin_id")
                    }
                    w.set_watched_offer_coins(offer_coin_ids)
            except Exception:
                pass  # Non-critical — watcher degrades gracefully

        print(
            f" {len(current_buy_ids)} buys, {len(current_sell_ids)} sells, {len(closed)} closed",
            flush=True,
        )
        log_event(
            "info",
            "wallet_sync",
            f"Wallet: {len(current_buy_ids)} open buys, {len(current_sell_ids)} open sells, "
            f"{len(closed)} closed",
        )
        if self._wallet_sync_stale_cycle:
            err = str(
                wallet_sync_meta.get("last_error") or "wallet get_offers unavailable"
            )
            cache_note = "cached" if wallet_sync_meta.get("using_cache") else "empty"
            if not self._wallet_sync_was_stale:
                log_event(
                    "warning",
                    "wallet_sync_stale",
                    f"Wallet offer sync stale — using {cache_note} wallet snapshot "
                    f"({len(current_buy_ids)}b/{len(current_sell_ids)}s). {err}",
                )
                # Initialise stale-cycle counter on first stale cycle
                self._wallet_stale_streak = 1
                self._wallet_stale_first_at = time.time()
            else:
                self._wallet_stale_streak = getattr(self, "_wallet_stale_streak", 0) + 1
            # F12 fix (2026-04-08): escalation timeout. The original code
            # only ever showed a single warning-level alert. If wallet
            # sync stays stale for many minutes the user might miss it.
            # Now: escalate to error severity after 10 stale cycles
            # (~7 minutes at LOOP_SECONDS=45) and to a CRITICAL alert
            # after 20 stale cycles (~15 minutes), with the same
            # action_label so the user can take action from either.
            stale_streak = getattr(self, "_wallet_stale_streak", 1)
            if stale_streak >= 20:
                _alert_sev = "error"
                _alert_title = "Wallet Sync CRITICAL"
                _alert_msg = (
                    f"Wallet offer sync has been stale for {stale_streak} cycles "
                    f"(~{int((time.time() - getattr(self, '_wallet_stale_first_at', time.time())) / 60)} min). "
                    f"Fill detection and coin management are blocked. "
                    f"Please restart Sage now."
                )
            elif stale_streak >= 10:
                _alert_sev = "warning"
                _alert_title = "Wallet Sync Degraded (sustained)"
                _alert_msg = (
                    f"Wallet sync has been stale for {stale_streak} cycles. "
                    f"Restart Sage if this persists."
                )
            else:
                _alert_sev = "warning"
                _alert_title = "Wallet Sync Degraded"
                _alert_msg = (
                    "Using the last known offer book until Sage responds again. "
                    "New fills and coin-management actions are paused."
                )
            self._emit_alert(
                "wallet_offer_sync",
                _alert_sev,
                _alert_title,
                _alert_msg,
                action="restart_sage",
                action_label="Restart Sage",
            )
            self._wallet_sync_was_stale = True
        else:
            # Reset stale streak counter on first fresh cycle
            self._wallet_stale_streak = 0
            self._wallet_stale_first_at = 0
            if self._wallet_sync_was_stale:
                log_event(
                    "info",
                    "wallet_sync_live_again",
                    "Wallet offer sync is fresh again — resuming normal trading actions",
                )
            self._clear_alert("wallet_offer_sync")
            self._wallet_sync_was_stale = False

        # ---- Step 4: Detect fills ----
        self._set_cycle_step("step4_fill_detection")
        # F11 fix (2026-04-08): drain any pending mempool watcher signals
        # right before fill detection. Originally signals were only checked
        # at the TOP of the next iteration, meaning a fill_imminent that
        # arrived mid-cycle would wait for the cycle to finish before being
        # logged + waking the loop. Now we drain at this in-cycle sync point
        # too — the wake itself is no longer needed because we're already
        # running the fill detection that would have benefited from it,
        # but the log entry + state update still happen.
        try:
            self._drain_mempool_signals(in_cycle=True)
        except Exception:
            pass  # Non-critical
        price_data, mid_price, arb_gap = self._refresh_cycle_price_after_mempool_move(
            price_data,
            mid_price,
            arb_gap,
        )
        print("   [4] Checking fills...", end="", flush=True)
        fill_result = self.fill_tracker.detect_fills(
            current_buy_ids, current_sell_ids, self.offer_manager._offer_details_cache
        )

        buy_fills = fill_result.get("buy_fills", [])
        sell_fills = fill_result.get("sell_fills", [])

        if buy_fills or sell_fills:
            print(
                f" [FILL] {len(buy_fills)} buys, {len(sell_fills)} sells FILLED!",
                flush=True,
            )
            log_event(
                "info",
                "fills_detected",
                f"Fills this cycle: {len(buy_fills)} buys, {len(sell_fills)} sells",
            )
            # Push fill events to GUI instantly
            for fill in buy_fills + sell_fills:
                self._emit(
                    "fill",
                    {
                        "side": fill.get("side", ""),
                        "price": str(fill.get("price", "")),
                        "size_xch": str(fill.get("size_xch", "")),
                        "tier": fill.get("tier", ""),
                    },
                )
            # Reset coin backoff on fills (new coins freed up)
            self.coin_manager.reset_backoff()
            # Coin snapshot after fills — coins consumed + new coins received
            self.coin_manager.snapshot_coins("offer_filled")
            self._emit_coin_update("offer_filled")
            # Fix H: invalidate position sanity baseline so the next
            # housekeeping tick re-snaps against the post-fill state.
            # Without this, recorded fills change net_position but the
            # stale baseline drifts, spamming position_sanity_drift warnings.
            self._position_baseline_cat = None
            self._clear_adaptive_target_backoff_for_confirmed_fills(
                buy_fills, sell_fills
            )
            self._apply_immediate_sweep_protection(buy_fills, sell_fills)

        toxicity_snapshot = self._update_market_toxicity(
            price_data=price_data,
            mid_price=mid_price,
            arb_gap=arb_gap,
            open_buys=open_buys,
            open_sells=open_sells,
            buy_fills=buy_fills,
            sell_fills=sell_fills,
        )
        toxicity_cancelled = self._cancel_toxicity_throttled_offers(
            toxicity_snapshot,
            current_buy_ids=current_buy_ids,
            current_sell_ids=current_sell_ids,
        )
        if toxicity_cancelled["buy"] or toxicity_cancelled["sell"]:
            current_buy_ids -= toxicity_cancelled["buy"]
            current_sell_ids -= toxicity_cancelled["sell"]
            open_buys = [
                offer
                for offer in open_buys
                if str(offer.get("trade_id") or "") not in toxicity_cancelled["buy"]
            ]
            open_sells = [
                offer
                for offer in open_sells
                if str(offer.get("trade_id") or "") not in toxicity_cancelled["sell"]
            ]

        if not buy_fills and not sell_fills:
            print(" none", flush=True)
            pass  # No fills — nothing to log

        # ---- AMM drift check — force requote if AMM price has moved ----
        # If AMMMonitor has data, check whether the current AMM price has
        # drifted far enough from our last quoted prices to make our offers
        # arb targets. If so, flag both sides for requote immediately.
        #
        # Fix 4: cooldown gate. The AMM drift trigger used to bypass
        # REQUOTE_COOLDOWN_SECS, which combined with a stale baseline
        # (which Fix 4's baseline-on-attempt advance also addresses) caused
        # a feedback loop where every cycle re-fired the same drift,
        # generating a requote storm. We now refuse to re-trigger drift
        # requote within REQUOTE_COOLDOWN_SECS of the last AMM-drift force.
        try:
            if self.amm_monitor.is_available() and self._loop_count > 5:
                amm_drift_bps = self.amm_monitor.get_drift_bps()
                if amm_drift_bps is not None:
                    _drift_threshold = Decimal(
                        str(getattr(cfg, "AMM_DRIFT_REQUOTE_BPS", "80"))
                    )
                    if amm_drift_bps >= _drift_threshold:
                        _now = time.time()
                        _cooldown = float(
                            getattr(cfg, "REQUOTE_COOLDOWN_SECS", 60) or 60
                        )

                        # Per-side AMM-drift cooldown. Previously one shared
                        # scalar gated both sides, so a buy-side force set
                        # at t=0 blocked a sell-side force at t=30 even
                        # though the opposite-direction move had just
                        # exposed the sell ladder. Track buy and sell
                        # independently; fall back to the legacy scalar
                        # once on upgrade so operators mid-session don't
                        # get an immediate double-fire.
                        if not isinstance(
                            getattr(self, "_last_amm_drift_force_at", None), dict
                        ):
                            _legacy = float(
                                getattr(self, "_last_amm_drift_force_at", 0) or 0
                            )
                            self._last_amm_drift_force_at = {
                                "buy": _legacy,
                                "sell": _legacy,
                            }

                            # Determine which side is vulnerable based on price direction
                        try:
                            _amm_state = self.amm_monitor._state or {}
                            _amm_price = Decimal(
                                str(_amm_state.get("amm_price", 0) or 0)
                            )
                            if _amm_price > 0 and self._current_mid_price > 0:
                                _target_sides = (
                                    ["buy"]
                                    if _amm_price < self._current_mid_price
                                    else ["sell"]
                                )
                                _direction_note = (
                                    "price DOWN"
                                    if _amm_price < self._current_mid_price
                                    else "price UP"
                                )
                            else:
                                _target_sides = ["buy", "sell"]
                                _direction_note = "direction unknown"
                        except Exception:
                            _target_sides = ["buy", "sell"]
                            _direction_note = "direction error"

                        for _target_side in _target_sides:
                            _backoff_remaining = self._requote_backoff_remaining(
                                _target_side
                            )
                            if _backoff_remaining > 0:
                                log_event(
                                    "info",
                                    "amm_drift_requote_backoff",
                                    f"AMM drift would force {_target_side} requote, "
                                    f"but that side is in requote failure backoff "
                                    f"for {_backoff_remaining:.0f}s",
                                    data={
                                        "side": _target_side,
                                        "remaining_secs": round(_backoff_remaining, 1),
                                    },
                                )
                                continue
                            _last_force = float(
                                self._last_amm_drift_force_at.get(_target_side, 0) or 0
                            )
                            if (_now - _last_force) < _cooldown:
                                continue  # per-side cooldown active
                            if not self._force_requote.get(_target_side):
                                log_event(
                                    "info",
                                    "amm_drift_requote_triggered",
                                    f"AMM drift {_bps_to_pct(amm_drift_bps)} "
                                    f"({_direction_note}) — forcing "
                                    f"{_target_side} requote",
                                    data={
                                        "drift_bps": str(
                                            amm_drift_bps.quantize(Decimal("0.1"))
                                        ),
                                        "side": _target_side,
                                    },
                                )
                            self._force_requote[_target_side] = True
                            self._last_amm_drift_force_at[_target_side] = _now
        except Exception as _amm_drift_err:
            log_event(
                "debug",
                "amm_drift_check_error",
                f"AMM drift check error (non-critical): {_amm_drift_err}",
            )

        fills_hour = None
        try:
            from database import count_recent_fills

            fills_hour = count_recent_fills(hours=1)
            self.risk_manager.update_fill_rate(Decimal(str(fills_hour)))
        except Exception as e:
            log_event(
                "debug", "fill_rate_update_failed", f"Fill-rate update failed: {e}"
            )

        # V3: Record trading pace after fill detection
        if buy_fills or sell_fills:
            try:
                from database import record_trading_pace

                if fills_hour is None:
                    from database import count_recent_fills

                    fills_hour = count_recent_fills(hours=1)
                if fills_hour > getattr(cfg, "FILLS_PER_HOUR_BUSY", 10):
                    pace = "busy"
                elif fills_hour < getattr(cfg, "FILLS_PER_HOUR_SLOW", 2):
                    pace = "slow"
                else:
                    pace = "normal"
                active_count = len(current_buy_ids) + len(current_sell_ids)
                record_trading_pace(fills_hour, pace, active_count)
                # trading_pace log removed — pace shown in GUI + console
            except Exception as e:
                log_event("debug", "pace_record_failed", f"Pace recording failed: {e}")

        # ---- Step 5: Match round-trip PnL ----
        # Always attempt matching — catches newly detected fills AND
        # previously-unmatched fills (e.g. after migration clears bad matches).
        matched = self.fill_tracker.match_round_trips()
        if matched:
            total_pnl = sum(m.get("pnl_xch", Decimal("0")) for m in matched)
            log_event(
                "info",
                "pnl_matched",
                f"Matched {len(matched)} round-trips, total PnL: {total_pnl:+.8f} XCH",
            )

        # ---- Step 6: Update inventory ----
        print("   [6] Updating inventory...", end="", flush=True)
        self.risk_manager.update_inventory()
        inv = self.risk_manager.get_inventory_state()
        net_pos = inv.get("net_position_cat", "0")
        print(f" net position: {net_pos} CAT", flush=True)
        # step6 inventory log removed — console print + GUI push covers this

        # ---- Step 7: Pre-emptive offer refresh ----
        # Detect offers approaching expiry and cancel them early so Step 10
        # can replace them at the current best price with correct tier sizing.
        # With 24h expiry and 30min refresh window, offers get replaced smoothly
        # with zero gap in market presence.
        expired = 0
        if (
            cfg.OFFER_EXPIRY_SECS > 0
            and not getattr(self, "_graceful_in_progress", False)
            and not self._recovery_is_active()
        ):
            all_open = open_buys + open_sells
            expiring_tids = self.offer_manager.detect_expiring_offers(
                all_open, refresh_before_secs=cfg.OFFER_REFRESH_BEFORE
            )
            # ── Incremental reaction: cap expiry refreshes per cycle ──
            _max_refresh = int(getattr(cfg, "CYCLE_MAX_EXPIRY_REFRESH", 4) or 4)
            if len(expiring_tids) > _max_refresh:
                log_event(
                    "info",
                    "step7_refresh_capped",
                    f"Expiry refresh: {len(expiring_tids)} expiring but "
                    f"capping at {_max_refresh} per cycle (rest deferred)",
                )
                expiring_tids = expiring_tids[:_max_refresh]
            if expiring_tids:
                log_event(
                    "info",
                    "step7_refresh",
                    f"Pre-emptive refresh: cancelling {len(expiring_tids)} "
                    f"offers expiring within {cfg.OFFER_REFRESH_BEFORE}s",
                )
                # skip_confirmation=True: expiry refreshes happen every cycle;
                # blocking 60-90s for coins to return would stall the loop.
                cancel_result = self.offer_manager.cancel_offers(
                    expiring_tids, reason="pre_emptive_refresh", skip_confirmation=True
                )
                expired = sum(
                    1 for r in cancel_result.values() if r and r.get("success")
                )
                # Update live counts so Step 10 sees the slots as free
                # and creates replacements THIS loop, not next loop
                cancelled_set = {
                    tid for tid, r in cancel_result.items() if r and r.get("success")
                }
                current_buy_ids -= cancelled_set
                current_sell_ids -= cancelled_set
        else:
            pass  # Expiry disabled — nothing to log

        # ---- Step 7b: Spacescan balance verification (periodic health check) ----
        # Every N loops, compare wallet balance vs on-chain truth.
        # Free tier users: balance checks are skipped (budget reserved for fill verification).
        # Pro tier users: runs every SPACESCAN_BALANCE_CHECK_EVERY_N loops.
        spacescan_check_every = getattr(cfg, "SPACESCAN_BALANCE_CHECK_EVERY_N", 10)
        if (
            getattr(cfg, "SPACESCAN_ENABLED", False)
            and self._loop_count > 0
            and self._loop_count % spacescan_check_every == 0
        ):
            try:
                from spacescan import check_balance_discrepancy, should_check_balance

                # Free tier: skip balance checks to preserve API budget for fills
                if should_check_balance():
                    # Get wallet's reported balance
                    from wallet import get_wallet_balance

                    wallet_bal = get_wallet_balance(cfg.WALLET_ID_XCH)
                    wallet_xch = Decimal("0")
                    if wallet_bal and wallet_bal.get("success"):
                        _wb = wallet_bal.get("wallet_balance") or wallet_bal
                        wallet_xch = Decimal(
                            str(_wb.get("confirmed_wallet_balance", 0))
                        ) / Decimal("1000000000000")

                    our_address = getattr(cfg, "WALLET_ADDRESS", "")
                    if our_address and wallet_xch > 0:
                        result = check_balance_discrepancy(our_address, wallet_xch)
                        if result.get("xch_ok"):
                            log_event(
                                "debug",
                                "spacescan_balance_ok",
                                f"Balance check OK — Wallet: {wallet_xch:.4f}, "
                                f"On-chain: {result.get('xch_onchain', '?')}",
                            )
                        else:
                            print(
                                f"\n   [WARN] BALANCE MISMATCH! Wallet: {wallet_xch:.4f} XCH, "
                                f"On-chain: {result.get('xch_onchain', '?')} XCH",
                                flush=True,
                            )
                else:
                    log_event(
                        "debug",
                        "spacescan_balance_skip",
                        "Spacescan free tier — skipping balance check (budget reserved for fills)",
                    )
            except ImportError:
                pass  # spacescan module not available
            except Exception as e:
                log_event(
                    "debug",
                    "spacescan_balance_error",
                    f"Spacescan balance check failed: {e}",
                )

        # ---- Step 7c: Retry failed cancels (V1 parity) ----
        retried = self.offer_manager.retry_failed_cancels()
        if retried > 0:
            suffix = "" if retried == 1 else "s"
            log_event(
                "info",
                "cancel_retries",
                f"Cancel retry pass completed for {retried} pending cancel{suffix}",
            )

        # Update cancel retry alert
        pending_retries = len(self.offer_manager._pending_cancel_retries)
        if pending_retries > 0:
            self._emit_alert(
                "cancel_retries",
                "warning",
                f"{pending_retries} stuck cancel(s)",
                "Some offers failed to cancel and are queued for retry.",
                action="stop_bot",
                action_label="Stop Bot",
            )
        else:
            self._clear_alert("cancel_retries")

        self._maybe_finalize_graceful_migration(
            current_buy_ids=current_buy_ids,
            current_sell_ids=current_sell_ids,
        )

        # ---- Step 7d: Refresh fee pool after all cancels ----
        # Steps 7/7c may have consumed fee coins via Sage auto-pick.
        # Re-query spendable fee coins so the pool only contains coins
        # that are actually available — prevents creates (steps 8-10)
        # from passing an already-spent coin to make_offer.
        try:
            self.coin_manager.refresh_fee_pool_from_wallet()
        except Exception:
            pass  # non-fatal — pool keeps its existing state

        # ---- Step 8: Sniper Probe — price discovery before main offers ----
        # FLOW:
        #   1. Fire buy+sell snipers near Tibet price (probing)
        #   2. Wait one loop (~60s) to see which survive
        #   3. If both survive → price confirmed → build main offers behind them
        #   4. If one taken → adjust that side's buffer wider → retry probe
        #   5. If both taken → widen both → retry
        #   6. Only after probe confirms do main offers deploy
        sniper_fired = False
        _sniper_on = getattr(cfg, "SNIPER_ENABLED", True)
        recovery_active_now = self._recovery_is_active()
        launch_reason = self._get_sniper_launch_reason(
            mid_price,
            arb_gap,
            current_buy_ids=current_buy_ids,
            current_sell_ids=current_sell_ids,
        )
        launch_reason = self._defer_probe_launch_for_pending_cancel(launch_reason)

        swap_recency_window = max(60, cfg.LOOP_SECONDS * 3)
        last_swap_ts = self._watcher_data.get("last_change_ts", 0)
        recent_swap = (time.time() - last_swap_ts) < swap_recency_window

        def _retire_probe_offers(reason: str) -> bool:
            """Cancel live probe offers once their linger window has elapsed."""
            probe = self._probe_state
            live_probe_ids = []
            open_now = current_buy_ids | current_sell_ids
            for tid in [probe.get("buy_tid"), probe.get("sell_tid")]:
                if tid and tid in open_now and tid not in live_probe_ids:
                    live_probe_ids.append(tid)

            if not live_probe_ids:
                return True

            log_event(
                "info",
                "probe_retire",
                f"Retiring {len(live_probe_ids)} probe offer(s) before main ladder "
                f"({reason})",
            )

            # skip_confirmation=True: probe retirement is fire-and-forget.
            # The main ladder builds immediately after; waiting 60-90s for
            # coins to return from the probe cancel blocks the whole cycle.
            cancel_result = self.offer_manager.cancel_offers(
                live_probe_ids,
                reason=reason,
                skip_confirmation=True,
            )
            cancelled = {
                tid for tid, res in cancel_result.items() if res and res.get("success")
            }
            failed = [tid for tid in live_probe_ids if tid not in cancelled]

            if cancelled:
                current_buy_ids.difference_update(cancelled)
                current_sell_ids.difference_update(cancelled)
                self._set_state(
                    open_buys=len(current_buy_ids), open_sells=len(current_sell_ids)
                )
                with self.sniper._snipe_lock:
                    self.sniper._active_snipe_ids = [
                        tid
                        for tid in self.sniper._active_snipe_ids
                        if tid not in cancelled
                    ]
                    for tid in cancelled:
                        self.sniper._active_snipe_sides.pop(tid, None)
                for tid in cancelled:
                    if probe.get("buy_tid") == tid:
                        self._clear_probe_side("buy", tid)
                    if probe.get("sell_tid") == tid:
                        self._clear_probe_side("sell", tid)

            if failed:
                log_event(
                    "warning",
                    "probe_retire_failed",
                    f"Failed to retire {len(failed)} probe offer(s) before main "
                    f"ladder: {', '.join(tid[:12] for tid in failed)}",
                )
                return False

            return True

        if recovery_active_now:
            if self._probe_state.get("active", False) or self.sniper._active_snipe_ids:
                retired = _retire_probe_offers("recovery_mode")
                # Always clear probe active flag — even on cancel failure the main
                # ladder must build. Step 8c will retry the cancel via sniper_cleanup.
                with self._probe_lock:
                    self._probe_state["active"] = False
                    self._probe_state["attempt"] = 0
                    self._probe_state["last_wait_log_at"] = 0
                if not retired:
                    log_event(
                        "warning",
                        "probe_retire_recovery_failed",
                        "Recovery: probe offer cancel failed — step 8c will retry "
                        "via sniper_cleanup. Main ladder will build around it.",
                    )
            self._clear_alert("probe_status")
            log_event(
                "debug",
                "sniper_recovery_pause",
                "Recovery mode active — skipping sniper probe and gap-closer churn",
            )
        elif not _sniper_on:
            log_event(
                "debug", "sniper_disabled", "Sniper disabled via config — skipping"
            )

        # ---- Phase 2: CHECK existing probe results ----
        elif self._probe_state["active"]:
            probe = self._probe_state
            buy_tid = probe["buy_tid"]
            sell_tid = probe["sell_tid"]
            now_ts = time.time()
            current_buy_ids, current_sell_ids = self._watch_active_probe_window(
                current_buy_ids,
                current_sell_ids,
            )
            now_ts = time.time()

            open_ids = current_buy_ids | current_sell_ids
            buy_state = self._classify_probe_offer("buy", buy_tid, open_ids)
            sell_state = self._classify_probe_offer("sell", sell_tid, open_ids)
            buy_alive = bool(buy_state.get("confirmable"))
            sell_alive = bool(sell_state.get("confirmable"))

            # If sell was never placed (no CAT sniper coins), buy-only probe is sufficient
            sell_required = bool(sell_tid)
            if buy_alive and (sell_alive or not sell_required):
                remaining_hold = self._probe_hold_seconds_remaining(probe, now_ts)
                if remaining_hold > 0:
                    last_notice_at = float(probe.get("last_wait_log_at") or 0)
                    if (now_ts - last_notice_at) >= 5:
                        probe_age = max(
                            0.0, now_ts - float(probe.get("launched_at") or now_ts)
                        )
                        log_event(
                            "info",
                            "probe_hold_wait",
                            f"Both probes alive for {probe_age:.1f}s — holding "
                            f"{remaining_hold:.1f}s more before confirming",
                        )
                        probe["last_wait_log_at"] = now_ts
                    log_event(
                        "debug",
                        "probe_hold_status",
                        "Probe pair still aging before main ladder deploy",
                    )
                else:
                    # BOTH survived long enough — price confirmed.
                    # Keep probes live for a linger window so the main ladder
                    # can build behind the discovered edge without losing it.
                    probe["confirmed_price"] = probe["tibet_price"]
                    probe["confirmed_at"] = now_ts
                    probe["active"] = False
                    mid_price = probe["tibet_price"]
                    self._current_mid_price = mid_price
                    self._set_state(mid_price=str(mid_price))
                    # Floor tightening: record this buffer as a known-safe floor.
                    # _get_sniper_launch_reason will schedule the next tighter
                    # round on the next cycle (after the cooldown).
                    confirmed_buffer = int(probe.get("tightening_buffer_bps") or 0)
                    if confirmed_buffer > 0:
                        probe["safe_buffer_bps"] = confirmed_buffer
                    linger_secs = int(getattr(cfg, "SNIPER_LINGER_SECS", 600) or 0)
                    log_event(
                        "info",
                        "probe_confirmed",
                        f"Both probes survived — price confirmed at Tibet "
                        f"{probe['tibet_price']:.8f} (buffer {_bps_to_pct(confirmed_buffer)}). "
                        f"Keeping probes live for {linger_secs}s while building "
                        f"main offers behind them.",
                    )
                    log_event(
                        "info",
                        "probe_confirmed_status",
                        "Price confirmed — deploying main offers behind live probes",
                    )
                    self._clear_alert("probe_status")

            elif any(
                s.get("unverified")
                for s in ([buy_state] + ([sell_state] if sell_required else []))
            ) and not any(
                s.get("taken")
                for s in ([buy_state] + ([sell_state] if sell_required else []))
            ):
                last_notice_at = float(probe.get("last_wait_log_at") or 0)
                if (now_ts - last_notice_at) >= 5:
                    unverified_sides = [
                        f"{s.get('side')}:{s.get('reason', 'unverified')}"
                        for s in ([buy_state] + ([sell_state] if sell_required else []))
                        if s.get("unverified")
                    ]
                    log_event(
                        "info",
                        "probe_state_wait",
                        "Probe edge not yet verified on Dexie "
                        f"({'+'.join(unverified_sides)}) - waiting before "
                        "main ladder deploy",
                    )
                    probe["last_wait_log_at"] = now_ts
                log_event(
                    "debug",
                    "probe_hold_status",
                    "Probe pair still awaiting Dexie verification",
                )

            else:
                # At least one probe was taken — adjust and retry.
                # If we were tightening below the safe buffer (floor-discovery
                # mode) and got arbed, we just empirically located the floor.
                # Mark converged so we stop tightening, and log it.
                taken_buffer = int(probe.get("tightening_buffer_bps") or 0)
                safe_buffer = int(probe.get("safe_buffer_bps") or 0)
                if taken_buffer and safe_buffer and taken_buffer < safe_buffer + 1:
                    if not probe.get("floor_converged", False):
                        probe["floor_converged"] = True
                        log_event(
                            "info",
                            "probe_floor_found",
                            f"Empirical floor located: probe at {_bps_to_pct(taken_buffer)} "
                            f"got taken; last safe buffer was {_bps_to_pct(safe_buffer)}. "
                            f"Floor sits between {_bps_to_pct(taken_buffer)} and "
                            f"{_bps_to_pct(safe_buffer)} "
                            f"from Tibet — tightening stopped.",
                        )
                if probe["attempt"] >= probe["max_attempts"]:
                    self._mark_recovery_probe_churn()
                    self._probe_warn(
                        "probe_max_retries",
                        f"Probe still unsafe after {probe['attempt']} attempts. "
                        "Continuing to back off; main ladder remains held.",
                    )
                    self._probe_warn(
                        "probe_timeout_status",
                        "Probe still unsafe - main ladder held until an edge survives",
                    )
                    probe["max_attempts"] = int(probe["attempt"]) + 5
                attempt = probe["attempt"] + 1
                base_buffer = getattr(cfg, "SNIPER_BUFFER_BPS", Decimal("50"))
                # Widen buffer by 50 BPS per failed attempt
                adjusted_buffer = base_buffer + Decimal(str(attempt * 50))
                tibet_p = probe["tibet_price"]
                probe_prices = self._get_market_aware_probe_prices(
                    tibet_p,
                    adjusted_buffer,
                    offer_edges=self._last_live_offer_edges,
                )

                taken_sides = []
                if buy_tid and (
                    buy_state.get("taken")
                    or (not buy_alive and not buy_state.get("unverified"))
                ):
                    taken_sides.append("buy")
                if sell_tid and (
                    sell_state.get("taken")
                    or (not sell_alive and not sell_state.get("unverified"))
                ):
                    taken_sides.append("sell")

                log_event(
                    "info",
                    "probe_retry",
                    f"Probe attempt {attempt}: {'+'.join(taken_sides)} taken, "
                    f"widening buffer to {_bps_to_pct(adjusted_buffer)}",
                )
                log_event(
                    "info",
                    "probe_retry_status",
                    f"Probe retry {attempt} — widening spread",
                )
                self._clear_alert("probe_status")

                new_sell_price = self._apply_probe_retry_backoff(
                    "sell",
                    probe_prices["sell_price"],
                    probe.get("sell_price"),
                )
                new_buy_price = self._apply_probe_retry_backoff(
                    "buy",
                    probe_prices["buy_price"],
                    probe.get("buy_price"),
                )

                # Fire new probes
                sell_results = None
                buy_results = None
                # Only retry sell if it was originally placed — don't hammer
                # CAT sniper creation when no CAT sniper coins exist
                if sell_tid and not sell_alive:
                    sell_results = self.sniper.try_snipe_single(
                        "sell", new_sell_price, arb_gap
                    )
                # Retry buy only if taken (alive→gone) OR never placed
                # (use truthy check to exclude empty-string trade_id "")
                if (buy_tid and not buy_alive) or not buy_tid:
                    self.sniper._last_snipe_time = 0  # Reset cooldown
                    buy_results = self.sniper.try_snipe_single(
                        "buy", new_buy_price, arb_gap
                    )

                # Update probe state
                probe["attempt"] = attempt
                if sell_results:
                    probe["sell_tid"] = sell_results[0].get("trade_id", "")
                    probe["sell_price"] = new_sell_price
                    sniper_fired = True
                elif sell_alive:
                    pass  # Keep existing surviving sell probe
                if buy_results:
                    probe["buy_tid"] = buy_results[0].get("trade_id", "")
                    probe["buy_price"] = new_buy_price
                    sniper_fired = True
                elif buy_alive:
                    pass  # Keep existing surviving buy probe

                if sniper_fired:
                    probe["launched_at"] = time.time()
                    probe["last_wait_log_at"] = 0
                    self._emit("sniper", {"count": 1})
                    probe_result = self._process_active_probe(
                        current_buy_ids,
                        current_sell_ids,
                        arb_gap,
                        force_refresh=True,
                    )
                    current_buy_ids = probe_result["buy_ids"]
                    current_sell_ids = probe_result["sell_ids"]
                    sniper_fired = sniper_fired or probe_result.get(
                        "sniper_fired", False
                    )

        # ---- Phase 1: LAUNCH new probe (empty book or material market shift) ----
        # The arb-gap gate is intended for *rearm* triggers (price_move,
        # arb_gap_shift) — there it stops the bot from churning probes when
        # the market is calm and a single rearm wouldn't tell us anything.
        # The empty-book case is fundamentally different: we have NO offers
        # live, so there's no floor to anchor the main ladder against. We
        # must probe regardless of arb gap, otherwise the startup ladder
        # builds blind and you eat the discovery cost on real-size offers.
        elif launch_reason and (
            launch_reason.startswith("startup_empty_book")
            or arb_gap > cfg.SNIPER_MIN_GAP_BPS
        ):
            # --- Arb pressure gate ---
            # Suppress probe launch when arb activity is critical. A probe fired
            # into a hot arb environment will be swept instantly, wasting coins
            # and giving no useful price signal. We'll retry next cycle.
            _arb_pressure_max = float(getattr(cfg, "SNIPER_ARB_PRESSURE_MAX", 0.7))
            _arb_pressure_now = (
                self.amm_monitor.get_arb_pressure()
                if hasattr(self, "amm_monitor") and self.amm_monitor is not None
                else 0.0
            )
            if _arb_pressure_now >= _arb_pressure_max:
                log_event(
                    "warning",
                    "probe_launch_skipped_arb",
                    f"Probe launch suppressed — arb pressure {_arb_pressure_now:.2f} "
                    f">= gate {_arb_pressure_max:.2f} ({launch_reason}). "
                    "Will retry next cycle.",
                )
                launch_reason = None  # existing guard below uses this to no-op
            dexie_p = Decimal(str(price_data.get("dexie_price", 0) or 0))
            tibet_p = Decimal(str(price_data.get("tibet_price", 0) or 0))
            if dexie_p > 0 and tibet_p > 0:
                if self.sniper._active_snipe_ids:
                    retired = _retire_probe_offers("probe_rearm")
                    if not retired:
                        log_event(
                            "warning",
                            "probe_rearm_blocked",
                            "Material market shift detected but existing probe edges "
                            "could not be retired cleanly",
                        )
                        launch_reason = None
                if not launch_reason:
                    pass
                else:
                    # Iterative floor tightening overrides the default buffer
                    # with progressively tighter values until a probe gets
                    # taken. Otherwise use the comfortable default.
                    if launch_reason.startswith("floor_tighten"):
                        step = max(
                            1, int(getattr(cfg, "SNIPER_FLOOR_TIGHTEN_STEP_BPS", 15))
                        )
                        prev_buffer = int(
                            self._probe_state.get("tightening_buffer_bps")
                            or getattr(cfg, "SNIPER_BUFFER_BPS", 50)
                        )
                        sniper_buffer_bps = Decimal(str(max(1, prev_buffer - step)))
                    else:
                        sniper_buffer_bps = Decimal(
                            str(getattr(cfg, "SNIPER_BUFFER_BPS", 50))
                        )
                    probe_prices = self._get_market_aware_probe_prices(
                        tibet_p,
                        sniper_buffer_bps,
                        offer_edges=self._last_live_offer_edges,
                    )
                    sell_price = probe_prices["sell_price"]
                    buy_price = probe_prices["buy_price"]
                    book_bid = probe_prices["overall_best_bid"]
                    book_ask = probe_prices["overall_best_ask"]

                    log_event(
                        "info",
                        "probe_launch",
                        f"Launching price probe — {launch_reason}: "
                        f"buy={buy_price:.8f}, sell={sell_price:.8f} "
                        f"(Tibet={tibet_p:.8f}, Dexie={dexie_p:.8f}, "
                        f"book_bid={book_bid:.8f}, book_ask={book_ask:.8f})",
                    )
                    log_event(
                        "info",
                        "probe_launch_status",
                        "Probing market — testing edge prices...",
                    )
                    self._clear_alert("probe_status")

                    total_snipes = 0
                    sell_tid = None
                    buy_tid = None

                    # Snapshot the set of sniper IDs BEFORE the probe fires
                    # so we can identify any orphan offers that land after
                    # an abandoned cycle. `try_snipe_single` always appends
                    # to _active_snipe_ids (even for an orphan), so by
                    # diffing before/after we can find the orphans to cancel.
                    with self.sniper._snipe_lock:
                        _pre_probe_snipe_ids = set(self.sniper._active_snipe_ids)

                    # Fire both sides in parallel — halves the probe deployment time
                    self.sniper._last_snipe_time = 0  # Reset cooldown for both
                    _probe_results = {}
                    _probe_cycle_valid = [True]  # Mutable flag; set False if we abandon

                    def _fire_probe(side, price):
                        result = self.sniper.try_snipe_single(side, price, arb_gap)
                        if _probe_cycle_valid[0]:  # Only write if cycle not abandoned
                            _probe_results[side] = result

                    sell_thread = threading.Thread(
                        target=_fire_probe, args=("sell", sell_price)
                    )
                    buy_thread = threading.Thread(
                        target=_fire_probe, args=("buy", buy_price)
                    )
                    sell_thread.start()
                    buy_thread.start()
                    sell_thread.join(timeout=30)
                    buy_thread.join(timeout=30)

                    cycle_abandoned = sell_thread.is_alive() or buy_thread.is_alive()

                    if cycle_abandoned:
                        _probe_cycle_valid[0] = False
                        log_event(
                            "warning",
                            "probe_thread_timeout",
                            f"Probe thread(s) timed out after 30s "
                            f"(sell_alive={sell_thread.is_alive()}, "
                            f"buy_alive={buy_thread.is_alive()}) — "
                            f"cycle abandoned.",
                        )
                        sell_results = None
                        buy_results = None
                    else:
                        sell_results = _probe_results.get("sell")
                        buy_results = _probe_results.get("buy")

                    # Compute any sniper IDs that landed since our snapshot.
                    # These may include orphans from an abandoned cycle OR
                    # the legitimate offers from this probe's successful
                    # threads. We'll only cancel the orphans below (after
                    # the legitimate IDs are captured into probe_state).
                    with self.sniper._snipe_lock:
                        _post_probe_snipe_ids = set(self.sniper._active_snipe_ids)
                    _new_snipe_ids = _post_probe_snipe_ids - _pre_probe_snipe_ids

                    if sell_results:
                        sell_tid = sell_results[0].get("trade_id", "")
                        total_snipes += 1
                    if buy_results:
                        buy_tid = buy_results[0].get("trade_id", "")
                        total_snipes += 1

                    if total_snipes > 0:
                        sniper_fired = True
                        with self._probe_lock:
                            self._probe_state.update(
                                {
                                    "active": True,
                                    "buy_tid": buy_tid,
                                    "sell_tid": sell_tid,
                                    "buy_price": buy_price,
                                    "sell_price": sell_price,
                                    "tibet_price": tibet_p,
                                    "attempt": 0,
                                    "max_attempts": 5,
                                    "confirmed_price": None,
                                    "confirmed_at": 0,
                                    "launched_at": time.time(),
                                    "last_wait_log_at": 0,
                                    # Track the buffer this probe is testing so the
                                    # next floor_tighten round can shrink from it.
                                    "tightening_buffer_bps": int(sniper_buffer_bps),
                                    # Reset convergence on a fresh launch (price_move
                                    # or arb_gap_shift). Floor-tighten rounds inherit
                                    # the previous safe_buffer_bps via _get_sniper_…
                                    "safe_buffer_bps": (
                                        self._probe_state.get("safe_buffer_bps", 0)
                                        if launch_reason.startswith("floor_tighten")
                                        else 0
                                    ),
                                    "floor_converged": False,
                                }
                            )
                        self._remember_probe_market_snapshot(
                            mid_price,
                            arb_gap,
                            tibet_p,
                            launch_reason,
                        )
                        self._emit("sniper", {"count": total_snipes})
                        probe_result = self._process_active_probe(
                            current_buy_ids,
                            current_sell_ids,
                            arb_gap,
                            force_refresh=True,
                        )
                        current_buy_ids = probe_result["buy_ids"]
                        current_sell_ids = probe_result["sell_ids"]
                        sniper_fired = sniper_fired or probe_result.get(
                            "sniper_fired", False
                        )

                    # Cancel any orphan sniper offers that landed during an
                    # abandoned cycle. These are sniper IDs that appeared
                    # after our snapshot but are NOT the ones we just wrote
                    # into _probe_state as the legitimate probe offers.
                    _probe_tids = set()
                    if buy_tid:
                        _probe_tids.add(buy_tid)
                    if sell_tid:
                        _probe_tids.add(sell_tid)
                    _orphan_tids = _new_snipe_ids - _probe_tids
                    if _orphan_tids:
                        log_event(
                            "info",
                            "probe_orphan_cleanup",
                            f"Cancelling {len(_orphan_tids)} orphan probe offer(s) "
                            f"that landed after cycle abandonment or race: "
                            f"{[t[:16] + '...' for t in _orphan_tids]}",
                        )
                        try:
                            self.offer_manager.cancel_offers(
                                list(_orphan_tids),
                                reason="probe_orphan_cleanup",
                                skip_confirmation=True,
                            )
                            # Also prune from sniper's tracking so the cap
                            # doesn't stay inflated.
                            with self.sniper._snipe_lock:
                                for _tid in _orphan_tids:
                                    if _tid in self.sniper._active_snipe_ids:
                                        self.sniper._active_snipe_ids.remove(_tid)
                                    self.sniper._active_snipe_sides.pop(_tid, None)
                        except Exception as _orphan_err:
                            log_event(
                                "error",
                                "probe_orphan_cleanup_failed",
                                f"Failed to cancel orphan probe offers: {_orphan_err}",
                            )

        elif arb_gap > cfg.SNIPER_MIN_GAP_BPS and not launch_reason:
            log_event(
                "debug",
                "sniper_no_swap",
                f"Arb gap {_bps_to_pct(arb_gap)} but no fresh sniper trigger "
                f"(startup or material market shift) — keeping sniper idle",
            )

        # ---- Step 8b: Re-evaluate prices after sniper ----
        # If the sniper just fired, the market has moved. Fetch fresh prices
        # so the main offer batch (Step 10) uses post-snipe pricing.
        if sniper_fired:
            log_event(
                "info",
                "post_snipe_reprice",
                "Sniper fired — re-fetching prices before creating main offer batch",
            )
            fresh_price = self.price_engine.get_price(
                cfg.CAT_ASSET_ID, cfg.CAT_DECIMALS, cfg.CAT_TICKER_ID
            )
            fresh_mid = Decimal(str(fresh_price.get("mid_price", 0)))
            if fresh_mid > 0:
                old_mid = mid_price
                mid_price = fresh_mid
                self._current_mid_price = mid_price
                self._set_state(mid_price=str(mid_price))

                # Update arb gap with fresh data
                arb_gap = Decimal(str(fresh_price.get("arb_gap_bps", 0)))
                self.risk_manager.update_arb_gap(arb_gap)

                log_event(
                    "info",
                    "post_snipe_price",
                    f"Post-snipe price: {old_mid:.8f} → {fresh_mid:.8f} "
                    f"(arb gap now {_bps_to_pct(arb_gap)})",
                )

                # Push updated price to GUI
                self._emit(
                    "price_update",
                    {
                        "mid_price": str(mid_price),
                        "dexie_price": str(fresh_price.get("dexie_price", "")),
                        "tibet_price": str(fresh_price.get("tibet_price", "")),
                        "arb_gap_bps": str(arb_gap),
                        "spread_bps": self._bot_state.get("spread_bps", "0"),
                    },
                )

        # ---- Step 8b2: Emergency requote of stale offers on price shock ----
        # When a TibetSwap swap causes a large arb gap, old offers on the
        # vulnerable side are mispriced and will be arbed. Force an immediate
        # requote of that side, bypassing normal cooldown and fill protection.
        # Sells are vulnerable when price goes UP (they're too cheap).
        # Buys are vulnerable when price goes DOWN (they're too expensive).
        #
        # IMPORTANT: This is NOT gated on sniper_fired. The sniper may be
        # suppressed (startup, cooldown, cap) but stale offers still need
        # emergency requoting. Triggers on: recent swap + large arb gap.
        emergency_requote_triggered = (
            recent_swap and arb_gap > cfg.ARB_ALERT_THRESHOLD_BPS and mid_price > 0
        )
        if emergency_requote_triggered and not recovery_active_now:
            for eq_side in ["sell", "buy"]:
                if not self._config_enables_side(eq_side):
                    self._force_requote[eq_side] = False
                    continue
                last_q = self._last_quoted_price.get(eq_side, Decimal("0"))
                if last_q <= 0:
                    continue

                move_bps = abs(mid_price - last_q) / last_q * Decimal("10000")

                is_vulnerable = (eq_side == "sell" and mid_price > last_q) or (
                    eq_side == "buy" and mid_price < last_q
                )

                if is_vulnerable and move_bps > cfg.ARB_ALERT_THRESHOLD_BPS:
                    _pending_settle = self._pending_cancel_wallet_ids(eq_side)
                    if _pending_settle:
                        self._force_requote[eq_side] = True
                        self._log_pending_cancel_notice(
                            "warning",
                            "emergency_requote_deferred_pending_cancel_settle",
                            f"Emergency {eq_side} requote deferred — "
                            f"{len(_pending_settle)} wallet-active pending cancel(s) "
                            f"must settle before replacement offers are created",
                            side=eq_side,
                            pending_count=len(_pending_settle),
                            data={
                                "side": eq_side,
                                "pending_count": len(_pending_settle),
                                "move_bps": str(move_bps),
                            },
                        )
                        continue
                    _backoff_remaining = self._requote_backoff_remaining(eq_side)
                    if _backoff_remaining > 0:
                        log_event(
                            "info",
                            "emergency_requote_backoff",
                            f"Emergency {eq_side} requote suppressed for "
                            f"{_backoff_remaining:.0f}s after a zero-progress "
                            f"requote failure",
                            data={
                                "side": eq_side,
                                "remaining_secs": round(_backoff_remaining, 1),
                                "move_bps": str(move_bps),
                            },
                        )
                        continue
                    msg = (
                        f"[EMERGENCY] requote of {eq_side} side -- "
                        f"price shock {_bps_to_pct(move_bps)} "
                        f"({last_q:.8f} -> {mid_price:.8f}, "
                        f"arb gap: {_bps_to_pct(arb_gap)})"
                    )
                    print(msg, flush=True)  # Terminal-visible
                    log_event("warning", "emergency_requote", msg)

                    spread = self.risk_manager.get_adjusted_spread(eq_side)
                    requote_mid = self._get_probe_anchored_mid(eq_side, mid_price)
                    price_cap = (
                        self._get_probe_price_boundary(eq_side)
                        if eq_side == "buy"
                        else None
                    )
                    price_floor = (
                        self._get_probe_price_boundary(eq_side)
                        if eq_side == "sell"
                        else None
                    )
                    if requote_mid != mid_price:
                        log_event(
                            "info",
                            "probe_anchor_emergency",
                            f"Anchoring {eq_side} emergency requote to probe edge: "
                            f"mid {mid_price:.8f} -> {requote_mid:.8f}",
                        )
                    _live_ids = (
                        current_buy_ids if eq_side == "buy" else current_sell_ids
                    )
                    requote_result = self.offer_manager.requote_side(
                        eq_side,
                        requote_mid,
                        dexie_manager=self.dexie_manager,
                        risk_manager=self.risk_manager,
                        spread_fraction=spread,
                        price_cap=price_cap,
                        price_floor=price_floor,
                        live_offer_ids=_live_ids,
                        force_cancel_storm=True,
                    )
                    # Note: do NOT add to _requoted_this_cycle until we
                    # know progress was made. Previously we stamped this
                    # unconditionally, which caused Step 10 to skip the
                    # side even when the emergency requote produced zero
                    # new offers — the side could stay underfilled for an
                    # entire cycle while the book was still exposed on a
                    # sharp move. The `any_progress`-gated stamp below
                    # keeps Step 10 eligible to refill on a failed
                    # emergency.
                    # During an emergency requote we ONLY advance the
                    # baseline when we actually made progress. Previously
                    # the else-branch always stamped `requote_mid` "on
                    # attempt," which hid stale quotes at the old mid from
                    # the next cycle's drift detection — the bot could
                    # appear to have re-anchored at the new price while
                    # old wrong-side offers were still live. Keeping the old
                    # baseline on a no-progress/partial outcome means
                    # the next cycle re-fires the emergency path, which
                    # chips away at the residual stale offers on each
                    # subsequent pass.
                    processed_requote = self._process_emergency_requote_result(
                        eq_side,
                        requote_result,
                        requote_mid,
                        mid_price,
                    )
                    new_offers = processed_requote["new_offers"]
                    any_progress = bool(processed_requote["any_progress"])
                    replaced = int(processed_requote.get("replaced", 0) or 0)
                    waiting_for_cancel_settle = bool(
                        processed_requote.get("waiting_for_cancel_settle", False)
                    )
                    buy_q = self._last_quoted_price.get("buy")
                    sell_q = self._last_quoted_price.get("sell")
                    self.amm_monitor.notify_quoted_price(buy_q, sell_q)
                    self.coin_manager.snapshot_coins("emergency_requote")
                    self._emit_coin_update("emergency_requote")
                    self._last_bulk_create_time = time.time()

                    # Only mark the cycle-requoted flag + clear the force
                    # flag when we actually made progress. If the emergency
                    # produced zero new offers (no spare coins, create
                    # failures, tier-filter drained) we want BOTH Step 10's
                    # ladder-fill AND Step 9's drift retry to remain
                    # eligible on this cycle — setting these unconditionally
                    # caused the side to sit underfilled for a full cycle
                    # while the book was still exposed on a sharp move.
                    if any_progress:
                        self._record_shock_requote(
                            eq_side,
                            requote_mid,
                            source="emergency_requote",
                            severity="emergency",
                            new_count=len(new_offers),
                            replaced_count=replaced
                            if isinstance(requote_result, dict)
                            else len(new_offers),
                        )
                        self._requoted_this_cycle.add(eq_side)
                        self._force_requote[eq_side] = False
                        done_msg = (
                            f"[OK] Emergency requote {eq_side}: "
                            f"{len(new_offers)} new offers at {requote_mid:.8f}"
                        )
                        print(done_msg, flush=True)  # Terminal-visible
                        log_event("info", "emergency_requote_done", done_msg)
                    else:
                        if waiting_for_cancel_settle:
                            skipped_msg = (
                                f"[INFO] Emergency requote {eq_side}: "
                                f"cancel submitted, waiting for wallet settlement"
                            )
                            event_level = "info"
                        else:
                            skipped_msg = (
                                f"[WARN] Emergency requote {eq_side}: "
                                f"0 new offers produced — backing off "
                                f"so wallet settlement/reconcile can catch up"
                            )
                            event_level = "warning"
                        print(skipped_msg, flush=True)
                        log_event(
                            event_level,
                            "emergency_requote_retry_eligible",
                            skipped_msg,
                            data={"side": eq_side},
                        )

        # ---- Step 8c-pre: Monitor confirmed probes — do not auto re-fire ----
        # Once discovery is done, the main ladder should take over. If an edge
        # probe gets consumed, clear that side and wait for a fresh market move
        # before probing again.
        #
        # F67 note: do NOT gate this on `self.sniper._active_snipe_ids` —
        # `prune_active_snipes()` in step 3 runs BEFORE this block and removes
        # expired probe TIDs from that list. Gating on it means on-chain probe
        # expirations slip past without `_clear_probe_side` firing, the
        # baseline snap never runs, and the next cycle triggers a spurious
        # requote once `_get_probe_anchored_mid` falls through to plain mid.
        # Gate on the probe state directly — if a probe TID is recorded, we
        # need to reconcile its wallet status regardless of sniper tracking.
        _has_probe_tid = bool(
            self._probe_state.get("buy_tid") or self._probe_state.get("sell_tid")
        )
        if (
            not recovery_active_now
            and not self._probe_state.get("active", False)
            and self._probe_state.get("confirmed_price")
            and _has_probe_tid
            and self._loop_count >= 3
        ):
            # Check if any probe was taken (disappeared from wallet)
            all_open = current_buy_ids | current_sell_ids
            _probe_buy = self._probe_state.get("buy_tid")
            _probe_sell = self._probe_state.get("sell_tid")
            _buy_gone = _probe_buy and _probe_buy not in all_open
            _sell_gone = _probe_sell and _probe_sell not in all_open

            if _buy_gone or _sell_gone:
                taken_sides = []
                if _buy_gone:
                    taken_sides.append("buy")
                if _sell_gone:
                    taken_sides.append("sell")

                if _buy_gone:
                    self._clear_probe_side("buy", _probe_buy)
                if _sell_gone:
                    self._clear_probe_side("sell", _probe_sell)

                log_event(
                    "info",
                    "probe_consumed",
                    f"Confirmed probe {'+'.join(taken_sides)} was gone from wallet "
                    f"(filled, expired, or cancelled) — main ladder stays live and "
                    f"sniper will wait for a fresh market move",
                )

        # ---- Step 8c: Clean up sniper offers once their edge window expires ----
        # Discovery probes are temporary edge markers. Leave them up long enough
        # for the main ladder to settle behind them, then remove them whether the
        # arb gap has closed or not.
        if (
            not recovery_active_now
            and not sniper_fired
            and self.sniper._active_snipe_ids
            and not self._probe_state.get("active", False)
            and self._loop_count >= 3
            and self._probe_cleanup_seconds_remaining(self._probe_state, time.time())
            <= 0
        ):
            snipe_ids_to_cancel = list(self.sniper._active_snipe_ids)
            cleanup_reason = (
                "gap_closed"
                if arb_gap <= cfg.ARB_ALERT_THRESHOLD_BPS
                else "linger_elapsed"
            )
            log_event(
                "info",
                "sniper_cleanup",
                f"Sniper edge window ended ({cleanup_reason}) — cancelling "
                f"{len(snipe_ids_to_cancel)} sniper offers",
            )
            try:
                # skip_confirmation=True: sniper cleanup is routine maintenance;
                # blocking 60-90s for coins freezes the cycle unnecessarily.
                result = self.offer_manager.cancel_offers(
                    snipe_ids_to_cancel, reason="sniper_cleanup", skip_confirmation=True
                )
                cancelled_ids = [
                    tid
                    for tid, res in (result or {}).items()
                    if res and res.get("success")
                ]
                failed_ids = [
                    tid
                    for tid, res in (result or {}).items()
                    if not (res and res.get("success"))
                ]
                cancelled = len(cancelled_ids)
                log_event(
                    "info",
                    "sniper_cleaned",
                    f"Cancelled {cancelled}/{len(snipe_ids_to_cancel)} sniper offers"
                    + (" — coins freed for main offer batch" if cancelled else "")
                    + (
                        f"; {len(failed_ids)} still active and queued for retry"
                        if failed_ids
                        else ""
                    ),
                )
                if cancelled > 0:
                    for tid in cancelled_ids:
                        if self._probe_state.get("buy_tid") == tid:
                            self._clear_probe_side("buy", tid)
                        if self._probe_state.get("sell_tid") == tid:
                            self._clear_probe_side("sell", tid)
                    self.coin_manager.snapshot_coins("sniper_cleanup")
                    self._emit_coin_update("sniper_cleanup")
                # Keep failed sniper IDs tracked until retry or wallet sync removes them.
                self.sniper._active_snipe_ids = failed_ids
            except Exception as e:
                log_event(
                    "warning",
                    "sniper_cleanup_failed",
                    f"Failed to cancel sniper offers: {e}",
                )

        # ---- Step 8d: Close the Gap — adaptive spread probing ----
        if (
            not recovery_active_now
            and not sniper_fired
            and self.sniper._active_snipe_ids
            and not self._probe_state.get("active", False)
            and self._loop_count >= 3
        ):
            linger_remaining = self._probe_cleanup_seconds_remaining(
                self._probe_state, time.time()
            )
            if linger_remaining > 0:
                log_event(
                    "debug",
                    "sniper_cleanup_wait",
                    f"Sniper linger active â€” keeping probes live for "
                    f"{linger_remaining:.1f}s more before cleanup",
                )

        if self.boost_manager._boost_active and not recovery_active_now:
            # 1. Keep offers alive and centred on price
            refreshed = self.boost_manager.refresh_if_needed(mid_price)

            # 2. Try to probe tighter (10% per stable period)
            # CRITICAL: Don't step (cancel + create) while coin topup is running.
            # The topup thread does send-to-self which needs stable coin state.
            # Gap closer steps change the UTXO set (cancel releases coins,
            # create locks coins) which can invalidate the topup's transaction.
            if self.coin_manager.is_busy():
                stepped = False
                log_event(
                    "debug",
                    "gap_closer_skip_busy",
                    "Gap closer step skipped — coin topup in progress",
                )
            else:
                stepped = self.boost_manager.step_tighter(arb_gap)

            # 2b. If probe was arbed, check if inner-tier offers are exposed
            if self.boost_manager.consume_inner_vulnerability_flag():
                log_event(
                    "warning",
                    "inner_vulnerability_check",
                    "Gap closer probe arbed — checking inner-tier offers for exposure",
                )
                print(
                    "   [8d] ⚠️ Probe arbed — triggering EMERGENCY inner check",
                    flush=True,
                )
                # Force an emergency requote of inner tiers on the next step 9
                self._force_requote["buy"] = True
                self._force_requote["sell"] = True

            # 3. Let main book follow proven safe levels
            converged = self.boost_manager.update_convergence()

            state = self.boost_manager.get_state()

            if refreshed:
                self._emit("boost", state)
                print(f"   [8d] Gap closer refreshed at {mid_price:.8f}", flush=True)

            if stepped:
                # Gap-closer tightened — let CASCADE handle main book tightening.
                # DO NOT force_requote here: it requotes ALL 100+ offers in one
                # loop (15-20 min), blocking further steps.  The cascade replaces
                # stale offers in small batches across multiple fast loops instead.
                self._emit("boost", state)
                print(
                    f"   [8d] Gap closer step {state['steps_taken']}: "
                    f"now {_bps_to_pct(state['current_spread_bps'])} "
                    f"(floor: {_bps_to_pct(state['arb_floor_bps'])}) — "
                    f"cascade will tighten main book",
                    flush=True,
                )

            if converged:
                # Convergence changed — cascade will handle replacing stale offers
                # in small batches.  Same reasoning: full requote blocks the loop.
                factor = self.boost_manager.get_convergence_factor()
                self._emit("boost", state)
                print(
                    f"   [8d] Main book converging — now {factor * 100:.0f}% of original — "
                    f"cascade will tighten main book",
                    flush=True,
                )

            # 4. Cascade: after probe survives ~60s, replace stale main
            #    book offers with tighter ones behind the proven level.
            #    CRITICAL: Creates new offers FIRST, then cancels stale.
            #    Never wipes the orderbook. Works in batches per cycle.
            cascaded = False
            if not stepped and not converged and self.boost_manager.should_cascade():
                if not self.coin_manager.is_busy() and not getattr(
                    self, "_graceful_in_progress", False
                ):
                    cascade_result = self.boost_manager.cascade_main_book(
                        mid_price, open_buys, open_sells
                    )
                    if cascade_result.get("success"):
                        cascaded = True
                        self.coin_manager.snapshot_coins("cascade_replace")
                        self._emit_coin_update("cascade_replace")
                        state = self.boost_manager.get_state()
                        self._emit("boost", state)
                        tc = cascade_result.get("total_created", 0)
                        tk = cascade_result.get("total_cancelled", 0)
                        print(
                            f"   [8d] CASCADE: +{tc} new, -{tk} stale "
                            f"(probe at {_bps_to_pct(state['current_spread_bps'])})",
                            flush=True,
                        )

            if not refreshed and not stepped and not converged and not cascaded:
                print(
                    f"   [8d] Gap closer: {_bps_to_pct(state['current_spread_bps'])} "
                    f"({state['steps_taken']} steps, "
                    f"floor: {_bps_to_pct(state['arb_floor_bps'])}, "
                    f"next step in {state['secs_until_step']}s)",
                    flush=True,
                )

        # ---- Step 9: Requote if price moved or forced by convergence ----
        self._set_cycle_step("step9_requote")
        force_buy = self._force_requote.get("buy", False)
        force_sell = self._force_requote.get("sell", False)
        force_tag = " FORCED!" if (force_buy or force_sell) else ""
        print(f"   [9] Requote check...{force_tag}", end="", flush=True)
        # step9 detail log removed — the actual requoting info log fires when needed
        self._handle_requoting(mid_price, current_buy_ids, current_sell_ids)
        print(" done", flush=True)
        # step9_done removed

        # ---- Step 9b: Reserve floor guard ----
        # Total confirmed wallet balance (including locked coins) must never
        # fall below XCH_RESERVE / CAT_RESERVE. If we're at or near the floor:
        #   - BREACHED (≤ floor): cancel all open offers to free locked coins, skip create
        #   - NEAR (≤ floor × 1.05): skip creating new offers, emit warning
        # The reserve coin itself CAN be used for coin prep splits — it's the
        # total balance that's protected, not any individual coin.
        _reserve_skip_create = False
        try:
            from wallet import get_wallet_balance as _get_wallet_balance

            _xch_reserve = getattr(cfg, "XCH_RESERVE", Decimal("0"))
            _cat_reserve = getattr(cfg, "CAT_RESERVE", Decimal("0"))

            if _xch_reserve > Decimal("0"):
                _xch_bal_raw = _get_wallet_balance(cfg.WALLET_ID_XCH)
                # Guard: a failed balance read must NOT be coerced to 0 — that
                # path used to mass-cancel every offer when Sage briefly 401'd.
                try:
                    _xch_bal = _extract_wallet_balance_or_defer(_xch_bal_raw)
                except _ReserveCheckDeferred as _defer:
                    log_event(
                        "warning",
                        "reserve_check_skipped",
                        f"XCH reserve check skipped: {_defer} (will retry next cycle)",
                    )
                    raise
                _xch_total = Decimal(
                    str(_xch_bal.get("confirmed_wallet_balance", 0))
                ) / Decimal("1000000000000")
                _xch_near_floor = _xch_reserve * Decimal("1.05")

                if _xch_total <= _xch_reserve:
                    log_event(
                        "error",
                        "reserve_floor_breached",
                        f"XCH balance ({_xch_total:.4f}) ≤ XCH_RESERVE ({_xch_reserve}) — "
                        f"cancelling all open offers to protect reserve",
                    )
                    # Distinct XCH alert id so it doesn't collide with the
                    # CAT path's alert and overwrite operator-facing
                    # context mid-triage when both reserves are stressed.
                    self._emit_alert(
                        "reserve_floor_xch",
                        "error",
                        "Reserve Floor Breached (XCH)",
                        f"XCH balance ({_xch_total:.4f} XCH) has reached the reserve floor "
                        f"({_xch_reserve} XCH). Cancelling open offers to free locked coins.",
                        action="stop_bot",
                        action_label="Stop Bot",
                    )
                    _all_open = list(current_buy_ids | current_sell_ids)
                    if _all_open:
                        self.offer_manager.cancel_offers(
                            _all_open, reason="reserve_floor_breached", force_storm=True
                        )
                        current_buy_ids.clear()
                        current_sell_ids.clear()
                    _reserve_skip_create = True
                elif _xch_total <= _xch_near_floor:
                    log_event(
                        "warning",
                        "reserve_floor_near",
                        f"XCH balance ({_xch_total:.4f}) approaching reserve floor "
                        f"({_xch_reserve}) — skipping offer creation",
                    )
                    self._emit_alert(
                        "reserve_floor_xch",
                        "warning",
                        "Reserve Floor Approaching (XCH)",
                        f"XCH balance ({_xch_total:.4f} XCH) is within 5% of reserve floor "
                        f"({_xch_reserve} XCH). Offer creation paused.",
                    )
                    _reserve_skip_create = True
                else:
                    self._clear_alert("reserve_floor_xch")
                    # Also clear the legacy alert id (pre-attribution fix)
                    # so an upgrade-in-place does not leave a stale banner
                    # glued to the screen.
                    self._clear_alert("reserve_floor")

            if not _reserve_skip_create and _cat_reserve > Decimal("0"):
                _cat_bal_raw = _get_wallet_balance(cfg.CAT_WALLET_ID)
                # Same guard as XCH: failed reads must not coerce to 0.
                try:
                    _cat_bal = _extract_wallet_balance_or_defer(_cat_bal_raw)
                except _ReserveCheckDeferred as _defer:
                    log_event(
                        "warning",
                        "reserve_check_skipped",
                        f"CAT reserve check skipped: {_defer} (will retry next cycle)",
                    )
                    raise
                _cat_scale = Decimal(10) ** Decimal(str(cfg.CAT_DECIMALS))
                _cat_total = (
                    Decimal(str(_cat_bal.get("confirmed_wallet_balance", 0)))
                    / _cat_scale
                )
                _cat_near_floor = _cat_reserve * Decimal("1.05")

                if _cat_total <= _cat_reserve:
                    log_event(
                        "error",
                        "reserve_floor_breached",
                        f"CAT balance ({_cat_total:,.2f}) ≤ CAT_RESERVE ({_cat_reserve}) — "
                        f"cancelling all open offers to protect reserve",
                    )
                    self._emit_alert(
                        "reserve_floor_cat",
                        "error",
                        "Reserve Floor Breached (CAT)",
                        f"CAT balance ({_cat_total:,.2f}) has reached the reserve floor "
                        f"({_cat_reserve}). Cancelling open offers to free locked coins.",
                        action="stop_bot",
                        action_label="Stop Bot",
                    )
                    _all_open = list(current_buy_ids | current_sell_ids)
                    if _all_open:
                        self.offer_manager.cancel_offers(
                            _all_open, reason="reserve_floor_breached", force_storm=True
                        )
                        current_buy_ids.clear()
                        current_sell_ids.clear()
                    _reserve_skip_create = True
                elif _cat_total <= _cat_near_floor:
                    log_event(
                        "warning",
                        "reserve_floor_near",
                        f"CAT balance ({_cat_total:,.2f}) approaching reserve floor "
                        f"({_cat_reserve}) — skipping offer creation",
                    )
                    self._emit_alert(
                        "reserve_floor_cat",
                        "warning",
                        "Reserve Floor Approaching (CAT)",
                        f"CAT balance ({_cat_total:,.2f}) is within 5% of reserve floor "
                        f"({_cat_reserve}). Offer creation paused.",
                    )
                    _reserve_skip_create = True
                else:
                    self._clear_alert("reserve_floor_cat")
        except _ReserveCheckDeferred:
            # Balance read failed — already logged. Skip the rest of the
            # reserve guard for this cycle and retry next tick. Do NOT
            # set _reserve_skip_create; offer creation can still proceed
            # since the breach is unverified.
            pass
        except Exception as _rf_err:
            log_event(
                "debug",
                "reserve_floor_check_error",
                f"Reserve floor check failed (non-fatal): {_rf_err}",
            )

        # ---- Step 10: Create new offers if needed ----
        self._set_cycle_step("step10_create_offers")
        # Cap is based on DB-open count (_db_open_*_ids) so zombie wallet offers
        # (cancelled in DB, still active in Sage) don't falsely fill the cap.
        # Both branches of the try/except above guarantee these are defined.
        print(
            f"   [10] Offers: buys {len(_db_open_buy_ids)}/{cfg.MAX_ACTIVE_BUY_OFFERS}, "
            f"sells {len(_db_open_sell_ids)}/{cfg.MAX_ACTIVE_SELL_OFFERS}",
            flush=True,
        )
        # step10 count log removed — console print covers this
        if _reserve_skip_create:
            log_event(
                "info",
                "step10_skipped_reserve",
                "Skipping offer creation — reserve floor guard active",
            )
            return

        # Sweep protection: skip re-posting on sides swept in the last cycle.
        # The dynamic AMM buffer has already widened; once it expires the bot
        # will requote at the wider buffer.  Log once per side, clear expired.
        _now = time.time()
        _skip_buy = _now < self._sweep_protection.get("buy", 0)
        _skip_sell = _now < self._sweep_protection.get("sell", 0)
        for _s in ("buy", "sell"):
            if _now >= self._sweep_protection.get(_s, 0):
                self._sweep_protection.pop(_s, None)
        if _skip_buy or _skip_sell:
            _skipped = [
                s for s, skip in (("buy", _skip_buy), ("sell", _skip_sell)) if skip
            ]
            log_event(
                "info",
                "sweep_protection_active",
                f"Sweep protection: skipping {_skipped} offer creation this cycle",
                data={"protected_sides": _skipped},
            )

        self._create_offers_if_needed(
            mid_price,
            len(_db_open_buy_ids),  # DB-open count (excludes zombie wallet offers)
            len(_db_open_sell_ids),  # DB-open count (excludes zombie wallet offers)
            current_buy_ids=_db_open_buy_ids,
            current_sell_ids=_db_open_sell_ids,
            arb_gap=arb_gap,
            skip_buy=_skip_buy,
            skip_sell=_skip_sell,
        )

        self._flush_public_offer_queues()

        # ---- Step 12: Coin management ----
        print("   [12] Coin health...", end="", flush=True)
        self._handle_coins(len(current_buy_ids), len(current_sell_ids))
        print(" done", flush=True)
        # step12 log removed

        # ---- Step 12a: Trim excess offers (Fix 3) ----
        # Belt-and-braces: if anything in steps 9-11 left the live book
        # over the per-side cap (e.g. a slow-confirming cancel meant a
        # create-first requote left both old and new offers alive), trim
        # the furthest-from-mid offers back down to cap. With Fix 1 in
        # place this should rarely fire — but when it does, it stops the
        # overshoot from accumulating across cycles.
        try:
            # Filter out zombie wallet offers (cancelled in DB but still active in
            # Sage) before passing to trim so the trim doesn't count those against
            # the cap and cancel freshly-created real offers.
            _db_filtered_buys = [
                o for o in open_buys if o.get("trade_id") in _db_open_buy_ids
            ]
            _db_filtered_sells = [
                o for o in open_sells if o.get("trade_id") in _db_open_sell_ids
            ]
            trimmed = self.offer_manager.trim_excess_offers(
                mid_price,
                wallet_buys=_db_filtered_buys,
                wallet_sells=_db_filtered_sells,
            )
            if trimmed > 0:
                log_event(
                    "info",
                    "trim_excess_done",
                    f"Trim pass cancelled {trimmed} excess offer(s)",
                )
                # F14 fix (2026-04-08): track trim activity. Repeated trim
                # firing means create-first requote is leaving offers
                # behind faster than they can be cancelled — likely a
                # wallet sync issue or aggressive requote schedule. Alert
                # if we trim >5 cycles in a row.
                self._trim_streak = getattr(self, "_trim_streak", 0) + 1
                if self._trim_streak >= 5:
                    log_event(
                        "warning",
                        "trim_excess_sustained",
                        f"Trim pass has fired for {self._trim_streak} "
                        f"consecutive cycles — create-first requote may "
                        f"be leaking offers. Investigate cancel latency "
                        f"or pause requotes.",
                    )
                    self._emit_alert(
                        "trim_sustained",
                        "warning",
                        "Offer Cleanup Lag",
                        f"The create-first requote dance has been over-creating "
                        f"offers for {self._trim_streak} cycles in a row. The "
                        f"trim pass is cleaning up but cancel latency seems high.",
                    )
            else:
                # Reset streak when a clean cycle happens
                if getattr(self, "_trim_streak", 0) > 0:
                    self._trim_streak = 0
                    self._clear_alert("trim_sustained")
        except Exception as e:
            log_event(
                "warning",
                "trim_excess_error",
                f"Trim excess pass failed (non-fatal): {e}",
            )

        # ---- Step 12b: Recovery mode evaluation ----
        # Subtract any confirmed probe slots so the probe offer doesn't inflate
        # the apparent buy count and mask a genuine under-target condition.
        _probe_offsets = self._confirmed_probe_slot_offsets(
            current_buy_ids, current_sell_ids
        )
        self._evaluate_recovery_mode(
            mid_price,
            max(0, len(current_buy_ids) - _probe_offsets["buy"]),
            max(0, len(current_sell_ids) - _probe_offsets["sell"]),
        )

        # ---- Step 13: Housekeeping ----
        print("   [13-15] Housekeeping + inventory + GUI push...", end="", flush=True)
        self._handle_housekeeping()

        # ---- Step 14: Record inventory snapshot ----
        self.risk_manager.record_snapshot(mid_price=mid_price)

        # ---- Step 15: Push full state to GUI ----
        self._emit(
            "state",
            {
                "loop_count": self._loop_count,
                "mid_price": str(mid_price),
                "open_buys": len(current_buy_ids),
                "open_sells": len(current_sell_ids),
                "status": self._bot_state.get("status", "running"),
                "spread_bps": self._bot_state.get("spread_bps", "0"),
            },
        )

        # Push dashboard command centre update (market health + performance)
        try:
            offer_edges = self._get_live_offer_edges(open_buys, open_sells)
            self._record_live_offer_edges(offer_edges)
            health_data = self._augment_health_with_spacescan(
                self.risk_manager.get_market_health(loop_count=self._loop_count)
            )
            # Include competitor spread + fill rate for Smart Advisor
            _competitor_bps = 0
            _fills_hr = 0
            _best_bid = "0"
            _best_ask = "0"
            try:
                if self.market_intel:
                    intel = self.market_intel.get_cached_data()
                    if intel:
                        _competitor_bps = intel.get("competitor_spread_bps", 0)
                        _best_bid = str(intel.get("best_bid", "0"))
                        _best_ask = str(intel.get("best_ask", "0"))
            except Exception:
                pass
            # Reuse fills_hour from earlier in this cycle instead of re-querying
            _fills_hr = fills_hour if fills_hour is not None else 0

            # Include net_position so the advisor doesn't rely on stale 120s dashboard poll
            _net_pos = "0"
            try:
                _net_pos = str(self.risk_manager._net_position_cat)
            except Exception:
                pass

            self._emit(
                "dashboard_update",
                {
                    "market_health": health_data,
                    "loop_count": self._loop_count,
                    "uptime_secs": int(time.time() - self._start_time)
                    if self._start_time
                    else 0,
                    "last_loop_time": round(self._last_loop_duration, 2),
                    "open_buys": len(current_buy_ids),
                    "open_sells": len(current_sell_ids),
                    "mid_price": str(mid_price),
                    "competitor_spread_bps": _competitor_bps,
                    "fills_per_hour": _fills_hr,
                    "net_position": _net_pos,
                    "our_best_bid": offer_edges.get("our_best_bid", "0"),
                    "our_best_ask": offer_edges.get("our_best_ask", "0"),
                    "best_bid": _best_bid,
                    "best_ask": _best_ask,
                },
            )
        except Exception as e:
            print(f"   [15] Dashboard emit error: {e}", flush=True)

        print(" done [OK]", flush=True)
        # step15 log removed

        # Console cycle summary — one clean line showing the cycle result
        fill_count = len(buy_fills) + len(sell_fills)
        fill_str = f", {fill_count} fills!" if fill_count > 0 else ""
        expired_str = f", {expired} expired" if expired > 0 else ""
        log_event(
            "success",
            "cycle_complete",
            f"Cycle #{self._loop_count} complete — "
            f"{len(current_buy_ids)}b/{len(current_sell_ids)}s active"
            f"{fill_str}{expired_str}",
        )
        # F27: clear step name on cycle exit so the SLA watchdog doesn't
        # alert on the inter-cycle sleep period
        self._set_cycle_step("idle")

    # -------------------------------------------------------------------
    # Requoting
    # -------------------------------------------------------------------

    def _graceful_migration_phase(self) -> str:
        migration = getattr(self, "_graceful_migration", {}) or {}
        return str(migration.get("phase") or "idle").lower()

    def _graceful_creation_blocked(self) -> bool:
        """Block fresh offer creation during ALL active graceful migration phases.

        Previously this only blocked during retrying/verifying (not cancelling),
        which caused coin exhaustion — new offers consumed spare coins while
        cancelled offers' coins hadn't been freed yet.  Now blocks during
        cancelling, retrying, and verifying alike.
        """
        if not getattr(self, "_graceful_in_progress", False):
            return False
        phase = self._graceful_migration_phase()
        # Only allow creation when migration is idle or done
        return phase not in ("idle", "done")

    def _maybe_finalize_graceful_migration(
        self, current_buy_ids: set = None, current_sell_ids: set = None
    ):
        """Release graceful migration only after cancelled IDs are truly gone."""
        if not getattr(self, "_graceful_in_progress", False):
            return

        migration = getattr(self, "_graceful_migration", {}) or {}
        if str(migration.get("phase") or "").lower() not in {"retrying", "verifying"}:
            return

        cancel_ids = set(migration.get("cancel_ids") or [])
        if not cancel_ids:
            migration["phase"] = "done"
            migration["active"] = False
            self._graceful_in_progress = False
            return

        if current_buy_ids is None or current_sell_ids is None:
            try:
                open_buys, open_sells, _ = self.offer_manager.sync_from_wallet()
                current_buy_ids = {
                    o.get("trade_id") for o in open_buys if o.get("trade_id")
                }
                current_sell_ids = {
                    o.get("trade_id") for o in open_sells if o.get("trade_id")
                }
            except Exception as e:
                log_event(
                    "debug",
                    "config_migration_retry_sync_fail",
                    f"Could not verify graceful migration retry state: {e}",
                )
                return

        visible_ids = set(current_buy_ids or set()) | set(current_sell_ids or set())
        remaining_ids = sorted(tid for tid in cancel_ids if tid in visible_ids)
        migration["remaining_cancel_ids"] = remaining_ids
        migration["remaining_cancel_count"] = len(remaining_ids)

        if remaining_ids:
            if not self.offer_manager._pending_cancel_retries and not migration.get(
                "stalled_logged", False
            ):
                log_event(
                    "warning",
                    "config_migration_stalled",
                    f"Graceful config migration still has {len(remaining_ids)} old "
                    f"offer(s) live after cancel retries exhausted; holding book "
                    f"steady until they are cleared",
                )
                migration["stalled_logged"] = True
            return

        migration["phase"] = "done"
        migration["active"] = False
        migration["cancel_done"] = migration.get("cancel_total", 0)
        migration["cancel_failed"] = 0
        migration["remaining_cancel_ids"] = []
        migration["remaining_cancel_count"] = 0
        self._graceful_in_progress = False
        log_event(
            "info",
            "config_migration_done",
            "Graceful config migration fully completed after cancel verification",
        )

    def _handle_requoting(
        self, mid_price: Decimal, current_buy_ids: set, current_sell_ids: set
    ):
        """Check if offers need requoting due to price movement or forced convergence."""
        if not cfg.AUTO_REQUOTE:
            return

        if self._recovery_is_active():
            log_event(
                "debug",
                "requote_skip_recovery",
                "Recovery mode active — skipping non-essential requotes",
            )
            return

        # Don't requote while graceful config migration is running — the migration
        # thread is already cancelling offers and competing for the wallet causes
        # cancel failures and 90-second timeouts on both sides.
        if getattr(self, "_graceful_in_progress", False):
            log_event(
                "debug",
                "requote_skip",
                "Graceful config migration in progress — skipping requote",
            )
            return

        # Don't requote while coin operations are running (prep/topup)
        if self.coin_manager.is_busy():
            log_event("debug", "requote_skip", "Coin manager busy — skipping requote")
            return

        # ---- Fresh price for forced requotes ----
        # When AMM drift forces a requote, the mid_price from Step 1 (cycle
        # start) may already be stale — the AMM monitor may have detected a
        # move AFTER get_price() ran but BEFORE we reach this point. The old
        # mid_price would make us requote at the SAME price, wasting coins.
        # Re-fetch now so the new offers are at the correct price.
        force_buy = self._force_requote.get("buy", False)
        force_sell = self._force_requote.get("sell", False)
        if force_buy or force_sell:
            try:
                fresh_price = self.price_engine.get_price(
                    cfg.CAT_ASSET_ID, cfg.CAT_DECIMALS, cfg.CAT_TICKER_ID
                )
                if fresh_price:
                    fresh_mid = Decimal(str(fresh_price.get("mid_price", 0)))
                    if fresh_mid > 0 and fresh_mid != mid_price:
                        old_mid = mid_price
                        mid_price = fresh_mid
                        self._current_mid_price = mid_price
                        self._set_state(mid_price=str(mid_price))
                        log_event(
                            "info",
                            "requote_price_refresh",
                            f"Refreshed mid_price before forced requote: "
                            f"{old_mid:.8f} -> {mid_price:.8f}",
                            data={"old_mid": str(old_mid), "new_mid": str(mid_price)},
                        )
                        # Push updated price to GUI
                        self._emit(
                            "price_update",
                            {
                                "mid_price": str(mid_price),
                                "dexie_price": str(fresh_price.get("dexie_price", "")),
                                "tibet_price": str(fresh_price.get("tibet_price", "")),
                                "arb_gap_bps": str(fresh_price.get("arb_gap_bps", "0")),
                                "spread_bps": self._bot_state.get("spread_bps", "0"),
                            },
                        )
            except Exception as _e:
                log_event(
                    "warning",
                    "requote_price_refresh_failed",
                    f"Could not refresh price before requote: {_e}",
                )

        # Track requote time budget — don't let requotes block the loop forever
        _requote_start_time = time.time()
        _REQUOTE_TIME_BUDGET_SECS = float(
            getattr(cfg, "REQUOTE_TIME_BUDGET_SECS", 30) or 30
        )

        # Sniper probe active: only defer requoting on SIDES that currently
        # have a live probe offer. The other side can still requote — it's
        # wrong to freeze the whole book for minutes while a probe widens
        # or retries.
        probe_active = self._probe_state.get("active", False)
        probe_sides_blocked = set()
        if probe_active:
            try:
                if self._probe_state.get("buy_tid"):
                    probe_sides_blocked.add("buy")
                if self._probe_state.get("sell_tid"):
                    probe_sides_blocked.add("sell")
            except Exception:
                # Defensive fallback: if probe state is malformed, skip both
                probe_sides_blocked = {"buy", "sell"}

        # Stale wallet guard — same rule as _create_offers_if_needed below.
        # If the wallet's offer view has been stale for several cycles, the
        # requote path could double-post or recreate offers we already have.
        # Block new requotes (existing offers stay open) until the view is
        # fresh again.
        _stale_streak = int(self._recovery_state.get("wallet_stale_streak", 0))
        _stale_limit = getattr(cfg, "WALLET_STALE_CREATE_LIMIT", 3)
        if self._wallet_sync_stale_cycle and _stale_streak >= _stale_limit:
            log_event(
                "warning",
                "stale_wallet_requote_blocked",
                f"Wallet sync stale for {_stale_streak} consecutive cycles — "
                f"blocking requote until data is fresh",
            )
            return

        for side in ["buy", "sell"]:
            if not self._config_enables_side(side):
                self._force_requote[side] = False
                continue

            # Time budget check — don't let requotes block the loop forever.
            # If buy-side requote took 20s, defer sell to next cycle.
            _requote_elapsed = time.time() - _requote_start_time
            if _requote_elapsed > _REQUOTE_TIME_BUDGET_SECS:
                log_event(
                    "info",
                    "requote_time_budget",
                    f"Requote time budget reached ({_requote_elapsed:.1f}s > "
                    f"{_REQUOTE_TIME_BUDGET_SECS}s) - deferring {side} to "
                    f"next cycle while wallet/Dexie settle",
                )
                # Keep the force flag so it fires next cycle
                break

            if side in probe_sides_blocked:
                continue  # Probe active — skip silently (logged once at probe start)

            # Circuit breaker side-enable check — without this, a position
            # CB on (e.g.) the buy side would still let _handle_requoting
            # rebuild buy offers at the new price on Tibet drift, defeating
            # the CB. _create_offers_if_needed already checks via
            # should_enable_side; this path was the second road to the
            # same action and was missing the gate.
            try:
                if not self.risk_manager.should_enable_side(side, mid_price):
                    continue  # CB-blocked — skip silently (CB state shown in GUI)
            except Exception as _e:
                # Fail-safe: if the check itself errors, log and continue
                # (don't lock the bot out of requoting due to a CB read bug)
                log_event(
                    "warning",
                    "requote_cb_check_failed",
                    f"should_enable_side({side}) raised {_e} — proceeding",
                )

            last_price = self._last_quoted_price.get(side, Decimal("0"))
            forced = self._force_requote.get(side, False)

            if last_price <= 0 and not forced:
                continue

            # ---- Smart startup: grace period for first 5 loops ----
            # Newly created offers need time to settle and post to Dexie.
            # Suppress ALL requotes (including AMM drift forces) during
            # startup — the ladder was JUST built at current prices.  Any
            # minor drift between ladder creation and first cycle is noise,
            # not a genuine market move.  Emergency requotes (Step 8b) are
            # a separate code path and are NOT affected by this gate.
            if self._loop_count <= 5:
                if forced:
                    self._force_requote[side] = False  # Clear stale flag
                continue  # Grace period — skip silently

            # Check fill protection (anti-churn) — but don't block forced convergence requotes
            if not forced and self.fill_tracker.should_protect_side(side):
                continue  # Fill protection — skip silently

            # Check if requote needed — graduated severity check
            from reaction_strategy import (
                RequoteSeverity,
                tiers_for_severity,
            )

            severity = RequoteSeverity.NONE

            # Compare apples-to-apples: the deployed baseline is probe-
            # anchored (set after create/requote), so the current mid must
            # also be anchored before we measure drift.  Without this, a
            # large probe offset looks like a permanent price move and
            # triggers pointless requotes at the exact same prices.
            compare_mid = self._get_probe_anchored_mid(side, mid_price)

            if forced:
                # Forced convergence — re-evaluate against CURRENT drift
                # before acting. A force that was set minutes ago (AMM
                # drift, ladder-anchor drift, CB-disabled side catching
                # up, etc.) may no longer reflect real pressure by the
                # time we run. If the price has returned to the last
                # quoted level, clearing the force quietly is cheaper
                # and safer than forcing at least INNER from a stale
                # boolean — which used to produce wasteful cancel/recreate
                # storms and churn coin-tier balances for no edge.
                if last_price > 0:
                    _move_frac = abs(compare_mid - last_price) / last_price
                    from reaction_strategy import classify_drift

                    severity = classify_drift(
                        _move_frac,
                        inner_threshold=getattr(
                            cfg, "REQUOTE_DRIFT_INNER", Decimal("0.003")
                        ),
                        mid_threshold=getattr(
                            cfg, "REQUOTE_DRIFT_MID", Decimal("0.008")
                        ),
                        full_threshold=getattr(
                            cfg, "REQUOTE_DRIFT_FULL", Decimal("0.02")
                        ),
                        emergency_threshold=getattr(
                            cfg, "REQUOTE_DRIFT_EMERGENCY", Decimal("0.05")
                        ),
                    )
                    if severity == RequoteSeverity.NONE:
                        # Pressure has already resolved (price converged
                        # back to last quoted). Clear the force instead
                        # of running a no-value requote.
                        log_event(
                            "debug",
                            "force_requote_stale_cleared",
                            f"{side} forced requote cleared — current "
                            f"drift resolved back to last quoted price "
                            f"({last_price:.8f} vs {compare_mid:.8f})",
                        )
                        self._force_requote[side] = False
                        continue
                else:
                    severity = RequoteSeverity.FULL
            else:
                severity = self.offer_manager.should_requote_graduated(
                    side, compare_mid, last_price
                )

            should_requote = severity != RequoteSeverity.NONE

            if should_requote:
                _same_price_guard = self._recent_same_price_shock_requote_guard(
                    side,
                    compare_mid,
                )
                if _same_price_guard:
                    self._last_quoted_price[side] = compare_mid
                    self._last_quoted_plain_mid[side] = mid_price
                    self._ladder_grid_mid[side] = compare_mid
                    self._ladder_anchor_plain_mid[side] = mid_price
                    self._force_requote[side] = False
                    log_event(
                        "info",
                        "requote_deferred_recent_shock_requote",
                        f"Requote {side} deferred - recent "
                        f"{_same_price_guard.get('source', 'shock')} already "
                        f"repriced this side at {compare_mid:.8f}; ladder-fill "
                        "will restore any temporarily missing slots",
                        data={
                            "side": side,
                            "age_secs": round(
                                float(_same_price_guard.get("age_secs") or 0), 1
                            ),
                            "drift_bps": str(_same_price_guard.get("drift_bps") or "0"),
                            "allowed_bps": str(
                                _same_price_guard.get("allowed_bps") or "0"
                            ),
                            "source": _same_price_guard.get("source", ""),
                            "severity": severity.value,
                            "forced": bool(forced),
                        },
                    )
                    continue

                _all_pending_counts = self._pending_cancel_settle_counts()
                _side_pending_count = int(_all_pending_counts.get(side, 0) or 0)
                _other_pending_total = (
                    sum(_all_pending_counts.values()) - _side_pending_count
                )
                if _other_pending_total > 0:
                    self._force_requote[side] = True
                    log_event(
                        "info",
                        "requote_deferred_global_pending_cancel_settle",
                        f"Requote {side} deferred - "
                        f"{_other_pending_total} wallet-active pending cancel(s) "
                        "on the other side must settle before starting another "
                        "side replacement wave",
                        data={
                            "side": side,
                            "pending_cancel_counts": _all_pending_counts,
                            "pending_cancel_total": sum(_all_pending_counts.values()),
                            "other_pending_total": _other_pending_total,
                            "severity": severity.value,
                            "forced": bool(forced),
                        },
                    )
                    continue

                _shock_guard = self._recent_tibet_shock_guard(side)
                if _shock_guard:
                    self._force_requote[side] = True
                    _remaining = max(
                        0.0,
                        float(_shock_guard.get("guard_secs", 0) or 0)
                        - float(_shock_guard.get("age_secs", 0) or 0),
                    )
                    _protected = "+".join(_shock_guard.get("protected_sides") or ())
                    log_event(
                        "info",
                        "requote_deferred_tibet_shock_guard",
                        f"Requote {side} deferred - recent Tibet "
                        f"{_shock_guard.get('direction', 'unknown')} shock "
                        f"protected {_protected or 'at-risk'} side; waiting "
                        f"{_remaining:.0f}s before create-first replacement "
                        f"on the non-vulnerable side",
                        data={
                            "side": side,
                            "direction": _shock_guard.get("direction", ""),
                            "pct": _shock_guard.get("pct", 0),
                            "protected_sides": _shock_guard.get("protected_sides", ()),
                            "remaining_secs": round(_remaining, 1),
                            "severity": severity.value,
                            "forced": bool(forced),
                        },
                    )
                    continue

                _pending_settle = self._pending_cancel_wallet_ids(side)
                if _pending_settle:
                    if forced:
                        self._force_requote[side] = True
                    log_event(
                        "info",
                        "requote_deferred_pending_cancel_settle",
                        f"Requote {side} deferred - {len(_pending_settle)} "
                        f"wallet-active pending cancel(s) must settle before "
                        f"replacement offers are created",
                        data={
                            "side": side,
                            "pending_count": len(_pending_settle),
                            "severity": severity.value,
                            "forced": bool(forced),
                        },
                    )
                    continue

                reason = (
                    f"convergence tightening [{severity.value}]"
                    if forced
                    else f"price moved {last_price:.8f} -> {compare_mid:.8f} [{severity.value}]"
                )
                print(f"\n   [REQUOTE] {side} side ({reason})", flush=True)
                log_event(
                    "info",
                    "requoting",
                    f"Requoting {side} side ({reason})",
                    data={"severity": severity.value},
                )

                # Keep the force flag LIT across spread/requote work so that
                # an exception, wallet RPC abort, or "no spares" short-circuit
                # below cannot silently drop a real requote obligation. The
                # flag is cleared only after requote_side() reports progress
                # (new offers placed or an intentional "nothing to do" state
                # like tier-filter drained). On failure paths we leave the
                # force set so the next cycle will retry the requote rather
                # than stranding stale offers at the old mid.
                force_clear_on_success = forced

                spread = self.risk_manager.get_adjusted_spread(side)
                # compare_mid is already probe-anchored — reuse it
                requote_mid = compare_mid
                price_cap = (
                    self._get_probe_price_boundary(side) if side == "buy" else None
                )
                price_floor = (
                    self._get_probe_price_boundary(side) if side == "sell" else None
                )
                if requote_mid != mid_price:
                    log_event(
                        "info",
                        "probe_anchor_requote",
                        f"Anchoring {side} requote to probe edge: mid {mid_price:.8f} "
                        f"-> {requote_mid:.8f}",
                    )

                # ── Incremental reaction: tier filter + budget cap ──
                _allowed_tiers = tiers_for_severity(severity)
                _max_offers = getattr(cfg, "CYCLE_MAX_CANCELS", 6)
                # For EMERGENCY, no budget cap — cancel everything arbable
                if severity == RequoteSeverity.EMERGENCY:
                    _max_offers = 0  # 0 = no limit

                # Post-mortem 2026-04-22: if the mempool watcher just fired
                # a defensive cancel on a tier, the requote shouldn't try to
                # cancel/recreate that same tier on its next pass — the
                # cancels are in flight and the coins are returning. Skip
                # any tier currently in-flight. For FULL/EMERGENCY severity
                # the allowed_tiers is None (no filter applied), so this
                # only tightens the INNER / INNER_MID paths.
                if severity not in (RequoteSeverity.FULL, RequoteSeverity.EMERGENCY):
                    _inflight = self._defensive_cancel_active_tiers(side=side)
                    if _inflight and _allowed_tiers:
                        _overlap = _allowed_tiers & _inflight
                        if _overlap:
                            _allowed_tiers = _allowed_tiers - _inflight
                            log_event(
                                "info",
                                "requote_skip_inflight_tiers",
                                f"Skipping {sorted(_overlap)} tier(s) for "
                                f"{side} — defensive cancel in flight "
                                f"(ladder-fill will restore them)",
                            )
                    # If the overlap drained the tier set, there's nothing
                    # for this requote to do. Defer cleanly instead of
                    # falling into the requote path with an empty filter.
                    if not _allowed_tiers:
                        log_event(
                            "info",
                            "requote_deferred_inflight",
                            f"All {severity.value} tiers for {side} "
                            f"are already being handled by a defensive "
                            f"cancel — no requote needed this cycle",
                        )
                        # Advance baselines so the drift check doesn't
                        # re-fire the same decision next cycle.
                        self._last_quoted_price[side] = requote_mid
                        self._last_quoted_plain_mid[side] = mid_price
                        continue

                _live_ids_req = current_buy_ids if side == "buy" else current_sell_ids
                requote_result = self.offer_manager.requote_side(
                    side,
                    requote_mid,
                    dexie_manager=self.dexie_manager,
                    risk_manager=self.risk_manager,
                    spread_fraction=spread,
                    price_cap=price_cap,
                    price_floor=price_floor,
                    live_offer_ids=_live_ids_req,
                    max_offers=_max_offers,
                    allowed_tiers=_allowed_tiers
                    if severity not in (RequoteSeverity.FULL, RequoteSeverity.EMERGENCY)
                    else None,
                    force_cancel_storm=severity
                    in (
                        RequoteSeverity.FULL,
                        RequoteSeverity.EMERGENCY,
                    ),
                )
                # Track whether the requote actually made progress so we can
                # decide whether to advance baselines and clear the force
                # flag. A no-op outcome (0 offers replaced, no tier-filter
                # drain) leaves the book exposed at the old mid, so we
                # must NOT advance baselines — doing so would hide the
                # stale offers from the next cycle's drift detection.
                replaced_count = 0
                target_count_trunc = 0
                original_target = 0
                tier_filter_drained = False
                made_progress = False
                if isinstance(requote_result, dict):
                    new_offers = requote_result.get("offers", [])
                    replaced_count = int(requote_result.get("replaced_count", 0) or 0)
                    target_count_trunc = int(requote_result.get("target_count", 0) or 0)
                    original_target = int(
                        requote_result.get("original_target_count", target_count_trunc)
                        or 0
                    )
                    tier_filter_drained = bool(
                        requote_result.get("tier_filter_drained", False)
                    )
                    made_progress = replaced_count > 0 or len(new_offers) > 0

                    if requote_result.get("fully_replaced"):
                        # True full replace: advance baselines + refresh
                        # ladder anchor so Step 10 doesn't see drift from
                        # stale anchors.
                        self._last_quoted_price[side] = requote_mid
                        self._last_quoted_plain_mid[side] = mid_price  # F67
                        self._ladder_grid_mid[side] = requote_mid
                        self._ladder_anchor_plain_mid[side] = mid_price
                    elif made_progress:
                        # Partial progress — still advance the AMM drift
                        # baseline so we don't loop re-detecting the same
                        # drift every cycle, and refresh the ladder anchor
                        # because at least some offers moved to the new
                        # mid. The remaining offers will be picked up by
                        # the trim/ladder-fill paths.
                        log_event(
                            "info",
                            "requote_incomplete",
                            f"{side} requote partial: replaced "
                            f"{replaced_count}/{target_count_trunc} offers "
                            f"(original target {original_target}); advancing baseline",
                        )
                        self._last_quoted_price[side] = requote_mid
                        self._last_quoted_plain_mid[side] = mid_price  # F67
                        self._ladder_grid_mid[side] = requote_mid
                        self._ladder_anchor_plain_mid[side] = mid_price
                    elif tier_filter_drained:
                        log_event(
                            "info",
                            "requote_scope_empty",
                            f"{side} requote had no matching tier slots left "
                            f"to replace (target {target_count_trunc}, "
                            f"original {original_target}); the active "
                            f"cancel/rebuild path is already handling it.",
                            data={
                                "side": side,
                                "replaced": replaced_count,
                                "target": target_count_trunc,
                                "original_target": original_target,
                                "tier_filter_drained": tier_filter_drained,
                            },
                        )
                    else:
                        # Zero progress. Do NOT advance baselines — that
                        # used to hide stale exposed offers from drift
                        # detection for a full cycle. Leave the force
                        # flag set so the next cycle retries.
                        log_event(
                            "warning",
                            "requote_no_progress",
                            f"{side} requote made zero progress "
                            f"(target {target_count_trunc}, original {original_target}) "
                            f"— backing off so wallet state can settle "
                            f"before retrying.",
                            data={
                                "side": side,
                                "replaced": replaced_count,
                                "target": target_count_trunc,
                                "original_target": original_target,
                                "tier_filter_drained": tier_filter_drained,
                            },
                        )
                        if forced or severity == RequoteSeverity.EMERGENCY:
                            self._set_requote_failure_backoff(
                                side,
                                "forced_requote_no_progress",
                                data={
                                    "target": target_count_trunc,
                                    "original_target": original_target,
                                    "severity": severity.value,
                                },
                            )
                else:
                    # Legacy return format (list)
                    new_offers = (
                        requote_result if isinstance(requote_result, list) else []
                    )
                    made_progress = bool(new_offers)
                    if made_progress:
                        self._last_quoted_price[side] = requote_mid
                        self._last_quoted_plain_mid[side] = mid_price  # F67
                        self._ladder_grid_mid[side] = requote_mid
                        self._ladder_anchor_plain_mid[side] = mid_price

                # Clear the force flag only on real progress OR on an
                # intentional "nothing to do" outcome (tier filter drained
                # because a defensive cancel is already handling the work).
                # Genuine failure paths leave the flag set so the next
                # cycle retries rather than stranding stale offers.
                if force_clear_on_success and (made_progress or tier_filter_drained):
                    self._force_requote[side] = False
                # Block step 10 expansion only when the requote actually consumed
                # spare coins.  If create_ladder returned 0 (all coin selections
                # failed — e.g. only large-tier coins are spare but inner-tier
                # offers were requested), the coins are untouched and step 10
                # must still run so it can fill the empty non-inner slots that
                # the requote never touched.  Marking the side "requoted" on a
                # zero-offer result would pin the ladder at its current depth
                # until the next price move.
                if made_progress and severity in (
                    RequoteSeverity.FULL,
                    RequoteSeverity.EMERGENCY,
                ):
                    self._record_shock_requote(
                        side,
                        requote_mid,
                        source="requote",
                        severity=severity.value,
                        new_count=len(new_offers),
                        replaced_count=replaced_count,
                    )
                if new_offers:
                    self._requoted_this_cycle.add(side)
                # Splash broadcast — requote_side queues to Dexie internally,
                # but does NOT know about splash. Mirror the bot_loop create
                # path (line ~4844) so requoted offers are also broadcast over
                # Splash. Without this, every cycle that goes through requote
                # (including the cold-start path) leaves Splash silent.
                if new_offers and getattr(cfg, "SPLASH_ENABLED", False):
                    for offer in new_offers:
                        bech32 = offer.get("offer_bech32", "")
                        trade_id = offer.get("trade_id", "")
                        if bech32 and trade_id:
                            try:
                                self.splash_manager.queue_post(bech32, trade_id)
                            except Exception as _se:
                                log_event(
                                    "warning",
                                    "requote_splash_queue_failed",
                                    f"Splash queue failed for {trade_id}: {_se}",
                                )

                _buy_q = self._last_quoted_price.get("buy")
                _sell_q = self._last_quoted_price.get("sell")
                self.amm_monitor.notify_quoted_price(_buy_q, _sell_q)

                # Coin snapshot after requote (old cancelled + new created)
                self.coin_manager.snapshot_coins("offer_requoted")
                self._emit_coin_update("offer_requoted")
                self._last_bulk_create_time = time.time()

    # -------------------------------------------------------------------
    # Offer creation
    # -------------------------------------------------------------------

    def _log_create_disabled_under_target(
        self,
        side: str,
        current_count: int,
        target_count: int,
        effective_count: int,
        recently_created: int,
        skip_side: bool,
        side_enabled: bool,
        reasons: list,
    ) -> None:
        side_norm = str(side or "").strip().lower()
        signature = "; ".join(reasons or ["unknown"])
        try:
            last = (getattr(self, "_last_create_disabled_log", {}) or {}).get(
                side_norm
            ) or {}
            cooldown = float(
                getattr(self, "_create_disabled_log_cooldown", 60.0) or 60.0
            )
            now = time.time()
            if (
                str(last.get("signature") or "") == signature
                and now - float(last.get("at") or 0) < cooldown
            ):
                return
            self._last_create_disabled_log[side_norm] = {
                "at": now,
                "signature": signature,
            }
        except Exception:
            pass

        cap_side = "Buy" if side_norm == "buy" else "Sell"
        log_event(
            "info",
            f"create_disabled_{side_norm}_under_target",
            f"{cap_side} side under target ({current_count}/{target_count}) but "
            f"creation disabled: {signature}",
            data={
                "current": current_count,
                "effective": effective_count,
                "target": int(target_count),
                "recently_created": recently_created,
                f"skip_{side_norm}": skip_side,
                f"enable_{side_norm}": side_enabled,
            },
        )

    def _create_offers_if_needed(
        self,
        mid_price: Decimal,
        current_buy_count: int,
        current_sell_count: int,
        current_buy_ids=None,
        current_sell_ids=None,
        arb_gap: Decimal = Decimal("0"),
        skip_buy: bool = False,
        skip_sell: bool = False,
    ):
        """Create new offers if we're below target count."""
        recovery_active = self._recovery_is_active()

        # Fix F: check if suspended slots can be unsuspended (coins available)
        for _side in ("buy", "sell"):
            self.offer_manager.unsuspend_slots_if_coins_available(_side)

        # 2026-04-22: early-return log levels promoted from debug to info for
        # sides that are under target. A silent debug-only skip when the
        # wallet is short of MAX_ACTIVE_*_OFFERS left operators without
        # visibility into why refills stalled (observed after a 5-fill buy
        # sweep: ladder stayed at 19/24 for 10+ minutes with every cycle
        # flagging ladder_anchor_drift and every early-return logged at
        # debug level). The helper below raises the log level whenever a
        # side actually needs a refill.
        def _skip_level(also_under_target: bool) -> str:
            return "info" if also_under_target else "debug"

        _buy_under = (
            cfg.ENABLE_BUY
            and not skip_buy
            and int(current_buy_count or 0) < int(cfg.MAX_ACTIVE_BUY_OFFERS)
        )
        _sell_under = (
            cfg.ENABLE_SELL
            and not skip_sell
            and int(current_sell_count or 0) < int(cfg.MAX_ACTIVE_SELL_OFFERS)
        )
        _any_under = bool(_buy_under or _sell_under)

        if cfg.DRY_RUN:
            log_event(
                _skip_level(_any_under),
                "create_skip_dry_run",
                "DRY_RUN is on — not creating offers",
            )
            return

        # Don't create offers while graceful migration is cancelling old ones —
        # new offers would just get cancelled by the migration thread.
        if self._graceful_creation_blocked():
            log_event(
                _skip_level(_any_under),
                "create_skip_graceful_migration",
                "Graceful config migration in progress — not creating offers",
            )
            return

        # Don't create main offers while sniper probe is still being confirmed.
        # Wait for probes to survive one loop before deploying the main book.
        if self._probe_state.get("active", False) and not recovery_active:
            log_event(
                _skip_level(_any_under),
                "create_skip_probe_active",
                "Sniper probe active — waiting for confirmation before main offers",
                data={"probe_state": dict(self._probe_state)},
            )
            return

        needs_main_ladder = _any_under
        if needs_main_ladder:
            current_buy_ids, current_sell_ids, probe_rearmed = (
                self._revalidate_confirmed_probe_edges(
                    current_buy_ids=current_buy_ids,
                    current_sell_ids=current_sell_ids,
                    arb_gap=arb_gap,
                )
            )
            if probe_rearmed:
                log_event(
                    _skip_level(_any_under),
                    "create_skip_probe_rearmed",
                    "Confirmed probe edge disappeared — waiting for re-test before main offers",
                )
                return

        current_buy_count = len(current_buy_ids or set())
        current_sell_count = len(current_sell_ids or set())

        # Don't create while coin operations are running
        if self.coin_manager.is_busy():
            log_event(
                _skip_level(_any_under),
                "create_skip_coin_mgr_busy",
                "Coin manager busy — not creating offers",
            )
            return

        if recovery_active and self._wallet_sync_stale_cycle:
            log_event(
                _skip_level(_any_under),
                "recovery_create_wait",
                "Recovery mode waiting for a fresh wallet view before creating offers",
            )
            return

        # Stale wallet data guard — applies outside recovery mode too.
        # After 3 consecutive stale cycles (~15s) we stop creating new offers
        # because the wallet's offer list may be outdated: we could double-post
        # or use an out-of-date view of what's already on the book.
        # Existing offers stay open; this only blocks NEW creation.
        _stale_streak = int(self._recovery_state.get("wallet_stale_streak", 0))
        _stale_limit = getattr(cfg, "WALLET_STALE_CREATE_LIMIT", 3)
        if self._wallet_sync_stale_cycle and _stale_streak >= _stale_limit:
            log_event(
                "warning",
                "stale_wallet_create_blocked",
                f"Wallet sync stale for {_stale_streak} consecutive cycles — "
                f"blocking new offer creation until data is fresh",
            )
            return

        # Default path creates buy/sell ladders in parallel because they spend
        # different assets. For debugging wallet selection issues, we can
        # force a single global queue so only one make_offer flow runs at once.
        created_any = False
        probe_slot_offsets = self._confirmed_probe_slot_offsets(
            current_buy_ids=current_buy_ids,
            current_sell_ids=current_sell_ids,
        )
        effective_buy_count = max(0, current_buy_count - probe_slot_offsets["buy"])
        effective_buy_count += self.offer_manager.get_recently_created_count("buy")
        effective_sell_count = max(0, current_sell_count - probe_slot_offsets["sell"])
        effective_sell_count += self.offer_manager.get_recently_created_count("sell")
        adaptive_targets = self._get_adaptive_offer_targets(
            mid_price,
            current_buy_count=current_buy_count,
            current_sell_count=current_sell_count,
        )
        position_pauses = self._position_guard_pauses()
        for _side, _pause in position_pauses.items():
            self._emit_position_guard_pause_alert(_side, _pause)
        for _side in ("buy", "sell"):
            if _side not in position_pauses:
                self._clear_alert(f"{_side}_position_paused")
        buy_target = int(adaptive_targets.get("buy", 0) or 0)
        sell_target = int(adaptive_targets.get("sell", 0) or 0)
        _buy_under = bool(
            cfg.ENABLE_BUY and not skip_buy and effective_buy_count < buy_target
        )
        _sell_under = bool(
            cfg.ENABLE_SELL and not skip_sell and effective_sell_count < sell_target
        )
        _any_under = bool(_buy_under or _sell_under)
        # Skip creation on sides that were requoted this cycle — the
        # requote already consumed spare coins and created what it could.
        # Without this, step 10 attempts creation with no spares left,
        # coin selection fails 3 times per slot, and those slots get
        # suspended (ladder degradation that persists across cycles).
        _rq = getattr(self, "_requoted_this_cycle", set())
        if "buy" in _rq:
            skip_buy = True
            log_event(
                _skip_level(_buy_under),
                "create_skip_requoted",
                "Skipping buy creation — already requoted this cycle",
                data={
                    "side": "buy",
                    "effective_count": effective_buy_count,
                    "target": int(buy_target),
                    "recently_created": self.offer_manager.get_recently_created_count(
                        "buy"
                    ),
                },
            )
        if "sell" in _rq:
            skip_sell = True
            log_event(
                _skip_level(_sell_under),
                "create_skip_requoted",
                "Skipping sell creation — already requoted this cycle",
                data={
                    "side": "sell",
                    "effective_count": effective_sell_count,
                    "target": int(sell_target),
                    "recently_created": self.offer_manager.get_recently_created_count(
                        "sell"
                    ),
                },
            )

        _pending_buy_settle = self._pending_cancel_wallet_ids("buy")
        if _pending_buy_settle and not skip_buy:
            skip_buy = True
            log_event(
                _skip_level(_buy_under),
                "create_skip_pending_cancel_settle",
                f"Skipping buy creation - {len(_pending_buy_settle)} "
                f"wallet-active pending cancel(s) still settling",
                data={
                    "side": "buy",
                    "pending_count": len(_pending_buy_settle),
                    "effective_count": effective_buy_count,
                    "target": int(buy_target),
                },
            )

        _pending_sell_settle = self._pending_cancel_wallet_ids("sell")
        if _pending_sell_settle and not skip_sell:
            skip_sell = True
            log_event(
                _skip_level(_sell_under),
                "create_skip_pending_cancel_settle",
                f"Skipping sell creation - {len(_pending_sell_settle)} "
                f"wallet-active pending cancel(s) still settling",
                data={
                    "side": "sell",
                    "pending_count": len(_pending_sell_settle),
                    "effective_count": effective_sell_count,
                    "target": int(sell_target),
                },
            )

        _buy_backoff = self._requote_backoff_remaining("buy")
        if _buy_backoff > 0:
            skip_buy = True
            log_event(
                _skip_level(_buy_under),
                "create_skip_requote_backoff",
                f"Skipping buy creation — requote failure backoff has "
                f"{_buy_backoff:.0f}s remaining",
                data={
                    "side": "buy",
                    "remaining_secs": round(_buy_backoff, 1),
                    "effective_count": effective_buy_count,
                    "target": int(buy_target),
                },
            )
        _sell_backoff = self._requote_backoff_remaining("sell")
        if _sell_backoff > 0:
            skip_sell = True
            log_event(
                _skip_level(_sell_under),
                "create_skip_requote_backoff",
                f"Skipping sell creation — requote failure backoff has "
                f"{_sell_backoff:.0f}s remaining",
                data={
                    "side": "sell",
                    "remaining_secs": round(_sell_backoff, 1),
                    "effective_count": effective_sell_count,
                    "target": int(sell_target),
                },
            )

        buy_enabled = (
            cfg.ENABLE_BUY
            and not skip_buy
            and effective_buy_count < buy_target
            and self.risk_manager.should_enable_side("buy", mid_price)
        )
        sell_enabled = (
            cfg.ENABLE_SELL
            and not skip_sell
            and effective_sell_count < sell_target
            and self.risk_manager.should_enable_side("sell", mid_price)
        )

        # 2026-04-22: when a side is under target but disabled, log the
        # reason at info-level so a stalled refill is diagnosable from the
        # event stream. Silent gating hides recovery-mode stalls for hours.
        if _buy_under and not buy_enabled:
            _rc_buy = self.offer_manager.get_recently_created_count("buy")
            _reason = []
            if not cfg.ENABLE_BUY:
                _reason.append("ENABLE_BUY=False")
            if skip_buy:
                _reason.append("skip_buy=True")
            if effective_buy_count >= buy_target:
                _reason.append(
                    f"effective_count {effective_buy_count} >= target "
                    f"{buy_target} (current={current_buy_count}, "
                    f"recently_created={_rc_buy})"
                )
            if not self.risk_manager.should_enable_side("buy", mid_price):
                _reason.append("risk_manager disabled buy side")
            self._log_create_disabled_under_target(
                "buy",
                current_buy_count,
                buy_target,
                effective_buy_count,
                _rc_buy,
                skip_buy,
                cfg.ENABLE_BUY,
                _reason,
            )
        if _sell_under and not sell_enabled:
            _rc_sell = self.offer_manager.get_recently_created_count("sell")
            _reason = []
            if not cfg.ENABLE_SELL:
                _reason.append("ENABLE_SELL=False")
            if skip_sell:
                _reason.append("skip_sell=True")
            if effective_sell_count >= sell_target:
                _reason.append(
                    f"effective_count {effective_sell_count} >= target "
                    f"{sell_target} (current={current_sell_count}, "
                    f"recently_created={_rc_sell})"
                )
            if not self.risk_manager.should_enable_side("sell", mid_price):
                _reason.append("risk_manager disabled sell side")
            self._log_create_disabled_under_target(
                "sell",
                current_sell_count,
                sell_target,
                effective_sell_count,
                _rc_sell,
                skip_sell,
                cfg.ENABLE_SELL,
                _reason,
            )

        # Results containers for parallel threads
        _parallel_results = {"buy": [], "sell": []}
        _parallel_mid = {"buy": mid_price, "sell": mid_price}
        _recovery_anchor_drift_refill_sides = set()

        def _create_side(side, needed, spread):
            """Create a ladder for one side (runs in a thread)."""
            total_slots = (
                cfg.MAX_ACTIVE_BUY_OFFERS
                if side == "buy"
                else cfg.MAX_ACTIVE_SELL_OFFERS
            )
            probe_anchored_mid = self._get_probe_anchored_mid(side, mid_price)
            if probe_anchored_mid != mid_price:
                log_event(
                    "info",
                    "probe_anchor_apply",
                    f"Anchoring {side} ladder to probe edge: mid {mid_price:.8f} "
                    f"-> {probe_anchored_mid:.8f}",
                )

            # Ladder-anchor logic — keep replacement offers on the same
            # price grid as the already-live offers. Two separate anchors:
            #   grid_mid  = what the original ladder priced against
            #               (probe-anchored or plain, whichever applied
            #                at build time). Replacements reuse this
            #                exact value so new offers slot into the
            #                existing grid regardless of current probe
            #                state.
            #   plain_mid = plain mid at stamp time, used ONLY for drift
            #               detection. Invariant of probe state so probes
            #               toggling on/off never fakes drift.
            _current_count_on_side = (
                len(current_buy_ids or set())
                if side == "buy"
                else len(current_sell_ids or set())
            )
            _grid = self._ladder_grid_mid.get(side, Decimal("0")) or Decimal("0")
            _plain_anchor = self._ladder_anchor_plain_mid.get(
                side, Decimal("0")
            ) or Decimal("0")
            _is_full_rebuild = _current_count_on_side == 0
            ladder_mid_price = probe_anchored_mid
            if _is_full_rebuild or _grid <= 0 or _plain_anchor <= 0:
                # Stamp BOTH anchors at this build. grid_mid captures
                # the exact value used to price this batch of offers;
                # plain_mid is the baseline for drift checks.
                self._ladder_grid_mid[side] = probe_anchored_mid
                self._ladder_anchor_plain_mid[side] = mid_price
                log_event(
                    "debug",
                    "ladder_anchor_set",
                    f"{side} ladder anchor set: grid={probe_anchored_mid:.8f} "
                    f"plain={mid_price:.8f} "
                    f"(full build — {_current_count_on_side} live offers)",
                )
            else:
                # Replacement: check drift of current plain mid vs the
                # stored plain anchor (invariant of probe state).
                try:
                    drift_pct = (
                        abs(mid_price - _plain_anchor) / _plain_anchor * Decimal("100")
                    )
                except Exception:
                    drift_pct = Decimal("0")
                if drift_pct > self._ladder_anchor_drift_pct and recovery_active:
                    log_event(
                        "info",
                        "recovery_ladder_anchor_drift_refill",
                        f"{side} ladder anchor drift {drift_pct:.2f}% > "
                        f"{self._ladder_anchor_drift_pct}% threshold during "
                        f"recovery (plain anchor {_plain_anchor:.8f} vs current "
                        f"plain {mid_price:.8f}) - filling missing slots now "
                        f"and preserving a forced requote for post-recovery "
                        f"realignment",
                    )
                    try:
                        self._force_requote[side] = True
                    except Exception:
                        pass
                    _recovery_anchor_drift_refill_sides.add(side)
                    ladder_mid_price = probe_anchored_mid
                elif drift_pct > self._ladder_anchor_drift_pct:
                    log_event(
                        "info",
                        "ladder_anchor_drift",
                        f"{side} ladder anchor drift {drift_pct:.2f}% > "
                        f"{self._ladder_anchor_drift_pct}% threshold "
                        f"(plain anchor {_plain_anchor:.8f} vs current plain "
                        f"{mid_price:.8f}) — flagging requote to realign ladder",
                    )
                    try:
                        self._force_requote[side] = True
                    except Exception:
                        pass
                    # Skip this replenishment build — requote will cancel
                    # existing offers and rebuild fresh next cycle.
                    _parallel_mid[side] = probe_anchored_mid
                    return
                else:
                    # Drift within bounds — reuse the EXACT grid_mid the
                    # original ladder was priced against. Probe state at
                    # replacement time doesn't matter: we stay on the
                    # ladder's original grid.
                    ladder_mid_price = _grid

            price_cap = self._get_probe_price_boundary(side) if side == "buy" else None
            price_floor = (
                self._get_probe_price_boundary(side) if side == "sell" else None
            )
            _parallel_mid[side] = ladder_mid_price
            # Pass live wallet IDs so replenishment can filter out DB offers
            # that have expired but haven't been reconciled yet (Step 13 runs
            # after Step 10, so there is a 1-cycle lag).  Without this, the
            # expired tier slots appear "full" and new offers land in the wrong
            # tier position on Dexie.
            _live_ids = current_buy_ids if side == "buy" else current_sell_ids
            slot_sequence = self.offer_manager.get_replenishment_slots(
                side,
                total_slots,
                cat_asset_id=cfg.CAT_ASSET_ID,
                live_offer_ids=_live_ids,
            )[:needed]
            offers = self.offer_manager.create_ladder(
                ladder_mid_price,
                side,
                num_offers=len(slot_sequence) or needed,
                spread_fraction=spread,
                risk_manager=self.risk_manager,
                total_slots=total_slots,
                coin_ids_enabled=cfg.COIN_IDS_ENABLED,
                slot_sequence=slot_sequence or None,
                price_cap=price_cap,
                price_floor=price_floor,
                interpolate_refill_prices=side
                not in _recovery_anchor_drift_refill_sides,
            )
            _parallel_results[side] = offers or []

        # Targets are already adjusted for suspended slots and observed spare
        # capacity; build only the missing slots that are actually fundable.
        work_items = []
        if buy_enabled:
            # Zombies are excluded from cap counting (they reference spent coins
            # and can't be filled), so they must also be excluded here.  Counting
            # them against buy_needed would prevent the bot from filling empty
            # real slots whenever zombie count ≥ (MAX - active).
            buy_needed = max(0, buy_target - effective_buy_count)
            buy_spread = self.risk_manager.get_adjusted_spread("buy")
            if buy_needed > 0:
                work_items.append(("buy", buy_needed, buy_spread))
            self._clear_alert("buy_disabled")
        else:
            if (
                cfg.ENABLE_BUY
                and effective_buy_count < buy_target
                and not self.risk_manager.should_enable_side("buy", mid_price)
            ):
                self._emit_alert(
                    "buy_disabled",
                    "warning",
                    "Buys disabled — position limit",
                    "Too much CAT held. Sell offers only until position reduces.",
                    action="view_position",
                    action_label="View Position",
                )

        if sell_enabled:
            sell_needed = max(0, sell_target - effective_sell_count)
            sell_spread = self.risk_manager.get_adjusted_spread("sell")
            if sell_needed > 0:
                work_items.append(("sell", sell_needed, sell_spread))
            self._clear_alert("sell_disabled")
        else:
            if (
                cfg.ENABLE_SELL
                and effective_sell_count < sell_target
                and not self.risk_manager.should_enable_side("sell", mid_price)
            ):
                self._emit_alert(
                    "sell_disabled",
                    "warning",
                    "Sells disabled — position limit",
                    "Too much XCH held. Buy offers only until position reduces.",
                    action="view_position",
                    action_label="View Position",
                )

        if getattr(cfg, "LADDER_CREATE_GLOBAL_SERIAL", False) or recovery_active:
            if recovery_active:
                log_event(
                    "info",
                    "recovery_create_serial",
                    "Recovery mode active — creating missing offers serially to reduce wallet stress",
                )
            log_event(
                "info",
                "ladder_global_serial",
                "Global ladder serialization enabled — creating one side at a time",
            )
            for side, needed, spread in work_items:
                _create_side(side, needed, spread)
        else:
            # Guard: skip if previous cycle's ladder threads are still alive.
            # Without this, a timed-out thread keeps mutating the wallet while
            # the new cycle spawns replacements — doubling offers or causing
            # MEMPOOL_CONFLICT.
            #
            # ESCAPE HATCH: a genuinely wedged wallet RPC call used to pin
            # the thread indefinitely, which kept this guard returning every
            # cycle and silently left the side underquoted until the operator
            # restarted the bot. Track each thread's start time and, once it
            # has been alive past a hard ceiling (default 4 × the 60s join
            # timeout = 240s), stop deferring to it. We log CRITICAL so the
            # operator knows the old thread is a zombie, then proceed with a
            # fresh ladder build. The daemon flag means the zombie dies when
            # its RPC finally returns (or at process exit) — we accept the
            # minor risk of duplicate offer creation in that window, which
            # the trim pass + mempool-conflict detection already handle.
            _now = time.time()
            _zombie_age_ceiling = float(
                getattr(cfg, "LADDER_THREAD_ZOMBIE_SECS", 240) or 240
            )
            live_now = []
            zombies = []
            for t in self._ladder_threads:
                if not t.is_alive():
                    continue
                _start = float(getattr(t, "_catalyst_start_at", 0) or 0)
                if _start > 0 and (_now - _start) >= _zombie_age_ceiling:
                    zombies.append((t, _now - _start))
                else:
                    live_now.append(t)
            if zombies:
                for t, age in zombies:
                    log_event(
                        "critical",
                        "ladder_thread_zombie",
                        f"Ladder thread {t.name!r} has been alive for "
                        f"{age:.0f}s (> {_zombie_age_ceiling:.0f}s zombie "
                        f"ceiling). Proceeding with a fresh ladder build; "
                        f"the stuck thread is now a daemon zombie and will "
                        f"be released when its wallet RPC returns.",
                        data={
                            "thread": t.name,
                            "age_secs": age,
                            "ceiling_secs": _zombie_age_ceiling,
                        },
                    )
            if live_now:
                names = ", ".join(t.name for t in live_now)
                log_event(
                    "warning",
                    "ladder_overlap_skip",
                    f"Skipping ladder creation — previous threads still alive: {names}",
                )
                # Count as a stall so recovery escalates if threads stay hung
                if work_items:
                    self._mark_recovery_create_stall()
                return
            self._ladder_threads = [t for t, _ in zombies]  # keep zombies referenced

            threads = []
            for side, needed, spread in work_items:
                t = threading.Thread(
                    target=_create_side,
                    args=(side, needed, spread),
                    name=f"create-{side}",
                    daemon=True,  # Don't block process exit
                )
                # Stamp start time so the zombie detector above can compute age.
                t._catalyst_start_at = time.time()
                threads.append(t)

            self._ladder_threads.extend(threads)

            # Start all threads and wait for completion
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)  # 60s timeout; wallet RPC hung = don't block forever
                if t.is_alive():
                    log_event(
                        "warning",
                        "ladder_thread_timeout",
                        f"Ladder creation thread {t.name!r} still alive after 60s timeout — "
                        f"wallet RPC may be hung; continuing main loop "
                        f"(will be escalated to zombie at "
                        f"{_zombie_age_ceiling:.0f}s if it stays stuck)",
                    )

        # Process results from both sides
        _notify_amm_after_create = False
        for side in ["buy", "sell"]:
            new_offers = _parallel_results[side]
            if new_offers:
                created_any = True
                _notify_amm_after_create = True
                if side in _recovery_anchor_drift_refill_sides:
                    log_event(
                        "info",
                        "recovery_requote_baseline_preserved",
                        f"{side} recovery refill created {len(new_offers)} offer(s) "
                        "without advancing the requote baseline; a forced "
                        "requote remains queued for full-side realignment after "
                        "recovery exits",
                    )
                else:
                    self._last_quoted_price[side] = _parallel_mid.get(side, mid_price)
                    # F67: remember the un-anchored mid that was current when this
                    # ladder was built. When the probe expires/clears, the baseline
                    # snaps to this value (in _clear_probe_side) to prevent the
                    # dead probe offset from triggering a spurious requote.
                    self._last_quoted_plain_mid[side] = mid_price
                for offer in new_offers:
                    bech32 = offer.get("offer_bech32", "")
                    trade_id = offer.get("trade_id", "")
                    if bech32 and trade_id:
                        self.dexie_manager.queue_post(bech32, trade_id)
                        if getattr(cfg, "SPLASH_ENABLED", False):
                            self.splash_manager.queue_post(bech32, trade_id)

        # Notify AMM monitor of newly quoted prices (for drift baseline)
        if _notify_amm_after_create:
            _buy_quoted = self._last_quoted_price.get("buy")
            _sell_quoted = self._last_quoted_price.get("sell")
            self.amm_monitor.notify_quoted_price(_buy_quoted, _sell_quoted)

        # Coin snapshot after creating offers (coins now locked into offers)
        if work_items and not created_any:
            attempted_sides = {side for side, _needed, _spread in work_items}
            paused_sides = set(self._position_guard_pauses().keys())
            if attempted_sides and attempted_sides.issubset(paused_sides):
                log_event(
                    "info",
                    "position_guard_create_paused",
                    "Offer creation paused by position guard; correcting-side "
                    "offers remain live to rebalance before rebuilding this side",
                    data={"sides": sorted(attempted_sides)},
                )
            else:
                self._mark_recovery_create_stall()

        if created_any:
            self.coin_manager.snapshot_coins("offer_created")
            self._emit_coin_update("offer_created")
            # Mark bulk creation time — coin health checks will be skipped
            # until the wallet settles (PENDING_ACCEPT → confirmed)
            self._last_bulk_create_time = time.time()

            # Re-fetch the current market for diagnostics/UI, but keep the
            # requote baseline pinned to the price we actually deployed.
            # Using the post-create market mid here can include our own fresh
            # book and trigger needless "replace what we just posted" churn.
            fresh = self.price_engine.get_price()
            if fresh and fresh.get("mid_price"):
                fresh_mid = fresh["mid_price"]
                self._current_mid_price = fresh_mid
                self._set_state(mid_price=str(fresh_mid))
                log_event(
                    "info",
                    "post_create_market_observed",
                    f"Observed market after creation: {fresh_mid:.8f} "
                    f"(keeping deployed quote baselines)",
                )

    # -------------------------------------------------------------------
    # Coin management
    # -------------------------------------------------------------------

    def _flush_public_offer_queues(self):
        """Flush public offer queues after reclaiming unsafe locked coins."""
        self._set_cycle_step("step11_dexie_post")

        # Recovered wallet offers can be inserted before offer-to-coin linking
        # finishes. Reclaim reserve/oversized locked coins before posting queued
        # offers publicly, so a just-recovered unsafe offer is cancelled and
        # purged from Dexie/Splash queues before it becomes visible.
        dexie_q_pending = (
            bool(cfg.DEXIE_AUTO_POST)
            and len(getattr(self.dexie_manager, "_queue", []) or []) > 0
        )
        splash_q_pending = (
            bool(getattr(cfg, "SPLASH_ENABLED", False))
            and len(getattr(self.splash_manager, "_queue", []) or []) > 0
        )
        if dexie_q_pending or splash_q_pending:
            try:
                if self._reclaim_oversized_locked_offers():
                    log_event(
                        "info",
                        "oversized_coin_reclaim_pre_public_post",
                        "Reclaimed unsafe locked-coin offer(s) before public posting",
                    )
            except Exception as e:
                log_event(
                    "debug",
                    "oversized_coin_reclaim_pre_public_post_failed",
                    f"Pre-public-post oversized reclaim failed: {e}",
                )

        # ---- Step 11: Direct post to Dexie first ----
        if cfg.DEXIE_AUTO_POST:
            q_len = len(getattr(self.dexie_manager, "_queue", []) or [])
            if q_len > 0:
                print(f"   [11] Posting {q_len} offers to Dexie...", end="", flush=True)
                log_event(
                    "debug", "dexie_flush_start", f"Flushing {q_len} offers to Dexie..."
                )
            result = self.dexie_manager.flush_queue()
            if q_len > 0:
                print(f" {result}", flush=True)
                log_event("info", "dexie_flush_result", f"Dexie flush: {result}")
            else:
                print("   [11] Dexie queue empty", flush=True)
        else:
            print("   [11] Dexie auto-post OFF", flush=True)
            log_event("debug", "dexie_disabled", "DEXIE_AUTO_POST is off")

        # ---- Step 11b: Submit to local Splash node after Dexie is live ----
        if getattr(cfg, "SPLASH_ENABLED", False):
            try:
                splash_q = len(getattr(self.splash_manager, "_queue", []) or [])
                if splash_q > 0:
                    print(
                        f"   [11b] Submitting {splash_q} offers to local Splash node...",
                        end="",
                        flush=True,
                    )
                    log_event(
                        "debug",
                        "splash_flush_start",
                        f"Submitting {splash_q} offers to local Splash node...",
                    )
                result = self.splash_manager.flush_queue()
                if splash_q > 0:
                    print(f" {result}", flush=True)
                    log_event("info", "splash_flush_result", f"Splash submit: {result}")
                else:
                    print("   [11b] Splash queue empty", flush=True)
            except Exception as e:
                print(f"   [11b] Splash submit error: {e}", flush=True)
                log_event("debug", "splash_error", f"Splash flush failed: {e}")
        else:
            print("   [11b] Splash OFF", flush=True)

    def _reclaim_oversized_locked_offers(self) -> bool:
        """Cancel offers that are pinning reserve/oversized topup coins."""
        if not getattr(cfg, "TIER_ENABLED", False):
            return False
        try:
            from database import get_oversized_locked_offers

            base_ratio = float(getattr(cfg, "COIN_MAX_SIZE_RATIO", 1.5) or 1.5)
            fallback_ratio = float(
                getattr(cfg, "COIN_OVERSIZE_FALLBACK_RATIO", 2.0) or 2.0
            )
            flagged = get_oversized_locked_offers(
                max_ratio=max(base_ratio, fallback_ratio),
                cat_decimals=int(getattr(cfg, "CAT_DECIMALS", 3) or 3),
            )
        except Exception as e:
            log_event(
                "debug",
                "oversized_coin_reclaim_scan_failed",
                f"Oversized locked coin scan failed: {e}",
            )
            return False

        if not flagged:
            return False

        trade_ids = sorted(
            {str(row.get("trade_id") or "") for row in flagged if row.get("trade_id")}
        )
        if not trade_ids:
            return False

        previews = []
        for row in flagged[:5]:
            try:
                amount = int(row.get("amount_mojos") or 0)
                if row.get("wallet_type") == "xch":
                    amount_str = f"{Decimal(amount) / Decimal('1000000000000'):.4f} XCH"
                else:
                    scale = Decimal(10) ** Decimal(
                        int(getattr(cfg, "CAT_DECIMALS", 3) or 3)
                    )
                    amount_str = f"{Decimal(amount) / scale:.2f} CAT"
                previews.append(
                    f"{str(row.get('trade_id'))[:12]}... {row.get('side')}/{row.get('tier')} "
                    f"{amount_str} ratio={row.get('ratio')} reason={row.get('reason')}"
                )
            except Exception:
                continue

        log_event(
            "warning",
            "oversized_coin_reclaim",
            f"Cancelling {len(trade_ids)} offer(s) pinning reserve/oversized coins so "
            f"topup can split them back into spare pools: {'; '.join(previews)}",
            data={"trade_ids": trade_ids, "flagged": flagged[:10]},
        )
        try:
            self.offer_manager.cancel_offers(
                trade_ids,
                reason="oversized_coin_reclaim",
                force_storm=True,
                skip_confirmation=True,
            )
            return True
        except Exception as e:
            log_event(
                "warning",
                "oversized_coin_reclaim_failed",
                f"Failed to cancel oversized-coin offers: {e}",
            )
            return False

    def _handle_coins(self, active_buy_count: int, active_sell_count: int):
        """Handle coin counting, topup, and prep.

        Three-tier checking (V1 parity):
        1. needs_coin_prep() — TOTAL coins critically low → full prep
        2. needs_topup() — FREE coins low → lightweight split
        3. check_runtime_health() — every 5 loops, independent free coin check
        """
        if self._reclaim_oversized_locked_offers():
            return

        if self._recovery_is_active():
            # During recovery, still allow coin prep and topup to fire when a
            # tier is empty. Without this, a coin-exhaustion-triggered recovery
            # deadlocks:
            #   coin failures → recovery mode → topup blocked → still no coins →
            #   offers keep failing → recovery never exits.
            # Both checks have their own cooldowns and busy-guards so they won't
            # double-fire or overwhelm the wallet. The health-check topup (Tier 3)
            # is still suppressed to avoid unnecessary churn.
            #
            # Refresh coin counts first so needs_topup() sees current tier state,
            # not the stale snapshot from before recovery mode was entered.
            if not self.coin_manager.is_busy():
                self.coin_manager.update_coin_counts()
            if self.coin_manager.needs_coin_prep(active_buy_count, active_sell_count):
                log_event(
                    "info",
                    "coin_prep_trigger_recovery",
                    "Coins critically low during recovery — forcing runtime top-up "
                    "to break coin-exhaustion deadlock",
                )
                self.coin_manager.start_topup(
                    active_buy_count, active_sell_count, is_drip=False
                )
            elif self.coin_manager.needs_topup(active_buy_count, active_sell_count):
                log_event(
                    "info",
                    "topup_trigger_recovery",
                    "Tier coin shortage during recovery — running topup to "
                    "restore genuinely low spare pools",
                )
                self.coin_manager.start_topup(active_buy_count, active_sell_count)
            else:
                log_event(
                    "debug",
                    "coin_ops_skip_recovery",
                    "Recovery mode active — skipping topup/prep until the book is healthy",
                )
            return

        # Re-check coins after startup — Sage needs time to report all coins.
        # The startup snapshot often shows too few coins because the wallet
        # hasn't fully synced yet. Re-snapshot after 3 loops (~3 min).
        if self._wallet_sync_stale_cycle:
            return

        if not self._startup_coin_recheck_done and self._loop_count >= 3:
            self._startup_coin_recheck_done = True
            self.coin_manager.snapshot_coins("startup_recheck")
            self._emit_coin_update("startup_recheck")
            readiness = self.coin_manager.coin_readiness_report()
            print(
                f"   [COINS] Startup coin re-check: {readiness.get('overall_status', '?')}",
                flush=True,
            )
            log_event(
                "info",
                "startup_coin_recheck",
                f"Re-checked coins after wallet settled: "
                f"{readiness.get('overall_status', 'UNKNOWN')}",
            )

        # Update coin counts + inventory every loop
        self.coin_manager.update_coin_counts()
        self._emit_coin_update("periodic")

        # V3: Periodic wallet reconciliation — authoritative sync every N loops
        # V4 FIX: Changed default from 5 to 2 — active trading causes rapid
        # coin churn (create/cancel destroys and recreates coins) so the DB
        # drifts quickly. Every 2 loops (~2 min) keeps it much tighter.
        #
        # F75: Short-circuit the cadence when a fast-reconcile has been
        # requested (e.g. cancel_offers just succeeded). Without this,
        # returned backing coins don't appear in tier pools until up to
        # 2 cycles after the cancel confirms, and rebuild attempts race
        # ahead of the reconcile.
        self.coin_manager._reconcile_counter += 1
        reconcile_every = getattr(cfg, "RECONCILE_EVERY_N_LOOPS", 2)
        fast_reconcile_requested = False
        try:
            from coin_manager import consume_fast_reconcile

            fast_reconcile_requested = consume_fast_reconcile()
        except Exception:
            pass
        if (
            fast_reconcile_requested
            or self.coin_manager._reconcile_counter >= reconcile_every
        ):
            reconcile = getattr(self.coin_manager, "reconcile_with_wallet", None)
            if callable(reconcile):
                reconcile()
                self.coin_manager._reconcile_counter = 0
            if fast_reconcile_requested:
                log_event(
                    "info",
                    "fast_reconcile_applied",
                    "Reconcile fast-path triggered by a recent cancel",
                )

        # Log coin inventory every 10 loops (gives a running picture)
        if self._loop_count % 10 == 0:
            self.coin_manager.log_inventory(reason="periodic")

        # F78 (2026-04-17): if the mempool watcher couldn't start at boot
        # (usually because TibetSwap pair_id wasn't resolved yet), retry
        # on each cycle until it succeeds. After that, the flag is cleared
        # and this branch is cheap.
        if getattr(self, "_mempool_watcher_needs_start", False):
            if self._try_start_mempool_watcher(log_skip=False):
                self._mempool_watcher_needs_start = False

        # F72: Ladder + coin-accounting integrity watchdog.
        # Runs every 10 cycles (same cadence as inventory log). Produces
        # no actions — only WARN/ERROR log events for drift. Scheduled
        # overnight monitors pick up violations and apply fixes.
        # Safe to fail silently — watchdog errors mustn't break trading.
        #
        # Skipped during:
        # - recovery mode: the bot is deliberately off-book, so invariants
        #   like "open_offers > 0" will fail legitimately.
        # - probe phase: only 1-2 probe offers are live by design; the
        #   watchdog would flag "ladder has 1 offers, configured total 23"
        #   as ERROR noise on every startup.
        # Both states produce noise that masks real issues.
        _probe_active = bool((getattr(self, "_probe_state", None) or {}).get("active"))
        if (
            self._loop_count % 10 == 0
            and not self._recovery_is_active()
            and not _probe_active
        ):
            try:
                self._run_ladder_watchdog()
            except Exception as _wd_err:
                log_event(
                    "debug",
                    "watchdog_failed",
                    f"Ladder watchdog raised (non-fatal): {_wd_err}",
                )

        # Tier-size drift monitor: every 10 cycles, check whether prepared
        # coin sizes still match the live tier targets. Smart Settings sizes
        # coins for the price at prep time; persistent price drift means a
        # re-prep is required. Read-only — just log + alert.
        if (
            self._loop_count % 10 == 0
            and not self._recovery_is_active()
            and not _probe_active
            and not self.coin_manager.is_busy()
        ):
            try:
                self._check_tier_size_drift()
            except Exception as _drift_err:
                log_event(
                    "debug",
                    "tier_size_drift_check_failed",
                    f"Tier size drift check raised (non-fatal): {_drift_err}",
                )

        # Periodic WAL checkpoint truncate. SQLite's autocheckpoint can
        # stall when a long-lived reader connection holds an old snapshot,
        # leaving the WAL to grow unbounded — that was the proximate cause
        # of the 26-04 "database disk image is malformed" episode (4MB WAL
        # vs 663KB main DB). Forcing a TRUNCATE checkpoint every 20 cycles
        # from a fresh connection collapses the WAL regardless of whatever
        # the thread-local connections are doing.
        if self._loop_count > 0 and self._loop_count % 20 == 0:
            try:
                from database import checkpoint_wal

                result = checkpoint_wal("TRUNCATE")
                if result:
                    busy = int(result.get("busy", 0) or 0)
                    log_pages = int(result.get("log_pages", 0) or 0)
                    if busy or log_pages > 0:
                        # log_pages > 0 means the WAL still has frames after
                        # the checkpoint attempt — usually a reader holding
                        # a snapshot. Worth surfacing at info level so we
                        # spot it before it compounds into corruption.
                        log_event(
                            "info",
                            "database_wal_checkpoint",
                            f"Periodic WAL checkpoint: busy={busy}, "
                            f"log_pages={log_pages}, "
                            f"checkpointed={result.get('checkpointed')}",
                        )
            except Exception as _ckpt_err:
                log_event(
                    "debug",
                    "database_wal_checkpoint_failed",
                    f"Periodic WAL checkpoint raised (non-fatal): {_ckpt_err}",
                )

        # Skip coin operations for first 2 loops — wallet needs time to settle
        # after startup. Sage especially returns incomplete coin data initially.
        if self._loop_count < 2:
            return

        # Skip all coin operations during active topup/prep
        if self.coin_manager.is_busy():
            # Check coin prep worker status (may have just finished)
            status = self.coin_manager.check_coin_prep_status()
            if status.get("cancelled_ids"):
                for tid in status["cancelled_ids"]:
                    self.offer_manager._bot_cancelled_ids.add(tid)
            return

        # Grace period after bulk offer creation — wallet needs time to settle.
        # During PENDING_ACCEPT, get_spendable_coins_rpc() returns inconsistent
        # results which would trigger a false topup.
        if self._last_bulk_create_time > 0:
            settle_elapsed = time.time() - self._last_bulk_create_time
            if settle_elapsed < self._coin_settle_grace_secs:
                if self._loop_count % 3 == 0:  # Log periodically
                    log_event(
                        "info",
                        "coin_settle_wait",
                        f"Waiting for wallet to settle ({int(settle_elapsed)}/"
                        f"{self._coin_settle_grace_secs}s) — skipping coin checks",
                    )
                return

        # Early-cycle topup suppression — don't trigger topup in the first N loops.
        # Coin prep just finished; coins are settling; no fills have happened yet
        # to confirm genuine shortages. Firing topup immediately just splits
        # reserve coins before the bot has traded at all, burning a transaction
        # for no benefit. The < 2 guard above handles wallet settle; this guard
        # specifically blocks topup triggers until the bot is past its warm-up phase.
        _topup_min_cycle = int(getattr(cfg, "MIN_TOPUP_CYCLE", 5))
        if self._loop_count < _topup_min_cycle:
            # Coin prep status check must still run (tracks background prep completion)
            status = self.coin_manager.check_coin_prep_status()
            if status.get("cancelled_ids"):
                for tid in status["cancelled_ids"]:
                    self.offer_manager._bot_cancelled_ids.add(tid)
            return

        # Tier 1: Full coin prep check — total coins critically low
        if self.coin_manager.needs_coin_prep(active_buy_count, active_sell_count):
            log_event(
                "info",
                "coin_prep_trigger",
                "TOTAL coins critically low — starting auto coin top-up",
            )
            self.coin_manager.start_topup(
                active_buy_count, active_sell_count, is_drip=False
            )
            return

        # Tier 2: Lightweight topup — free coins running low
        # needs_topup() has just set self._topup_is_drip (False for emergency
        # path, True for drip path); start_topup with no is_drip arg preserves
        # that, so the worker uses the matching action threshold.
        if self.coin_manager.needs_topup(active_buy_count, active_sell_count):
            if self._defer_topup_for_zero_offer_targets(
                active_buy_count,
                active_sell_count,
            ):
                return
            if bool(
                getattr(self.coin_manager, "_topup_is_drip", False)
            ) and self._defer_drip_topup_for_offer_rebuild(
                active_buy_count,
                active_sell_count,
            ):
                return
            if self._defer_spare_topup_until_source_available(
                active_buy_count,
                active_sell_count,
            ):
                return
            log_event(
                "info",
                "topup_trigger",
                "Starting coin top-up to replenish free coins (existing offers stay active)...",
            )
            self.coin_manager.start_topup(active_buy_count, active_sell_count)
            return

        # Tier 3: Runtime health check — every 5 loops, independent check
        if self.coin_manager.check_runtime_health(active_buy_count, active_sell_count):
            log_event(
                "info",
                "health_topup_trigger",
                "Runtime health check triggered coin top-up",
            )
            self.coin_manager.start_topup(
                active_buy_count, active_sell_count, is_drip=False
            )
            return

        # Check coin prep worker status (may have finished in background)
        status = self.coin_manager.check_coin_prep_status()
        if status.get("cancelled_ids"):
            # Pass worker-cancelled IDs to fill tracker
            for tid in status["cancelled_ids"]:
                self.offer_manager._bot_cancelled_ids.add(tid)

    # -------------------------------------------------------------------
    # Housekeeping
    # -------------------------------------------------------------------

    def _handle_housekeeping(self):
        """Periodic cleanup tasks (every 5 minutes)."""
        now = time.time()
        if now - self._last_housekeeping < self._housekeeping_interval:
            return

        self._last_housekeeping = now

        # F15 fix (2026-04-08): background thread liveness watchdog. Each
        # of the bot's background threads (health-monitor, price-watcher,
        # coin-watcher, splash-receive) is a daemon — if one dies silently
        # via an unhandled exception, no part of the rest of the bot
        # notices. We check each registered thread on every housekeeping
        # tick (5 min) and emit an alert if any have died. The bot then
        # attempts to restart the dead thread on its next opportunity.
        try:
            self._check_background_thread_liveness()
        except Exception as _live_err:
            log_event(
                "warning",
                "thread_liveness_check_failed",
                f"Background thread liveness check failed: {_live_err}",
            )

        # F16 fix (2026-04-08): bot loop heartbeat metric. Track the
        # last successful cycle completion timestamp and warn if a cycle
        # is taking >3× LOOP_SECONDS — likely a deadlock somewhere in
        # the cycle. The watchdog is purely diagnostic; we don't try
        # to forcibly recover because that's risky (state could be
        # corrupted). The operator decides what to do.
        try:
            self._check_bot_loop_heartbeat()
        except Exception as _hb_err:
            log_event(
                "warning",
                "loop_heartbeat_check_failed",
                f"Bot loop heartbeat check failed: {_hb_err}",
            )

        # F27 (2026-04-08): per-step SLA timer. Detects single steps
        # that hang on a slow RPC or deadlock without aborting the
        # cycle (which would risk corrupting state).
        try:
            self._check_step_sla()
        except Exception as _sla_err:
            log_event(
                "warning",
                "step_sla_check_failed",
                f"Per-step SLA check failed: {_sla_err}",
            )

        # F17 fix (2026-04-08): daily DB-vs-wallet deep reconciliation.
        # Runs once per 24h. Cross-checks every fill in the DB against
        # the wallet's view of trade history. Backfills missing fills
        # and flags fills that exist in DB but not in wallet (suggests
        # a phantom record). Does not auto-correct — only logs + alerts.
        try:
            self._maybe_run_daily_reconcile()
        except Exception as _rec_err:
            log_event(
                "warning",
                "daily_reconcile_failed",
                f"Daily reconciliation failed: {_rec_err}",
            )

        # F22 (2026-04-08): WAL size monitoring + auto-checkpoint.
        # Long-running bots can build up large WAL files when checkpoints
        # are slow. We check the WAL size each housekeeping tick and
        # force a TRUNCATE checkpoint if it exceeds 50 MB. If the WAL
        # keeps growing despite checkpoints we alert the operator.
        try:
            self._check_wal_size()
        except Exception as _wal_err:
            log_event(
                "warning", "wal_check_failed", f"WAL size check failed: {_wal_err}"
            )

        # F23 (2026-04-08): atomic offer-coin link recovery sweep.
        # add_offer + lock_coin run as separate transactions, so a
        # failed lock_coin call leaves the offer with a coin_id
        # reference but the coin row missing the locked status. Sweep
        # finds these and re-runs lock_coin so coin counts stay
        # accurate.
        try:
            self._repair_unlinked_offer_coins()
        except Exception as _repair_err:
            log_event(
                "warning",
                "offer_coin_repair_failed",
                f"Offer-coin link repair failed: {_repair_err}",
            )

        # F19 (2026-04-08): position sanity check. Verify the bot's
        # running net position estimate matches the wallet's actual
        # CAT balance change since session start. Divergence indicates
        # silently lost fills (or phantom fills).
        try:
            self._check_position_sanity()
        except Exception as _pos_err:
            log_event(
                "warning",
                "position_sanity_check_failed",
                f"Position sanity check failed: {_pos_err}",
            )

        # F76 (2026-04-18): runtime health verifier. Cross-checks the bot's
        # DB against external sources of truth (Dexie, Sage). Currently
        # detects/repairs the "zombie offer" anomaly — DB marked cancelled
        # but Dexie still shows the offer as ACTIVE because the bulk-cancel
        # TX (forced fee=0) didn't confirm. Auto-repair re-fires the cancel
        # via the single-offer path with a priority fee.
        try:
            from bot_health import run_runtime_checks

            health = run_runtime_checks(auto_repair=True)
            if health.repaired:
                _repaired_names = health.repaired_check_names
                _names_txt = ", ".join(_repaired_names) if _repaired_names else "?"
                log_event(
                    "info",
                    "bot_health_repaired",
                    f"bot_health repaired {health.repaired} anomalies "
                    f"in {_names_txt} ({health.summary})",
                    data={"repaired_checks": _repaired_names},
                )
            elif health.anomalies:
                if self._should_log_bot_health_anomalies(health):
                    _anom_names = health.anomaly_check_names
                    _anom_txt = ", ".join(_anom_names) if _anom_names else "?"
                    log_event(
                        "info",
                        "bot_health_anomalies",
                        f"bot_health found {health.anomalies} anomalies "
                        f"in {_anom_txt} ({health.summary})",
                        data={"anomaly_checks": _anom_names},
                    )
        except Exception as _hc_err:
            log_event(
                "warning",
                "bot_health_check_failed",
                f"Runtime health check failed: {_hc_err}",
            )

        # F21 (2026-04-08): lifecycle FSM observability snapshot.
        # Logs noop-transition counts so misuse of the state machine
        # surfaces over time without forcing strict mode (which would
        # break legacy callers).
        try:
            from database import (
                get_lifecycle_observability_stats,
                reset_lifecycle_observability_stats,
            )

            stats = get_lifecycle_observability_stats()
            noops = stats.get("noop_transitions", {})
            invalids = stats.get("invalid_signals", {})
            if noops or invalids:
                log_event(
                    "info",
                    "lifecycle_observability",
                    f"Lifecycle FSM observability over last {self._housekeeping_interval}s: "
                    f"{sum(noops.values())} noop transitions, "
                    f"{sum(invalids.values())} invalid signals",
                    data={"noops": noops, "invalids": invalids},
                )
                # Escalate if noop count is high
                if sum(noops.values()) > 50:
                    log_event(
                        "warning",
                        "lifecycle_observability_high_noops",
                        f"High noop transition count detected. Top: "
                        f"{sorted(noops.items(), key=lambda x: -x[1])[:5]}. "
                        f"Likely a caller is sending the wrong signal for the "
                        f"offer's current state. Investigate.",
                    )
                reset_lifecycle_observability_stats()
        except Exception as _lc_err:
            log_event(
                "debug",
                "lifecycle_observability_failed",
                f"Lifecycle observability check failed: {_lc_err}",
            )

        # Prune Dexie mappings — use DB open offers instead of re-fetching from wallet
        # (sync_from_wallet already ran in the main cycle)
        try:
            from database import get_open_offers

            db_open = get_open_offers()
            active_ids = {o["trade_id"] for o in db_open if o.get("trade_id")}
        except Exception:
            active_ids = set()
        self.dexie_manager.prune_mappings(active_ids)

        # Prune offer manager caches
        self.offer_manager.prune_caches(active_ids)

        # Prune Splash fingerprints + old incoming offers (V3)
        if getattr(cfg, "SPLASH_ENABLED", False):
            self.splash_manager.prune_fingerprints()
        if getattr(cfg, "SPLASH_RECEIVE_ENABLED", False):
            try:
                from database import prune_splash_incoming

                prune_splash_incoming(max_age_hours=24)
            except Exception:
                pass

        # ---- Prune unbounded tables ----
        try:
            from database import (
                cleanup_old_pool_snapshots,
                cleanup_old_trading_pace,
                cleanup_old_events,
            )

            cleanup_old_pool_snapshots(days=30)
            cleanup_old_trading_pace(days=7)
            # Events table was growing unbounded on long-running installs —
            # 30 days of full history, 90 days of error/warning tail.
            cleanup_old_events(days=30, severity_keep_days=90)
        except Exception:
            pass

        # ---- Coin sanity check + orphan cleanup ----
        # Runs every housekeeping cycle (5 min). Catches:
        # - Locked coins whose offers were cancelled outside the bot
        # - Divergence between locked count and open offer count
        try:
            from database import (
                coin_sanity_check,
                cleanup_orphaned_locked_coins,
                get_open_offers as _hk_get_open_offers,
                batch_cancel_stale_offers as _hk_batch_cancel_stale_offers,
                get_locked_coin_ids_for_trade as _hk_get_locked_coin_ids_for_trade,
            )

            # Sanity check: locked vs offers
            db_open = _hk_get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
            sanity = coin_sanity_check(len(db_open))
            wallet_open_ids = set(active_ids)

            # If stale locked coins detected, clean them up
            if sanity.get("stale_locked", 0) > 0 or sanity.get("warnings"):
                # V5: Build wallet_confirmed_locked for Sage
                _hk_wallet_locked = set()
                try:
                    from wallet import get_wallet_type as _hk_gwt

                    if _hk_gwt() == "sage":
                        from wallet_sage import get_owned_coins_detailed

                        for _wid in [cfg.WALLET_ID_XCH, cfg.CAT_WALLET_ID]:
                            _detail = get_owned_coins_detailed(_wid)
                            if _detail:
                                for _cid, _info in _detail.items():
                                    if _info.get("offer_id"):
                                        _sid = (
                                            _cid
                                            if _cid.startswith("0x")
                                            else "0x" + _cid
                                        )
                                        _hk_wallet_locked.add(_sid)
                except Exception:
                    pass
                orphan_stats = cleanup_orphaned_locked_coins(
                    wallet_open_ids, wallet_confirmed_locked=_hk_wallet_locked
                )
                if orphan_stats["total_freed"] > 0:
                    log_event(
                        "info",
                        "housekeeping_orphan_cleanup",
                        f"Freed {orphan_stats['total_freed']} orphaned locked coins "
                        f"during housekeeping",
                    )

            # If the DB still has offers marked open that are not in the
            # wallet-open set and no longer have locked coins, retire those
            # stale rows so the dashboard stays aligned during long runs.
            stale_db_ids = []
            recent_ids = {
                tid
                for tid, ts in self.offer_manager._recently_created.items()
                if tid and (now - float(ts or 0)) < 45
            }
            for db_offer in db_open:
                tid = db_offer.get("trade_id", "")
                if not tid or tid in wallet_open_ids or tid in recent_ids:
                    continue
                # Time-based override: if offer has been in DB for >120s and
                # is NOT in the wallet, mark it stale regardless of coin lock
                # status. This catches offers that failed on-chain
                # (MEMPOOL_CONFLICT) but were recorded in the DB.
                offer_age_s = 0
                try:
                    _ca = db_offer.get("created_at", "")
                    if _ca:
                        from datetime import datetime as _dt_cls

                        _created_ts = _dt_cls.strptime(
                            _ca, "%Y-%m-%d %H:%M:%S"
                        ).timestamp()
                        offer_age_s = now - _created_ts
                except Exception:
                    pass
                has_locked = bool(_hk_get_locked_coin_ids_for_trade(tid))
                if has_locked and offer_age_s <= 120:
                    continue
                if has_locked and offer_age_s > 120:
                    log_event(
                        "info",
                        "stale_offer_cleanup",
                        f"Cleaned up stale DB offer {tid[:16]}... "
                        f"(not in wallet after {int(offer_age_s)}s, had locked coins)",
                    )
                stale_db_ids.append(tid)

            if stale_db_ids:
                cleaned = _hk_batch_cancel_stale_offers(stale_db_ids)
                if cleaned:
                    log_event(
                        "info",
                        "housekeeping_offer_cleanup",
                        f"Marked {cleaned}/{len(stale_db_ids)} stale DB offers cancelled "
                        f"(not open in wallet and no locked coin remained)",
                    )
        except Exception as hk_e:
            log_event(
                "debug",
                "housekeeping_sanity_failed",
                f"Coin sanity check failed: {hk_e}",
            )

        # ---- Sage offer cleanup (F47, 2026-04-09): CONSERVATIVE PURGE ----
        #
        # IMPORTANT HISTORY: an earlier version of this block (pre-F47)
        # treated ANY status >= 2 as "terminal" and deleted it from Sage's
        # local DB. That included status=4 (CONFIRMED == filled), which
        # meant the bot was silently deleting records of every profitable
        # trade before the operator could see them in Sage's UI. This
        # masked a separate fill-rejection bug and made retroactive
        # verification impossible. See the 2026-04-08 "9 phantom_rejected
        # fills" incident for the full post-mortem.
        #
        # NEW POLICY (F47): only CANCELLED and EXPIRED offers are ever
        # deleted, and only after THREE safety gates:
        #
        #   Gate A: Sage's reported status is explicitly 'cancelled',
        #           'expired', or the Chia enum int 3 (CANCELLED).
        #           COMPLETED/CONFIRMED/FILLED are NEVER touched — the
        #           operator keeps those in Sage for bookkeeping.
        #
        #   Gate B: The offer has been in that terminal state for at
        #           least 24 HOURS. This leaves a big observation window
        #           so fill-detection, reconciliation, and manual audits
        #           can all catch any surprises before the evidence
        #           disappears.
        #
        #   Gate C: Our local DB has a matching row with the SAME
        #           terminal status (cancelled/expired). If the local
        #           record is missing, open, filled, or anything else
        #           unexpected, we REFUSE to delete and log a warning —
        #           the operator decides what to do.
        #
        # If any single offer fails its gate checks, we skip THAT offer
        # but continue checking the rest. If Sage returns an offer we
        # don't recognize at all, we log a 'sage_cleanup_anomaly' warning
        # (which is visible in Recommendations) and leave it alone.
        try:
            from wallet import get_wallet_type

            if get_wallet_type() == "sage":
                from wallet import sage_delete_offer

                all_sage_offers = get_all_offers(
                    include_completed=True, start=0, end=500
                )
                if all_sage_offers:
                    # SAFE = only explicit cancellations and expirations.
                    # 'FAILED' / 'PENDING_CANCEL' are intentionally excluded
                    # because they represent transitional or error states
                    # that might still convert to a fill.
                    SAFE_TO_DELETE_STRINGS = {"CANCELLED", "CANCELED", "EXPIRED"}
                    # Chia TradeStatus enum: 3 == CANCELLED.
                    # Explicitly NOT including 4 (CONFIRMED), 5 (FAILED),
                    # or 2 (PENDING_CANCEL).
                    SAFE_TO_DELETE_INTS = {3}

                    # Local-DB statuses that are allowed to match a
                    # Sage-terminal offer. If the local DB shows
                    # anything else, refuse to delete.
                    LOCAL_SAFE_STATUSES = {"cancelled", "canceled", "expired"}

                    # 24h age gate — a terminal offer must have been in
                    # that state for at least this long before deletion.
                    MIN_AGE_SECS = 24 * 3600

                    to_delete = []
                    anomalies = 0
                    new_anomalies = 0  # only first-time occurrences
                    now_ts = int(time.time())
                    # Per-session set — each unique trade_id is only
                    # warned about once.  Stored on self so it survives
                    # across cycles for the lifetime of the bot session.
                    if not hasattr(self, "_sage_anomaly_seen"):
                        self._sage_anomaly_seen: set = set()

                    for offer in all_sage_offers:
                        status_val = offer.get("status")
                        trade_id = offer.get("trade_id", "")
                        if not trade_id:
                            continue

                        # ---- Gate A: is this status explicitly deletable? ----
                        is_safe_status = False
                        reason = None
                        if isinstance(status_val, str):
                            if status_val.upper() in SAFE_TO_DELETE_STRINGS:
                                is_safe_status = True
                                reason = status_val.upper()
                        elif isinstance(status_val, int):
                            if status_val in SAFE_TO_DELETE_INTS:
                                is_safe_status = True
                                reason = "CANCELLED_INT"

                        # Time-expiry bypass: offers past their max_time
                        # are effectively EXPIRED even if Sage still
                        # reports them active. Must be >=24h past expiry.
                        if not is_safe_status:
                            valid_times = offer.get("valid_times") or {}
                            max_time = (
                                valid_times.get("max_time", 0)
                                or offer.get("max_time", 0)
                                or 0
                            )
                            if max_time and int(max_time) > 0:
                                seconds_past_expiry = now_ts - int(max_time)
                                if seconds_past_expiry >= MIN_AGE_SECS:
                                    is_safe_status = True
                                    reason = "EXPIRED_VALID_TIMES"

                        if not is_safe_status:
                            # Not cancelled, not expired — leave it alone.
                            # This is the big departure from the old code:
                            # filled/confirmed offers stay in Sage for the
                            # operator to see.
                            continue

                        # ---- Gate B: is the offer at least 24h old? ----
                        # Prefer the Sage-side creation timestamp; fall
                        # back to our local DB timestamps.
                        local_offer = get_offer(trade_id)
                        age_secs = 0
                        sage_ct = offer.get("creation_timestamp") or offer.get(
                            "created_at_height"
                        )
                        if sage_ct:
                            try:
                                age_secs = now_ts - int(sage_ct)
                            except Exception:
                                pass
                        if age_secs <= 0 and local_offer:
                            try:
                                from datetime import datetime as _dt

                                created_raw = str(local_offer.get("created_at") or "")
                                if created_raw:
                                    _created = _dt.strptime(
                                        created_raw[:19], "%Y-%m-%d %H:%M:%S"
                                    )
                                    age_secs = now_ts - int(_created.timestamp())
                            except Exception:
                                pass

                        if age_secs < MIN_AGE_SECS:
                            # Too young to delete — keep it visible in
                            # Sage so the operator can audit.
                            continue

                        # ---- Gate C: does our local DB agree? ----
                        if local_offer is None:
                            anomalies += 1
                            if trade_id not in self._sage_anomaly_seen:
                                self._sage_anomaly_seen.add(trade_id)
                                new_anomalies += 1
                                # Per-offer detail at DEBUG — the summary below
                                # handles operator visibility at WARNING level.
                                # These fire ~40 times per cleanup run for
                                # historical offers that pre-date the local DB,
                                # generating thousands of WARNING lines per session.
                                log_event(
                                    "debug",
                                    "sage_cleanup_anomaly",
                                    f"Sage reports offer {trade_id[:16]}... as "
                                    f"{reason} but no local DB record exists. "
                                    f"Refusing to delete — check Recommendations.",
                                )
                            continue

                        local_status = str(local_offer.get("status") or "").lower()
                        if local_status not in LOCAL_SAFE_STATUSES:
                            # Local says something else — open, filled,
                            # pending, etc. Don't touch it.
                            anomalies += 1
                            if trade_id not in self._sage_anomaly_seen:
                                self._sage_anomaly_seen.add(trade_id)
                                new_anomalies += 1
                                # Per-offer detail at DEBUG (see comment above).
                                log_event(
                                    "debug",
                                    "sage_cleanup_anomaly",
                                    f"Sage says {reason} for {trade_id[:16]}... "
                                    f"but local DB shows '{local_status}'. "
                                    f"Refusing to delete — manual review needed.",
                                )
                            continue

                        # All three gates passed.
                        to_delete.append(
                            (
                                trade_id,
                                map_sage_terminal_offer_status(
                                    status_val,
                                    sage_offer=offer,
                                    local_offer=local_offer,
                                    now_ts=now_ts,
                                ),
                            )
                        )

                    if to_delete:
                        deleted = 0
                        status_updates = 0
                        for tid, local_status in to_delete[:50]:  # Cap at 50 per cycle
                            if sage_delete_offer(tid):
                                deleted += 1
                                if local_status:
                                    try:
                                        if update_offer_status(tid, local_status):
                                            status_updates += 1
                                    except Exception:
                                        pass
                            time.sleep(0.05)
                        if deleted > 0:
                            log_event(
                                "info",
                                "sage_offer_cleanup",
                                f"Deleted {deleted}/{len(to_delete)} "
                                f"safe-to-delete (cancelled/expired >24h, "
                                f"DB-verified) offers from Sage",
                            )
                            if status_updates > 0:
                                log_event(
                                    "debug",
                                    "sage_offer_cleanup_db",
                                    f"Updated {status_updates} local offer "
                                    f"statuses during Sage cleanup",
                                )
                    if anomalies > 0:
                        # Only emit the summary when there are NEW (first-seen)
                        # anomalies this cycle.  Known anomalies are silently
                        # counted so the summary still shows the total, but
                        # repeated cycles won't flood the log.
                        repeated = anomalies - new_anomalies
                        if new_anomalies > 0:
                            log_event(
                                "info",
                                "sage_cleanup_anomalies_summary",
                                _format_sage_cleanup_skip_summary(
                                    anomalies,
                                    new_anomalies,
                                    repeated,
                                ),
                            )
                        # else: all anomalies already seen — no summary spam

                    repaired_fills = backfill_verified_fills_from_offers(
                        limit=50,
                        since=getattr(cfg, "RUN_HISTORY_CUTOFF", None),
                    )
                    if repaired_fills:
                        log_event(
                            "success",
                            "sage_fill_backfill",
                            f"Recovered {len(repaired_fills)} verified fill "
                            f"record{'s' if len(repaired_fills) != 1 else ''} from "
                            f"Sage-confirmed offer state",
                        )
                        for fill in repaired_fills[:10]:
                            try:
                                price = float(fill.get("price_xch"))
                            except Exception:
                                price = None
                            try:
                                size_xch = float(fill.get("size_xch"))
                            except Exception:
                                size_xch = None
                            try:
                                size_cat = float(fill.get("size_cat"))
                            except Exception:
                                size_cat = None
                            side_label = str(fill.get("side") or "").upper()
                            trade_preview = str(fill.get("trade_id") or "")[:16]
                            size_part = (
                                f" size {size_xch:.4f} XCH"
                                if isinstance(size_xch, float)
                                else ""
                            )
                            price_part = (
                                f" at {price:.8f}" if isinstance(price, float) else ""
                            )
                            fill_msg = (
                                f"Sage confirmed {side_label} fill for {trade_preview}..."
                                f"{size_part}{price_part}"
                                if side_label
                                else f"Sage confirmed fill for {trade_preview}..."
                                f"{size_part}{price_part}"
                            )
                            log_event(
                                "info",
                                "offer_filled",
                                fill_msg,
                                data={
                                    "fill_id": fill.get("fill_id"),
                                    "trade_id": fill.get("trade_id"),
                                    "side": fill.get("side"),
                                    "price": price,
                                    "size_xch": size_xch,
                                    "size_cat": size_cat,
                                    "tier": fill.get("tier") or "unknown",
                                    "verification_source": "sage_cleanup",
                                },
                            )
        except Exception as sage_e:
            log_event(
                "debug", "sage_cleanup_failed", f"Sage offer cleanup failed: {sage_e}"
            )

        log_event("debug", "housekeeping", "Periodic housekeeping completed")

    # -------------------------------------------------------------------
    # F15/F16: liveness watchdogs
    # -------------------------------------------------------------------

    def _check_background_thread_liveness(self) -> None:
        """Check that all critical background threads are still alive.

        Each daemon thread is checked. If any has died (e.g. unhandled
        exception inside its run loop), an operator-visible alert fires
        and the thread is marked for restart on the next opportunity.

        Threads checked:
          - health-monitor (Sage health watcher)
          - price-watcher (fast Tibet poller)
          - coin-watcher (DB↔wallet coin diff)
          - splash-receive (Splash incoming offer poller)
        """
        critical_threads = [
            ("health-monitor", "_health_thread", self._start_health_monitor),
            ("price-watcher", "_watcher_thread", self._start_price_watcher),
            ("coin-watcher", "_coin_watcher_thread", self._start_coin_watcher),
        ]
        # Splash incoming watcher classifies inbound P2P offers. Previously
        # the watchdog named it in the docstring but never actually checked
        # it, so a crash left Splash receive silently dead while the bot
        # kept quoting. Include it here — only when Splash is enabled, so
        # we don't "restart" a thread that was intentionally not started.
        if getattr(cfg, "SPLASH_ENABLED", False):
            critical_threads.append(
                ("splash-receive", "_splash_receive_thread", self._start_splash_receive)
            )
        # During shutdown the watchers exit cleanly as they observe
        # self._running == False. Treating those clean exits as "thread
        # crashed" spams ERROR logs and triggers futile restarts that
        # immediately re-exit. Skip the whole check when shutting down.
        if not self._running:
            return
        for name, attr, restart_fn in critical_threads:
            t = getattr(self, attr, None)
            # If thread was never started or is alive, no action needed
            if t is None:
                continue
            if t.is_alive():
                continue
            # Thread is dead — log critical, attempt restart
            log_event(
                "error",
                "background_thread_died",
                f"CRITICAL: background thread '{name}' is dead. "
                f"Attempting restart. The previous instance crashed silently — "
                f"check the superlog for an unhandled exception in {name}.",
            )
            try:
                self._emit_alert(
                    f"thread_dead_{name}",
                    "error",
                    f"Background thread crashed: {name}",
                    f"The {name} background thread is no longer running. "
                    f"This usually means an unhandled exception inside its "
                    f"run loop. The bot is attempting to restart it.",
                )
            except Exception:
                pass
            try:
                restart_fn()
                log_event(
                    "info",
                    "background_thread_restarted",
                    f"Successfully restarted background thread '{name}'",
                )
                # Clear the "crashed" banner and raise a transient
                # "recovered" banner so the operator sees the outcome —
                # previously the original crash banner stayed up whether
                # the restart succeeded or not, which left the GUI
                # misleading.
                self._clear_alert(f"thread_dead_{name}")
                try:
                    self._emit_alert(
                        f"thread_recovered_{name}",
                        "info",
                        f"Background thread recovered: {name}",
                        f"The {name} background thread has been restarted "
                        f"after a crash. No action required.",
                    )
                except Exception:
                    pass
            except Exception as _restart_err:
                log_event(
                    "error",
                    "background_thread_restart_failed",
                    f"Failed to restart background thread '{name}': {_restart_err}",
                )
                # Upgrade the existing thread_dead_* banner to reflect the
                # failed restart so operators don't believe a fix is in
                # progress when it has already failed.
                try:
                    self._emit_alert(
                        f"thread_dead_{name}",
                        "error",
                        f"Background thread crashed: {name} (restart FAILED)",
                        f"The {name} background thread is still down. "
                        f"Automatic restart failed with: {_restart_err}. "
                        f"Manual intervention (bot restart) is required.",
                        action="stop_bot",
                        action_label="Stop Bot",
                    )
                except Exception:
                    pass

    def _set_cycle_step(self, name: str) -> None:
        """F27 (2026-04-08): mark the start of a cycle step.

        The housekeeping watchdog (in _check_step_sla) reads this and
        warns if a single step has been running longer than the SLA.
        Cheap call — just updates two instance vars. Sprinkle at the
        start of each major cycle step for granular hung-step detection.
        """
        self._current_cycle_step = name
        self._cycle_step_started_at = time.monotonic()
        # Reset per-step alert tracker so a new step is allowed to alert.
        # Also clear the "step_sla" banner once the bot has moved on —
        # otherwise the old hung-step warning stayed glued to the GUI
        # until the next stuck step rewrote it, even though the cycle
        # was demonstrably making progress.
        if self._step_sla_alerted_for and self._step_sla_alerted_for != name:
            self._step_sla_alerted_for = None
            try:
                self._clear_alert("step_sla")
            except Exception:
                pass

    def _check_step_sla(self) -> None:
        """F27 (2026-04-08): SLA timer for individual cycle steps.

        Called from housekeeping (every 5 min). If the bot has been
        stuck on a single step for longer than _step_sla_secs (60s),
        log a warning and fire an alert. Per-step alert dedup so we
        don't spam the same step.
        """
        started = self._cycle_step_started_at
        if not started:
            return
        elapsed = time.monotonic() - started
        if elapsed < self._step_sla_secs:
            return
        step_name = self._current_cycle_step
        # Already alerted for this exact step in this stuck window?
        if self._step_sla_alerted_for == step_name:
            return
        self._step_sla_alerted_for = step_name
        log_event(
            "warning",
            "step_sla_violation",
            f"Cycle step '{step_name}' has been running for {elapsed:.1f}s "
            f"(SLA {self._step_sla_secs:.0f}s). The bot may be stuck on a "
            f"hung RPC, deadlock, or slow API. Investigate if this persists.",
        )
        try:
            self._emit_alert(
                "step_sla",
                "warning",
                f"Step hung: {step_name}",
                f"Cycle step '{step_name}' has been running for {elapsed:.0f}s "
                f"(SLA {self._step_sla_secs:.0f}s). Most likely a hung RPC. "
                f"Check Sage/Coinset/Tibet connectivity.",
            )
        except Exception:
            pass

    def _check_bot_loop_heartbeat(self) -> None:
        """Diagnostic: warn if cycles are taking >3× LOOP_SECONDS.

        Doesn't try to recover (state could be corrupted) — just emits
        a loud operator signal so the user knows to investigate.
        """
        loop_secs = float(getattr(cfg, "LOOP_SECONDS", 45) or 45)
        max_acceptable = loop_secs * 3
        last_dur = float(getattr(self, "_last_loop_duration", 0) or 0)
        if last_dur > max_acceptable:
            log_event(
                "warning",
                "loop_heartbeat_slow",
                f"Bot loop is slow: last cycle took {last_dur:.0f}s "
                f"(threshold {int(max_acceptable)}s = 3× LOOP_SECONDS). "
                f"This may indicate a hung RPC, deadlock, or excessive "
                f"queue depth. Investigate if this persists.",
            )
            try:
                self._emit_alert(
                    "loop_slow",
                    "warning",
                    "Bot Loop Slow",
                    f"Cycle took {last_dur:.0f}s vs threshold {int(max_acceptable)}s. "
                    f"Investigate for hung RPCs or queue overflow.",
                )
            except Exception:
                pass
        else:
            try:
                self._clear_alert("loop_slow")
            except Exception:
                pass

    def _run_startup_self_test(self) -> None:
        """F18: ping every external service the bot needs and emit
        warning alerts for each one that's down.

        Never blocks startup. The user sees the alerts in the
        Recommendations panel and decides whether to start trading
        with degraded capability.

        Services tested:
          - Sage RPC (critical — required for everything)
          - Coinset API (degrades fast fill detection if down)
          - TibetSwap API (degrades pricing if down)
          - Dexie API (degrades offer posting + competitor intel)
          - Spacescan API (degrades fill verification + token context)
          - SQLite DB write (critical — bot can't track state without it)

        Results stored in self._startup_self_test_results so the
        Recommendations panel + an API endpoint can show them later.
        """
        import requests as _req

        results: Dict[str, Dict] = {}

        def _check_http(
            name: str,
            url: str,
            timeout: float = 5.0,
            method: str = "GET",
            json_body=None,
        ) -> Dict:
            """F32 (2026-04-08): a server is REACHABLE if it answers at all,
            even with a 4xx. The previous version treated 404 as "down",
            which falsely flagged Spacescan because the path used in the
            test wasn't a real endpoint. We now distinguish:
              - 2xx/3xx → reachable, endpoint working ✓
              - 4xx     → reachable but endpoint mismatch (still ✓ for health)
              - 5xx     → server has problems → DOWN
              - timeout / connection refused → DOWN
            401/403 are still treated as reachable (auth failure means the
            server is alive and rejecting our request).
            """
            try:
                if method == "POST":
                    r = _req.post(url, json=(json_body or {}), timeout=(2, timeout))
                else:
                    r = _req.get(url, timeout=(2, timeout))
                # 2xx/3xx clearly OK; 4xx means reachable; 5xx means server problem
                if r.status_code < 500:
                    return {
                        "name": name,
                        "url": url,
                        "ok": True,
                        "status_code": r.status_code,
                        "error": None,
                    }
                return {
                    "name": name,
                    "url": url,
                    "ok": False,
                    "status_code": r.status_code,
                    "error": f"HTTP {r.status_code} (server error)",
                }
            except _req.exceptions.Timeout:
                return {"name": name, "url": url, "ok": False, "error": "timeout"}
            except _req.exceptions.ConnectionError as e:
                return {
                    "name": name,
                    "url": url,
                    "ok": False,
                    "error": f"connection refused ({type(e).__name__})",
                }
            except Exception as e:
                return {
                    "name": name,
                    "url": url,
                    "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:80]}",
                }

        # 1. Sage RPC — already checked by sync, but report explicitly
        try:
            from wallet import get_wallet_sync_status

            sage_info = get_wallet_sync_status() or {}
            sage_ok = bool(sage_info.get("reachable") or sage_info.get("synced"))
            results["sage"] = {
                "name": "Sage Wallet RPC",
                "ok": sage_ok,
                "missing_if_down": (
                    "EVERYTHING — fill detection, offer creation, cancellation, "
                    "and balance checks all require Sage. Bot will not be able "
                    "to trade."
                ),
                "critical": True,
                "error": None if sage_ok else "Sage RPC not reachable",
            }
        except Exception as e:
            results["sage"] = {
                "name": "Sage Wallet RPC",
                "ok": False,
                "critical": True,
                "missing_if_down": "EVERYTHING (see above).",
                "error": f"check failed: {e}",
            }

        # 2. TibetSwap API — pricing source
        # F32 fix: use the real /pairs endpoint that price_engine actually
        # consumes (was /router which doesn't exist on tibetswap.io v2).
        tibet_url = str(
            getattr(cfg, "TIBET_API_BASE", "https://api.v2.tibetswap.io")
            or "https://api.v2.tibetswap.io"
        )
        r = _check_http("TibetSwap API", f"{tibet_url}/pairs?skip=0&limit=1")
        r["missing_if_down"] = (
            "Real-time price feed. Bot will fall back to Dexie-only pricing "
            "(less accurate) and AMM drift detection will not work."
        )
        r["critical"] = False
        results["tibet"] = r

        # 3. Dexie API — offer posting + competitor orderbook
        # F32 fix: use the real /v1/offers endpoint that dexie_manager and
        # market_intel actually consume (was /v2/prices/tickers which is
        # a different surface and may not exist on the v1 base).
        dexie_url = str(
            getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
            or "https://api.dexie.space"
        )
        r = _check_http(
            "Dexie API", f"{dexie_url.rstrip('/')}/v1/offers?compact=true&page_size=1"
        )
        r["missing_if_down"] = (
            "Offer posting to Dexie + competitor orderbook intelligence. "
            "Offers will still be created on-chain but won't be visible on "
            "the Dexie GUI to retail buyers/sellers. Splash broadcast still "
            "works."
        )
        r["critical"] = False
        results["dexie"] = r

        # 4. Coinset API — fast fill detection + mempool watcher
        # F32: this one was already correct — keeping the POST shape since
        # /get_blockchain_state is the real endpoint coinset_client + tx_fees
        # both consume.
        if getattr(cfg, "COINSET_ENABLED", True):
            coinset_url = str(
                getattr(cfg, "COINSET_API_URL", "https://api.coinset.org")
                or "https://api.coinset.org"
            )
            r = _check_http(
                "Coinset API",
                f"{coinset_url.rstrip('/')}/get_blockchain_state",
                method="POST",
                json_body={},
            )
            r["missing_if_down"] = (
                "Fast fill detection (5–18s early warning before "
                "block confirms). Fills will still be detected via "
                "wallet RPC poll on the next 45s cycle. Mempool "
                "watcher background thread will idle."
            )
            r["critical"] = False
            results["coinset"] = r
        else:
            results["coinset"] = {
                "name": "Coinset API",
                "ok": True,
                "skipped": True,
                "missing_if_down": "n/a (disabled in config)",
                "critical": False,
            }

        # 5. Spacescan API — fill verification + token context
        # F32 fix: Spacescan has NO /health endpoint, and /coin/info/ with
        # an all-zero coin ID can timeout because Spacescan scans the chain
        # looking for it. The cheapest reachability check is the BARE API
        # root: it returns HTTP 404 (no path matches) but the SERVER is up,
        # which is what we care about. Our improved _check_http treats any
        # non-5xx as reachable.
        if getattr(cfg, "SPACESCAN_ENABLED", False):
            spacescan_url = str(
                getattr(cfg, "SPACESCAN_API_BASE", "https://api.spacescan.io")
                or "https://api.spacescan.io"
            )
            r = _check_http(
                "Spacescan API",
                f"{spacescan_url.rstrip('/')}/",
            )
            r["missing_if_down"] = (
                "On-chain fill verification (golden source of truth) and "
                "Spacescan token context. Bot will fall back to wallet-only "
                "verification for fills. Some fills may be deferred or "
                "marked unverified."
            )
            r["critical"] = False
            results["spacescan"] = r
        else:
            results["spacescan"] = {
                "name": "Spacescan API",
                "ok": True,
                "skipped": True,
                "missing_if_down": "n/a (disabled in config)",
                "critical": False,
            }

        # 6. SQLite DB writability
        try:
            from database import log_event as _le

            _le("debug", "self_test_db_write", "Self-test DB write probe")
            results["database"] = {
                "name": "SQLite Database",
                "ok": True,
                "missing_if_down": "Bot CANNOT operate without DB writes.",
                "critical": True,
            }
        except Exception as e:
            results["database"] = {
                "name": "SQLite Database",
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "missing_if_down": "Bot CANNOT operate without DB writes.",
                "critical": True,
            }

        # ── Process results ──
        self._startup_self_test_results = results
        all_ok = all(
            r.get("ok", False) for r in results.values() if not r.get("skipped", False)
        )
        critical_failures = [
            r
            for r in results.values()
            if not r.get("ok", False)
            and r.get("critical", False)
            and not r.get("skipped", False)
        ]
        warn_failures = [
            r
            for r in results.values()
            if not r.get("ok", False)
            and not r.get("critical", False)
            and not r.get("skipped", False)
        ]

        if all_ok:
            log_event(
                "info",
                "self_test_pass",
                "Startup self-test PASSED — all external services reachable",
            )
            return

        # Report each failure with what's missing
        for r in critical_failures + warn_failures:
            sev = "error" if r.get("critical") else "warning"
            log_event(
                sev,
                "self_test_service_down",
                f"{r['name']} is DOWN ({r.get('error', 'unknown error')}). "
                f"Missing if you continue: {r.get('missing_if_down', 'unknown')}",
            )
            try:
                self._emit_alert(
                    f"self_test_{r['name'].lower().replace(' ', '_')}",
                    sev,
                    f"{r['name']} unreachable",
                    f"{r.get('error', 'connection failed')}\n\n"
                    f"What you'll be missing: {r.get('missing_if_down', 'unknown')}",
                    action="run_doctor",
                    action_label="Run Doctor",
                )
            except Exception:
                pass

        # Final summary
        ok_count = sum(
            1
            for r in results.values()
            if r.get("ok", False) and not r.get("skipped", False)
        )
        total = sum(1 for r in results.values() if not r.get("skipped", False))
        log_event(
            "warning" if warn_failures and not critical_failures else "error",
            "self_test_partial",
            f"Startup self-test: {ok_count}/{total} services OK. "
            f"{len(critical_failures)} critical failure(s), "
            f"{len(warn_failures)} non-critical failure(s). "
            f"You can continue but some features will be unavailable — "
            f"see Recommendations panel.",
        )

    def _check_wal_size(self) -> None:
        """F22: monitor WAL size and force checkpoint if too large.

        Tracks consecutive large-WAL events. If the WAL stays above the
        threshold despite three consecutive checkpoint attempts, escalate
        to a critical operator alert.
        """
        try:
            from database import get_wal_size_mb, force_wal_checkpoint
        except ImportError:
            return

        size_mb = get_wal_size_mb()
        threshold_mb = 50.0
        critical_mb = 500.0

        if size_mb < threshold_mb:
            # Healthy — reset escalation counter
            if getattr(self, "_wal_oversize_streak", 0) > 0:
                self._wal_oversize_streak = 0
                self._clear_alert("wal_oversize")
            return

        # Above threshold — force a checkpoint and track the streak
        self._wal_oversize_streak = getattr(self, "_wal_oversize_streak", 0) + 1
        log_event(
            "info",
            "wal_oversize_checkpoint",
            f"WAL is {size_mb:.1f} MB (>{threshold_mb} MB threshold) — "
            f"forcing TRUNCATE checkpoint (streak {self._wal_oversize_streak})",
        )
        ok = force_wal_checkpoint()
        post_size_mb = get_wal_size_mb()

        # If checkpoint didn't shrink the WAL meaningfully, escalate
        if not ok or post_size_mb > threshold_mb * 0.8:
            if self._wal_oversize_streak >= 3 or size_mb > critical_mb:
                log_event(
                    "error",
                    "wal_oversize_persistent",
                    f"WAL is {post_size_mb:.1f} MB after checkpoint and has "
                    f"been oversized for {self._wal_oversize_streak} consecutive "
                    f"housekeeping cycles. Long-running readers may be blocking "
                    f"checkpoints. Restart the app or investigate.",
                )
                try:
                    self._emit_alert(
                        "wal_oversize",
                        "error",
                        "Database WAL too large",
                        f"The SQLite write-ahead log is {post_size_mb:.0f} MB and "
                        f"checkpoints aren't shrinking it. Long-running queries "
                        f"may be blocking. Restart the app to clear it.",
                    )
                except Exception:
                    pass
        else:
            log_event(
                "info",
                "wal_checkpoint_succeeded",
                f"WAL shrunk from {size_mb:.1f} MB to {post_size_mb:.1f} MB",
            )

    def _check_position_sanity(self) -> None:
        """F19: position sanity check.

        Compares the bot's running net_position estimate against the
        delta of the wallet's actual CAT balance since session start.
        Significant divergence indicates either silently lost fills or
        phantom recordings.

        Tolerance: ±5% of the CONFIGURED max position. Bigger gaps
        log a warning and fire an alert.
        """
        try:
            from wallet import get_wallet_balance
        except ImportError:
            return

        # Initialise baseline on first run after _startup_complete is set
        if getattr(self, "_position_baseline_cat", None) is None:
            try:
                _bal_raw = get_wallet_balance(cfg.CAT_WALLET_ID)
                _bal = (
                    (_bal_raw.get("wallet_balance") or _bal_raw) if _bal_raw else None
                )
                if _bal:
                    _scale = Decimal(10) ** Decimal(str(cfg.CAT_DECIMALS))
                    _baseline = (
                        Decimal(str(_bal.get("confirmed_wallet_balance", 0))) / _scale
                    )
                    self._position_baseline_cat = _baseline
                    # F48 (2026-04-09): also snapshot the bot's current
                    # all-time net position so the sanity check compares
                    # SINCE-baseline deltas against SINCE-baseline deltas.
                    try:
                        _net_at_baseline = self.risk_manager._net_position_cat
                    except Exception:
                        _net_at_baseline = Decimal("0")
                    self._position_baseline_net_cat = Decimal(
                        str(_net_at_baseline or 0)
                    )
                    self._position_baseline_at = time.time()
                    log_event(
                        "debug",
                        "position_baseline_set",
                        f"Position sanity baseline: wallet={_baseline:.2f} CAT, "
                        f"bot_net={self._position_baseline_net_cat:+.2f} CAT "
                        f"at session start",
                    )
            except Exception:
                pass
            return  # First call only sets baseline; check on next housekeeping tick

        # Read current wallet CAT balance
        try:
            _cat_raw = get_wallet_balance(cfg.CAT_WALLET_ID)
            _cat_bal = (
                (_cat_raw.get("wallet_balance") or _cat_raw) if _cat_raw else None
            )
            if not _cat_bal:
                return
            _scale = Decimal(10) ** Decimal(str(cfg.CAT_DECIMALS))
            _current_cat = (
                Decimal(str(_cat_bal.get("confirmed_wallet_balance", 0))) / _scale
            )
        except Exception:
            return

        # Bot's all-time net position (cumulative from fills table)
        net_position_cat = Decimal("0")
        try:
            net_position_cat = self.risk_manager._net_position_cat
        except Exception:
            return

        # F48 (2026-04-09): compute net position SINCE session baseline so
        # we compare like-for-like. Previously we compared the wallet delta
        # since baseline against the bot's ALL-TIME net position, which is
        # guaranteed to drift by whatever net position existed before the
        # session started.
        baseline_net = getattr(self, "_position_baseline_net_cat", None)
        if baseline_net is None:
            baseline_net = Decimal("0")
        net_position_since_baseline = net_position_cat - baseline_net

        # F62 (2026-04-09, broadened 2026-04-10): stale-baseline self-healing.
        # Original condition only re-snapped when baseline_net == 0, which
        # missed the case where a daily reconcile sets baseline_net to a
        # non-zero value and subsequent fills cause growing drift. Now
        # re-snap whenever the baseline is older than 60 seconds AND the
        # net position has changed since the baseline was taken. This
        # catches both the reconcile case and the new-fills case.
        _baseline_age = time.time() - (self._position_baseline_at or 0)
        _net_changed = net_position_cat != baseline_net
        if (
            _net_changed
            and _baseline_age > 60
            and self._position_baseline_cat is not None
        ):
            # Re-snap both the wallet balance and the net-position baselines.
            self._position_baseline_cat = _current_cat
            self._position_baseline_net_cat = Decimal(str(net_position_cat or 0))
            self._position_baseline_at = time.time()
            log_event(
                "info",
                "position_baseline_resnap",
                f"Position sanity baseline re-snapped (age={_baseline_age:.0f}s, "
                f"baseline_net={baseline_net:+.2f} -> all-time net="
                f"{net_position_cat:+.2f}). New baseline: wallet="
                f"{_current_cat:.2f}, net={net_position_cat:+.2f}.",
            )
            return  # Don't run the drift check this tick; next tick uses fresh baseline

        # Expected current = wallet baseline + changes since baseline
        # If we bought 100 CAT since session start, wallet should be baseline+100
        expected_current = self._position_baseline_cat + net_position_since_baseline
        delta = _current_cat - expected_current

        # Tolerance: ±5% of max position (configured), with a minimum
        # absolute floor for tiny positions
        try:
            max_pos_xch = Decimal(str(getattr(cfg, "MAX_POSITION_XCH", "5") or "5"))
        except Exception:
            max_pos_xch = Decimal("5")
        try:
            mid = Decimal(str(getattr(self, "_current_mid_price", 0) or 0))
        except Exception:
            mid = Decimal("0")
        if mid > 0:
            max_pos_cat = max_pos_xch / mid
        else:
            max_pos_cat = Decimal("1000")
        tolerance = max_pos_cat * Decimal("0.05")
        # Floor: at least 100 CAT to avoid noise on tiny positions
        if tolerance < Decimal("100"):
            tolerance = Decimal("100")

        if abs(delta) > tolerance:
            deposit_match = _find_cat_deposit_for_position_delta(
                delta,
                _scale,
                getattr(self, "_position_baseline_at", 0) or 0,
            )
            if deposit_match is not None:
                try:
                    amount_cat = Decimal(str(deposit_match["amount_mojos"])) / _scale
                except Exception:
                    amount_cat = delta
                coin_id = str(deposit_match.get("coin_id") or "")
                short_id = coin_id[:12] + "..." if coin_id else "unknown coin"
                self._position_baseline_cat = _current_cat
                self._position_baseline_net_cat = Decimal(str(net_position_cat or 0))
                self._position_baseline_at = time.time()
                log_event(
                    "info",
                    "position_sanity_external_deposit",
                    f"Wallet CAT balance changed by {delta:+.2f} CAT, "
                    f"matching external deposit {short_id} "
                    f"({amount_cat:.3f} CAT). Re-snapped position sanity "
                    f"baseline instead of treating it as missing PnL.",
                )
                try:
                    self._clear_alert("position_sanity")
                except Exception:
                    pass
                return

            log_event(
                "info",
                "position_sanity_drift",
                f"Position sanity check: bot estimate since baseline "
                f"{net_position_since_baseline:+.2f} CAT (all-time "
                f"{net_position_cat:+.2f}), wallet delta {delta:+.2f} CAT "
                f"(tolerance ±{tolerance:.0f}). This usually means the bot "
                f"silently missed a fill (likely a Spacescan-deferred fill "
                f"that never confirmed) or a phantom fill was recorded. "
                f"Check fills table for the gap.",
            )
            try:
                self._emit_alert(
                    "position_sanity",
                    "warning",
                    "Position drift detected",
                    f"The bot's tracked position differs from your wallet's actual "
                    f"CAT balance change by {delta:+.0f} CAT. Some fills may be "
                    f"missing from PnL. Run a manual reconciliation.",
                    action="run_doctor",
                    action_label="Run Doctor",
                )
            except Exception:
                pass
        else:
            try:
                self._clear_alert("position_sanity")
            except Exception:
                pass

    def _repair_unlinked_offer_coins(self) -> None:
        """F23: repair offer-coin links that didn't fully complete.

        add_offer + lock_coin run as separate DB transactions in
        offer_manager.create_ladder. If lock_coin fails (lock contention,
        unique violation, etc.), the offer row has a coin_id but the
        coin row doesn't have status='locked'. The fill detector still
        works because it walks via offers.coin_id, but coin_manager
        thinks the coin is free → may try to use it for another offer.

        This sweep scans for offers where status='open' and the
        referenced coin is NOT marked locked, and re-runs lock_coin.
        """
        try:
            from database import get_connection, lock_coin, update_offer_status
        except ImportError:
            return

        try:
            conn = get_connection()
            rows = conn.execute(
                """
                SELECT o.trade_id, o.coin_id, o.side
                FROM offers o
                LEFT JOIN coins c ON o.coin_id = c.coin_id
                WHERE o.status = 'open'
                  AND o.coin_id IS NOT NULL
                  AND o.coin_id != ''
                  AND (c.status IS NULL OR c.status != 'locked')
                LIMIT 50
                """
            ).fetchall()
        except Exception as e:
            log_event(
                "debug", "offer_coin_repair_query_failed", f"Repair query failed: {e}"
            )
            return

        if not rows:
            return

        # F62 (2026-04-09): before re-locking, check whether the wallet still
        # knows about this offer. The old repair loop re-locked blindly, which
        # created an infinite self-healing cycle whenever the wallet had
        # dropped an offer but the DB still had it open: Pass 2 of the Sage
        # reconciler would free the orphan lock on every cycle, then this
        # repair would re-lock it, then Pass 2 would free it again. The two
        # sniper offers at startup were stuck in this loop for ~45 minutes.
        #
        # Fix: snapshot the wallet's open trade_ids once up front. If an
        # offer is in our DB but the wallet doesn't know about it, the offer
        # is dead (cancelled/filled/expired and the wallet is the source of
        # truth), so mark it cancelled in the DB instead of re-locking its
        # coin. Otherwise, re-lock as before.
        wallet_open_ids = None
        try:
            from wallet import get_all_offers

            _open = get_all_offers(include_completed=False, start=0, end=500) or []
            wallet_open_ids = {
                (o.get("trade_id") or "").lower()
                for o in _open
                if o.get("trade_id")
                and str(o.get("status", "")).lower()
                not in ("cancelled", "canceled", "completed", "expired", "failed")
            }
        except Exception:
            wallet_open_ids = None  # Unknown — fall back to old behaviour

        repaired = 0
        orphan_closed = 0
        orphan_closed_by_side = {"buy": 0, "sell": 0}
        for row in rows:
            tid = row["trade_id"]
            cid = row["coin_id"]
            side = str(row["side"] or "").lower()
            if not tid or not cid:
                continue
            # If we have a reliable wallet snapshot and the offer isn't there,
            # it's a dead offer — close it instead of relocking.
            if wallet_open_ids is not None and tid.lower() not in wallet_open_ids:
                try:
                    update_offer_status(tid, "cancelled")
                    orphan_closed += 1
                    if side in orphan_closed_by_side:
                        orphan_closed_by_side[side] += 1
                except Exception:
                    pass
                continue
            try:
                if lock_coin(cid, tid):
                    repaired += 1
            except Exception:
                pass

        if repaired > 0:
            log_event(
                "info",
                "offer_coin_link_repaired",
                f"Repaired {repaired} offer-coin link(s) where the offer "
                f"existed but the coin row wasn't marked locked. Indicates "
                f"a previous lock_coin call failed silently.",
            )
        if orphan_closed > 0:
            retry_backoff = max(
                0.0,
                float(getattr(cfg, "DB_ONLY_OFFER_RETRY_BACKOFF_SECS", 300) or 300),
            )
            if retry_backoff > 0:
                now = time.time()
                for side, count in orphan_closed_by_side.items():
                    if count <= 0:
                        continue
                    current_until = float(
                        self._adaptive_target_backoff_until.get(side, 0.0) or 0.0
                    )
                    self._adaptive_target_backoff_until[side] = max(
                        current_until,
                        now + retry_backoff,
                    )
            log_event(
                "info",
                "offer_coin_orphan_closed",
                f"Closed {orphan_closed} DB offer(s) that no longer exist "
                f"in the wallet — breaks the re-lock/free loop on dead "
                f"sniper/ladder offers.",
                data={
                    "buy": orphan_closed_by_side["buy"],
                    "sell": orphan_closed_by_side["sell"],
                    "adaptive_backoff_secs": retry_backoff,
                },
            )

    def _maybe_run_daily_reconcile(self) -> None:
        """Run a deep DB↔wallet reconciliation once per 24 hours.

        Backfills missing fills via the existing `backfill_verified_fills_from_offers`
        helper, then logs a delta summary. Does NOT auto-correct anything
        beyond what the existing backfill function already does (idempotent
        record_fill calls for offers marked filled).
        """
        now = time.time()
        last = float(getattr(self, "_last_daily_reconcile_at", 0) or 0)
        if last and (now - last) < 86400:  # 24 hours
            return

        log_event(
            "info", "daily_reconcile_start", "Daily DB↔wallet reconciliation starting"
        )
        self._last_daily_reconcile_at = now

        try:
            run_history_cutoff = getattr(cfg, "RUN_HISTORY_CUTOFF", None)
            backfilled = backfill_verified_fills_from_offers(
                limit=200,
                since=run_history_cutoff,
            )
            backfilled = backfilled or []
            # F48 (2026-04-09): distinguish between newly-inserted rows and
            # existing rows whose verification_status was upgraded from
            # 'legacy' to 'verified'. The old code lumped them together and
            # logged "backfilled N missing fill rows" even when nothing new
            # was inserted — misleading for the operator reviewing logs.
            created_count = sum(1 for r in backfilled if r.get("created"))
            upgraded_count = sum(1 for r in backfilled if r.get("upgraded"))
            if created_count > 0:
                log_event(
                    "info",
                    "daily_reconcile_backfilled",
                    f"Daily reconcile: backfilled {created_count} fill records "
                    f"for historical offers. PnL tracking is now up to date.",
                )
            if upgraded_count > 0:
                log_event(
                    "info",
                    "daily_reconcile_upgraded",
                    f"Daily reconcile upgraded verification_status on "
                    f"{upgraded_count} legacy fill row(s) to 'verified'. "
                    f"No new rows inserted.",
                )
            if created_count == 0 and upgraded_count == 0:
                log_event(
                    "info",
                    "daily_reconcile_clean",
                    "Daily reconcile: no missing fills found (PnL is in sync)",
                )

            # F62 (2026-04-09): if the backfill changed the all-time net
            # position, the position-sanity baseline snapshot is now stale.
            # The baseline was taken at session start when the fills table
            # was still missing these rows, so it captured `baseline_net=0`.
            # Leaving it stale makes `_check_position_sanity` fire every
            # cycle with a phantom "wallet delta" equal to whatever the
            # reconcile added. Reset it so the next housekeeping tick
            # re-snaps against the fresh (correct) all-time position.
            if created_count > 0 or upgraded_count > 0:
                try:
                    self.risk_manager.update_inventory()  # refresh net_position_cat
                except Exception:
                    pass
                self._position_baseline_cat = None
                self._position_baseline_net_cat = None
                self._position_baseline_at = None
                log_event(
                    "info",
                    "position_baseline_invalidated",
                    "Position sanity baseline cleared after reconcile "
                    "updated the fills table — will re-snap next tick.",
                )
        except Exception as _bf_err:
            log_event(
                "warning",
                "daily_reconcile_backfill_failed",
                f"Daily reconcile backfill step failed: {_bf_err}",
            )

        # Sanity check: number of open offers in DB vs in wallet
        try:
            from database import get_open_offers as _ro_get_open_offers

            db_open = _ro_get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
            db_count = len(db_open)
            wallet_offers = get_all_offers(include_completed=False, start=0, end=500)
            if wallet_offers:
                terminal_statuses = (
                    "cancelled",
                    "canceled",
                    "completed",
                    "expired",
                    "failed",
                )
                open_wallet_offers = [
                    o
                    for o in wallet_offers
                    if str(o.get("status", "")).lower() not in terminal_statuses
                ]
                wallet_open = len(open_wallet_offers)
                wallet_open_ids = {
                    str(
                        o.get("trade_id") or o.get("offer_id") or o.get("id") or ""
                    ).lower()
                    for o in open_wallet_offers
                }
                if abs(db_count - wallet_open) > 2:
                    grace = max(
                        15.0,
                        float(
                            getattr(cfg, "DB_ONLY_OFFER_CONFIRM_GRACE_SECS", 90) or 90
                        ),
                    )
                    now_ts = time.time()
                    recent_db_only = []
                    for offer in db_open:
                        tid = str((offer or {}).get("trade_id") or "").lower()
                        if not tid or tid in wallet_open_ids:
                            continue
                        if str((offer or {}).get("tier") or "").lower() in (
                            "sniper",
                            "boost",
                        ):
                            continue
                        if self._offer_age_seconds(offer, now_ts) < grace:
                            recent_db_only.append(tid)
                    db_excess = max(0, db_count - wallet_open)
                    if db_excess > 0 and len(recent_db_only) >= db_excess:
                        log_event(
                            "info",
                            "daily_reconcile_count_pending_visibility",
                            f"Daily reconcile: DB has {db_count} open offers, wallet has "
                            f"{wallet_open}; {len(recent_db_only)} fresh DB-only offer(s) "
                            "are still inside wallet visibility grace.",
                            data={
                                "db_count": db_count,
                                "wallet_open": wallet_open,
                                "recent_db_only": len(recent_db_only),
                                "grace_secs": round(grace, 1),
                            },
                        )
                    else:
                        log_event(
                            "warning",
                            "daily_reconcile_count_mismatch",
                            f"Daily reconcile: DB has {db_count} open offers, wallet has "
                            f"{wallet_open}. Drift suggests DB is out of sync. "
                            f"Recommend investigating offers table.",
                        )
                else:
                    log_event(
                        "info",
                        "daily_reconcile_count_ok",
                        f"Daily reconcile count check OK: DB={db_count}, "
                        f"wallet={wallet_open}",
                    )
        except Exception as _cnt_err:
            log_event(
                "debug",
                "daily_reconcile_count_skipped",
                f"Daily reconcile count check skipped: {_cnt_err}",
            )

        # F24 (2026-04-08): daily Spacescan fill spot-check.
        # Pick a random sample of recent fills from the DB and re-verify
        # them against Spacescan. If any verification disagrees with the
        # DB record, alert — that's evidence of a phantom fill that
        # slipped past the verification gate.
        if getattr(cfg, "SPACESCAN_ENABLED", False):
            try:
                self._spot_check_recent_fills()
            except Exception as _sc_err:
                log_event(
                    "debug",
                    "daily_spot_check_failed",
                    f"Daily Spacescan spot-check failed: {_sc_err}",
                )

    def _spot_check_recent_fills(self) -> None:
        """F24 (2026-04-08): random Spacescan re-verification of recent fills.

        Picks a random sample of 5 fills from the last 24h that were
        recorded with verification_status='verified', then asks Spacescan
        to confirm them again. If any disagree (Spacescan says the coin
        wasn't actually spent, or was spent to ourselves), it's a phantom
        fill that slipped past the original verification gate.
        """
        try:
            from database import get_connection
            from spacescan import verify_fill as _verify_fill
            from wallet import get_first_address
            import random as _random
        except Exception:
            return

        try:
            conn = get_connection()
            rows = conn.execute(
                """
                SELECT f.fill_id, f.trade_id, f.side, f.price_xch
                FROM fills f
                LEFT JOIN offers o ON f.trade_id = o.trade_id
                WHERE f.verification_status = 'verified'
                  AND f.filled_at > datetime('now', '-1 day')
                  AND o.coin_id IS NOT NULL
                ORDER BY f.fill_id DESC
                LIMIT 100
                """
            ).fetchall()
        except Exception:
            return

        if not rows:
            log_event(
                "debug",
                "spot_check_skip_no_fills",
                "Spot-check skipped: no fills in last 24h",
            )
            return

        sample_size = min(5, len(rows))
        sample = _random.sample(list(rows), sample_size)

        # Get our wallet address for self-spend detection
        try:
            our_address = get_first_address(cfg.WALLET_ID_XCH) or ""
        except Exception:
            our_address = ""

        if not our_address:
            log_event(
                "debug",
                "spot_check_skip_no_address",
                "Spot-check skipped: could not determine wallet address",
            )
            return

        verified = 0
        phantom = 0
        unknown = 0
        for row in sample:
            tid = row["trade_id"]
            try:
                # Look up coin_id for this trade
                coin_row = conn.execute(
                    "SELECT coin_id FROM offers WHERE trade_id=?", (tid,)
                ).fetchone()
                if not coin_row or not coin_row["coin_id"]:
                    continue
                result = _verify_fill(coin_row["coin_id"], our_address)
                if result is True:
                    verified += 1
                elif result is False:
                    phantom += 1
                    log_event(
                        "error",
                        "spot_check_phantom_detected",
                        f"PHANTOM FILL detected by spot-check: trade {tid[:16]}... "
                        f"is recorded as a verified fill but Spacescan says the "
                        f"coin was either unspent or spent back to us. Investigate "
                        f"the original verification path — this fill should NOT "
                        f"be in PnL.",
                        data={
                            "trade_id": tid,
                            "fill_id": row["fill_id"],
                            "side": row["side"],
                        },
                    )
                else:
                    unknown += 1
            except Exception:
                unknown += 1

        log_event(
            "info",
            "daily_spot_check_done",
            f"Daily Spacescan fill spot-check: {verified} verified, "
            f"{phantom} phantoms, {unknown} unknown out of {sample_size} sampled "
            f"(of {len(rows)} eligible fills in last 24h)",
        )

        if phantom > 0:
            try:
                self._emit_alert(
                    "spot_check_phantom",
                    "error",
                    f"{phantom} phantom fill(s) detected",
                    f"The daily fill audit found {phantom} fills marked verified "
                    f"in the database that Spacescan disagrees with. PnL may be "
                    f"inflated. Run a manual reconciliation.",
                    action="run_doctor",
                    action_label="Run Doctor",
                )
            except Exception:
                pass

    def _start_health_monitor(self) -> None:
        """(Re)start the Sage health monitor thread. Idempotent."""
        if self._health_thread and self._health_thread.is_alive():
            return
        self._health_thread = threading.Thread(
            target=self._health_monitor_thread,
            daemon=True,
            name="bot-health-watch",
        )
        self._health_thread.start()

    def _start_price_watcher(self) -> None:
        """(Re)start the price watcher thread. Idempotent."""
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._watcher_thread = threading.Thread(
            target=self._price_watcher_thread,
            daemon=True,
            name="price-watcher",
        )
        self._watcher_thread.start()

    def _start_coin_watcher(self) -> None:
        """(Re)start the coin watcher thread. Idempotent."""
        if self._coin_watcher_thread and self._coin_watcher_thread.is_alive():
            return
        self._coin_watcher_thread = threading.Thread(
            target=self._coin_watcher_thread_run,
            daemon=True,
            name="coin-watcher",
        )
        self._coin_watcher_thread.start()

    # -------------------------------------------------------------------
    # Dexie Repost (V1 parity)
    # -------------------------------------------------------------------

    def _schedule_repost_active_offers_to_dexie(
        self,
        reason: str = "startup_resume",
        total_offers: Optional[int] = None,
    ) -> bool:
        """Run Dexie/Splash visibility confirmation in a background thread."""
        if not cfg.DEXIE_AUTO_POST:
            return False

        with self._startup_repost_lock:
            if self._startup_repost_thread and self._startup_repost_thread.is_alive():
                return False

            if reason == "startup_resume":
                msg = (
                    f"Checking {int(total_offers or 0)} existing offers on Dexie in the background "
                    "while the bot resumes"
                )
            elif reason == "connectivity_recovery":
                msg = "Pricing recovered — checking existing offers on Dexie in the background"
            else:
                msg = "Checking existing offers on Dexie in the background"
            log_event("info", "dexie_repost_background", msg)

            if getattr(cfg, "SPLASH_ENABLED", False):
                log_event(
                    "info",
                    "splash_repost_background",
                    "Confirming existing offers over Splash in the background",
                )

            def _worker():
                try:
                    if reason == "startup_resume":
                        self._startup_complete.wait(timeout=120)
                    if not self._running:
                        return
                    self._repost_active_offers_to_dexie(
                        reason=reason,
                        background=True,
                        total_offers=total_offers,
                    )
                finally:
                    with self._startup_repost_lock:
                        self._startup_repost_thread = None

            self._startup_repost_thread = threading.Thread(
                target=_worker,
                daemon=True,
                name="dexie-repost",
            )
            self._startup_repost_thread.start()
            return True

    def _repost_active_offers_to_dexie(
        self,
        reason: str = "startup_resume",
        background: bool = False,
        total_offers: Optional[int] = None,
    ):
        """Re-post all active wallet offers to Dexie.

        OPTIMISED (V3): reads bech32 strings from database instead of calling
        wallet RPC per offer (~2s each). Only offers missing a bech32 fall back
        to wallet RPC. Offers already mapped to a Dexie ID are skipped entirely
        (they're already live on Dexie). Dexie posting uses concurrent workers.

        Startup time improvement: ~200s → ~10-15s for 98 offers.

        Called on startup and after connectivity recovery.
        """
        if not cfg.DEXIE_AUTO_POST:
            return
        if not self._running:
            return

        try:
            from database import get_offers_for_repost

            # Get open offers with their stored bech32 strings from DB
            cat_id = cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else ""
            db_offers = get_offers_for_repost(cat_asset_id=cat_id)

            if not db_offers:
                # Fallback: sync from wallet (first run or empty DB)
                open_buys, open_sells, _ = self.offer_manager.sync_from_wallet()
                all_open = open_buys + open_sells
                if not all_open:
                    return
                # Use legacy path for offers without DB bech32
                db_offers = [
                    {
                        "trade_id": o.get("trade_id", ""),
                        "offer_bech32": None,
                        "dexie_id": None,
                        "side": o.get("side", ""),
                    }
                    for o in all_open
                ]

            # Split into: already posted (skip), have bech32 (fast), need RPC (slow)
            skip_count = 0
            fast_queue = []  # Have bech32 in DB, just need to post to Dexie
            slow_queue = []  # Missing bech32, need wallet RPC

            for offer in db_offers:
                trade_id = offer.get("trade_id", "")
                if not trade_id:
                    continue

                bech32 = offer.get("offer_bech32") or ""
                dexie_id = offer.get("dexie_id") or ""

                # If already posted to Dexie and we have the mapping, skip
                if dexie_id:
                    skip_count += 1
                    # Still register the fingerprint so flush_queue deduplicates
                    if bech32:
                        with self.dexie_manager._lock:
                            self.dexie_manager._posted_fingerprints.add(
                                self.dexie_manager._fingerprint(bech32)
                            )
                    continue

                if bech32:
                    fast_queue.append((trade_id, bech32))
                else:
                    slow_queue.append(trade_id)

            plan_message = (
                f"Repost plan: {skip_count} already on Dexie (skip), "
                f"{len(fast_queue)} from DB (fast), "
                f"{len(slow_queue)} need wallet RPC (slow)"
            )
            if background and reason == "startup_resume" and total_offers:
                plan_message = (
                    f"Resume visibility check for {int(total_offers)} offers — "
                    f"{skip_count} already mapped, {len(fast_queue)} fast, "
                    f"{len(slow_queue)} via wallet RPC"
                )
            log_event("info", "dexie_repost_plan", plan_message)

            # Fast path: queue all offers that have bech32 stored
            count = 0
            for trade_id, bech32 in fast_queue:
                self.dexie_manager.queue_post(bech32, trade_id, force=True)
                if getattr(cfg, "SPLASH_ENABLED", False):
                    self.splash_manager.queue_post(bech32, trade_id, force=True)
                count += 1

            # Slow path: fetch bech32 from wallet RPC for offers without it
            if slow_queue:
                from wallet import get_offer_bech32

                for trade_id in slow_queue:
                    try:
                        bech32 = get_offer_bech32(trade_id)
                        if bech32:
                            self.dexie_manager.queue_post(bech32, trade_id, force=True)
                            if getattr(cfg, "SPLASH_ENABLED", False):
                                self.splash_manager.queue_post(
                                    bech32, trade_id, force=True
                                )
                            count += 1
                            # Save to DB for next time
                            try:
                                from database import update_offer_bech32

                                update_offer_bech32(trade_id, bech32)
                            except Exception:
                                pass
                            time.sleep(0.3)  # Gentle on wallet RPC
                    except Exception as e:
                        log_event(
                            "debug",
                            "repost_error",
                            f"Error getting bech32 for {trade_id[:16]}...: {e}",
                        )

            if count > 0:
                self.dexie_manager.flush_queue(flush_all=True)
                log_event(
                    "info",
                    "dexie_repost_done",
                    f"Re-posted {count} offers to Dexie"
                    + (" in the background " if background else " ")
                    + f"({len(fast_queue)} fast + {len(slow_queue)} via RPC, "
                    f"{skip_count} already live)",
                )
                # Also broadcast to Splash if enabled (V3)
                if getattr(cfg, "SPLASH_ENABLED", False):
                    self.splash_manager.flush_queue(flush_all=True)
                    log_event(
                        "info",
                        "splash_repost_done",
                        f"Confirmed {count} offers over Splash"
                        + (" in the background" if background else ""),
                    )
            else:
                log_event(
                    "info",
                    "dexie_repost_done",
                    f"All {skip_count} offers already live on Dexie — nothing to repost",
                )

        except Exception as e:
            log_event("error", "dexie_repost_failed", f"Dexie repost failed: {e}")

    # -------------------------------------------------------------------
    # Health Monitor Thread (V1 parity)
    # -------------------------------------------------------------------

    def _check_sage_fingerprint_drift(self):
        """Detect when Sage's active fingerprint has drifted from the one
        CATalyst logged in with. This catches the case where the operator
        switches wallets inside Sage without stopping the bot.
        """
        try:
            import sage_node
            from wallet_sage import get_current_key
        except Exception:
            return

        expected_fp = getattr(sage_node, "_selected_fingerprint", None)
        if not expected_fp:
            return

        key = get_current_key()
        live_fp = (
            str(key.get("fingerprint"))
            if key and key.get("fingerprint") is not None
            else None
        )
        if not live_fp:
            # Can't tell — Sage might be restarting. Leave the alert state
            # alone; downstream health checks will surface the outage.
            return

        if live_fp == expected_fp:
            if getattr(self, "_sage_fp_mismatch_active", False):
                log_event(
                    "success",
                    "sage_fingerprint_restored",
                    f"Sage active fingerprint back to {expected_fp}.",
                )
                self._clear_alert("sage_fingerprint_mismatch")
                self._sage_fp_mismatch_active = False
            return

        # Mismatch — emit once per entry into the mismatched state so the
        # log doesn't fill up every 15s.
        if not getattr(self, "_sage_fp_mismatch_active", False):
            log_event(
                "warning",
                "sage_fingerprint_changed_externally",
                f"Sage active fingerprint changed: expected {expected_fp}, now {live_fp}. "
                "Signing calls will fail until restored.",
            )
            self._sage_fp_mismatch_active = True

        self._emit_alert(
            "sage_fingerprint_mismatch",
            "warning",
            "Sage Wallet Switched",
            f"Sage's active key changed from fingerprint {expected_fp} to {live_fp}. "
            "Switch it back in Sage, or stop the bot and re-select the wallet here. "
            "Until then, fills, cancels, and new offers will fail.",
            action="restart_sage",
            action_label="Open Sage",
        )

    def _health_monitor_thread(self):
        """Background thread — polls Chia wallet & node sync every 15s.

        V1 had this as _health_monitor_thread(). After 5 minutes of consecutive
        unhealthy status, it logs a critical warning. (Auto-restart of Chia
        services requires platform-specific commands, so we log + emit events
        for the GUI to display rather than attempting blind restarts.)

        The thread exits when self._running becomes False.
        """
        # Wait for startup_sync to finish before writing to DB
        slog("THREAD", "health-monitor waiting for startup_complete gate...")
        self._startup_complete.wait(timeout=120)
        slog("THREAD", "health-monitor gate released — starting work")
        log_thread_start("health-monitor")
        startup_wallet_type = (
            str(os.getenv("WALLET_TYPE", "sage") or "sage").strip().lower()
        )
        monitor_label = (
            "Sage wallet" if startup_wallet_type == "sage" else "Chia wallet/full node"
        )
        log_event(
            "info",
            "health_monitor",
            f"{monitor_label} health monitor started (checking every 15s)",
        )

        while self._running:
            try:
                # Sage-only: detect if the user changed the active fingerprint
                # in Sage outside of CATalyst. The old session keeps 401-ing
                # for signing calls, which downstream surfaces as "balance=0
                # → reserve_floor_breached → mass cancel." Catching the
                # mismatch here gives the user the real cause first.
                if startup_wallet_type == "sage":
                    self._check_sage_fingerprint_drift()

                health = get_chia_health()

                wallet_info = health.get("wallet", {}) or {}
                node_info = health.get("node", {}) or {}
                wallet_type = (
                    str(
                        health.get("wallet_type")
                        or os.getenv("WALLET_TYPE", "sage")
                        or "sage"
                    )
                    .strip()
                    .lower()
                )
                wallet_sync_state = (
                    str(wallet_info.get("sync_state") or "").strip().lower()
                )
                # Only treat Sage as "service ok" when the RPC explicitly
                # says synced. Previously "unknown" was accepted which let
                # a failing get_sync_status RPC look healthy — operators
                # could miss wallet outages entirely. A Sage wallet that
                # genuinely has nothing to sync (empty) will still flow
                # through the node/peer/fill-tracker checks and is not
                # relevant for live-operation health anyway.
                sage_wallet_service_ok = (
                    wallet_type == "sage"
                    and bool(wallet_info.get("reachable"))
                    and wallet_sync_state == "synced"
                )
                effective_status = health.get("status", "unknown")
                effective_healthy = (
                    bool(health.get("healthy")) or sage_wallet_service_ok
                )

                with self._chia_health_lock:
                    self._chia_health["status"] = effective_status
                    self._chia_health["wallet_sync_state"] = (
                        wallet_sync_state or "unknown"
                    )
                    self._chia_health["wallet_reachable"] = wallet_info.get(
                        "reachable", False
                    )
                    self._chia_health["wallet_synced"] = wallet_info.get(
                        "synced", False
                    )
                    self._chia_health["node_synced"] = node_info.get("synced", False)
                    self._chia_health["last_check"] = time.time()

                if effective_healthy:
                    if self._consecutive_unhealthy > 0:
                        was_down = (
                            self._consecutive_unhealthy * self._health_check_interval
                        )
                        service_label = (
                            "Sage wallet" if wallet_type == "sage" else "Chia services"
                        )
                        log_event(
                            "success",
                            "chia_recovered",
                            f"{service_label} recovered (was unhealthy for {was_down}s)",
                        )
                        self._emit(
                            "health", {"status": "recovered", "downtime_secs": was_down}
                        )
                    self._consecutive_unhealthy = 0
                    with self._chia_health_lock:
                        self._chia_health["consecutive_failures"] = 0
                else:
                    self._consecutive_unhealthy += 1
                    with self._chia_health_lock:
                        self._chia_health["consecutive_failures"] = (
                            self._consecutive_unhealthy
                        )
                    secs = self._consecutive_unhealthy * self._health_check_interval

                    # Build descriptive reason
                    reasons = []
                    if not wallet_info.get("reachable"):
                        reasons.append("wallet RPC unreachable")
                    elif wallet_type != "sage" and not wallet_info.get("synced"):
                        reasons.append("wallet not synced")
                    if wallet_type != "sage" and not node_info.get("reachable"):
                        reasons.append("full node unreachable")
                    elif wallet_type != "sage" and not node_info.get("synced"):
                        reasons.append("full node not synced")
                    if wallet_type == "sage" and wallet_sync_state == "no_peers":
                        reasons.append("no network peers")
                    reason_str = " — " + ", ".join(reasons) if reasons else ""

                    # Sage-specific: no_peers gets its own prominent alert immediately
                    # (cycle 1) because it silently blocks fills and TX submissions.
                    if wallet_type == "sage" and wallet_sync_state == "no_peers":
                        if self._consecutive_unhealthy == 1:
                            log_event(
                                "warning",
                                "wallet_no_peers",
                                "Sage wallet has no network peers — fill detection "
                                "and offer submission are paused.",
                            )
                        self._emit_alert(
                            "wallet_no_peers",
                            "warning",
                            "Sage Wallet: No Network Peers",
                            "Sage has lost all peer connections. Fills cannot be detected "
                            "and new offers cannot be submitted. Check your internet "
                            "connection or restart Sage.",
                            action="restart_sage",
                            action_label="Restart Sage",
                        )
                    else:
                        self._clear_alert("wallet_no_peers")

                    if self._consecutive_unhealthy == 1:
                        health_label = (
                            "Sage wallet" if wallet_type == "sage" else "Chia health"
                        )
                        log_event(
                            "warning",
                            "chia_unhealthy",
                            f"{health_label}: {effective_status}{reason_str}",
                        )
                    elif (
                        self._consecutive_unhealthy >= 4
                        and self._consecutive_unhealthy % 4 == 0
                    ):
                        still_label = "Sage wallet" if wallet_type == "sage" else "Chia"
                        log_event(
                            "warning",
                            "chia_still_unhealthy",
                            f"{still_label} still unhealthy ({secs}s){reason_str}",
                        )

                    # After threshold, emit critical alert
                    if secs >= self._auto_restart_threshold:
                        time_since_last = time.time() - self._last_auto_restart_time
                        if time_since_last >= self._auto_restart_cooldown:
                            self._last_auto_restart_time = time.time()
                            self._consecutive_unhealthy = 0
                            restart_label = (
                                "Sage wallet" if wallet_type == "sage" else "Chia"
                            )
                            log_event(
                                "error",
                                "chia_restart_needed",
                                f"{restart_label} unhealthy for {secs}s ({', '.join(reasons) or 'unknown'}) "
                                f"— manual restart recommended",
                            )
                            self._emit(
                                "health",
                                {
                                    "status": "restart_needed",
                                    "unhealthy_secs": secs,
                                    "reasons": reasons,
                                },
                            )

                    self._emit(
                        "health",
                        {
                            "status": effective_status,
                            "consecutive_failures": self._consecutive_unhealthy,
                            "unhealthy_secs": secs,
                        },
                    )

            except Exception as e:
                log_event("debug", "health_error", f"Health check error: {e}")

            # Sleep in 1s increments so we can exit promptly on stop
            for _ in range(self._health_check_interval):
                if not self._running:
                    break
                time.sleep(1)

        log_event("info", "health_monitor_exit", "Health monitor stopped")

    # -------------------------------------------------------------------
    # Price Watcher Thread (V1 parity)
    # -------------------------------------------------------------------

    def _price_watcher_thread(self):
        """Background thread — polls TibetSwap reserves every 12s.

        When reserves change (= someone swapped on the AMM), this thread:
          1. Sets a flag with swap details
          2. Invalidates the Tibet cache (so price_engine gets fresh data)
          3. Wakes the main bot loop immediately via threading.Event

        Net effect: arb exposure window drops from ~90s to ~12s.

        V1 had this as _price_watcher_thread().
        """
        # Wait for startup_sync to finish before polling prices
        slog("THREAD", "price-watcher waiting for startup_complete gate...")
        self._startup_complete.wait(timeout=120)
        slog("THREAD", "price-watcher gate released — starting work")
        log_thread_start("price-watcher")

        if not cfg.CAT_ASSET_ID:
            log_event(
                "info", "watcher_disabled", "Price watcher disabled — no CAT_ASSET_ID"
            )
            return

        log_event(
            "info",
            "watcher_started",
            f"Fast price watcher active (polling Tibet every {self._watcher_interval}s)",
        )

        consecutive_errors = 0
        session = requests.Session()

        while self._running:
            try:
                # Only poll when bot is running
                if not self._bot_state.get("running"):
                    time.sleep(5)
                    continue

                # Fetch Tibet reserves directly (lightweight — just one pair)
                xch_res, token_res = self._fetch_tibet_reserves(session)

                if xch_res is None or token_res is None:
                    consecutive_errors += 1
                    if consecutive_errors >= 5 and consecutive_errors % 10 == 0:
                        log_event(
                            "debug",
                            "watcher_error",
                            f"Tibet API unreachable ({consecutive_errors} consecutive failures)",
                        )
                    time.sleep(self._watcher_interval)
                    continue

                consecutive_errors = 0

                is_baseline = False
                with self._watcher_lock:
                    self._watcher_data["polls"] += 1
                    prev_xch = self._watcher_data["last_xch_reserve"]
                    prev_token = self._watcher_data["last_token_reserve"]

                    # First poll — just store baseline (sleep OUTSIDE lock)
                    if prev_xch == 0 or prev_token == 0:
                        self._watcher_data["last_xch_reserve"] = xch_res
                        self._watcher_data["last_token_reserve"] = token_res
                        is_baseline = True

                    if not is_baseline:
                        # Compare reserves
                        if prev_xch > 0 and prev_token > 0:
                            xch_change = abs(xch_res - prev_xch) / prev_xch * 100
                            token_change = (
                                abs(token_res - prev_token) / prev_token * 100
                            )
                            change_pct = max(xch_change, token_change)

                            if change_pct >= self._watcher_min_change_pct:
                                direction = (
                                    "buy_pressure"
                                    if xch_res > prev_xch
                                    else "sell_pressure"
                                )

                                self._watcher_data["triggered"] = True
                                self._watcher_data["change_pct"] = change_pct
                                self._watcher_data["direction"] = direction
                                self._watcher_data["last_change_ts"] = time.time()
                                self._watcher_data["triggers"] += 1

                                swap_msg = (
                                    f"🔔 Tibet swap detected! Reserves moved {change_pct:.3f}% "
                                    f"({direction}) — waking bot for immediate requote"
                                )
                                print(swap_msg, flush=True)
                                log_event("info", "tibet_swap_detected", swap_msg)

                                # Invalidate Tibet cache so price_engine fetches fresh
                                try:
                                    self.price_engine.invalidate_tibet_cache()
                                except Exception:
                                    pass

                                # Wake up the main loop!
                                self._watcher_event.set()

                        # Always update stored reserves
                        self._watcher_data["last_xch_reserve"] = xch_res
                        self._watcher_data["last_token_reserve"] = token_res

                if is_baseline:
                    time.sleep(self._watcher_interval)
                    continue

            except Exception as e:
                log_event("debug", "watcher_thread_error", f"Price watcher error: {e}")

            time.sleep(self._watcher_interval)

        log_event("info", "watcher_exit", "Price watcher stopped")

    def _fetch_tibet_reserves(self, session: requests.Session):
        """Fetch TibetSwap reserves directly (lightweight).

        Returns (xch_reserve, token_reserve) or (None, None) on failure.
        """
        try:
            pair_info = self.price_engine.get_tibet_pool_info(cfg.CAT_ASSET_ID)
            if pair_info:
                xch_res = float(pair_info.get("xch_reserve", 0))
                token_res = float(pair_info.get("token_reserve", 0))
                if xch_res > 0 and token_res > 0:
                    return xch_res, token_res
        except Exception:
            pass

        # Fallback: direct API call
        try:
            url = f"{cfg.TIBET_API_BASE}/pairs"
            resp = session.get(url, params={"skip": 0, "limit": 100}, timeout=5)
            if resp.status_code == 200:
                pairs = resp.json()
                normalized = cfg.CAT_ASSET_ID.lower().strip()
                cat_decimals = int(getattr(cfg, "CAT_DECIMALS", 3) or 3)
                cat_scale = 10**cat_decimals
                for pair in pairs:
                    pair_asset = str(pair.get("short_name", "")).lower().strip()
                    pair_asset_id = str(pair.get("asset_id", "")).lower().strip()
                    # Exact match only — avoid zero-appending false matches.
                    if normalized in (pair_asset, pair_asset_id):
                        # API returns mojos — divide to match price_engine units
                        xch_res = float(pair.get("xch_reserve", 0)) / 1e12
                        token_res = float(pair.get("token_reserve", 0)) / cat_scale
                        if xch_res > 0 and token_res > 0:
                            return xch_res, token_res
        except Exception:
            pass

        return None, None

    # -------------------------------------------------------------------
    # Coin Watcher Thread (lifecycle tracking)
    # -------------------------------------------------------------------

    def _coin_watcher_thread_run(self):
        """Background thread — polls wallet coins every 12s for lifecycle tracking.

        Detects:
          1. NEW coins appearing (not in last snapshot)
          2. COINS DISAPPEARING (in last snapshot but gone now)
          3. STATUS CHANGES (DB status differs from expected)

        Thread is read-only — it only reads wallet + DB state and logs changes.
        The main bot loop remains the authority for DB writes.
        """
        # Wait for startup_sync to finish before polling coins
        slog("THREAD", "coin-watcher waiting for startup_complete gate...")
        self._startup_complete.wait(timeout=120)
        slog("THREAD", "coin-watcher gate released — starting work")
        log_thread_start("coin-watcher")
        log_event(
            "info",
            "coin_watcher_started",
            f"Coin watcher active (polling every {self._coin_watcher_interval}s)",
        )

        from coin_manager import _coin_id_from_record, _extract_coin_records
        from wallet import get_spendable_coins_rpc
        from database import get_all_coins_state

        while self._running:
            try:
                # Only poll when bot is running
                if not self._bot_state.get("running"):
                    for _ in range(5):
                        if not self._running:
                            break
                        time.sleep(1)
                    continue

                # Fetch current wallet state (2 RPC calls)
                xch_result = get_spendable_coins_rpc(cfg.WALLET_ID_XCH)
                cat_result = get_spendable_coins_rpc(cfg.CAT_WALLET_ID)
                snapshot_reliable = self._is_coin_watcher_snapshot_reliable(
                    xch_result, cat_result
                )
                xch_records = (
                    _extract_coin_records(xch_result) if snapshot_reliable else []
                )
                cat_records = (
                    _extract_coin_records(cat_result) if snapshot_reliable else []
                )

                # Build current wallet snapshot
                wallet_coins = {}
                for rec in xch_records:
                    cid = _coin_id_from_record(rec)
                    if cid:
                        coin = rec.get("coin", {})
                        wallet_coins[cid] = {
                            "amount": coin.get("amount", 0),
                            "wallet_type": "xch",
                            "source": "wallet",
                        }
                for rec in cat_records:
                    cid = _coin_id_from_record(rec)
                    if cid:
                        coin = rec.get("coin", {})
                        wallet_coins[cid] = {
                            "amount": coin.get("amount", 0),
                            "wallet_type": "cat",
                            "source": "wallet",
                        }

                # Get DB state (includes locked coins not visible in wallet)
                db_state = get_all_coins_state()

                # Build combined current snapshot (wallet + DB-locked)
                current_snapshot = dict(wallet_coins)
                for cid, info in db_state.items():
                    if info["status"] == "locked" and cid not in current_snapshot:
                        current_snapshot[cid] = {
                            "amount": info["amount_mojos"],
                            "wallet_type": info["wallet_type"],
                            "source": "db_locked",
                        }

                # Process the snapshot only when the wallet view is complete.
                self._handle_coin_watcher_snapshot(
                    current_snapshot, db_state, snapshot_reliable=snapshot_reliable
                )

            except Exception as e:
                log_event("debug", "coin_watcher_error", f"Coin watcher error: {e}")

            # Sleep in 1s increments for clean exit
            for _ in range(self._coin_watcher_interval):
                if not self._running:
                    break
                time.sleep(1)

        log_event("info", "coin_watcher_exit", "Coin watcher stopped")

    def _is_coin_watcher_snapshot_reliable(self, xch_result, cat_result) -> bool:
        """Return True when both wallet coin RPC responses are usable."""
        if self._wallet_sync_stale_cycle:
            return False

        for result in (xch_result, cat_result):
            if not isinstance(result, dict):
                return False
            if result.get("error") or result.get("success") is False:
                return False

        return True

    def _handle_coin_watcher_snapshot(
        self, current_snapshot: Dict, db_state: Dict, *, snapshot_reliable: bool
    ) -> None:
        """Log coin watcher changes without poisoning the baseline on stale RPC data."""
        if not snapshot_reliable:
            self._log_coin_watcher_stale_snapshot_skip()
            return

        if not self._coin_snapshot:
            xch_free = sum(
                1
                for c in current_snapshot.values()
                if c["wallet_type"] == "xch" and c.get("source") == "wallet"
            )
            xch_locked = sum(
                1
                for c in current_snapshot.values()
                if c["wallet_type"] == "xch" and c.get("source") == "db_locked"
            )
            cat_free = sum(
                1
                for c in current_snapshot.values()
                if c["wallet_type"] == "cat" and c.get("source") == "wallet"
            )
            cat_locked = sum(
                1
                for c in current_snapshot.values()
                if c["wallet_type"] == "cat" and c.get("source") == "db_locked"
            )
            xch_total_mojos = sum(
                c["amount"]
                for c in current_snapshot.values()
                if c["wallet_type"] == "xch"
            )
            cat_total_mojos = sum(
                c["amount"]
                for c in current_snapshot.values()
                if c["wallet_type"] == "cat"
            )
            log_event(
                "info",
                "coin_watcher_baseline",
                f"[CoinWatch] Baseline established: "
                f"XCH {xch_free} free + {xch_locked} locked "
                f"({xch_total_mojos / 1_000_000_000_000:.4f} XCH) | "
                f"CAT {cat_free} free + {cat_locked} locked "
                f"({cat_total_mojos} mojos) | "
                f"{len(current_snapshot)} coins tracked",
                data={
                    "xch_free": xch_free,
                    "xch_locked": xch_locked,
                    "xch_total_mojos": xch_total_mojos,
                    "cat_free": cat_free,
                    "cat_locked": cat_locked,
                    "cat_total_mojos": cat_total_mojos,
                    "total_tracked": len(current_snapshot),
                },
            )
            self._coin_snapshot = current_snapshot
            return

        changes = self._detect_coin_changes(
            self._coin_snapshot, current_snapshot, db_state
        )

        for change in changes:
            log_event(
                change["severity"],
                change["event_type"],
                change["message"],
                data=change.get("data"),
            )

        self._coin_snapshot = current_snapshot

    def _log_coin_watcher_stale_snapshot_skip(self) -> None:
        now = time.time()
        if now - self._coin_watcher_stale_log_last < 60:
            return

        self._coin_watcher_stale_log_last = now
        log_event(
            "info",
            "coin_watcher_stale_snapshot_skipped",
            "[CoinWatch] Skipping coin lifecycle check while wallet "
            "coin RPC data is stale or unavailable",
            data={"wallet_sync_stale": self._wallet_sync_stale_cycle},
        )

    def _detect_coin_changes(
        self, old_snapshot: Dict, new_snapshot: Dict, db_state: Dict
    ) -> list:
        """Compare old vs new coin snapshots and return change events.

        Returns list of dicts with severity, event_type, message.
        Skips the first poll (no old_snapshot to compare against).
        """
        if not old_snapshot:
            return []  # First poll — just establishing baseline

        changes = []

        # Detect NEW coins (in new snapshot, not in old)
        for cid, info in new_snapshot.items():
            if cid not in old_snapshot:
                wt = info["wallet_type"].upper()
                if info["wallet_type"] == "xch":
                    amt_str = f"{info['amount'] / 1_000_000_000_000:.4f} XCH"
                else:
                    amt_str = f"{info['amount']} mojos"
                # Check if this coin is in DB (has designation info)
                db_info = db_state.get(cid, {})
                desig = db_info.get("designation", "unknown")
                tier = db_info.get("assigned_tier", "none")
                changes.append(
                    {
                        "severity": "info",
                        "event_type": "coin_watcher_new",
                        "message": (
                            f"[CoinWatch] NEW {wt} coin {cid[:16]}... ({amt_str})"
                            f" | {desig}/{tier}"
                        ),
                        "data": {
                            "coin_id": cid,
                            "amount_mojos": info["amount"],
                            "wallet_type": info["wallet_type"],
                            "designation": desig,
                            "assigned_tier": tier,
                        },
                    }
                )

        # Detect DISAPPEARED coins (in old snapshot, not in new)
        for cid, info in old_snapshot.items():
            if cid not in new_snapshot:
                wt = info["wallet_type"].upper()
                if info["wallet_type"] == "xch":
                    amt_str = f"{info['amount'] / 1_000_000_000_000:.4f} XCH"
                else:
                    amt_str = f"{info['amount']} mojos"
                changes.append(
                    {
                        "severity": "info",
                        "event_type": "coin_watcher_gone",
                        "message": (
                            f"[CoinWatch] GONE {wt} coin {cid[:16]}... ({amt_str})"
                            f" — left the current wallet snapshot"
                        ),
                        "data": {
                            "coin_id": cid,
                            "amount_mojos": info["amount"],
                            "wallet_type": info["wallet_type"],
                        },
                    }
                )

        # Detect STATUS CHANGES (coin exists in both but source changed)
        for cid in new_snapshot:
            if cid in old_snapshot:
                old_src = old_snapshot[cid].get("source", "")
                new_src = new_snapshot[cid].get("source", "")
                if old_src != new_src:
                    wt = new_snapshot[cid]["wallet_type"].upper()
                    if new_snapshot[cid]["wallet_type"] == "xch":
                        amt_str = (
                            f"{new_snapshot[cid]['amount'] / 1_000_000_000_000:.4f} XCH"
                        )
                    else:
                        amt_str = f"{new_snapshot[cid]['amount']} mojos"
                    changes.append(
                        {
                            "severity": "info",
                            "event_type": "coin_watcher_status",
                            "message": (
                                f"[CoinWatch] {wt} coin {cid[:16]}... ({amt_str})"
                                f" source changed: {old_src} → {new_src}"
                            ),
                            "data": {
                                "coin_id": cid,
                                "amount_mojos": new_snapshot[cid]["amount"],
                                "wallet_type": new_snapshot[cid]["wallet_type"],
                                "old_source": old_src,
                                "new_source": new_src,
                            },
                        }
                    )

        return changes

    # -------------------------------------------------------------------
    # Orphan Cleanup — cancel wallet offers not tracked by the bot
    # -------------------------------------------------------------------

    def cleanup_orphaned_offers(self) -> Dict:
        """Find and cancel offers in the wallet that the bot doesn't track.

        This happens when cancel operations fail silently (Sage wallet
        removes them from its local list but the on-chain cancel didn't
        go through). The offers are still live on-chain and visible on
        Dexie, but the bot thinks they're gone.

        Returns dict with counts and details of what was found/cancelled.
        """
        from database import get_open_offers
        from wallet import get_all_offers, cancel_offers_batch

        log_event("info", "orphan_cleanup_start", "Starting orphaned offer cleanup...")

        result = {
            "wallet_count": 0,
            "db_count": 0,
            "orphan_count": 0,
            "orphan_ids": [],
            "cancel_results": {},
        }

        try:
            # Step 1: Get all active offers from wallet
            wallet_offers = get_all_offers(include_completed=False, start=0, end=500)
            if wallet_offers is None:
                log_event(
                    "warning", "orphan_cleanup_failed", "Could not fetch wallet offers"
                )
                result["error"] = "Wallet returned None"
                return result

            # Build set of wallet trade_ids
            wallet_ids = set()
            for o in wallet_offers:
                tid = o.get("trade_id", "") or o.get("offer_id", "")
                if tid:
                    wallet_ids.add(tid)
            result["wallet_count"] = len(wallet_ids)

            # Step 2: Get all DB-tracked open offers
            db_offers = get_open_offers()
            db_ids = set(o["trade_id"] for o in db_offers if o.get("trade_id"))
            result["db_count"] = len(db_ids)

            # Step 3: Find orphans (in wallet but not in DB)
            orphan_ids = list(wallet_ids - db_ids)
            result["orphan_count"] = len(orphan_ids)
            result["orphan_ids"] = [tid[:16] + "..." for tid in orphan_ids]

            log_event(
                "info",
                "orphan_cleanup_found",
                f"Found {len(orphan_ids)} orphaned offers "
                f"(wallet={len(wallet_ids)}, db={len(db_ids)})",
            )

            if not orphan_ids:
                log_event("info", "orphan_cleanup_done", "No orphaned offers found")
                return result

            # Step 4: Cancel orphans in small batches (3 at a time, 5s delay)
            # Uses the fixed confirmation logic that verifies on-chain
            BATCH_SIZE = 3
            BATCH_DELAY = 5.0
            total = len(orphan_ids)
            all_cancel_results = {}

            for i in range(0, total, BATCH_SIZE):
                batch = orphan_ids[i : i + BATCH_SIZE]
                done = min(i + BATCH_SIZE, total)
                log_event(
                    "info",
                    "orphan_cancel_batch",
                    f"Cancelling orphan batch {done}/{total}...",
                )

                batch_results = cancel_offers_batch(batch, secure=True)
                all_cancel_results.update(batch_results)

                # Summary for this batch
                successes = sum(
                    1 for r in batch_results.values() if r and r.get("success")
                )
                failures = len(batch_results) - successes
                log_event(
                    "info",
                    "orphan_cancel_batch_result",
                    f"Batch {done}/{total}: {successes} confirmed, {failures} failed",
                )

                if i + BATCH_SIZE < total:
                    time.sleep(BATCH_DELAY)

            result["cancel_results"] = {
                "total": total,
                "confirmed": sum(
                    1 for r in all_cancel_results.values() if r and r.get("success")
                ),
                "failed": sum(
                    1
                    for r in all_cancel_results.values()
                    if not r or not r.get("success")
                ),
            }

            log_event(
                "info",
                "orphan_cleanup_done",
                f"Orphan cleanup complete: "
                f"{result['cancel_results']['confirmed']}/{total} "
                f"confirmed cancelled, "
                f"{result['cancel_results']['failed']} failed",
            )

        except Exception as e:
            log_event("error", "orphan_cleanup_error", f"Cleanup failed: {e}")
            result["error"] = str(e)

        return result

    # -------------------------------------------------------------------
    # Graceful Config Migration (V2 — config-first, batched cancels)
    # -------------------------------------------------------------------

    def graceful_config_change(self, _new_config: Dict = None) -> Dict:
        """Handle config changes while maintaining market presence.

        V2 approach (improved from V1):
          1. Reload config FIRST — new settings take effect immediately
          2. Identify the 2 tightest offers per side ("core liquidity")
          3. Queue outer offers for batched cancellation in background thread
          4. Return immediately — bot loop creates new offers at new spread
             while old offers are cancelled gently in small batches

        This avoids:
          - BAD_AGGREGATE_SIGNATURE errors from mass cancellation
          - Market coverage gaps (config applies before any cancels)
          - Blocking the API thread for minutes during cancel polling

        Called from the /api/config/apply and /api/config/live routes.
        Returns dict with status and details.
        """
        if not self._running:
            return {
                "status": "error",
                "message": "Bot not running — just restart with new config",
            }

        # Guard against concurrent graceful applies (e.g. rapid slider changes)
        if getattr(self, "_graceful_in_progress", False):
            return {
                "status": "skipped",
                "message": "Graceful apply already in progress",
            }
        self._graceful_in_progress = True

        log_event("info", "config_migration", "Starting graceful config migration...")

        try:
            # ── Step 1: Reload config FIRST ──
            # New spread/skew/sizing settings take effect immediately.
            # The next bot loop cycle will create new offers using the new config,
            # so there's no coverage gap.
            cfg.reload()
            log_event(
                "info", "config_migration", "Config reloaded — new settings active"
            )

            # ── Step 2: Sync current offers ──
            open_buys, open_sells, _ = self.offer_manager.sync_from_wallet()
            total = len(open_buys) + len(open_sells)

            if total == 0:
                log_event(
                    "info",
                    "config_migration",
                    "No active offers — config applied directly",
                )
                self._graceful_in_progress = False
                return {
                    "status": "ok",
                    "message": "Config reloaded, no offers to migrate",
                }

            if total <= 4:
                log_event(
                    "info",
                    "config_migration",
                    f"Only {total} offers — config applied, bot will requote naturally",
                )
                self._graceful_in_progress = False
                return {
                    "status": "ok",
                    "message": "Config reloaded, few offers will requote naturally",
                }

            # ── Step 3: Sort by distance from mid price, keep 2 tightest per side ──
            mid_price = self._current_mid_price
            if mid_price <= 0:
                price_data = self.price_engine.get_price()
                if price_data:
                    mid_price = Decimal(str(price_data.get("mid_price", 0)))

            if mid_price <= 0:
                self._graceful_in_progress = False
                return {
                    "status": "ok",
                    "message": "No price available — config reloaded, will requote naturally",
                }

            keep_per_side = 2

            def sort_by_distance(offers):
                """Sort offers by distance from mid price (tightest first)."""
                cat_scale = 10**cfg.CAT_DECIMALS
                decorated = []
                for o in offers:
                    summary = o.get("summary", {})
                    offered = summary.get("offered", {})
                    requested = summary.get("requested", {})
                    all_assets = {**offered, **requested}
                    xch_mojos = abs(float(all_assets.get("xch", 0)))
                    cat_mojos = 0
                    for k, v in all_assets.items():
                        if k != "xch" and k != "unknown":
                            cat_mojos = abs(float(v))
                            break
                    if xch_mojos > 0 and cat_mojos > 0:
                        xch_val = xch_mojos / 1e12
                        cat_val = cat_mojos / cat_scale
                        offer_price = (
                            Decimal(str(xch_val / cat_val))
                            if cat_val > 0
                            else Decimal("999")
                        )
                    else:
                        offer_price = Decimal("999")
                    distance = abs(offer_price - mid_price)
                    decorated.append((distance, o))
                decorated.sort(key=lambda x: x[0])
                return [o for _, o in decorated]

            sorted_buys = sort_by_distance(open_buys)
            sorted_sells = sort_by_distance(open_sells)

            retain_buy = sorted_buys[:keep_per_side]
            retain_sell = sorted_sells[:keep_per_side]
            cancel_buy = sorted_buys[keep_per_side:]
            cancel_sell = sorted_sells[keep_per_side:]

            retain_buy_ids = [
                o.get("trade_id") for o in retain_buy if o.get("trade_id")
            ]
            retain_sell_ids = [
                o.get("trade_id") for o in retain_sell if o.get("trade_id")
            ]
            cancel_ids = [
                o.get("trade_id") for o in cancel_buy + cancel_sell if o.get("trade_id")
            ]

            log_event(
                "info",
                "config_migration",
                f"Keeping {len(retain_buy_ids)}b + {len(retain_sell_ids)}s core offers, "
                f"queuing {len(cancel_ids)} outer offers for batched cancellation",
            )

            # ── Step 4: Store protected IDs ──
            self._graceful_migration = {
                "active": True,
                "phase": "cancelling",
                "protected_buy_ids": retain_buy_ids,
                "protected_sell_ids": retain_sell_ids,
                "cancel_ids": cancel_ids,
                "cancel_total": len(cancel_ids),
                "cancel_done": 0,
                "cancel_failed": 0,
                "remaining_cancel_ids": list(cancel_ids),
                "remaining_cancel_count": len(cancel_ids),
                "started_at": time.time(),
            }

            # ── Step 5: Launch background thread for batched cancellation ──
            # Small batches of 5 with 2s pauses between batches.
            # This prevents BAD_AGGREGATE_SIGNATURE errors in Sage wallet
            # and avoids overwhelming the wallet RPC.
            if cancel_ids:
                cancel_thread = threading.Thread(
                    target=self._batched_cancel_worker,
                    args=(cancel_ids,),
                    name="graceful-cancel",
                    daemon=True,
                )
                cancel_thread.start()
                log_event(
                    "info",
                    "config_migration",
                    f"Background cancel started — {len(cancel_ids)} offers "
                    f"in batches of 5",
                )
            else:
                self._graceful_migration["phase"] = "done"
                self._graceful_migration["active"] = False
                self._graceful_in_progress = False

            return {
                "status": "ok",
                "retained": len(retain_buy_ids) + len(retain_sell_ids),
                "cancelled": len(cancel_ids),
                "message": "Config applied — old offers being cancelled in background",
            }

        except Exception as e:
            log_event("error", "config_migration_failed", f"Migration failed: {e}")
            # Config was already reloaded in Step 1, so new settings are active
            self._graceful_migration = {"active": False}
            self._graceful_in_progress = False
            return {
                "status": "partial",
                "message": f"Migration partially failed ({e}), config is reloaded",
            }

    def _batched_cancel_worker(self, cancel_ids: list):
        """Background worker: cancel offers in small batches with pauses.

        Runs in a daemon thread so it doesn't block the API or bot loop.
        Cancels 3 offers at a time with a 5-second pause between batches.
        Small batches + longer pauses prevent MEMPOOL_CONFLICT errors in Sage
        by giving each batch time to confirm before the next one starts.
        """
        BATCH_SIZE = 3
        BATCH_DELAY = 5.0  # seconds between batches — enough for mempool to clear

        total = len(cancel_ids)
        done = 0
        failed = 0
        aborted_reason = ""

        log_event(
            "info",
            "batched_cancel",
            f"Starting batched cancel: {total} offers, "
            f"batch size {BATCH_SIZE}, delay {BATCH_DELAY}s",
        )

        try:
            for i in range(0, total, BATCH_SIZE):
                if not self._running:
                    log_event(
                        "warning",
                        "batched_cancel",
                        f"Bot stopped — aborting cancel after {done}/{total}",
                    )
                    aborted_reason = "bot stopped"
                    break

                if self.risk_manager.is_full_halt():
                    # CB active — brief pause to allow short-lived flaps to clear
                    # before aborting. Cancels are safe and desirable during CB
                    # (safeguard is trying to get offers off the market anyway).
                    _cb_wait = 0
                    _cb_max_wait = 30  # seconds
                    while self.risk_manager.is_full_halt() and _cb_wait < _cb_max_wait:
                        if not self._running:
                            break
                        time.sleep(2)
                        _cb_wait += 2
                    if self.risk_manager.is_full_halt():
                        # CB still active after wait — abort
                        aborted_reason = (
                            getattr(self.risk_manager, "_circuit_breaker_reason", "")
                            or "circuit breaker active"
                        )
                        log_event(
                            "warning",
                            "batched_cancel",
                            f"Circuit breaker still active after {_cb_max_wait}s — "
                            f"aborting cancel after {done}/{total}; "
                            f"leaving remaining offers in place ({aborted_reason})",
                        )
                        break
                    # CB cleared — continue with the cancel
                    log_event(
                        "info",
                        "batched_cancel",
                        f"Circuit breaker cleared after {_cb_wait}s — resuming cancel",
                    )

                batch = cancel_ids[i : i + BATCH_SIZE]
                batch_num = (i // BATCH_SIZE) + 1
                total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

                log_event(
                    "info",
                    "batched_cancel",
                    f"Batch {batch_num}/{total_batches}: cancelling {len(batch)} offers",
                )

                try:
                    results = self.offer_manager.cancel_offers(
                        batch, reason="config_migration"
                    )
                    batch_successes = sum(
                        1
                        for tid in batch
                        if results.get(tid) and results[tid].get("success")
                    )
                    batch_failures = len(batch) - batch_successes
                    done += batch_successes
                    failed += batch_failures
                except Exception as e:
                    log_event(
                        "warning",
                        "batched_cancel",
                        f"Batch {batch_num} error: {e} — continuing with next batch",
                    )
                    failed += len(batch)

                # Update migration progress
                self._graceful_migration["cancel_done"] = done
                self._graceful_migration["cancel_failed"] = failed

                # Pause between batches to let the wallet breathe
                if i + BATCH_SIZE < total:
                    time.sleep(BATCH_DELAY)

        except Exception as e:
            log_event("error", "batched_cancel", f"Worker failed: {e}")

        needs_retry_phase = bool(aborted_reason or failed > 0)
        if needs_retry_phase:
            self._graceful_migration["phase"] = "retrying"
            self._graceful_migration["active"] = True
            self._graceful_migration["cancel_done"] = done
            self._graceful_migration["cancel_failed"] = failed
        else:
            self._graceful_migration["phase"] = "verifying"
            self._graceful_migration["active"] = True
            self._graceful_migration["cancel_done"] = done
            self._graceful_migration["cancel_failed"] = failed

        if aborted_reason:
            remaining = max(0, total - done)
            log_event(
                "warning",
                "batched_cancel_done",
                f"Batched cancel stopped early: {done} cancelled, {failed} failed, "
                f"{remaining} left live out of {total} ({aborted_reason})",
            )
        elif failed > 0:
            log_event(
                "warning",
                "batched_cancel_done",
                f"Batched cancel queued retries: {done} cancelled, {failed} failed "
                f"out of {total}; waiting for retry confirmation before migration unlocks.",
            )
        else:
            log_event(
                "info",
                "batched_cancel_done",
                f"Batched cancel complete: {done} cancelled, {failed} failed "
                f"out of {total}. Waiting for wallet verification before "
                f"migration unlocks.",
            )

    # -------------------------------------------------------------------
    # State queries (for API/GUI)
    # -------------------------------------------------------------------

    def get_state(self) -> Dict:
        """Get full bot state for the GUI/API."""
        # Guard: if __init__ hasn't finished, return minimal state to avoid
        # AttributeError crashes when SSE connects during startup.
        if not getattr(self, "_init_complete", False):
            with self._state_lock:
                return dict(self._bot_state)
        with self._state_lock:
            state = dict(self._bot_state)
        state["loop_duration"] = round(self._last_loop_duration, 2)
        state["loop_seconds"] = cfg.LOOP_SECONDS
        state["dry_run"] = cfg.DRY_RUN

        # Add module states
        state["coins"] = self.coin_manager.get_status()
        state["risk"] = self.risk_manager.get_inventory_state()
        state["market_toxicity"] = state["risk"].get("market_toxicity", {})
        state["dexie"] = self.dexie_manager.get_stats()
        try:
            from database import get_fills

            recent_fills = get_fills(
                cat_asset_id=cfg.CAT_ASSET_ID,
                limit=10,
                since=getattr(cfg, "RUN_HISTORY_CUTOFF", None),
            )
        except Exception:
            recent_fills = []
        state["fills"] = {
            "recent": recent_fills,
            "counts": self.fill_tracker.get_fill_counts(),
        }

        # Add sniper stats
        state["sniper"] = self.sniper.get_stats()

        # Add market intelligence stats (NEW — ecosystem)
        state["market_intel"] = self.market_intel.get_stats()
        state["diagnostics"] = self.runtime_monitor.get_state()
        state["requote_diagnostics"] = self._get_requote_diagnostics()

        # Add Splash stats (V3 — P2P broadcasting)
        state["splash"] = self.splash_manager.get_stats()
        state["splash_node"] = self.splash_node.get_status()
        state["splash_receive"] = self.get_splash_receive_stats()

        # Add Coinset stats (V3 — fast coin queries)
        state["coinset"] = self.coinset_client.get_stats()
        try:
            with self._state_lock:
                state["recovery"] = dict(self._recovery_state)
        except (RuntimeError, Exception):
            state["recovery"] = {}  # dict mutated during copy — skip this cycle

        # V3: Trading pace and reserve status for GUI
        try:
            state["trading_pace"] = self.coin_manager.get_trading_pace()
            state["reserve_coins_xch"] = len(self.coin_manager._reserve_ids_xch)
            state["reserve_coins_cat"] = len(self.coin_manager._reserve_ids_cat)
            state["tier_spares"] = self.coin_manager._tier_spares
            # Total reserve value
            xch_reserve_coins = self.coin_manager._xch_inventory.get("reserve", [])
            if xch_reserve_coins:
                from coin_manager import _coin_amount

                total_reserve_mojos = sum(_coin_amount(c) for c in xch_reserve_coins)
                state["reserve_total_xch"] = f"{total_reserve_mojos / 1e12:.4f}"
            else:
                state["reserve_total_xch"] = "0"
        except Exception:
            state["trading_pace"] = "normal"
            state["reserve_coins_xch"] = 0
            state["reserve_total_xch"] = "0"

        # Add health monitor state (V1 parity)
        state["chia_health"] = dict(self._chia_health)

        # Add wallet type info
        try:
            from wallet import get_wallet_type

            state["wallet_type"] = get_wallet_type()
        except Exception:
            state["wallet_type"] = "unknown"

        # Add price watcher stats (V1 parity)
        with self._watcher_lock:
            state["price_watcher"] = dict(self._watcher_data)

        # Add stats from database
        try:
            state["stats"] = get_stats(
                cfg.CAT_ASSET_ID,
                since=getattr(cfg, "RUN_HISTORY_CUTOFF", None),
            )
        except Exception:
            state["stats"] = {}

        return state

    def _get_requote_diagnostics(self) -> Dict:
        """Expose compact requote pressure for the GUI."""
        try:
            mid_price = Decimal(str(self._current_mid_price or 0))
        except Exception:
            mid_price = Decimal("0")

        def threshold_bps(name: str, fallback: str) -> str:
            try:
                fraction = Decimal(str(getattr(cfg, name, Decimal(fallback))))
                return f"{(fraction * Decimal('10000')):.1f}"
            except Exception:
                return "0"

        thresholds = {
            "inner_bps": threshold_bps("REQUOTE_DRIFT_INNER", "0.003"),
            "mid_bps": threshold_bps("REQUOTE_DRIFT_MID", "0.008"),
            "full_bps": threshold_bps("REQUOTE_DRIFT_FULL", "0.02"),
            "emergency_bps": threshold_bps("REQUOTE_DRIFT_EMERGENCY", "0.05"),
        }

        sides = {}
        for side in ("buy", "sell"):
            baseline = Decimal(str(self._last_quoted_price.get(side, "0") or "0"))
            if mid_price > 0 and baseline > 0:
                move_bps = abs(mid_price - baseline) / baseline * Decimal("10000")
            else:
                move_bps = Decimal("0")
            sides[side] = {
                "baseline": str(baseline),
                "move_bps": f"{move_bps:.1f}",
                "forced": bool(self._force_requote.get(side, False)),
                "backoff_secs": round(self._requote_backoff_remaining(side), 1),
                "pending_cancel": len(self._pending_cancel_wallet_ids(side)),
            }

        retry_map = (
            getattr(getattr(self, "offer_manager", None), "_pending_cancel_retries", {})
            or {}
        )
        return {
            "mid_price": str(mid_price),
            "thresholds": thresholds,
            "batch_size": int(getattr(cfg, "REQUOTE_BATCH_SIZE", 0) or 0),
            "cooldown_secs": int(getattr(cfg, "REQUOTE_COOLDOWN_SECS", 0) or 0),
            "pending_cancel_total": sum(
                int(v.get("pending_cancel", 0) or 0) for v in sides.values()
            ),
            "pending_cancel_retries": len(retry_map),
            "sides": sides,
        }

    def get_price_info(self) -> Dict:
        """Get current price information."""
        return {
            "mid_price": str(self._current_mid_price),
            "last_quoted_buy": str(self._last_quoted_price.get("buy", "0")),
            "last_quoted_sell": str(self._last_quoted_price.get("sell", "0")),
        }
