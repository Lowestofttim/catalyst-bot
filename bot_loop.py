"""
V2 Bot Loop — Main Orchestrator

The central event loop that coordinates all modules each cycle:
  1. Fetch prices
  2. Check circuit breakers
  3. Detect fills
  4. Match round-trip PnL
  5. Requote stale offers
  6. Create new offers if needed
  7. Post to Dexie
  8. Manage coins
  9. Housekeeping

Additional background threads (V1 parity):
  - Health monitor: polls Chia wallet/node every 15s, auto-restarts after 5min failure
  - Price watcher: polls TibetSwap reserves every 12s, wakes main loop on swap detection

This replaces V1's bot_loop() function (which was embedded in the
7,500-line api_server.py monolith).

Usage:
    from bot_loop import BotLoop
    loop = BotLoop()
    loop.start()       # Background thread
    loop.stop()        # Clean shutdown
"""

import os
import time
import threading
import traceback
import requests
from decimal import Decimal
from typing import Dict, Optional

from config import cfg
from database import (
    init_database,
    log_event,
    get_stats,
    get_offer,
    update_offer_status,
    backfill_verified_fills_from_offers,
)
try:
    from super_log import slog, log_thread_start
except ImportError:
    def slog(cat, msg, data=None): pass
    def log_thread_start(name=None): pass
from price_engine import PriceEngine
from offer_manager import OfferManager
from fill_tracker import FillTracker
from dexie_manager import DexieManager
from splash_manager import SplashManager
from splash_node import SplashNode
from coinset_client import CoinsetClient
from coin_manager import CoinManager
from risk_manager import RiskManager
from sniper import Sniper
from boost_manager import BoostManager
from market_intel import MarketIntel
from runtime_monitor import RuntimeMonitor
from amm_monitor import AMMMonitor
from splash_receive import classify_offer_for_asset
from wallet import get_all_offers, classify_offers_from_list, get_chia_health, cancel_offer
try:
    import mempool_watcher as _mempool_watcher_mod
except ImportError:
    _mempool_watcher_mod = None


def _bps_to_pct(val):
    """Convert a BPS value (int, float, str, Decimal) to a formatted % string."""
    try:
        n = float(val) / 100
        if n < 1:
            return f"{n:.2f}%"
        return f"{n:.1f}%"
    except (ValueError, TypeError):
        return str(val)


def map_sage_terminal_offer_status(status_val, sage_offer=None, local_offer=None, now_ts=None):
    """Map Sage terminal offer states onto the local offer status enum.

    The local SQLite schema only supports:
      - open
      - filled
      - cancelled
      - expired

    Sage exposes richer terminal states like PENDING_CANCEL / CONFIRMED / FAILED.
    For local bookkeeping we collapse those onto the nearest safe terminal state
    so housekeeping can retire stale offers without violating DB constraints.
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
    max_time = (valid_times.get("max_time", 0) or
               sage_offer.get("max_time", 0) or 0)
    if max_time:
        try:
            if int(max_time) <= now_ts:
                return "expired"
        except Exception:
            pass

    if isinstance(status_val, int):
        if status_val in {2, 3, 5}:  # pending_cancel / cancelled / failed
            return "cancelled"
        if status_val == 4:  # confirmed
            return "filled"
        return None

    if status_text in {"EXPIRED"}:
        return "expired"
    if status_text in {"CANCELLED", "CANCELED", "PENDING_CANCEL", "FAILED"}:
        return "cancelled"
    if status_text in {"CONFIRMED", "COMPLETED", "SUCCEEDED", "SUCCESS"}:
        return "filled"
    return None


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
        self.coin_manager = CoinManager()
        self.coin_manager._price_engine = self.price_engine  # For CAT size derivation
        self.risk_manager = RiskManager(
            price_engine=self.price_engine,
            market_intel=self.market_intel
        )
        self.sniper = Sniper(
            offer_manager=self.offer_manager,
            risk_manager=self.risk_manager,
            dexie_manager=self.dexie_manager,
            splash_manager=self.splash_manager,
        )
        self.boost_manager = BoostManager(
            offer_manager=self.offer_manager,
            dexie_manager=self.dexie_manager,
            risk_manager=self.risk_manager
        )
        self.runtime_monitor = RuntimeMonitor(self)
        # AMM monitor — live TibetSwap reserve polling and drift detection
        self.amm_monitor = AMMMonitor(price_engine=self.price_engine)
        # Wire amm_monitor into offer_manager so buffer guard has AMM data
        self.offer_manager.amm_monitor = self.amm_monitor
        # Wire boost_manager into risk_manager for spread convergence
        self.risk_manager._boost_manager = self.boost_manager

        # ---- Loop state ----
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._loop_count: int = 0
        self._start_time: float = 0  # Set when bot starts, used for uptime
        self._last_loop_time: float = 0
        self._last_loop_duration: float = 0

        # ---- Price tracking ----
        self._last_quoted_price: Dict[str, Decimal] = {"buy": Decimal("0"), "sell": Decimal("0")}
        self._force_requote: Dict[str, bool] = {"buy": False, "sell": False}
        self._current_mid_price: Decimal = Decimal("0")

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
        self._probe_lock = threading.Lock()   # Protects _probe_state multi-key updates

    def _set_state(self, **updates):
        """Update _bot_state under the state lock (GUI-visible state).

        All bot_state writes must go through this method so the read side
        in get_state() sees a consistent snapshot.
        """
        with self._state_lock:
            self._bot_state.update(updates)

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
        self._startup_coin_recheck_done: bool = False  # Re-check coins after wallet settles

        # ---- Startup gate ----
        # Background threads wait on this before doing DB writes.
        # Set once _startup_sync() finishes, so cleanup + recovery
        # can run without lock contention from other threads.
        self._startup_complete = threading.Event()

        # ---- Housekeeping timer ----
        self._last_housekeeping: float = 0
        self._housekeeping_interval: int = 300  # 5 minutes

        # ---- Sweep protection (Tier 3) ----
        # Maps side ("buy"/"sell") → wall-clock expiry time.
        # When active, offer creation on that side is paused for one cycle
        # to avoid immediately re-posting stale-priced offers into an arb window.
        self._sweep_protection: Dict[str, float] = {}

        # ---- Health monitor state (V1 parity) ----
        self._health_thread: Optional[threading.Thread] = None
        self._health_check_interval: int = 15  # seconds between checks
        self._consecutive_unhealthy: int = 0
        self._last_auto_restart_time: float = 0
        self._auto_restart_threshold: int = 300   # 5 min unhealthy → restart
        self._auto_restart_cooldown: int = 1800   # 30 min between restarts
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
            "polls": 0,
            "triggers": 0,
        }

        # ---- Coin watcher state (lifecycle tracking) ----
        self._coin_watcher_thread: Optional[threading.Thread] = None
        self._coin_watcher_lock = threading.Lock()
        self._coin_watcher_interval: int = 30  # seconds between polls (reduced from 12s — read-only thread)
        self._coin_snapshot: Dict[str, Dict] = {}  # {coin_id: {amount, status, wallet_type}}
        self._coin_watcher_polls: int = 0
        self._coin_watcher_changes: int = 0

        # ---- Splash incoming watcher state ----
        self._splash_receive_thread: Optional[threading.Thread] = None
        self._splash_receive_interval: int = max(2, int(getattr(cfg, "SPLASH_RECEIVE_POLL_SECS", 5) or 5))
        self._splash_receive_batch_size: int = max(1, int(getattr(cfg, "SPLASH_RECEIVE_BATCH_SIZE", 10) or 10))
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
            "active": False,          # True while probing
            "buy_tid": None,          # trade_id of buy probe
            "sell_tid": None,         # trade_id of sell probe
            "buy_price": Decimal("0"),
            "sell_price": Decimal("0"),
            "tibet_price": Decimal("0"),
            "attempt": 0,             # retry count (widens buffer each time)
            "max_attempts": 5,        # give up after 5 retries
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

        # ---- Startup repost tracking (V1 parity) ----
        self._startup_repost_done: bool = False
        self._startup_repost_thread: Optional[threading.Thread] = None
        self._startup_repost_lock = threading.Lock()

        # ---- Connectivity recovery tracking (V1 parity) ----
        self._last_pricing_success_ts: float = 0
        self._connectivity_gap_threshold: int = 1800  # 30 min gap → repost to Dexie

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
        }
        self._recovery_under_target_cycles: int = 4
        self._recovery_wallet_stale_cycles: int = 2
        self._recovery_probe_churn_cycles: int = 3
        self._recovery_create_stall_cycles: int = 3
        self._recovery_exit_healthy_cycles: int = 2
        self._recovery_min_side_deficit: int = 2
        self._recovery_min_total_deficit: int = 2

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
            asset_id  = str(getattr(cfg, "CAT_ASSET_ID",  "") or "").strip().lower()
            ticker_id = str(getattr(cfg, "CAT_NAME", "") or
                            getattr(cfg, "CAT_TICKER_ID", "") or "").strip().upper()
            decimals  = int(getattr(cfg, "CAT_DECIMALS", 3) or 3)
            mid_price = float(self._current_mid_price or 0)
            spacescan = self._spacescan_context_getter(
                asset_id, ticker_id, decimals,
                executable_mid_price=mid_price,
            )
            metrics = health_data.setdefault("metrics", {})
            metrics["spacescan_enabled"]     = spacescan.get("enabled", False)
            metrics["spacescan_has_data"]    = spacescan.get("has_data", False)
            metrics["spacescan_holder_count"]= spacescan.get("holder_count", 0)
            metrics["spacescan_activity_level"] = spacescan.get("activity_level", "unknown")
            metrics["spacescan_risk_level"]  = spacescan.get("risk_level", "unknown")
            metrics["spacescan_price_gap_bps"] = str(spacescan.get("price_gap_bps", 0))
        except Exception as e:
            log_event("debug", "spacescan_augment_failed",
                      f"Spacescan health augment failed (non-critical): {e}")
        return health_data

    def _emit_alert(self, alert_id: str, severity: str, title: str, message: str,
                    action: str = None, action_label: str = None):
        """Emit a persistent alert to the GUI."""
        if self._event_bus:
            try:
                self._event_bus.alert(alert_id, severity, title, message, action, action_label)
            except Exception:
                pass  # Non-critical

    def _clear_alert(self, alert_id: str):
        """Clear a resolved alert."""
        if self._event_bus:
            try:
                if hasattr(self._event_bus, '_alert_store'):
                    self._event_bus._alert_store.clear(alert_id)
            except Exception:
                pass

    def _recovery_is_active(self) -> bool:
        return bool((self._recovery_state or {}).get("active"))

    def _mark_recovery_probe_churn(self):
        self._recovery_state["cycle_probe_churn"] = True

    def _mark_recovery_create_stall(self):
        self._recovery_state["cycle_create_stalled"] = True

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

    def _enter_recovery_mode(self, reason: str, buy_deficit: int, sell_deficit: int):
        state = self._recovery_state
        if state.get("active"):
            state["reason"] = reason
            state["buy_deficit"] = int(buy_deficit)
            state["sell_deficit"] = int(sell_deficit)
            self._set_state(status="recovering")
            return

        now = time.time()
        state.update({
            "active": True,
            "phase": "rebuilding",
            "reason": reason,
            "started_at": now,
            "last_transition_at": now,
            "entered_loop": int(self._loop_count),
            "healthy_streak": 0,
            "buy_deficit": int(buy_deficit),
            "sell_deficit": int(sell_deficit),
        })
        self._set_state(status="recovering")
        log_event(
            "warning",
            "recovery_mode_enter",
            f"Entering recovery mode — {reason}. "
            f"Current deficit: {buy_deficit} buy, {sell_deficit} sell.",
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
        state.update({
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
        })
        if self._running:
            self._set_state(status="running")
        self._clear_alert("bot_recovery")

    def _evaluate_recovery_mode(self, mid_price: Decimal,
                                current_buy_count: int,
                                current_sell_count: int):
        """Enter or clear recovery mode based on persistent degraded state."""
        state = self._recovery_state
        targets = self._get_expected_offer_targets(mid_price)
        buy_effective = int(current_buy_count) + int(self.offer_manager.get_recently_created_count("buy"))
        sell_effective = int(current_sell_count) + int(self.offer_manager.get_recently_created_count("sell"))
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
        state["wallet_stale_streak"] = (
            int(state.get("wallet_stale_streak", 0)) + 1
            if self._wallet_sync_stale_cycle else 0
        )
        state["under_target_streak"] = (
            int(state.get("under_target_streak", 0)) + 1
            if under_target else 0
        )
        state["probe_churn_streak"] = (
            int(state.get("probe_churn_streak", 0)) + 1
            if state.get("cycle_probe_churn") else 0
        )
        state["create_stall_streak"] = (
            int(state.get("create_stall_streak", 0)) + 1
            if state.get("cycle_create_stalled") else 0
        )

        # Escalate if create-stall streak is very long — bot may be stuck
        _stall_streak = int(state.get("create_stall_streak", 0))
        if _stall_streak > 0 and _stall_streak % 10 == 0:
            log_event("critical", "recovery_create_stall_escalation",
                      f"Offer creation has been stalled for {_stall_streak} consecutive "
                      f"recovery cycles — bot may be unable to rebuild the book. "
                      f"Check wallet connectivity and coin inventory.")

        reasons = []
        wallet_stale_recovery_relevant = under_target or state.get("active")
        if (
            state["wallet_stale_streak"] >= self._recovery_wallet_stale_cycles
            and wallet_stale_recovery_relevant
        ):
            reasons.append(
                f"wallet offer sync has been stale for {state['wallet_stale_streak']} cycles"
            )
        if under_target and state["under_target_streak"] >= self._recovery_under_target_cycles:
            reasons.append(
                f"book is still under target ({buy_effective}/{targets['buy']} buys, "
                f"{sell_effective}/{targets['sell']} sells)"
            )
        if under_target and state["probe_churn_streak"] >= self._recovery_probe_churn_cycles:
            reasons.append("probe retries keep churning while the main ladder is under target")
        if under_target and state["create_stall_streak"] >= self._recovery_create_stall_cycles:
            reasons.append("offer creation is stalling while the book is trying to recover")

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

        self._circuit_breaker_offer_safed = True
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

            cancelled = sum(1 for item in result.values() if item and item.get("success"))
            failed = sum(1 for item in result.values() if item and not item.get("success"))
            log_event(
                "warning",
                "circuit_breaker_cancel_done",
                f"CB safety cancel complete — cancelled {cancelled}, failed {failed}",
            )
            try:
                self.coin_manager.snapshot_coins("circuit_breaker_cancel")
                self._emit_coin_update("circuit_breaker_cancel")
            except Exception as e:
                log_event("warning", "circuit_breaker_coin_snapshot_failed",
                          f"Coin snapshot after circuit breaker cancel failed: {e}")
        except Exception as e:
            log_event(
                "error",
                "circuit_breaker_cancel_failed",
                f"Circuit breaker could not cancel live offers: {e}",
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
            log_event("debug", "probe_anchor_failed",
                      f"Probe-anchored mid calculation failed (falling back to Tibet mid): {e}")
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

    def _remember_probe_market_snapshot(self, mid_price: Decimal, arb_gap: Decimal,
                                        tibet_price: Decimal = Decimal("0"),
                                        reason: str = ""):
        """Persist the market snapshot that justified the latest probe discovery."""
        probe = self._probe_state or {}
        probe["last_discovery_mid_price"] = Decimal(str(mid_price or 0))
        probe["last_discovery_arb_gap_bps"] = Decimal(str(arb_gap or 0))
        probe["last_discovery_tibet_price"] = Decimal(str(tibet_price or 0))
        probe["last_discovery_reason"] = reason or ""
        probe["last_discovery_at"] = time.time()

    def _get_sniper_launch_reason(self, mid_price: Decimal, arb_gap: Decimal,
                                  current_buy_ids=None, current_sell_ids=None) -> Optional[str]:
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
            price_move_bps = (
                abs(mid_price - last_mid) / last_mid * Decimal("10000")
            )
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

        return None

    def _get_market_aware_probe_prices(self, tibet_price: Decimal,
                                       buffer_bps: Decimal) -> Dict[str, Decimal]:
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

        orderbook = {}
        if getattr(self, "market_intel", None) and hasattr(self.market_intel, "refresh_orderbook"):
            try:
                orderbook = self.market_intel.refresh_orderbook(force=True) or {}
            except Exception:
                orderbook = {}

        overall_best_bid = Decimal(str(orderbook.get("overall_best_bid") or 0))
        overall_best_ask = Decimal(str(orderbook.get("overall_best_ask") or 0))

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

        result.update({
            "buy_price": buy_price,
            "sell_price": sell_price,
            "overall_best_bid": overall_best_bid,
            "overall_best_ask": overall_best_ask,
        })
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

    def _apply_probe_retry_backoff(self, side: str, candidate_price: Decimal,
                                   previous_price: Decimal) -> Decimal:
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

    def _probe_hold_seconds_remaining(self, probe: Optional[Dict] = None,
                                      now_ts: Optional[float] = None) -> float:
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

        try:
            buy_prices = [
                Decimal(str(o.get("price_xch") or 0))
                for o in (open_buys or [])
                if o.get("price_xch") not in (None, "")
            ]
            if buy_prices:
                best_bid = max(buy_prices)
        except Exception:
            best_bid = Decimal("0")

        try:
            sell_prices = [
                Decimal(str(o.get("price_xch") or 0))
                for o in (open_sells or [])
                if o.get("price_xch") not in (None, "")
            ]
            positive_sell_prices = [p for p in sell_prices if p > 0]
            if positive_sell_prices:
                best_ask = min(positive_sell_prices)
        except Exception:
            best_ask = Decimal("0")

        return {
            "our_best_bid": str(best_bid),
            "our_best_ask": str(best_ask),
        }

    def _probe_has_matured(self, probe: Optional[Dict] = None,
                           now_ts: Optional[float] = None) -> bool:
        """True once a probe pair has survived for the minimum hold time."""
        return self._probe_hold_seconds_remaining(probe, now_ts) <= 0

    def _probe_cleanup_seconds_remaining(self, probe: Optional[Dict] = None,
                                         now_ts: Optional[float] = None) -> float:
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

    def _confirmed_probe_slot_offsets(self, current_buy_ids=None,
                                      current_sell_ids=None) -> Dict[str, int]:
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
            requested = {str(k).lower() for k in (summary.get("requested") or {}).keys()}

            if "xch" in offered and asset_id in requested:
                buy_ids.add(trade_id)
            elif asset_id in offered and "xch" in requested:
                sell_ids.add(trade_id)

        return buy_ids, sell_ids

    def _refresh_live_offer_ids_from_wallet(self, current_buy_ids=None, current_sell_ids=None):
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
            self._set_state(open_buys=len(current_buy_ids), open_sells=len(current_sell_ids))
        except Exception as e:
            log_event("debug", "probe_wallet_poll_failed",
                      f"Fast probe wallet poll failed: {e}")

        return current_buy_ids, current_sell_ids

    def _watch_active_probe_window(self, current_buy_ids=None, current_sell_ids=None,
                                   force_refresh: bool = False):
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
                current_buy_ids, current_sell_ids = self._refresh_live_offer_ids_from_wallet(
                    current_buy_ids,
                    current_sell_ids,
                )
                force_refresh = False

            probe = self._probe_state or {}
            open_ids = current_buy_ids | current_sell_ids
            buy_tid = probe.get("buy_tid")
            sell_tid = probe.get("sell_tid")
            buy_alive = bool(buy_tid and buy_tid in open_ids)
            sell_alive = bool(sell_tid and sell_tid in open_ids)
            remaining_hold = self._probe_hold_seconds_remaining(probe, time.time())

            # For a buy-only probe (sell_tid=None), treat buy-alive as "all expected alive"
            sell_required = bool(sell_tid)
            all_expected_alive = buy_alive and (sell_alive or not sell_required)

            if all_expected_alive and remaining_hold > 0:
                time.sleep(min(float(poll_secs), float(remaining_hold)))
                first_pass = False
                continue

            probe_age = max(0.0, time.time() - float(probe.get("launched_at") or time.time()))
            # Only wait for visibility if at least one expected probe isn't visible yet
            if (buy_tid or sell_tid) and probe_age < visibility_grace and not all_expected_alive:
                time.sleep(min(float(poll_secs), max(0.0, visibility_grace - probe_age)))
                first_pass = False
                continue

            break

        return current_buy_ids, current_sell_ids

    def _revalidate_confirmed_probe_edges(self, current_buy_ids=None,
                                          current_sell_ids=None,
                                          arb_gap: Decimal = Decimal("0")):
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
            current_buy_ids, current_sell_ids = self._refresh_live_offer_ids_from_wallet(
                current_buy_ids,
                current_sell_ids,
            )
        open_ids = current_buy_ids | current_sell_ids
        buy_alive = bool(buy_tid and buy_tid in open_ids)
        sell_alive = bool(sell_tid and sell_tid in open_ids)

        # If sell was never placed (no CAT sniper coins), treat buy-only as sufficient
        sell_required = bool(sell_tid)
        if buy_alive and (sell_alive or not sell_required):
            return current_buy_ids, current_sell_ids, False

        missing_sides = []
        if buy_tid and not buy_alive:
            missing_sides.append("buy")
        if sell_tid and not sell_alive:
            missing_sides.append("sell")
        if not missing_sides:
            return current_buy_ids, current_sell_ids, False

        self._mark_recovery_probe_churn()
        log_event(
            "warning",
            "probe_edge_lost",
            f"Confirmed probe edge disappeared before ladder creation: "
            f"{'+'.join(missing_sides)} missing. Re-arming probe first.",
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

    def _process_active_probe(self, current_buy_ids=None, current_sell_ids=None,
                              arb_gap: Decimal = Decimal("0"),
                              force_refresh: bool = False):
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

        buy_alive = buy_tid and buy_tid in (current_buy_ids | current_sell_ids)
        sell_alive = sell_tid and sell_tid in (current_buy_ids | current_sell_ids)

        # If sell was never placed (no CAT sniper coins), treat buy-only as sufficient
        sell_required = bool(sell_tid)
        if buy_alive and (sell_alive or not sell_required):
            remaining_hold = self._probe_hold_seconds_remaining(probe, now_ts)
            if remaining_hold > 0:
                last_notice_at = float(probe.get("last_wait_log_at") or 0)
                if (now_ts - last_notice_at) >= 5:
                    probe_age = max(0.0, now_ts - float(probe.get("launched_at") or now_ts))
                    log_event(
                        "info",
                        "probe_hold_wait",
                        f"Buy probe alive for {probe_age:.1f}s - holding "
                        f"{remaining_hold:.1f}s more before confirming",
                    )
                    probe["last_wait_log_at"] = now_ts
                log_event("debug", "probe_hold_status",
                          "Probe pair still aging before main ladder deploy")
            else:
                probe["confirmed_price"] = probe["tibet_price"]
                probe["confirmed_at"] = now_ts
                probe["active"] = False
                self._current_mid_price = probe["tibet_price"]
                self._set_state(mid_price=str(probe["tibet_price"]))
                linger_secs = int(getattr(cfg, "SNIPER_LINGER_SECS", 600) or 0)
                log_event("info", "probe_confirmed",
                          f"Both probes survived - price confirmed at Tibet "
                          f"{probe['tibet_price']:.8f}. Keeping probes live for "
                          f"{linger_secs}s while building main offers behind them.")
                log_event("info", "probe_confirmed_status",
                          "Price confirmed - deploying main offers behind live probes")
                self._clear_alert("probe_status")
        else:
            poll_secs = max(1, int(getattr(cfg, "SNIPER_POLL_SECS", 5) or 5))
            visibility_grace = max(float(poll_secs), 10.0)
            probe_age = max(0.0, now_ts - float(probe.get("launched_at") or now_ts))
            if probe_age < visibility_grace and (buy_tid or sell_tid):
                log_event("debug", "probe_visibility_wait",
                          f"Probe offers not fully visible yet ({probe_age:.1f}s old) - waiting")
            elif probe["attempt"] >= probe["max_attempts"]:
                survived_price = probe["sell_price"] if sell_alive else (
                    probe["buy_price"] if buy_alive else probe["tibet_price"])
                probe["confirmed_price"] = survived_price
                probe["confirmed_at"] = now_ts
                probe["active"] = False
                log_event("warning", "probe_max_retries",
                          f"Probe gave up after {probe['attempt']} attempts. "
                          f"Using best available price: {survived_price:.8f}")
                log_event("warning", "probe_timeout_status",
                          "Probe timed out - deploying main offers from best probe edge")
                self._clear_alert("probe_status")
            else:
                # ----------------------------------------------------------
                # PROBE-FILL = POSITIVE SIGNAL logic
                #
                # A probe disappearing after the visibility grace window means
                # it was FILLED — someone took it. This is market confirmation
                # that the price was right, NOT a failure.
                #
                # Old behaviour: always retry at a wider spread (wastes a coin
                # and delays the main ladder by SNIPER_CONFIRM_SECS).
                #
                # New behaviour:
                #   - First fill (attempt 0 → 1): treat as price confirmation.
                #     Deploy main ladder at Tibet price immediately. The fill IS
                #     the confirmation. Only surviving probes need to age out.
                #   - Repeated fills (attempt ≥ 1): also confirm. If the market
                #     keeps taking our probes, the price is clearly correct.
                #     Stop retrying and build the book.
                #
                # Exception: only widen + retry if the probe disappeared during
                # an extreme arb gap (>SNIPER_BUFFER_BPS), suggesting mispricing
                # rather than genuine market interest.
                # ----------------------------------------------------------

                taken_sides = []
                if not buy_alive and buy_tid:
                    taken_sides.append("buy")
                if not sell_alive and sell_tid:
                    taken_sides.append("sell")

                arb_gap_bps_float = float(arb_gap or 0)
                buffer_bps = float(getattr(cfg, "SNIPER_BUFFER_BPS", Decimal("50")) or 50)
                is_extreme_arb = arb_gap_bps_float > (buffer_bps * 3)

                if taken_sides and not is_extreme_arb:
                    # Filled under normal market conditions = positive signal.
                    # Confirm at Tibet price and deploy main ladder.
                    confirmed_price = probe["tibet_price"]
                    probe["confirmed_price"] = confirmed_price
                    probe["confirmed_at"] = now_ts
                    probe["active"] = False
                    log_event("info", "probe_fill_confirmed",
                              f"Probe {'+'.join(taken_sides)} filled at attempt "
                              f"{probe['attempt']} — positive price signal. "
                              f"Confirming at Tibet {confirmed_price:.8f} and "
                              f"deploying main ladder (fill = market confirmation).")
                    log_event("info", "probe_confirmed_status",
                              "Price confirmed via fill - deploying main offers")
                    self._clear_alert("probe_status")
                else:
                    # Either extreme arb gap (probe taken by arb bot not genuine
                    # market interest) or retry is warranted. Widen and retry.
                    attempt = probe["attempt"] + 1
                    base_buffer = getattr(cfg, "SNIPER_BUFFER_BPS", Decimal("50"))
                    adjusted_buffer = base_buffer + Decimal(str(attempt * 50))
                    tibet_p = probe["tibet_price"]
                    probe_prices = self._get_market_aware_probe_prices(
                        tibet_p,
                        adjusted_buffer,
                    )

                    log_event("info", "probe_retry",
                              f"Probe attempt {attempt}: {'+'.join(taken_sides) or 'none visible'} "
                              f"taken during extreme arb ({arb_gap_bps_float:.0f} BPS), "
                              f"widening buffer to {adjusted_buffer} BPS")
                    log_event("info", "probe_retry_status",
                              f"Probe retry {attempt} - extreme arb, widening spread")
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
                    # Only retry sell if it was originally placed — don't hammer
                    # CAT sniper creation when no CAT sniper coins exist
                    if sell_tid and not sell_alive:
                        sell_results = self.sniper.try_snipe_single(
                            "sell", new_sell_price, arb_gap)
                    # Retry buy only if it was taken (alive→gone) OR was never
                    # successfully placed (use truthy check to exclude empty string "")
                    if (buy_tid and not buy_alive) or not buy_tid:
                        self.sniper._last_snipe_time = 0
                        buy_results = self.sniper.try_snipe_single(
                            "buy", new_buy_price, arb_gap)

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
            self._emit("coin_update", {
                "reason": reason,
                "xch_free": status.get("xch_coins", 0),
                "xch_locked": status.get("xch_locked_coins", 0),
                "xch_total": status.get("xch_total_coins", 0),
                "cat_free": status.get("cat_coins", 0),
                "cat_locked": status.get("cat_locked_coins", 0),
                "cat_total": status.get("cat_total_coins", 0),
                "xch_locked_amount": inv.get("xch_locked_amount", "0"),
                "cat_locked_amount": inv.get("cat_locked_amount", "0"),
                "tier_counts": tier_counts,
            })
        except Exception as e:
            # Non-critical GUI update — log at debug so it's findable but doesn't alarm
            log_event("debug", "coin_update_event_failed",
                      f"Coin update GUI event failed (non-critical): {e}")

    def _reset_runtime_state(self) -> None:
        """Reset all per-session runtime state before (re)starting.

        Called at the top of start() to ensure a clean slate on every
        start — whether first run or a stop/start within the same process.
        Without this, stale _probe_state, _last_quoted_price, etc. from
        the previous session cause incorrect behaviour on the second start.
        """
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
            }
        self._last_quoted_price = {"buy": Decimal("0"), "sell": Decimal("0")}
        self._watcher_data = {
            "last_xch_reserve": 0,
            "last_token_reserve": 0,
            "triggered": False,
            "change_pct": 0.0,
            "direction": "",
            "last_change_ts": 0,
            "polls": 0,
            "triggers": 0,
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
        self._last_pricing_success_ts = 0
        # Clear startup gate so background threads re-wait on next start
        self._startup_complete.clear()
        log_event("debug", "runtime_state_reset",
                  "Runtime state reset for new session")

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
        self._recovery_state.update({
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
        })
        self._clear_alert("bot_recovery")

        # ---- Preflight / Doctor checks ----
        # Run structured readiness checks before starting.
        # If any check fails, block startup with a detailed report.
        try:
            from doctor import run_preflight
            preflight = run_preflight(force=True)
            log_event("info", "preflight_run", preflight.summary,
                      data={"can_start": preflight.can_start,
                            "duration_ms": round(preflight.duration_ms, 1)})
            if not preflight.can_start:
                failures = [c for c in preflight.checks if c.status == "fail"]
                fail_msgs = "; ".join(f"{c.name}: {c.message}" for c in failures[:3])
                log_event("error", "preflight_blocked",
                          f"Bot start blocked by preflight: {fail_msgs}")
                self._emit_alert(
                    "preflight_blocked",
                    "error",
                    "Preflight Failed",
                    f"Cannot start: {fail_msgs}",
                    action="run_doctor",
                    action_label="View Report",
                )
                self._set_state(running=False, status="blocked", preflight=preflight.to_dict())
                return False
        except Exception as e:
            # Preflight failure should not block startup — fall through
            # to the legacy watch-only check below.
            log_event("warning", "preflight_error",
                      f"Preflight could not run: {str(e)[:160]}")

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
                        msg + ". Log in to a Sage wallet with secrets before starting or resuming the bot.",
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

        # Clear any previous stop signal so ladder creation works
        self.offer_manager._stop_requested = False

        # Emit immediately (on the request thread) so the console and
        # system log have something to show before the background thread
        # finishes its slower startup sync.
        log_event("info", "bot_starting",
                  "Bot starting — syncing with wallet, please wait...")

        slog("STARTUP", "Launching bot-loop thread")
        # Main trading loop
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="bot-loop"
        )
        self._thread.start()

        # Health monitor thread (V1 parity)
        self._health_thread = threading.Thread(
            target=self._health_monitor_thread,
            daemon=True,
            name="health-monitor"
        )
        self._health_thread.start()

        # Price watcher thread (V1 parity)
        self._watcher_thread = threading.Thread(
            target=self._price_watcher_thread,
            daemon=True,
            name="price-watcher"
        )
        self._watcher_thread.start()

        # Mempool watcher — pre-emptive price intelligence
        # Polls Coinset mempool every 5s for pending Tibet pool spends,
        # giving up to one block (~18-54s) early warning of incoming swaps.
        # Only starts if COINSET_ENABLED is True.
        if _mempool_watcher_mod and getattr(cfg, "COINSET_ENABLED", True) and cfg.CAT_ASSET_ID:
            try:
                pair_id = getattr(cfg, "_cached_tibet_pair_id", "") or ""
                if not pair_id:
                    # Try to resolve pair_id from the price engine cache
                    from price_engine import PriceEngine as _PE
                    _tmp_pe = _PE()
                    _tmp_pair = _tmp_pe._find_tibet_pair(cfg.CAT_ASSET_ID) or {}
                    pair_id = _tmp_pair.get("pair_id", "")
                if pair_id:
                    _mempool_watcher_mod.start_watcher(
                        pair_id=pair_id,
                        asset_id=cfg.CAT_ASSET_ID,
                        cat_decimals=int(getattr(cfg, "CAT_DECIMALS", 3) or 3),
                    )
                    log_event("info", "mempool_watcher_init",
                              f"Mempool watcher started (pair {pair_id[:16]}...)")
            except Exception as _mw_err:
                log_event("warning", "mempool_watcher_skip",
                          f"Mempool watcher could not start: {_mw_err}")

        # Coin watcher thread (lifecycle tracking)
        self._coin_watcher_thread = threading.Thread(
            target=self._coin_watcher_thread_run,
            daemon=True,
            name="coin-watcher"
        )
        self._coin_watcher_thread.start()
        # If outbound Splash broadcasting is enabled, keep inbound listening on
        # as well so the listener stats and pair-specific intake stay live.
        if getattr(cfg, "SPLASH_ENABLED", False) and not getattr(cfg, "SPLASH_RECEIVE_ENABLED", False):
            cfg.SPLASH_RECEIVE_ENABLED = True
            log_event(
                "info",
                "splash_receive_auto",
                "Splash incoming listener auto-enabled alongside outbound broadcast",
            )

        # Splash incoming watcher thread (classifies inbound P2P offers)
        self._splash_receive_thread = threading.Thread(
            target=self._splash_receive_thread_run,
            daemon=True,
            name="splash-receive"
        )
        self._splash_receive_thread.start()

        # V3: Auto-start Splash P2P node if enabled
        if getattr(cfg, "SPLASH_ENABLED", False) and getattr(cfg, "SPLASH_AUTO_START", True):
            try:
                started = self.splash_node.start()
                if started:
                    log_event("info", "splash_node_auto",
                              "Splash P2P node auto-started with bot")
                # If binary not found, splash_node.start() already logged the message
            except Exception as e:
                log_event("warning", "splash_node_auto_failed",
                          f"Failed to auto-start Splash node: {e}")

        # AMM monitor — starts background polling thread for live reserve data
        if getattr(cfg, "TIBET_PAIR_ID", "").strip():
            try:
                self.amm_monitor.start()
            except Exception as _amm_err:
                log_event("warning", "amm_monitor_start_failed",
                          f"AMM Monitor could not start: {_amm_err}")

        log_event("info", "bot_started",
                  "Bot loop started (with health, price, coin, AMM, and runtime monitors)")
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
            log_event("debug", "stop_topup_failed", f"stop_topup raised during shutdown: {e}")

        # Wake the watcher event so price watcher thread exits promptly
        self._watcher_event.set()

        # Stop AMM monitor
        try:
            self.amm_monitor.stop()
        except Exception as e:
            log_event("debug", "amm_monitor_stop_failed",
                      f"AMM Monitor stop raised during shutdown: {e}")

        # Stop mempool watcher
        if _mempool_watcher_mod:
            try:
                _mempool_watcher_mod.stop_watcher()
            except Exception as e:
                log_event("debug", "mempool_watcher_stop_failed",
                          f"Mempool watcher stop raised during shutdown: {e}")

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
                    log_event("warning", "thread_join_timeout",
                              f"{_t_name} thread did not exit within 10s")

        # V3: Stop Splash node
        if self.splash_node.is_running():
            try:
                self.splash_node.stop()
            except Exception as e:
                log_event("debug", "splash_node_stop_failed",
                          f"Splash node stop raised during shutdown: {e}")

        self._set_state(status="stopped")
        log_event("info", "bot_stopped", "Bot loop stopped")
        return True

    def is_running(self) -> bool:
        """Check if the bot loop is running."""
        return self._running

    def _current_splash_pair_label(self) -> str:
        ticker = str(getattr(cfg, "CAT_TICKER_ID", "") or getattr(cfg, "CAT_NAME", "CAT")).strip().upper()
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
        stats["poll_secs"] = self._splash_receive_interval
        stats["batch_size"] = self._splash_receive_batch_size
        return stats

    def _resolve_splash_view_offer(self):
        """Return a wallet-native view_offer callable when available."""
        try:
            from wallet import get_wallet_type
            wallet_type = str(get_wallet_type() or "").strip().lower()
        except Exception:
            wallet_type = str(getattr(cfg, "WALLET_TYPE", "sage") or "sage").strip().lower()

        if wallet_type != "sage":
            return wallet_type, None

        try:
            from wallet_sage import view_offer as sage_view_offer
            return wallet_type, sage_view_offer
        except Exception as e:
            if not self._splash_receive_parser_warned:
                self._splash_receive_parser_warned = True
                log_event("warning", "splash_receive_parser",
                          f"Incoming Splash parsing unavailable: {e}")
            return wallet_type, None

    def _process_splash_incoming_batch(self):
        """Classify newly received Splash offers for the active CAT/XCH pair."""
        if not getattr(cfg, "SPLASH_RECEIVE_ENABLED", False):
            return

        from database import get_splash_incoming_offers, update_splash_incoming_status

        pending = get_splash_incoming_offers(
            status="new",
            limit=self._splash_receive_batch_size
        )
        if not pending:
            return

        asset_id = str(getattr(cfg, "CAT_ASSET_ID", "") or "").strip().lower()
        pair_label = self._current_splash_pair_label()
        wallet_type, view_offer = self._resolve_splash_view_offer()

        if view_offer is None:
            if wallet_type != "sage":
                for offer in pending:
                    update_splash_incoming_status(offer["id"], "ignored", pair_hint="unsupported")
                self._emit("splash_incoming", self.get_splash_receive_stats())
            return

        relevant_found = 0
        processed_any = False

        for offer in reversed(pending):
            offer_id = int(offer.get("id", 0) or 0)
            bech32 = str(offer.get("offer_bech32") or "").strip()
            if not offer_id or not bech32:
                if offer_id:
                    update_splash_incoming_status(offer_id, "ignored", pair_hint="invalid")
                    processed_any = True
                continue

            try:
                viewed = view_offer(bech32)
            except Exception as e:
                log_event("debug", "splash_receive_view_error",
                          f"Could not inspect inbound Splash offer {offer_id}: {e}")
                continue

            if not viewed:
                continue

            classified = classify_offer_for_asset(viewed, asset_id)
            pair_hint = classified.get("pair_hint") or "unknown"

            if classified.get("relevant"):
                update_splash_incoming_status(offer_id, "processed", pair_hint=pair_hint)
                relevant_found += 1
                processed_any = True
                log_event(
                    "info",
                    "splash_incoming_relevant",
                    f"Relevant inbound Splash offer ({classified.get('side', 'unknown')} {pair_label})"
                )
            else:
                update_splash_incoming_status(offer_id, "ignored", pair_hint=pair_hint)
                processed_any = True

        if processed_any:
            payload = self.get_splash_receive_stats()
            payload["relevant_found"] = relevant_found
            self._emit("splash_incoming", payload)

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
            f"Splash receive watcher active (polling every {self._splash_receive_interval}s)"
        )

        while self._running:
            try:
                self._process_splash_incoming_batch()
            except Exception as e:
                log_event("debug", "splash_receive_error",
                          f"Splash receive watcher error: {e}")

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
        slog("STARTUP", "========== _startup_sync COMPLETE — releasing thread gates ==========")
        self._startup_complete.set()  # Ungate background threads

        self._set_state(status="running")

        while self._running:
            loop_start = time.time()

            try:
                self._run_one_cycle()
            except Exception as e:
                log_event("error", "loop_error",
                          f"Error in bot loop cycle: {e}\n{traceback.format_exc()}")

            # Update timing
            self._last_loop_duration = time.time() - loop_start

            # Slow-iteration watchdog: warn if a single cycle took > 5 minutes.
            # This usually means a wallet RPC call is hanging. The cycle already
            # completed (or errored out) — this is purely diagnostic logging.
            if self._last_loop_duration > 300:
                log_event("warning", "slow_iteration",
                          f"Slow cycle: {self._last_loop_duration:.0f}s — possible RPC hang. "
                          f"Normal cycles should complete in < {cfg.LOOP_SECONDS}s.")
            self._last_loop_time = time.time()
            self._loop_count += 1
            self._set_state(loop_count=self._loop_count)

            # Sleep until next cycle — OR wake early if price watcher detects a swap
            sleep_time = max(1, cfg.LOOP_SECONDS - self._last_loop_duration)
            watcher_triggered = self._watcher_event.wait(timeout=sleep_time)
            self._watcher_event.clear()

            if watcher_triggered and self._running:
                with self._watcher_lock:
                    watcher_info = (f"{self._watcher_data['direction']} "
                                    f"({self._watcher_data['change_pct']:.3f}%)")
                    self._watcher_data["triggered"] = False
                log_event("info", "watcher_wake",
                          f"Fast wake — Tibet swap detected: {watcher_info}")

            # Process mempool watcher signals (pre-emptive price intelligence)
            if _mempool_watcher_mod and self._running:
                try:
                    w = _mempool_watcher_mod._watcher_instance
                    if w:
                        for sig in w.get_pending_signals():
                            sig_type = sig.get("type")
                            if sig_type == "imminent_swap":
                                log_event("info", "mempool_imminent_wake",
                                          "Mempool: pending pool-coin spend detected — "
                                          "pre-emptive requote cycle triggered")
                                # Treat exactly like a price-watcher wake so the bot
                                # reruns pricing/requote without waiting for next timer.
                                # (We're already past the sleep so next cycle starts
                                #  immediately in the next iteration.)
                            elif sig_type == "fill_imminent":
                                coin_id = sig.get("coin_id", "?")[:16]
                                log_event("info", "mempool_fill_wake",
                                          f"Mempool: offer coin {coin_id}... spent — "
                                          f"waking early for fill detection")
                                # Wake the bot immediately so fill_tracker can confirm
                                # via wallet RPC and post a replacement this cycle
                                # rather than waiting up to LOOP_SECONDS.
                                self._watcher_event.set()
                            elif sig_type == "price_move":
                                direction = sig.get("direction", "?")
                                pct = sig.get("magnitude_pct", 0)
                                log_event("info", "mempool_price_confirmed",
                                          f"Pool reserves confirmed: {direction} {pct:.3f}% "
                                          f"(XCH {sig.get('delta_xch', 0):+d} mojos)")
                except Exception:
                    pass

        log_event("info", "bot_loop_exit", "Bot loop exited cleanly")

    # -------------------------------------------------------------------
    # Startup sync
    # -------------------------------------------------------------------

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
                slog("STARTUP", f"⚠️  CONFIG WARNING: {warn}")
            for err in validation.get("errors", []):
                log_event("error", "config_validation", f"CONFIG ERROR: {err}")
                slog("STARTUP", f"❌ CONFIG ERROR: {err}")
            if validation.get("errors"):
                print(f"[STARTUP] {len(validation['errors'])} config error(s) — check settings")
            if not validation["warnings"] and not validation["errors"]:
                log_event("info", "config_validation", "Config validation OK")
        except Exception as e:
            log_event("warning", "config_validation_failed",
                      f"Config validation raised an exception: {e}")

        # ── Clear stale reservations from previous runtime ──────────
        try:
            from reservation_manager import ReservationManager
            cleared = ReservationManager().expire_all()
            if cleared:
                slog("STARTUP", f"Cleared {cleared} stale reservation(s) from previous runtime")
        except Exception as e:
            log_event("warning", "reservation_clear_failed",
                      f"Could not clear stale reservations on startup: {e}")

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
                        msg = (f"🚫 SAFETY STOP: wallet_id {cfg.CAT_WALLET_ID} resolves to "
                               f"asset {resolved[:20]}... but .env has {configured[:20]}... "
                               f"— these are DIFFERENT tokens! Refusing to start.")
                        log_event("error", "cat_mapping_mismatch", msg)
                        slog("STARTUP", msg)
                        self._running = False
                        self._set_state(status="error", error="CAT asset_id mismatch — wrong token detected")
                        return
                    else:
                        slog("STARTUP", f"✅ CAT mapping verified: wallet_id {cfg.CAT_WALLET_ID} "
                             f"→ {resolved[:20]}... matches .env config")
        except Exception as e:
            slog("STARTUP", f"⚠️ CAT mapping check skipped: {e}")

        try:
            # ---- Auto-detect wallet address for Spacescan verification ----
            # Pulls the receive address from the connected wallet (Sage or Chia).
            # This means it works for any wallet without needing .env config.
            try:
                from wallet import get_next_address, get_wallet_type
                addr_result = get_next_address(new_address=False)
                if addr_result and addr_result.get("success"):
                    cfg.WALLET_ADDRESS = addr_result["address"]
                    log_event("info", "wallet_address_detected",
                              f"Wallet address: {cfg.WALLET_ADDRESS[:20]}...")

                    if (get_wallet_type() == "sage" and
                            getattr(cfg, "SAGE_SET_CHANGE_ADDRESS", False)):
                        try:
                            from wallet_sage import set_change_address as _sage_set_change_address
                            change_result = _sage_set_change_address(cfg.WALLET_ADDRESS)
                            if change_result and change_result.get("success"):
                                log_event("success", "sage_change_address_set",
                                          f"Sage change address set to "
                                          f"{cfg.WALLET_ADDRESS[:20]}... "
                                          f"for fingerprint {change_result.get('fingerprint')}")
                            else:
                                log_event("warning", "sage_change_address_failed",
                                          f"Could not set Sage change address: "
                                          f"{(change_result or {}).get('error', 'unknown_error')}")
                        except Exception as e:
                            log_event("warning", "sage_change_address_failed",
                                      f"Error setting Sage change address: {e}")
                else:
                    log_event("warning", "wallet_address_failed",
                              "Could not auto-detect wallet address — "
                              "Spacescan self-spend detection will be limited")
            except Exception as e:
                log_event("warning", "wallet_address_error",
                          f"Wallet address detection failed: {e}")

            # ---- Auto-resolve CAT metadata (TIBET_PAIR_ID, CAT_TICKER_ID, CAT_NAME) ----
            # Given only CAT_ASSET_ID, queries TibetSwap to fill any empty derived fields.
            # Only fills fields that are unset in .env — never overwrites explicit config.
            try:
                from cat_resolver import resolve_and_apply as _resolve_cat
                _cat_meta = _resolve_cat(cfg)
                if _cat_meta.get("pair_id"):
                    slog("STARTUP", f"CAT metadata resolved — "
                         f"pair_id={_cat_meta['pair_id'][:20]}... "
                         f"ticker={_cat_meta.get('ticker_id')} "
                         f"name={_cat_meta.get('name')}")
                else:
                    slog("STARTUP", "CAT metadata: token not found on TibetSwap "
                         "(no pair/ticker auto-resolved — check CAT_TICKER_ID in .env)")
            except Exception as e:
                log_event("warning", "cat_resolver_failed",
                          f"CAT metadata auto-resolution failed (non-critical): {e}")

            # Sync offers from wallet
            log_event("info", "startup_wallet_sync", "Fetching offers from wallet RPC...")
            open_buys, open_sells, closed = self.offer_manager.sync_from_wallet()
            log_event("info", "startup_wallet_result",
                      f"Wallet returned: {len(open_buys)} open buys, "
                      f"{len(open_sells)} open sells, {len(closed)} closed")

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
            slog("STARTUP", f"DB cleanup: wallet has {len(wallet_open_ids)} open offer IDs")
            try:
                from database import get_open_offers, batch_cancel_stale_offers
                db_open = get_open_offers()
                log_event("info", "db_cleanup_start",
                          f"DB has {len(db_open)} offers marked 'open', "
                          f"wallet has {len(wallet_open_ids)} truly open")

                # Collect stale trade_ids (in DB but not in wallet)
                stale_ids = []
                stale_db_map = {}
                for db_offer in db_open:
                    tid = db_offer.get("trade_id", "")
                    if tid and tid not in wallet_open_ids:
                        stale_ids.append(tid)
                        stale_db_map[tid] = db_offer

                if stale_ids:
                    slog("STARTUP", f"Found {len(stale_ids)} stale offers — checking status")
                    # Check completed offers from wallet to distinguish fills from cancels.
                    # Offers that filled while the bot was down must be marked "filled"
                    # not "cancelled" — otherwise PnL is permanently wrong.
                    fill_ids = set()
                    expire_ids = set()
                    try:
                        from wallet_sage import get_all_offers as sage_get_all
                        now_ts_cleanup = int(time.time())
                        completed = sage_get_all(include_completed=True, start=0, end=500) or []
                        completed_map = {
                            (o.get("trade_id") or o.get("offer_id", "")): o
                            for o in completed if isinstance(o, dict)
                        }
                        for tid in stale_ids:
                            sage_offer = completed_map.get(tid)
                            if sage_offer:
                                mapped = map_sage_terminal_offer_status(
                                    sage_offer.get("status"),
                                    sage_offer=sage_offer,
                                    local_offer=stale_db_map.get(tid, {}),
                                    now_ts=now_ts_cleanup,
                                )
                                if mapped == "filled":
                                    fill_ids.add(tid)
                                elif mapped == "expired":
                                    expire_ids.add(tid)
                    except Exception as e:
                        log_event("warning", "db_cleanup_status_check_failed",
                                  f"Could not check completed offers at startup: {e}")

                    cancel_ids = [t for t in stale_ids
                                  if t not in fill_ids and t not in expire_ids]

                    if fill_ids:
                        from database import update_offer_status
                        for tid in fill_ids:
                            update_offer_status(tid, "filled")
                        log_event("info", "db_cleanup_fills_recovered",
                                  f"Marked {len(fill_ids)} offers as filled "
                                  f"(filled while bot was offline)")

                    # Emit SSE fill events for offline fills + create fill rows
                    # so PnL matching works correctly without waiting for the
                    # housekeeping backfill cycle.
                    if fill_ids:
                        try:
                            from database import backfill_verified_fills_from_offers
                            _offline_fills = backfill_verified_fills_from_offers(
                                limit=len(fill_ids) + 10
                            )
                            for _off_fill in (_offline_fills or []):
                                self._emit("fill", {
                                    "side": _off_fill.get("side", ""),
                                    "price": str(_off_fill.get("price_xch") or
                                                 _off_fill.get("price") or ""),
                                    "size_xch": str(_off_fill.get("xch_amount") or ""),
                                    "tier": _off_fill.get("tier", ""),
                                    "offline": True,
                                })
                            if _offline_fills:
                                log_event("info", "offline_fill_sse_emitted",
                                          f"Emitted SSE events for "
                                          f"{len(_offline_fills)} offline fill(s)")
                        except Exception as _sse_err:
                            log_event("warning", "offline_fill_sse_failed",
                                      f"Offline fill SSE emit failed (non-critical): "
                                      f"{_sse_err}")

                    if expire_ids:
                        from database import update_offer_status as _uos
                        for tid in expire_ids:
                            _uos(tid, "expired")
                        log_event("info", "db_cleanup_expired",
                                  f"Marked {len(expire_ids)} offers as expired")

                    cleaned = batch_cancel_stale_offers(cancel_ids) if cancel_ids else 0
                    total_cleaned = cleaned + len(fill_ids) + len(expire_ids)
                    slog("STARTUP", f"Cleanup result: {cleaned} cancelled, "
                                    f"{len(fill_ids)} filled, {len(expire_ids)} expired")
                    if total_cleaned == len(stale_ids):
                        log_event("info", "db_cleanup_done",
                                  f"Cleaned {total_cleaned} stale DB offers "
                                  f"({cleaned} cancelled, {len(fill_ids)} filled, "
                                  f"{len(expire_ids)} expired)")
                    else:
                        log_event("warning", "db_cleanup_done",
                                  f"Cleaned {total_cleaned}/{len(stale_ids)} stale DB offers "
                                  f"(some failed due to DB lock)")
                else:
                    log_event("info", "db_cleanup_done", "No stale DB offers found")
            except Exception as e:
                log_event("warning", "db_cleanup_failed", f"DB offer cleanup failed: {e}")

            # ---- Recover unknown offers ----
            # If the bot created offers on-chain but couldn't write to the DB
            # (e.g., DB was locked, bot crashed), import them now.
            try:
                from database import recover_unknown_offers
                all_wallet_offers = open_buys + open_sells
                recovery = recover_unknown_offers(all_wallet_offers, cfg.CAT_ASSET_ID)
                if recovery.get("recovered", 0) > 0:
                    log_event("info", "startup_offer_recovery",
                              f"Recovered {recovery['recovered']} unknown offers from wallet "
                              f"(skipped {recovery['skipped']}, errors {recovery['errors']})")
                    # Update wallet_open_ids with the newly recovered offers
                    for offer in all_wallet_offers:
                        tid = offer.get("trade_id", "")
                        if tid:
                            wallet_open_ids.add(tid)
                else:
                    log_event("info", "startup_offer_recovery",
                              "No unknown offers — DB and wallet in sync")
            except Exception as e:
                log_event("warning", "startup_recovery_failed",
                          f"Offer recovery failed: {e}")

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
                    log_event("debug", "startup_orderbook_refresh_failed",
                              f"Startup orderbook refresh failed (non-critical): {e}")

                try:
                    health_data = self._augment_health_with_spacescan(
                        self.risk_manager.get_market_health()
                    )
                    offer_edges = self._get_live_offer_edges(open_buys, open_sells)
                    cached_intel = {}
                    try:
                        cached_intel = self.market_intel.get_cached_data() if self.market_intel else {}
                    except Exception as e:
                        log_event("debug", "startup_cached_intel_failed",
                                  f"Startup cached intel fetch failed (non-critical): {e}")
                        cached_intel = {}

                    self._emit("dashboard_update", {
                        "market_health": health_data,
                        "loop_count": self._loop_count,
                        "open_buys": len(buy_ids),
                        "open_sells": len(sell_ids),
                        "mid_price": str(self._current_mid_price or Decimal("0")),
                        "our_best_bid": offer_edges.get("our_best_bid", "0"),
                        "our_best_ask": offer_edges.get("our_best_ask", "0"),
                        "best_bid": str(cached_intel.get("best_bid", "0")),
                        "best_ask": str(cached_intel.get("best_ask", "0")),
                    })
                except Exception as e:
                    log_event("debug", "startup_dashboard_update_failed",
                              f"Startup dashboard update failed (non-critical): {e}")

            # ---- Reload Dexie mappings + repost missing ----
            # The in-memory dexie_manager map is empty on startup.
            # 1) Load persisted trade_id→dexie_id mappings from DB
            # 2) For offers WITHOUT dexie_id, repost to Dexie to get links
            try:
                from database import get_trade_dexie_map, get_open_offers
                db_dexie_map = get_trade_dexie_map(cfg.CAT_ASSET_ID)
                if db_dexie_map:
                    self.dexie_manager._trade_dexie_map.update(db_dexie_map)
                    log_event("info", "dexie_recovery",
                              f"Loaded {len(db_dexie_map)} Dexie mappings from database")

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
                    for db_offer in missing_dexie:
                        tid = db_offer.get("trade_id", "")
                        bech = bech32_map.get(tid, "")
                        if bech and tid:
                            self.dexie_manager.queue_post(bech, tid, force=True)
                            repost_count += 1

                    if repost_count > 0:
                        log_event("info", "dexie_repost",
                                  f"Queued {repost_count} offers for Dexie posting "
                                  f"(will post during loop cycles, "
                                  f"{cfg.MAX_POSTS_PER_LOOP} per cycle)")
                        # DON'T flush here — let the normal loop cycle handle
                        # posting gradually via step 11. Flushing 80 offers
                        # during startup causes DB locking with concurrent reads.
                    elif not db_dexie_map:
                        log_event("info", "dexie_recovery",
                                  "No Dexie mappings found — will post on first cycle")
            except Exception as e:
                log_event("warning", "dexie_recovery_failed",
                          f"Failed to recover Dexie links: {e}")

            # Warm orderbook + advisor context before the slower resume repost path.
            # This keeps Market Health / Advisor responsive during startup.
            _push_startup_market_snapshot()

            # Count and classify all coins — first snapshot of the session
            self.coin_manager.snapshot_coins("startup")
            self._emit_coin_update("startup")

            # Coin readiness report — shows per-tier availability vs requirements
            # so we know exactly what's available before creating offers
            readiness = self.coin_manager.coin_readiness_report()
            if not readiness.get("overall_ready", True):
                status = readiness.get("overall_status", "UNKNOWN")
                resumed_live_book = len(wallet_open_ids) > 0
                if status == "CRITICAL":
                    log_event("warning", "startup_coins_critical",
                              "COIN READINESS: CRITICAL — some tiers have zero coins! "
                              "Run coin prep before starting offers.")
                else:
                    severity = "info" if resumed_live_book else "warning"
                    event_type = "startup_coins_low_resume" if resumed_live_book else "startup_coins_low"
                    resume_note = (
                        " Existing offers are still live, and topup will rebuild spares as needed."
                        if resumed_live_book else ""
                    )
                    log_event(
                        severity,
                        event_type,
                        f"COIN READINESS: {status} — some tiers are below target. "
                        f"Topup will activate when needed.{resume_note}"
                    )

            # V3: Startup collateral advisory — tells user how XCH is allocated
            try:
                advisory = self.coin_manager.get_startup_advisory()
                assessment = advisory.get("assessment", "UNKNOWN")
                msg = advisory.get("message", "")
                if assessment in ("CRITICAL", "LOW"):
                    log_event("warning", "collateral_advisory",
                              f"COLLATERAL ADVISORY: {msg}")
                else:
                    log_event("info", "collateral_advisory",
                              f"Collateral: {msg}")
            except Exception as e:
                log_event("warning", "advisory_failed",
                          f"Startup advisory failed: {e}")

            # ---- Old-style coin reconciliation — DISABLED (V4 fix) ----
            # This old approach was WRONG for Sage wallet: it used _xch_inventory
            # which includes owned-only coins (from the Sage "selectable" bug
            # workaround). Owned-only coins ARE locked by offers, but this code
            # treated them as "spendable" and freed them — undoing the correct
            # status set by reconcile_coins_with_wallet().
            #
            # The full reconcile below (owned-selectable=locked) handles this
            # correctly, so this block is no longer needed.
            log_event("info", "coin_reconciliation",
                      "Skipping old-style reconcile (replaced by full owned-selectable reconcile)")

            # ---- Full coin reconcile + offer-to-coin linking ----
            # If offers were recovered above, we need to run the full
            # reconcile (owned + selectable) and link offers to locked coins.
            wallet_confirmed_locked = set()
            try:
                from database import reconcile_coins_with_wallet, link_offers_to_locked_coins
                from wallet import get_owned_coins, get_selectable_coins_map

                xch_wid = cfg.WALLET_ID_XCH      # int — don't str(), Sage _is_cat_wallet needs int
                cat_wid = cfg.CAT_WALLET_ID      # int

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
                                    wallet_confirmed_locked.add(cid if cid.startswith("0x") else "0x" + cid)
                                else:
                                    xch_selectable[cid] = info["amount"]
                            cat_owned, cat_selectable = {}, {}
                            for cid, info in cat_detail.items():
                                cat_owned[cid] = info["amount"]
                                if info.get("offer_id"):
                                    wallet_confirmed_locked.add(cid if cid.startswith("0x") else "0x" + cid)
                                else:
                                    cat_selectable[cid] = info["amount"]
                except Exception as e:
                    log_event("warning", "detailed_coin_fetch_failed",
                              f"Detailed Sage coin fetch failed, falling back to basic mode: {e}")

                if not _startup_detailed:
                    xch_owned = get_owned_coins(xch_wid) or {}
                    xch_selectable = get_selectable_coins_map(xch_wid) or {}
                    cat_owned = get_owned_coins(cat_wid) or {}
                    cat_selectable = get_selectable_coins_map(cat_wid) or {}

                # Reconcile XCH coins
                xch_stats = reconcile_coins_with_wallet(
                    wallet_selectable=xch_selectable,
                    wallet_owned=xch_owned,
                    wallet_type='xch'
                )
                # Reconcile CAT coins
                cat_stats = reconcile_coins_with_wallet(
                    wallet_selectable=cat_selectable,
                    wallet_owned=cat_owned,
                    wallet_type='cat'
                )
                log_event("info", "startup_full_reconcile",
                          f"Full reconcile: XCH({xch_stats}) CAT({cat_stats})")

                # Link offers to locked coins (both sides per offer)
                all_wallet_open = open_buys + open_sells
                link_stats = link_offers_to_locked_coins(all_wallet_open, cfg.CAT_ASSET_ID)
                log_event("info", "startup_offer_linking",
                          f"Offer-coin linking: {link_stats}")

            except Exception as e:
                log_event("warning", "startup_reconcile_link_failed",
                          f"Full reconcile + link failed: {e}")

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
                                        store_id = _cid if _cid.startswith("0x") else "0x" + _cid
                                        wallet_confirmed_locked.add(store_id)
                except Exception as e:
                    log_event("debug", "sage_locked_coin_fetch_failed",
                              f"Sage locked coin set build failed (non-critical, proceeding without protection): {e}")
                orphan_stats = cleanup_orphaned_locked_coins(
                    wallet_open_ids,
                    wallet_confirmed_locked=wallet_confirmed_locked
                )
                if orphan_stats["total_freed"] > 0:
                    log_event("info", "startup_orphan_cleanup",
                              f"Freed {orphan_stats['total_freed']} orphaned locked coins "
                              f"({orphan_stats['freed_no_trade']} no trade_id, "
                              f"{orphan_stats['freed_stale_trade']} stale trade_id, "
                              f"{orphan_stats.get('skipped_wallet_locked', 0)} wallet-protected)")
                else:
                    log_event("info", "startup_orphan_cleanup",
                              "No orphaned locked coins found")
            except Exception as e:
                log_event("warning", "startup_orphan_cleanup_failed",
                          f"Orphaned locked coin cleanup failed: {e}")

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
                                datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                                for ts in candidates
                            )
                    except Exception as e:
                        log_event("debug", "sniper_anchor_ts_failed",
                                  f"Sniper anchor timestamp parse failed (using now): {e}")
                    with self._probe_lock:
                        self._probe_state.update({
                            "active": False,
                            "buy_tid": latest_buy.get("trade_id") if latest_buy else None,
                            "sell_tid": latest_sell.get("trade_id") if latest_sell else None,
                            "buy_price": Decimal(str(latest_buy.get("price_xch") or 0)) if latest_buy else Decimal("0"),
                            "sell_price": Decimal(str(latest_sell.get("price_xch") or 0)) if latest_sell else Decimal("0"),
                            "confirmed_at": anchor_ts,
                            "launched_at": anchor_ts,
                        })
                    log_event("info", "sniper_recovery",
                              f"Recovered {len(sniper_ids)} active sniper offer IDs from DB")
            except Exception as e:
                log_event("warning", "sniper_recovery_failed",
                          f"Sniper ID recovery failed: {e}")

            # ---- Gap closer recovery ----
            # Restore gap-closer IDs from database (memory-only in boost_manager)
            # IMPORTANT: Don't just look for status='open' — during gap closer
            # stepping, old offers are marked 'cancelled' in DB when replaced.
            # If bot restarts mid-step, wallet may still have offers the DB
            # says are cancelled. Cross-reference DB boost tier with wallet.
            try:
                # Strategy 1: DB open boost offers (ideal case)
                boost_offers = _get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
                boost_ids = [o["trade_id"] for o in boost_offers
                             if o.get("tier") == "boost"]

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
                            print(f"   📈 Found {len(boost_ids)} boost offers in wallet "
                                  f"(DB status was not 'open' — recovered via cross-reference)",
                                  flush=True)
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
                            evt_data = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
                            recovered_spread = int(evt_data.get("spread_bps", 0))
                            recovered_floor = int(evt_data.get("arb_floor_bps", 0))
                            recovered_steps = int(evt_data.get("steps_taken", 0))
                    except Exception as e:
                        log_event("debug", "gap_closer_recovery_db_failed",
                                  f"Gap closer spread recovery from DB failed (non-critical): {e}")

                    if recovered_spread > 0:
                        self.boost_manager._gap_spread_bps = recovered_spread
                        self.boost_manager._arb_floor_bps = recovered_floor
                        self.boost_manager._steps_taken = recovered_steps
                        self.boost_manager._start_spread_bps = int(
                            getattr(cfg, "BOOST_SPREAD_BPS", recovered_spread))
                        log_event("info", "gap_closer_recovery",
                                  f"Recovered {len(boost_ids)} gap-closer offers — "
                                  f"resuming at {_bps_to_pct(recovered_spread)} (step {recovered_steps}, "
                                  f"floor {_bps_to_pct(recovered_floor)})")
                        print(f"📈 Gap closer recovery: {len(boost_ids)} offers restored "
                              f"at {_bps_to_pct(recovered_spread)} (step {recovered_steps})", flush=True)
                    else:
                        # Fallback: no event data → use config default
                        self.boost_manager._gap_spread_bps = getattr(cfg, "BOOST_SPREAD_BPS", 200)
                        self.boost_manager._start_spread_bps = self.boost_manager._gap_spread_bps
                        log_event("info", "gap_closer_recovery",
                                  f"Recovered {len(boost_ids)} gap-closer offers from DB "
                                  f"(spread data lost — starting fresh at {_bps_to_pct(self.boost_manager._gap_spread_bps)})")
                        print(f"📈 Gap closer recovery: {len(boost_ids)} offers restored "
                              f"(will resume probing from default)", flush=True)
                # Pre-register recovered boost IDs so fill tracker ignores
                # them if they disappear during gap closer stepping
                for tid in boost_ids:
                    self.offer_manager._bot_cancelled_ids.add(tid)
            except Exception as e:
                log_event("warning", "gap_closer_recovery_failed",
                          f"Gap closer ID recovery failed: {e}")

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
                stale_boost_ids = [r[0] for r in all_boost_rows
                                   if r[0] not in self.boost_manager._active_boost_ids]
                for tid in stale_boost_ids:
                    self.offer_manager._bot_cancelled_ids.add(tid)
                if stale_boost_ids:
                    log_event("info", "boost_stale_protection",
                              f"Pre-cancelled {len(stale_boost_ids)} stale boost offers "
                              f"from fill tracker (any status)")
                    print(f"   🛡️ Pre-cancelled {len(stale_boost_ids)} stale boost IDs "
                          f"from fill detection", flush=True)
            except Exception as e:
                log_event("debug", "boost_stale_protection_failed",
                          f"Stale boost ID pre-cancel protection failed (non-critical): {e}")

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
            if total_offers > 0 and cfg.DEXIE_AUTO_POST and not self._startup_repost_done:
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
                    self.amm_monitor.notify_quoted_price(startup_mid, startup_mid)
                    self._current_mid_price = startup_mid
                    with self._probe_lock:
                        if self._probe_state.get("confirmed_price") in (None, Decimal("0")):
                            self._probe_state["confirmed_price"] = startup_mid
                    self._remember_probe_market_snapshot(
                        startup_mid,
                        startup_arb_gap,
                        startup_tibet,
                        "startup_baseline",
                    )
                    baseline_msg = (f"📌 Requote baseline set: {startup_mid:.8f} XCH "
                                    f"(enables requoting + emergency requote)")
                    print(baseline_msg, flush=True)
                    log_event("info", "startup_baseline_price", baseline_msg)
                else:
                    print("⚠️ WARNING: Could not set requote baseline — mid_price is 0!", flush=True)
                    log_event("warning", "startup_baseline_zero",
                              "mid_price was 0 — requoting will be disabled until offers are created")
            except Exception as e:
                err_msg = f"⚠️ WARNING: Could not set baseline price: {e}"
                print(err_msg, flush=True)
                log_event("warning", "startup_baseline_failed", err_msg)

            # ---- V3: Initialize Coinset puzzle hash cache ----
            # Coinset requires a full node for puzzle hashes — skip for Sage light wallet
            wallet_type = getattr(cfg, "WALLET_TYPE", "sage").lower().strip() \
                if hasattr(cfg, "WALLET_TYPE") else os.getenv("WALLET_TYPE", "sage").lower().strip()
            if getattr(cfg, "COINSET_ENABLED", True) and wallet_type != "sage":
                try:
                    ok = self.coinset_client.initialize_puzzle_hashes()
                    if ok:
                        log_event("info", "coinset_ready",
                                  "Coinset puzzle hash cache initialized — fast coin queries enabled")
                        # Pass coinset client to coin_manager for use in queries
                        self.coin_manager._coinset_client = self.coinset_client
                    else:
                        log_event("warning", "coinset_init_skipped",
                                  "Coinset initialization returned no puzzle hashes — "
                                  "using wallet RPC for coin queries")
                except Exception as e:
                    log_event("warning", "coinset_init_error",
                              f"Coinset initialization failed: {e} — "
                              f"using wallet RPC for coin queries")

            inv = self.coin_manager.get_inventory_summary()
            log_event("info", "startup_sync_done",
                      f"Synced: {len(buy_ids)} buys, {len(sell_ids)} sells | "
                      f"Coins — XCH: {inv['xch_trading']} trading, "
                      f"{inv['xch_reserve']} reserve ({inv['xch_reserve_total']}), "
                      f"{inv['xch_small']} small | "
                      f"CAT: {inv['cat_trading']} trading, "
                      f"{inv['cat_reserve']} reserve ({inv['cat_reserve_total']}), "
                      f"{inv['cat_small']} small")

            # Push early dashboard update so Command Centre populates immediately
            # instead of waiting for the first full cycle to complete (~90s).
            _push_startup_market_snapshot()

        except Exception as e:
            log_event("error", "startup_sync_failed", f"Startup sync failed: {e}")

    # -------------------------------------------------------------------
    # One trading cycle
    # -------------------------------------------------------------------

    def _run_one_cycle(self):
        """Execute one complete trading cycle."""
        self._recovery_state["cycle_probe_churn"] = False
        self._recovery_state["cycle_create_stalled"] = False

        # ---- Step 0: Expire stale reservations ----
        try:
            from reservation_manager import ReservationManager
            ReservationManager().expire_stale()
        except Exception as e:
            log_event("warning", "reservation_expire_failed",
                      f"Could not expire stale reservations: {e}")

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
                _prot_secs_known   = float(getattr(cfg, "SWEEP_PROTECTION_SECS",         90))
                _prot_secs_unknown = float(getattr(cfg, "SWEEP_PROTECTION_UNKNOWN_SECS", 30))
                _protected_sides: dict = {}   # side → expiry timestamp
                for _entry in _sweep_evt.fills:
                    if _entry.classification == _FT.ARB_SWEEP_BUY:
                        # Arb bought from us → our SELL offers swept
                        _protected_sides["sell"] = time.time() + _prot_secs_known
                    elif _entry.classification == _FT.ARB_SWEEP_SELL:
                        # Arb sold to us → our BUY offers swept
                        _protected_sides["buy"] = time.time() + _prot_secs_known
                    elif _entry.side in ("buy", "sell"):
                        # Direction from fill side: the offer that got swept
                        _protected_sides[_entry.side] = time.time() + _prot_secs_known
                    else:
                        # No direction data — protect both but only briefly
                        for _s in ("buy", "sell"):
                            _protected_sides.setdefault(
                                _s, time.time() + _prot_secs_unknown
                            )
                self._sweep_protection.update(_protected_sides)

                _prot_summary = {s: round(e - time.time()) for s, e in _protected_sides.items()}
                log_event(
                    "info", "sweep_detected",
                    f"Sweep detected: {_sweep_evt.fill_count} fills in block "
                    f"{_sweep_evt.spent_block_index} — group {_sweep_evt.sweep_group_id}"
                    + (f" — protecting {_prot_summary}"
                       if _protected_sides else ""),
                    data={
                        "sweep_group_id":    _sweep_evt.sweep_group_id,
                        "spent_block_index": _sweep_evt.spent_block_index,
                        "fill_count":        _sweep_evt.fill_count,
                        "trade_ids":         _sweep_evt.trade_ids,
                        "protected_sides":   _prot_summary,
                    },
                )
        except Exception:
            pass  # Sweep coordinator is additive — never block main cycle

        # ---- Step 1: Fetch prices ----
        price_data = self.price_engine.get_price(
            cfg.CAT_ASSET_ID, cfg.CAT_DECIMALS, cfg.CAT_TICKER_ID
        )

        if price_data is None:
            # get_price() returns None when the price fails a safety guard
            # (dynamic band, hard min/max, or step-change guard). The guard
            # already logged the reason. Skip this cycle rather than crashing.
            log_event("warning", "no_price",
                      "Price rejected by safety guard — skipping cycle. "
                      "Check HARD_MAX_PRICE_XCH / HARD_MIN_PRICE_XCH or dynamic band settings.")
            return

        mid_price = Decimal(str(price_data.get("mid_price", 0)))
        if mid_price <= 0:
            log_event("warning", "no_price", "Could not fetch price — skipping cycle")
            return

        self._current_mid_price = mid_price
        self._set_state(mid_price=str(mid_price))

        # Pricing strategy is logged but not emitted as an alert (not actionable)

        # ---- Connectivity recovery check (V1 parity) ----
        # If pricing was down for >30 min and just recovered, repost all offers
        pricing_now = time.time()
        if self._last_pricing_success_ts > 0:
            gap = pricing_now - self._last_pricing_success_ts
            if gap > self._connectivity_gap_threshold:
                log_event("info", "connectivity_recovery",
                          f"Pricing recovered after {gap:.0f}s gap — rechecking Dexie visibility in the background")
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
            print(f"   ⚠️ Spread calc failed: {e}", flush=True)
            # Fallback: use config base spread so modal isn't stuck on zero
            try:
                fallback = int(getattr(cfg, "BASE_SPREAD_BPS", 0) or getattr(cfg, "SPREAD_BPS", 0))
                if fallback > 0:
                    self._set_state(spread_bps=str(fallback))
            except Exception as e2:
                log_event("debug", "spread_fallback_failed",
                          f"Spread fallback also failed: {e2}")

        # Push price update to GUI (after spread calc so it's included)
        self._emit("price_update", {
            "mid_price": str(mid_price),
            "dexie_price": str(price_data.get("dexie_price", "")),
            "tibet_price": str(price_data.get("tibet_price", "")),
            "arb_gap_bps": str(arb_gap),
            "spread_bps": self._bot_state.get("spread_bps", "0"),
        })

        # Terminal heartbeat — every loop (terminal is dev-only, GUI is for users)
        dexie_p = price_data.get("dexie_price", "")
        tibet_p = price_data.get("tibet_price", "")
        print(f"\n{'='*70}", flush=True)
        print(f"💓 Loop {self._loop_count} | mid: {mid_price:.8f} | "
              f"arb gap: {_bps_to_pct(arb_gap)} | "
              f"spread: {_bps_to_pct(self._bot_state.get('spread_bps', '0'))}", flush=True)

        # Console heartbeat — user-visible cycle start
        log_event("info", "cycle_start",
                  f"Cycle #{self._loop_count} — mid price: {mid_price:.8f} XCH, "
                  f"arb gap: {_bps_to_pct(arb_gap)}, spread: {_bps_to_pct(self._bot_state.get('spread_bps', '0'))}")
        baseline_val = self._last_quoted_price.get('sell', Decimal('0'))
        baseline_str = f"{baseline_val:.8f}" if baseline_val > 0 else "pending requote"
        print(f"   Dexie: {dexie_p} | Tibet: {tibet_p} | "
              f"baseline: {baseline_str}",
              flush=True)

        # ---- Step 1b: Refresh market intelligence (NEW — ecosystem) ----
        print(f"   [1b] Market intel...", end="", flush=True)
        log_event("debug", "step1b_intel", "Refreshing market intelligence...")
        try:
            intel_data = self.market_intel.refresh_orderbook()
            if intel_data:
                print(f" competitor spread: {_bps_to_pct(intel_data.get('competitor_spread_bps', '0'))}, "
                      f"thin side: {intel_data.get('thin_side', 'none')}", flush=True)
                payload = {
                    "competitor_spread_bps": str(intel_data.get("competitor_spread_bps", "0")),
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
                    log_event("debug", "splash_stats_failed",
                              f"Splash stats fetch failed (non-critical): {e}")
                try:
                    payload["splash_node"] = self.splash_node.get_status()
                except Exception as e:
                    log_event("debug", "splash_node_status_failed",
                              f"Splash node status fetch failed (non-critical): {e}")
                try:
                    payload["splash_receive"] = self.get_splash_receive_stats()
                except Exception as e:
                    log_event("debug", "splash_receive_stats_failed",
                              f"Splash receive stats failed (non-critical): {e}")
                self._emit("market_intel", payload)
            else:
                print(" no data", flush=True)
        except Exception as e:
            print(f" error: {e}", flush=True)
            log_event("debug", "intel_error", f"Market intel refresh failed: {e}")

        # ---- Step 2: Check circuit breakers ----
        print(f"   [2] Circuit breakers...", end="", flush=True)
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
                print(" ⚠️ PRICE CB — skipping cycle", flush=True)
                log_event("warning", "circuit_breaker",
                          "PRICE circuit breaker — skipping cycle (both sides halted)")
                return
            else:
                # Position CB: position is too large on one side.
                # Cancel the accumulating side (done above) but CONTINUE the cycle
                # so the correcting side can place new offers to reduce position.
                blocked = self.risk_manager.get_circuit_breaker_blocked_side()
                print(f" ⚠️ POSITION CB ({blocked} blocked) — continuing correcting side",
                      flush=True)
                log_event("warning", "circuit_breaker_partial",
                          f"POSITION circuit breaker — '{blocked}' side halted, "
                          f"correcting side continues to reduce position")
                # Fall through — cycle continues below

        else:
            print(" OK", flush=True)
            log_event("debug", "step2_breakers", "Circuit breakers OK")
            self._set_state(status="running")
            self._clear_alert("circuit_breaker")
            self._circuit_breaker_offer_safed = False

        # ---- Step 3: Get current offers from wallet ----
        print(f"   [3] Syncing offers from wallet...", end="", flush=True)
        log_event("debug", "step3_sync", "Syncing offers from wallet...")
        open_buys, open_sells, closed = self.offer_manager.sync_from_wallet()
        wallet_sync_meta = self.offer_manager.get_wallet_sync_meta()
        self._wallet_sync_stale_cycle = not bool(wallet_sync_meta.get("fresh", True))

        current_buy_ids = {o.get("trade_id", "") for o in open_buys if o.get("trade_id")}
        current_sell_ids = {o.get("trade_id", "") for o in open_sells if o.get("trade_id")}

        # Remove recently-created offers now visible in wallet (prevents double-counting)
        self.offer_manager.clean_visible_recently_created(current_buy_ids | current_sell_ids)

        # Prune closed sniper/boost offers so caps stay accurate
        all_open_ids = current_buy_ids | current_sell_ids
        self.sniper.prune_active_snipes(all_open_ids)
        self.boost_manager.prune_active_boosts(all_open_ids)

        self._set_state(open_buys=len(current_buy_ids), open_sells=len(current_sell_ids))

        # ---- Update mempool watcher with current offer coin IDs ----
        # The watcher scans Coinset mempool every 5s for these specific coins.
        # When one appears as a removal (taker spending our locked coin), a
        # fill_imminent signal fires and the bot wakes early for fill detection.
        if _mempool_watcher_mod:
            try:
                w = _mempool_watcher_mod._watcher_instance
                if w:
                    all_open_offers = list(open_buys) + list(open_sells)
                    offer_coin_ids = {
                        o.get("coin_id", "") for o in all_open_offers
                        if o.get("coin_id")
                    }
                    w.set_watched_offer_coins(offer_coin_ids)
            except Exception:
                pass  # Non-critical — watcher degrades gracefully

        print(f" {len(current_buy_ids)} buys, {len(current_sell_ids)} sells, {len(closed)} closed", flush=True)
        log_event("info", "wallet_sync",
                  f"Wallet: {len(current_buy_ids)} open buys, {len(current_sell_ids)} open sells, "
                  f"{len(closed)} closed")
        if self._wallet_sync_stale_cycle:
            err = str(wallet_sync_meta.get("last_error") or "wallet get_offers unavailable")
            cache_note = "cached" if wallet_sync_meta.get("using_cache") else "empty"
            if not self._wallet_sync_was_stale:
                log_event(
                    "warning",
                    "wallet_sync_stale",
                    f"Wallet offer sync stale — using {cache_note} wallet snapshot "
                    f"({len(current_buy_ids)}b/{len(current_sell_ids)}s). {err}",
                )
            self._emit_alert(
                "wallet_offer_sync",
                "warning",
                "Wallet Sync Degraded",
                "Using the last known offer book until Sage responds again. "
                "New fills and coin-management actions are paused.",
                action="restart_sage",
                action_label="Restart Sage",
            )
            self._wallet_sync_was_stale = True
        else:
            if self._wallet_sync_was_stale:
                log_event("info", "wallet_sync_live_again",
                          "Wallet offer sync is fresh again — resuming normal trading actions")
            self._clear_alert("wallet_offer_sync")
            self._wallet_sync_was_stale = False

        # ---- Step 4: Detect fills ----
        print(f"   [4] Checking fills...", end="", flush=True)
        fill_result = self.fill_tracker.detect_fills(
            current_buy_ids, current_sell_ids,
            self.offer_manager._offer_details_cache
        )

        buy_fills = fill_result.get("buy_fills", [])
        sell_fills = fill_result.get("sell_fills", [])

        if buy_fills or sell_fills:
            print(f" 🎯 {len(buy_fills)} buys, {len(sell_fills)} sells FILLED!", flush=True)
            log_event("info", "fills_detected",
                      f"Fills this cycle: {len(buy_fills)} buys, {len(sell_fills)} sells")
            # Push fill events to GUI instantly
            for fill in buy_fills + sell_fills:
                self._emit("fill", {
                    "side": fill.get("side", ""),
                    "price": str(fill.get("price", "")),
                    "size_xch": str(fill.get("size_xch", "")),
                    "tier": fill.get("tier", ""),
                })
            # Reset coin backoff on fills (new coins freed up)
            self.coin_manager.reset_backoff()
            # Coin snapshot after fills — coins consumed + new coins received
            self.coin_manager.snapshot_coins("offer_filled")
            self._emit_coin_update("offer_filled")

        if not buy_fills and not sell_fills:
            print(" none", flush=True)
            log_event("debug", "step4_fills", "No fills this cycle")

        # ---- AMM drift check — force requote if AMM price has moved ----
        # If AMMMonitor has data, check whether the current AMM price has
        # drifted far enough from our last quoted prices to make our offers
        # arb targets. If so, flag both sides for requote immediately.
        try:
            if self.amm_monitor.is_available():
                amm_drift_bps = self.amm_monitor.get_drift_bps()
                if amm_drift_bps is not None:
                    _drift_threshold = Decimal(str(getattr(cfg, "AMM_DRIFT_REQUOTE_BPS", "40")))
                    if amm_drift_bps >= _drift_threshold:
                        if not self._force_requote.get("buy") or not self._force_requote.get("sell"):
                            log_event("info", "amm_drift_requote_triggered",
                                      f"AMM drift {amm_drift_bps:.1f}bps ≥ {_drift_threshold}bps "
                                      f"— forcing requote both sides",
                                      data={"drift_bps": str(amm_drift_bps.quantize(Decimal("0.1")))})
                        self._force_requote["buy"] = True
                        self._force_requote["sell"] = True
        except Exception as _amm_drift_err:
            log_event("debug", "amm_drift_check_error",
                      f"AMM drift check error (non-critical): {_amm_drift_err}")

        fills_hour = None
        try:
            from database import count_recent_fills
            fills_hour = count_recent_fills(hours=1)
            self.risk_manager.update_fill_rate(Decimal(str(fills_hour)))
        except Exception as e:
            log_event("debug", "fill_rate_update_failed",
                      f"Fill-rate update failed: {e}")

        # V3: Record trading pace after fill detection
        if buy_fills or sell_fills:
            try:
                from database import record_trading_pace
                if fills_hour is None:
                    from database import count_recent_fills
                    fills_hour = count_recent_fills(hours=1)
                if fills_hour > getattr(cfg, "FILLS_PER_HOUR_BUSY", 10):
                    pace = 'busy'
                elif fills_hour < getattr(cfg, "FILLS_PER_HOUR_SLOW", 2):
                    pace = 'slow'
                else:
                    pace = 'normal'
                active_count = len(current_buy_ids) + len(current_sell_ids)
                record_trading_pace(fills_hour, pace, active_count)
                log_event("debug", "trading_pace",
                          f"Trading pace: {pace} ({fills_hour} fills/hr)")
            except Exception as e:
                log_event("debug", "pace_record_failed", f"Pace recording failed: {e}")

        # ---- Step 5: Match round-trip PnL ----
        # Always attempt matching — catches newly detected fills AND
        # previously-unmatched fills (e.g. after migration clears bad matches).
        matched = self.fill_tracker.match_round_trips()
        if matched:
            total_pnl = sum(m.get("pnl_xch", Decimal("0")) for m in matched)
            log_event("info", "pnl_matched",
                      f"Matched {len(matched)} round-trips, "
                      f"total PnL: {total_pnl:+.8f} XCH")

        # ---- Step 6: Update inventory ----
        print(f"   [6] Updating inventory...", end="", flush=True)
        self.risk_manager.update_inventory()
        inv = self.risk_manager.get_inventory_state()
        net_pos = inv.get("net_position_cat", "0")
        print(f" net position: {net_pos} CAT", flush=True)
        log_event("debug", "step6_inventory", f"Inventory updated — net position: {net_pos} CAT")

        # ---- Step 7: Pre-emptive offer refresh ----
        # Detect offers approaching expiry and cancel them early so Step 10
        # can replace them at the current best price with correct tier sizing.
        # With 24h expiry and 30min refresh window, offers get replaced smoothly
        # with zero gap in market presence.
        expired = 0
        if (
            cfg.OFFER_EXPIRY_SECS > 0
            and not getattr(self, '_graceful_in_progress', False)
            and not self._recovery_is_active()
        ):
            all_open = open_buys + open_sells
            expiring_tids = self.offer_manager.detect_expiring_offers(
                all_open, refresh_before_secs=cfg.OFFER_REFRESH_BEFORE)
            if expiring_tids:
                log_event("info", "step7_refresh",
                          f"Pre-emptive refresh: cancelling {len(expiring_tids)} "
                          f"offers expiring within {cfg.OFFER_REFRESH_BEFORE}s")
                cancel_result = self.offer_manager.cancel_offers(
                    expiring_tids, reason="pre_emptive_refresh")
                expired = sum(1 for r in cancel_result.values()
                              if r and r.get("success"))
                # Update live counts so Step 10 sees the slots as free
                # and creates replacements THIS loop, not next loop
                cancelled_set = {tid for tid, r in cancel_result.items()
                                 if r and r.get("success")}
                current_buy_ids -= cancelled_set
                current_sell_ids -= cancelled_set
        else:
            log_event("debug", "step7_skipped", "Offer expiry disabled — no cleanup needed")

        # ---- Step 7b: Spacescan balance verification (periodic health check) ----
        # Every N loops, compare wallet balance vs on-chain truth.
        # Free tier users: balance checks are skipped (budget reserved for fill verification).
        # Pro tier users: runs every SPACESCAN_BALANCE_CHECK_EVERY_N loops.
        spacescan_check_every = getattr(cfg, "SPACESCAN_BALANCE_CHECK_EVERY_N", 10)
        if (getattr(cfg, "SPACESCAN_ENABLED", False) and
                self._loop_count > 0 and
                self._loop_count % spacescan_check_every == 0):
            try:
                from spacescan import check_balance_discrepancy, should_check_balance
                # Free tier: skip balance checks to preserve API budget for fills
                if not should_check_balance():
                    log_event("debug", "spacescan_balance_skip",
                              "Spacescan free tier — skipping balance check (budget reserved for fills)")
                    raise ImportError("skip")  # Jump to except block cleanly
                # Get wallet's reported balance
                from wallet import get_wallet_balance
                wallet_bal = get_wallet_balance(cfg.WALLET_ID_XCH)
                wallet_xch = Decimal("0")
                if wallet_bal and wallet_bal.get("success"):
                    wallet_xch = Decimal(str(wallet_bal.get("confirmed_wallet_balance", 0))) / Decimal("1000000000000")

                our_address = getattr(cfg, "WALLET_ADDRESS", "")
                if our_address and wallet_xch > 0:
                    result = check_balance_discrepancy(our_address, wallet_xch)
                    if result.get("xch_ok"):
                        log_event("debug", "spacescan_balance_ok",
                                  f"Balance check OK — Wallet: {wallet_xch:.4f}, "
                                  f"On-chain: {result.get('xch_onchain', '?')}")
                    else:
                        print(f"\n   ⚠️  BALANCE MISMATCH! Wallet: {wallet_xch:.4f} XCH, "
                              f"On-chain: {result.get('xch_onchain', '?')} XCH", flush=True)
            except ImportError:
                pass  # spacescan module not available
            except Exception as e:
                log_event("debug", "spacescan_balance_error",
                          f"Spacescan balance check failed: {e}")

        # ---- Step 7c: Retry failed cancels (V1 parity) ----
        retried = self.offer_manager.retry_failed_cancels()
        if retried > 0:
            log_event("info", "cancel_retries", f"Retried {retried} failed cancels")

        # Update cancel retry alert
        pending_retries = len(self.offer_manager._pending_cancel_retries)
        if pending_retries > 0:
            self._emit_alert("cancel_retries", "warning",
                f"{pending_retries} stuck cancel(s)",
                "Some offers failed to cancel and are queued for retry.",
                action="stop_bot", action_label="Stop Bot")
        else:
            self._clear_alert("cancel_retries")

        self._maybe_finalize_graceful_migration(
            current_buy_ids=current_buy_ids,
            current_sell_ids=current_sell_ids,
        )

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

            cancel_result = self.offer_manager.cancel_offers(
                live_probe_ids,
                reason=reason,
            )
            cancelled = {
                tid for tid, res in cancel_result.items()
                if res and res.get("success")
            }
            failed = [tid for tid in live_probe_ids if tid not in cancelled]

            if cancelled:
                current_buy_ids.difference_update(cancelled)
                current_sell_ids.difference_update(cancelled)
                self._set_state(open_buys=len(current_buy_ids), open_sells=len(current_sell_ids))
                with self.sniper._snipe_lock:
                    self.sniper._active_snipe_ids = [
                        tid for tid in self.sniper._active_snipe_ids
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
                    log_event("warning", "probe_retire_recovery_failed",
                              "Recovery: probe offer cancel failed — step 8c will retry "
                              "via sniper_cleanup. Main ladder will build around it.")
            self._clear_alert("probe_status")
            log_event("debug", "sniper_recovery_pause",
                      "Recovery mode active — skipping sniper probe and gap-closer churn")
        elif not _sniper_on:
            log_event("debug", "sniper_disabled", "Sniper disabled via config — skipping")

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

            # Check which probes survived (still in wallet open offers)
            buy_alive = buy_tid and buy_tid in (current_buy_ids | current_sell_ids)
            sell_alive = sell_tid and sell_tid in (current_buy_ids | current_sell_ids)

            # If sell was never placed (no CAT sniper coins), buy-only probe is sufficient
            sell_required = bool(sell_tid)
            if buy_alive and (sell_alive or not sell_required):
                remaining_hold = self._probe_hold_seconds_remaining(probe, now_ts)
                if remaining_hold > 0:
                    last_notice_at = float(probe.get("last_wait_log_at") or 0)
                    if (now_ts - last_notice_at) >= 5:
                        probe_age = max(0.0, now_ts - float(probe.get("launched_at") or now_ts))
                        log_event(
                            "info",
                            "probe_hold_wait",
                            f"Both probes alive for {probe_age:.1f}s — holding "
                            f"{remaining_hold:.1f}s more before confirming",
                        )
                        probe["last_wait_log_at"] = now_ts
                    log_event("debug", "probe_hold_status",
                              "Probe pair still aging before main ladder deploy")
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
                    linger_secs = int(getattr(cfg, "SNIPER_LINGER_SECS", 600) or 0)
                    log_event("info", "probe_confirmed",
                              f"Both probes survived — price confirmed at Tibet "
                              f"{probe['tibet_price']:.8f}. Keeping probes live for "
                              f"{linger_secs}s while building main offers behind them.")
                    log_event("info", "probe_confirmed_status",
                              "Price confirmed — deploying main offers behind live probes")
                    self._clear_alert("probe_status")

            elif probe["attempt"] >= probe["max_attempts"]:
                # Too many retries — use whatever we have and proceed
                survived_price = probe["sell_price"] if sell_alive else (
                    probe["buy_price"] if buy_alive else probe["tibet_price"])
                probe["confirmed_price"] = survived_price
                probe["confirmed_at"] = now_ts
                probe["active"] = False
                self._mark_recovery_probe_churn()
                log_event("warning", "probe_max_retries",
                          f"Probe gave up after {probe['attempt']} attempts. "
                          f"Using best available price: {survived_price:.8f}")
                log_event("warning", "probe_timeout_status",
                          "Probe timed out — deploying main offers from best probe edge")
                self._clear_alert("probe_status")

            else:
                # At least one probe was taken — adjust and retry
                attempt = probe["attempt"] + 1
                base_buffer = getattr(cfg, "SNIPER_BUFFER_BPS", Decimal("50"))
                # Widen buffer by 50 BPS per failed attempt
                adjusted_buffer = base_buffer + Decimal(str(attempt * 50))
                tibet_p = probe["tibet_price"]
                probe_prices = self._get_market_aware_probe_prices(
                    tibet_p,
                    adjusted_buffer,
                )

                taken_sides = []
                if not buy_alive and buy_tid:
                    taken_sides.append("buy")
                if not sell_alive and sell_tid:
                    taken_sides.append("sell")

                log_event("info", "probe_retry",
                          f"Probe attempt {attempt}: {'+'.join(taken_sides)} taken, "
                          f"widening buffer to {adjusted_buffer} BPS")
                log_event("info", "probe_retry_status",
                          f"Probe retry {attempt} — widening spread")
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
                        "sell", new_sell_price, arb_gap)
                # Retry buy only if taken (alive→gone) OR never placed
                # (use truthy check to exclude empty-string trade_id "")
                if (buy_tid and not buy_alive) or not buy_tid:
                    self.sniper._last_snipe_time = 0  # Reset cooldown
                    buy_results = self.sniper.try_snipe_single(
                        "buy", new_buy_price, arb_gap)

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
                    sniper_fired = sniper_fired or probe_result.get("sniper_fired", False)

        # ---- Phase 1: LAUNCH new probe (empty book or material market shift) ----
        elif arb_gap > cfg.SNIPER_MIN_GAP_BPS and launch_reason:
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
                    sniper_buffer_bps = getattr(cfg, "SNIPER_BUFFER_BPS", Decimal("50"))
                    probe_prices = self._get_market_aware_probe_prices(
                        tibet_p,
                        sniper_buffer_bps,
                    )
                    sell_price = probe_prices["sell_price"]
                    buy_price = probe_prices["buy_price"]
                    book_bid = probe_prices["overall_best_bid"]
                    book_ask = probe_prices["overall_best_ask"]

                    log_event("info", "probe_launch",
                              f"Launching price probe — {launch_reason}: "
                              f"buy={buy_price:.8f}, sell={sell_price:.8f} "
                              f"(Tibet={tibet_p:.8f}, Dexie={dexie_p:.8f}, "
                              f"book_bid={book_bid:.8f}, book_ask={book_ask:.8f})")
                    log_event("info", "probe_launch_status",
                              "Probing market — testing edge prices...")
                    self._clear_alert("probe_status")

                    total_snipes = 0
                    sell_tid = None
                    buy_tid = None

                    # Fire both sides in parallel — halves the probe deployment time
                    self.sniper._last_snipe_time = 0  # Reset cooldown for both
                    _probe_results = {}
                    _probe_cycle_valid = [True]  # Mutable flag; set False if we abandon

                    def _fire_probe(side, price):
                        result = self.sniper.try_snipe_single(side, price, arb_gap)
                        if _probe_cycle_valid[0]:  # Only write if cycle not abandoned
                            _probe_results[side] = result

                    sell_thread = threading.Thread(target=_fire_probe, args=("sell", sell_price))
                    buy_thread = threading.Thread(target=_fire_probe, args=("buy", buy_price))
                    sell_thread.start()
                    buy_thread.start()
                    sell_thread.join(timeout=30)
                    buy_thread.join(timeout=30)

                    if sell_thread.is_alive():
                        log_event("warning", "probe_thread_timeout",
                                  "Sell probe thread timed out after 30s — "
                                  "proceeding with buy-only probe")
                    if buy_thread.is_alive():
                        _probe_cycle_valid[0] = False  # Abandon: late result would corrupt state
                        log_event("warning", "probe_thread_timeout",
                                  "Buy probe thread timed out after 30s — "
                                  "probe skipped this cycle (cycle abandoned)")

                    sell_results = _probe_results.get("sell")
                    buy_results = _probe_results.get("buy")

                    if sell_results:
                        sell_tid = sell_results[0].get("trade_id", "")
                        total_snipes += 1
                    if buy_results:
                        buy_tid = buy_results[0].get("trade_id", "")
                        total_snipes += 1

                    if total_snipes > 0:
                        sniper_fired = True
                        with self._probe_lock:
                            self._probe_state.update({
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
                            })
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
                        sniper_fired = sniper_fired or probe_result.get("sniper_fired", False)

        elif arb_gap > cfg.SNIPER_MIN_GAP_BPS and not launch_reason:
            log_event("debug", "sniper_no_swap",
                      f"Arb gap {_bps_to_pct(arb_gap)} but no fresh sniper trigger "
                      f"(startup or material market shift) — keeping sniper idle")

        # ---- Step 8b: Re-evaluate prices after sniper ----
        # If the sniper just fired, the market has moved. Fetch fresh prices
        # so the main offer batch (Step 10) uses post-snipe pricing.
        if sniper_fired:
            log_event("info", "post_snipe_reprice",
                      "Sniper fired — re-fetching prices before creating main offer batch")
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

                log_event("info", "post_snipe_price",
                          f"Post-snipe price: {old_mid:.8f} → {fresh_mid:.8f} "
                          f"(arb gap now {_bps_to_pct(arb_gap)})")

                # Push updated price to GUI
                self._emit("price_update", {
                    "mid_price": str(mid_price),
                    "dexie_price": str(fresh_price.get("dexie_price", "")),
                    "tibet_price": str(fresh_price.get("tibet_price", "")),
                    "arb_gap_bps": str(arb_gap),
                    "spread_bps": self._bot_state.get("spread_bps", "0"),
                })

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
            recent_swap
            and arb_gap > cfg.ARB_ALERT_THRESHOLD_BPS
            and mid_price > 0
        )
        if emergency_requote_triggered and not recovery_active_now:
            for eq_side in ["sell", "buy"]:
                last_q = self._last_quoted_price.get(eq_side, Decimal("0"))
                if last_q <= 0:
                    continue

                move_bps = abs(mid_price - last_q) / last_q * Decimal("10000")

                is_vulnerable = (
                    (eq_side == "sell" and mid_price > last_q) or
                    (eq_side == "buy" and mid_price < last_q)
                )

                if is_vulnerable and move_bps > cfg.ARB_ALERT_THRESHOLD_BPS:
                    msg = (f"⚡ EMERGENCY requote of {eq_side} side — "
                           f"price shock {_bps_to_pct(move_bps)} "
                           f"({last_q:.8f} → {mid_price:.8f}, "
                           f"arb gap: {_bps_to_pct(arb_gap)})")
                    print(msg, flush=True)  # Terminal-visible
                    log_event("warning", "emergency_requote", msg)

                    spread = self.risk_manager.get_adjusted_spread(eq_side)
                    requote_mid = self._get_probe_anchored_mid(eq_side, mid_price)
                    price_cap = self._get_probe_price_boundary(eq_side) if eq_side == "buy" else None
                    price_floor = self._get_probe_price_boundary(eq_side) if eq_side == "sell" else None
                    if requote_mid != mid_price:
                        log_event(
                            "info",
                            "probe_anchor_emergency",
                            f"Anchoring {eq_side} emergency requote to probe edge: "
                            f"mid {mid_price:.8f} -> {requote_mid:.8f}",
                        )
                    _live_ids = current_buy_ids if eq_side == "buy" else current_sell_ids
                    new_offers = self.offer_manager.requote_side(
                        eq_side, requote_mid,
                        dexie_manager=self.dexie_manager,
                        risk_manager=self.risk_manager,
                        spread_fraction=spread,
                        price_cap=price_cap,
                        price_floor=price_floor,
                        live_offer_ids=_live_ids,
                    )
                    self._last_quoted_price[eq_side] = requote_mid
                    buy_q = self._last_quoted_price.get("buy")
                    sell_q = self._last_quoted_price.get("sell")
                    self.amm_monitor.notify_quoted_price(buy_q, sell_q)
                    self.coin_manager.snapshot_coins("emergency_requote")
                    self._emit_coin_update("emergency_requote")
                    self._last_bulk_create_time = time.time()

                    done_msg = (f"✅ Emergency requote {eq_side}: "
                                f"{len(new_offers)} new offers at {requote_mid:.8f}")
                    print(done_msg, flush=True)  # Terminal-visible
                    log_event("info", "emergency_requote_done", done_msg)

        # ---- Step 8c-pre: Monitor confirmed probes — do not auto re-fire ----
        # Once discovery is done, the main ladder should take over. If an edge
        # probe gets consumed, clear that side and wait for a fresh market move
        # before probing again.
        if (not recovery_active_now
                and not self._probe_state.get("active", False)
                and self._probe_state.get("confirmed_price")
                and self.sniper._active_snipe_ids
                and self._loop_count >= 3):
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
                    f"Confirmed probe {'+'.join(taken_sides)} was consumed — main ladder "
                    f"stays live and sniper will wait for a fresh market move",
                )

        # ---- Step 8c: Clean up sniper offers once their edge window expires ----
        # Discovery probes are temporary edge markers. Leave them up long enough
        # for the main ladder to settle behind them, then remove them whether the
        # arb gap has closed or not.
        if (not recovery_active_now
                and not sniper_fired
                and self.sniper._active_snipe_ids
                and not self._probe_state.get("active", False)
                and self._loop_count >= 3
                and self._probe_cleanup_seconds_remaining(
                    self._probe_state, time.time()
                ) <= 0):
            snipe_ids_to_cancel = list(self.sniper._active_snipe_ids)
            cleanup_reason = (
                "gap_closed"
                if arb_gap <= cfg.ARB_ALERT_THRESHOLD_BPS
                else "linger_elapsed"
            )
            log_event("info", "sniper_cleanup",
                      f"Sniper edge window ended ({cleanup_reason}) — cancelling "
                      f"{len(snipe_ids_to_cancel)} sniper offers")
            try:
                result = self.offer_manager.cancel_offers(
                    snipe_ids_to_cancel, reason="sniper_cleanup")
                cancelled_ids = [
                    tid for tid, res in (result or {}).items()
                    if res and res.get("success")
                ]
                failed_ids = [
                    tid for tid, res in (result or {}).items()
                    if not (res and res.get("success"))
                ]
                cancelled = len(cancelled_ids)
                log_event("info", "sniper_cleaned",
                          f"Cancelled {cancelled}/{len(snipe_ids_to_cancel)} sniper offers"
                          + (" — coins freed for main offer batch" if cancelled else "")
                          + (f"; {len(failed_ids)} still active and queued for retry"
                             if failed_ids else ""))
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
                log_event("warning", "sniper_cleanup_failed",
                          f"Failed to cancel sniper offers: {e}")

        # ---- Step 8d: Close the Gap — adaptive spread probing ----
        if (not recovery_active_now
                and not sniper_fired
                and self.sniper._active_snipe_ids
                and not self._probe_state.get("active", False)
                and self._loop_count >= 3):
            linger_remaining = self._probe_cleanup_seconds_remaining(
                self._probe_state, time.time()
            )
            if linger_remaining > 0:
                log_event("debug", "sniper_cleanup_wait",
                          f"Sniper linger active â€” keeping probes live for "
                          f"{linger_remaining:.1f}s more before cleanup")

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
                log_event("debug", "gap_closer_skip_busy",
                          "Gap closer step skipped — coin topup in progress")
            else:
                stepped = self.boost_manager.step_tighter(arb_gap)

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
                print(f"   [8d] Gap closer step {state['steps_taken']}: "
                      f"now {_bps_to_pct(state['current_spread_bps'])} "
                      f"(floor: {_bps_to_pct(state['arb_floor_bps'])}) — "
                      f"cascade will tighten main book",
                      flush=True)

            if converged:
                # Convergence changed — cascade will handle replacing stale offers
                # in small batches.  Same reasoning: full requote blocks the loop.
                factor = self.boost_manager.get_convergence_factor()
                self._emit("boost", state)
                print(f"   [8d] Main book converging — now {factor*100:.0f}% of original — "
                      f"cascade will tighten main book", flush=True)

            # 4. Cascade: after probe survives ~60s, replace stale main
            #    book offers with tighter ones behind the proven level.
            #    CRITICAL: Creates new offers FIRST, then cancels stale.
            #    Never wipes the orderbook. Works in batches per cycle.
            cascaded = False
            if not stepped and not converged and self.boost_manager.should_cascade():
                if not self.coin_manager.is_busy() and not getattr(self, '_graceful_in_progress', False):
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
                        print(f"   [8d] 🔄 CASCADE: +{tc} new, -{tk} stale "
                              f"(probe at {_bps_to_pct(state['current_spread_bps'])})",
                              flush=True)

            if not refreshed and not stepped and not converged and not cascaded:
                print(f"   [8d] Gap closer: {_bps_to_pct(state['current_spread_bps'])} "
                      f"({state['steps_taken']} steps, "
                      f"floor: {_bps_to_pct(state['arb_floor_bps'])}, "
                      f"next step in {state['secs_until_step']}s)",
                      flush=True)

        # ---- Step 9: Requote if price moved or forced by convergence ----
        force_buy = self._force_requote.get("buy", False)
        force_sell = self._force_requote.get("sell", False)
        force_tag = f" FORCED!" if (force_buy or force_sell) else ""
        print(f"   [9] Requote check...{force_tag}", end="", flush=True)
        log_event("debug", "step9_requote",
                  f"Checking requote: last_buy={self._last_quoted_price.get('buy', 0)}, "
                  f"last_sell={self._last_quoted_price.get('sell', 0)}, mid={mid_price}, "
                  f"force_buy={force_buy}, force_sell={force_sell}")
        self._handle_requoting(mid_price, current_buy_ids, current_sell_ids)
        print(" done", flush=True)
        log_event("debug", "step9_done", "Requote check done")

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
                _xch_bal = _get_wallet_balance(cfg.WALLET_ID_XCH)
                _xch_total = (
                    Decimal(str(_xch_bal.get("confirmed_wallet_balance", 0)))
                    / Decimal("1000000000000")
                    if _xch_bal else Decimal("0")
                )
                _xch_near_floor = _xch_reserve * Decimal("1.05")

                if _xch_total <= _xch_reserve:
                    log_event("error", "reserve_floor_breached",
                              f"XCH balance ({_xch_total:.4f}) ≤ XCH_RESERVE ({_xch_reserve}) — "
                              f"cancelling all open offers to protect reserve")
                    self._emit_alert(
                        "reserve_floor",
                        "error",
                        "Reserve Floor Breached",
                        f"XCH balance ({_xch_total:.4f} XCH) has reached the reserve floor "
                        f"({_xch_reserve} XCH). Cancelling open offers to free locked coins.",
                        action="stop_bot",
                        action_label="Stop Bot",
                    )
                    _all_open = list(current_buy_ids | current_sell_ids)
                    if _all_open:
                        self.offer_manager.cancel_offers(_all_open, reason="reserve_floor_breached")
                        current_buy_ids.clear()
                        current_sell_ids.clear()
                    _reserve_skip_create = True
                elif _xch_total <= _xch_near_floor:
                    log_event("warning", "reserve_floor_near",
                              f"XCH balance ({_xch_total:.4f}) approaching reserve floor "
                              f"({_xch_reserve}) — skipping offer creation")
                    self._emit_alert(
                        "reserve_floor",
                        "warning",
                        "Reserve Floor Approaching",
                        f"XCH balance ({_xch_total:.4f} XCH) is within 5% of reserve floor "
                        f"({_xch_reserve} XCH). Offer creation paused.",
                    )
                    _reserve_skip_create = True
                else:
                    self._clear_alert("reserve_floor")

            if not _reserve_skip_create and _cat_reserve > Decimal("0"):
                _cat_bal = _get_wallet_balance(cfg.CAT_WALLET_ID)
                _cat_scale = Decimal(10) ** Decimal(str(cfg.CAT_DECIMALS))
                _cat_total = (
                    Decimal(str(_cat_bal.get("confirmed_wallet_balance", 0))) / _cat_scale
                    if _cat_bal else Decimal("0")
                )
                _cat_near_floor = _cat_reserve * Decimal("1.05")

                if _cat_total <= _cat_reserve:
                    log_event("error", "reserve_floor_breached",
                              f"CAT balance ({_cat_total:,.2f}) ≤ CAT_RESERVE ({_cat_reserve}) — "
                              f"cancelling all open offers to protect reserve")
                    self._emit_alert(
                        "reserve_floor",
                        "error",
                        "Reserve Floor Breached",
                        f"CAT balance ({_cat_total:,.2f}) has reached the reserve floor "
                        f"({_cat_reserve}). Cancelling open offers to free locked coins.",
                        action="stop_bot",
                        action_label="Stop Bot",
                    )
                    _all_open = list(current_buy_ids | current_sell_ids)
                    if _all_open:
                        self.offer_manager.cancel_offers(_all_open, reason="reserve_floor_breached")
                        current_buy_ids.clear()
                        current_sell_ids.clear()
                    _reserve_skip_create = True
                elif _cat_total <= _cat_near_floor:
                    log_event("warning", "reserve_floor_near",
                              f"CAT balance ({_cat_total:,.2f}) approaching reserve floor "
                              f"({_cat_reserve}) — skipping offer creation")
                    self._emit_alert(
                        "reserve_floor",
                        "warning",
                        "Reserve Floor Approaching",
                        f"CAT balance ({_cat_total:,.2f}) is within 5% of reserve floor "
                        f"({_cat_reserve}). Offer creation paused.",
                    )
                    _reserve_skip_create = True
        except Exception as _rf_err:
            log_event("debug", "reserve_floor_check_error",
                      f"Reserve floor check failed (non-fatal): {_rf_err}")

        # ---- Step 10: Create new offers if needed ----
        print(f"   [10] Offers: buys {len(current_buy_ids)}/{cfg.MAX_ACTIVE_BUY_OFFERS}, "
              f"sells {len(current_sell_ids)}/{cfg.MAX_ACTIVE_SELL_OFFERS}", flush=True)
        log_event("debug", "step10_create",
                  f"Check create: buys={len(current_buy_ids)}/{cfg.MAX_ACTIVE_BUY_OFFERS}, "
                  f"sells={len(current_sell_ids)}/{cfg.MAX_ACTIVE_SELL_OFFERS}")
        if _reserve_skip_create:
            log_event("warning", "step10_skipped_reserve",
                      "Skipping offer creation — reserve floor guard active")
            return

        # Sweep protection: skip re-posting on sides swept in the last cycle.
        # The dynamic AMM buffer has already widened; once it expires the bot
        # will requote at the wider buffer.  Log once per side, clear expired.
        _now = time.time()
        _skip_buy  = _now < self._sweep_protection.get("buy",  0)
        _skip_sell = _now < self._sweep_protection.get("sell", 0)
        for _s in ("buy", "sell"):
            if _now >= self._sweep_protection.get(_s, 0):
                self._sweep_protection.pop(_s, None)
        if _skip_buy or _skip_sell:
            _skipped = [s for s, skip in (("buy", _skip_buy), ("sell", _skip_sell)) if skip]
            log_event("info", "sweep_protection_active",
                      f"Sweep protection: skipping {_skipped} offer creation this cycle",
                      data={"protected_sides": _skipped})

        self._create_offers_if_needed(
            mid_price,
            len(current_buy_ids),
            len(current_sell_ids),
            current_buy_ids=current_buy_ids,
            current_sell_ids=current_sell_ids,
            arb_gap=arb_gap,
            skip_buy=_skip_buy,
            skip_sell=_skip_sell,
        )

        # ---- Step 11: Direct post to Dexie first ----
        if cfg.DEXIE_AUTO_POST:
            q_len = len(self.dexie_manager._queue)
            if q_len > 0:
                print(f"   [11] Posting {q_len} offers to Dexie...", end="", flush=True)
                log_event("debug", "dexie_flush_start", f"Flushing {q_len} offers to Dexie...")
            result = self.dexie_manager.flush_queue()
            if q_len > 0:
                print(f" {result}", flush=True)
                log_event("info", "dexie_flush_result",
                          f"Dexie flush: {result}")
            else:
                print(f"   [11] Dexie queue empty", flush=True)
        else:
            print(f"   [11] Dexie auto-post OFF", flush=True)
            log_event("debug", "dexie_disabled", "DEXIE_AUTO_POST is off")

        # ---- Step 11b: Broadcast to Splash after Dexie visibility is live ----
        if getattr(cfg, "SPLASH_ENABLED", False):
            try:
                splash_q = len(self.splash_manager._queue)
                if splash_q > 0:
                    print(f"   [11b] Broadcasting {splash_q} offers to Splash...", end="", flush=True)
                    log_event("debug", "splash_flush_start",
                              f"Broadcasting {splash_q} offers to Splash...")
                result = self.splash_manager.flush_queue()
                if splash_q > 0:
                    print(f" {result}", flush=True)
                    log_event("info", "splash_flush_result",
                              f"Splash broadcast: {result}")
                else:
                    print(f"   [11b] Splash queue empty", flush=True)
            except Exception as e:
                print(f"   [11b] Splash broadcast error: {e}", flush=True)
                log_event("debug", "splash_error", f"Splash flush failed: {e}")
        else:
            print(f"   [11b] Splash OFF", flush=True)

        # ---- Step 11c: Cross-post to Offerpool (NEW — ecosystem) ----
        if getattr(cfg, "OFFERPOOL_ENABLED", False):
            try:
                self.market_intel.flush_offerpool_queue()
            except Exception as e:
                log_event("debug", "offerpool_error", f"Offerpool flush failed: {e}")

        # ---- Step 12: Coin management ----
        print(f"   [12] Coin health...", end="", flush=True)
        self._handle_coins(len(current_buy_ids), len(current_sell_ids))
        print(" done", flush=True)
        log_event("debug", "step12_coins", "Coin health check done")

        # ---- Step 12b: Recovery mode evaluation ----
        # Subtract any confirmed probe slots so the probe offer doesn't inflate
        # the apparent buy count and mask a genuine under-target condition.
        _probe_offsets = self._confirmed_probe_slot_offsets(current_buy_ids, current_sell_ids)
        self._evaluate_recovery_mode(
            mid_price,
            max(0, len(current_buy_ids) - _probe_offsets["buy"]),
            max(0, len(current_sell_ids) - _probe_offsets["sell"]),
        )

        # ---- Step 13: Housekeeping ----
        print(f"   [13-15] Housekeeping + inventory + GUI push...", end="", flush=True)
        self._handle_housekeeping()

        # ---- Step 14: Record inventory snapshot ----
        self.risk_manager.record_snapshot(mid_price=mid_price)

        # ---- Step 15: Push full state to GUI ----
        self._emit("state", {
            "loop_count": self._loop_count,
            "mid_price": str(mid_price),
            "open_buys": len(current_buy_ids),
            "open_sells": len(current_sell_ids),
            "status": self._bot_state.get("status", "running"),
            "spread_bps": self._bot_state.get("spread_bps", "0"),
        })

        # Push dashboard command centre update (market health + performance)
        try:
            health_data = self._augment_health_with_spacescan(
                self.risk_manager.get_market_health()
            )
            offer_edges = self._get_live_offer_edges(open_buys, open_sells)
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

            self._emit("dashboard_update", {
                "market_health": health_data,
                "loop_count": self._loop_count,
                "uptime_secs": int(time.time() - self._start_time) if self._start_time else 0,
                "last_loop_time": round(self._last_loop_duration, 2),
                "open_buys": len(current_buy_ids),
                "open_sells": len(current_sell_ids),
                "mid_price": str(mid_price),
                "competitor_spread_bps": _competitor_bps,
                "fills_per_hour": _fills_hr,
                "our_best_bid": offer_edges.get("our_best_bid", "0"),
                "our_best_ask": offer_edges.get("our_best_ask", "0"),
                "best_bid": _best_bid,
                "best_ask": _best_ask,
            })
        except Exception as e:
            print(f"   [15] Dashboard emit error: {e}", flush=True)

        print(f" done ✅", flush=True)
        log_event("debug", "step15_gui", "Housekeeping + inventory + GUI push done")

        # Console cycle summary — one clean line showing the cycle result
        fill_count = len(buy_fills) + len(sell_fills)
        fill_str = f", {fill_count} fills!" if fill_count > 0 else ""
        expired_str = f", {expired} expired" if expired > 0 else ""
        log_event("success", "cycle_complete",
                  f"Cycle #{self._loop_count} complete — "
                  f"{len(current_buy_ids)}b/{len(current_sell_ids)}s active"
                  f"{fill_str}{expired_str}")

    # -------------------------------------------------------------------
    # Requoting
    # -------------------------------------------------------------------

    def _graceful_migration_phase(self) -> str:
        migration = getattr(self, "_graceful_migration", {}) or {}
        return str(migration.get("phase") or "idle").lower()

    def _graceful_creation_blocked(self) -> bool:
        """Only block fresh creation when migration is beyond safe batched cancels."""
        if not getattr(self, "_graceful_in_progress", False):
            return False
        return self._graceful_migration_phase() != "cancelling"

    def _maybe_finalize_graceful_migration(self, current_buy_ids: set = None,
                                           current_sell_ids: set = None):
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
            if (not self.offer_manager._pending_cancel_retries
                    and not migration.get("stalled_logged", False)):
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

    def _handle_requoting(self, mid_price: Decimal,
                          current_buy_ids: set, current_sell_ids: set):
        """Check if offers need requoting due to price movement or forced convergence."""
        if not cfg.AUTO_REQUOTE:
            return

        if self._recovery_is_active():
            log_event("debug", "requote_skip_recovery",
                      "Recovery mode active — skipping non-essential requotes")
            return

        # Don't requote while graceful config migration is running — the migration
        # thread is already cancelling offers and competing for the wallet causes
        # cancel failures and 90-second timeouts on both sides.
        if getattr(self, '_graceful_in_progress', False):
            log_event("debug", "requote_skip",
                      "Graceful config migration in progress — skipping requote")
            return

        # Don't requote while coin operations are running (prep/topup)
        if self.coin_manager.is_busy():
            log_event("debug", "requote_skip", "Coin manager busy — skipping requote")
            return

        # Don't requote while sniper probe is active
        if self._probe_state.get("active", False):
            log_event("debug", "requote_skip", "Sniper probe active — skipping requote")
            return

        for side in ["buy", "sell"]:
            last_price = self._last_quoted_price.get(side, Decimal("0"))
            forced = self._force_requote.get(side, False)

            if last_price <= 0 and not forced:
                continue

            # ---- Smart startup: grace period for first 3 loops ----
            # Newly created offers need time to settle and post to Dexie.
            # Suppress normal requotes (not forced convergence) for the first
            # 3 loops to avoid immediate churn from minor price differences.
            if not forced and self._loop_count <= 3:
                log_event("debug", "startup_grace",
                          f"Startup grace period (loop {self._loop_count}/3) — "
                          f"skipping {side} requote")
                continue

            # Check fill protection (anti-churn) — but don't block forced convergence requotes
            if not forced and self.fill_tracker.should_protect_side(side):
                log_event("debug", "fill_protect",
                          f"Fill protection active for {side} side — skipping requote")
                continue

            # Check if requote needed — either forced (convergence) or price moved
            should_requote = forced or self.offer_manager.should_requote(side, mid_price, last_price)

            if should_requote:
                reason = "convergence tightening" if forced else f"price moved {last_price:.8f} → {mid_price:.8f}"
                print(f"\n   🔄 REQUOTING {side} side ({reason})", flush=True)
                log_event("info", "requoting",
                          f"Requoting {side} side ({reason})")

                # Clear force flag BEFORE requoting (so it doesn't re-trigger)
                if forced:
                    self._force_requote[side] = False

                spread = self.risk_manager.get_adjusted_spread(side)
                requote_mid = self._get_probe_anchored_mid(side, mid_price)
                price_cap = self._get_probe_price_boundary(side) if side == "buy" else None
                price_floor = self._get_probe_price_boundary(side) if side == "sell" else None
                if requote_mid != mid_price:
                    log_event(
                        "info",
                        "probe_anchor_requote",
                        f"Anchoring {side} requote to probe edge: mid {mid_price:.8f} "
                        f"-> {requote_mid:.8f}",
                    )
                _live_ids_req = current_buy_ids if side == "buy" else current_sell_ids
                new_offers = self.offer_manager.requote_side(
                    side, requote_mid, dexie_manager=self.dexie_manager,
                    risk_manager=self.risk_manager,
                    spread_fraction=spread,
                    price_cap=price_cap,
                    price_floor=price_floor,
                    live_offer_ids=_live_ids_req,
                )
                self._last_quoted_price[side] = requote_mid
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

    def _create_offers_if_needed(self, mid_price: Decimal,
                                  current_buy_count: int, current_sell_count: int,
                                  current_buy_ids=None, current_sell_ids=None,
                                  arb_gap: Decimal = Decimal("0"),
                                  skip_buy: bool = False,
                                  skip_sell: bool = False):
        """Create new offers if we're below target count."""
        recovery_active = self._recovery_is_active()
        if cfg.DRY_RUN:
            log_event("debug", "create_skip", "DRY_RUN is on — not creating offers")
            return

        # Don't create offers while graceful migration is cancelling old ones —
        # new offers would just get cancelled by the migration thread.
        if self._graceful_creation_blocked():
            log_event("debug", "create_skip",
                      "Graceful config migration in progress — not creating offers")
            return

        # Don't create main offers while sniper probe is still being confirmed.
        # Wait for probes to survive one loop before deploying the main book.
        if self._probe_state.get("active", False) and not recovery_active:
            log_event("debug", "create_skip",
                      "Sniper probe active — waiting for confirmation before main offers")
            return

        needs_main_ladder = (
            (cfg.ENABLE_BUY  and not skip_buy
             and int(current_buy_count  or 0) < int(cfg.MAX_ACTIVE_BUY_OFFERS))
            or
            (cfg.ENABLE_SELL and not skip_sell
             and int(current_sell_count or 0) < int(cfg.MAX_ACTIVE_SELL_OFFERS))
        )
        if needs_main_ladder:
            current_buy_ids, current_sell_ids, probe_rearmed = self._revalidate_confirmed_probe_edges(
                current_buy_ids=current_buy_ids,
                current_sell_ids=current_sell_ids,
                arb_gap=arb_gap,
            )
            if probe_rearmed:
                log_event("debug", "create_skip",
                          "Confirmed probe edge disappeared — waiting for re-test before main offers")
                return

        current_buy_count = len(current_buy_ids or set())
        current_sell_count = len(current_sell_ids or set())

        # Don't create while coin operations are running
        if self.coin_manager.is_busy():
            log_event("debug", "create_skip", "Coin manager busy — not creating offers")
            return

        if recovery_active and self._wallet_sync_stale_cycle:
            log_event("debug", "recovery_create_wait",
                      "Recovery mode waiting for a fresh wallet view before creating offers")
            return

        # Stale wallet data guard — applies outside recovery mode too.
        # After 3 consecutive stale cycles (~15s) we stop creating new offers
        # because the wallet's offer list may be outdated: we could double-post
        # or use an out-of-date view of what's already on the book.
        # Existing offers stay open; this only blocks NEW creation.
        _stale_streak = int(self._recovery_state.get("wallet_stale_streak", 0))
        _stale_limit = getattr(cfg, "WALLET_STALE_CREATE_LIMIT", 3)
        if self._wallet_sync_stale_cycle and _stale_streak >= _stale_limit:
            log_event("warning", "stale_wallet_create_blocked",
                      f"Wallet sync stale for {_stale_streak} consecutive cycles — "
                      f"blocking new offer creation until data is fresh")
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
        buy_enabled  = (cfg.ENABLE_BUY  and not skip_buy
                        and effective_buy_count  < cfg.MAX_ACTIVE_BUY_OFFERS
                        and self.risk_manager.should_enable_side("buy",  mid_price))
        sell_enabled = (cfg.ENABLE_SELL and not skip_sell
                        and effective_sell_count < cfg.MAX_ACTIVE_SELL_OFFERS
                        and self.risk_manager.should_enable_side("sell", mid_price))

        # Results containers for parallel threads
        _parallel_results = {"buy": [], "sell": []}
        _parallel_mid = {"buy": mid_price, "sell": mid_price}

        def _create_side(side, needed, spread):
            """Create a ladder for one side (runs in a thread)."""
            total_slots = (
                cfg.MAX_ACTIVE_BUY_OFFERS
                if side == "buy"
                else cfg.MAX_ACTIVE_SELL_OFFERS
            )
            ladder_mid_price = self._get_probe_anchored_mid(side, mid_price)
            price_cap = self._get_probe_price_boundary(side) if side == "buy" else None
            price_floor = self._get_probe_price_boundary(side) if side == "sell" else None
            if ladder_mid_price != mid_price:
                log_event(
                    "info",
                    "probe_anchor_apply",
                    f"Anchoring {side} ladder to probe edge: mid {mid_price:.8f} "
                    f"-> {ladder_mid_price:.8f}",
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
                ladder_mid_price, side,
                num_offers=len(slot_sequence) or needed,
                spread_fraction=spread,
                risk_manager=self.risk_manager,
                total_slots=total_slots,
                coin_ids_enabled=cfg.COIN_IDS_ENABLED,
                slot_sequence=slot_sequence or None,
                price_cap=price_cap,
                price_floor=price_floor,
            )
            _parallel_results[side] = offers or []

        work_items = []
        if buy_enabled:
            buy_needed = cfg.MAX_ACTIVE_BUY_OFFERS - effective_buy_count
            buy_spread = self.risk_manager.get_adjusted_spread("buy")
            work_items.append(("buy", buy_needed, buy_spread))
            self._clear_alert("buy_disabled")
        else:
            if cfg.ENABLE_BUY and effective_buy_count < cfg.MAX_ACTIVE_BUY_OFFERS and not self.risk_manager.should_enable_side("buy", mid_price):
                self._emit_alert("buy_disabled", "warning",
                    "Buys disabled — position limit",
                    "Too much CAT held. Sell offers only until position reduces.",
                    action="view_position",
                    action_label="View Position")

        if sell_enabled:
            sell_needed = cfg.MAX_ACTIVE_SELL_OFFERS - effective_sell_count
            sell_spread = self.risk_manager.get_adjusted_spread("sell")
            work_items.append(("sell", sell_needed, sell_spread))
            self._clear_alert("sell_disabled")
        else:
            if cfg.ENABLE_SELL and effective_sell_count < cfg.MAX_ACTIVE_SELL_OFFERS and not self.risk_manager.should_enable_side("sell", mid_price):
                self._emit_alert("sell_disabled", "warning",
                    "Sells disabled — position limit",
                    "Too much XCH held. Buy offers only until position reduces.",
                    action="view_position",
                    action_label="View Position")

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
            threads = []
            for side, needed, spread in work_items:
                t = threading.Thread(
                    target=_create_side,
                    args=(side, needed, spread),
                    name=f"create-{side}",
                )
                threads.append(t)

            # Start all threads and wait for completion
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)  # 60s timeout; wallet RPC hung = don't block forever
                if t.is_alive():
                    log_event("warning", "ladder_thread_timeout",
                              f"Ladder creation thread {t.name!r} still alive after 60s timeout — "
                              f"wallet RPC may be hung; continuing main loop")

        # Process results from both sides
        _notify_amm_after_create = False
        for side in ["buy", "sell"]:
            new_offers = _parallel_results[side]
            if new_offers:
                created_any = True
                _notify_amm_after_create = True
                self._last_quoted_price[side] = _parallel_mid.get(side, mid_price)
                for offer in new_offers:
                    bech32 = offer.get("offer_bech32", "")
                    trade_id = offer.get("trade_id", "")
                    if bech32 and trade_id:
                        self.dexie_manager.queue_post(bech32, trade_id)
                        self.market_intel.queue_offerpool_post(bech32, trade_id)
                        if getattr(cfg, "SPLASH_ENABLED", False):
                            self.splash_manager.queue_post(bech32, trade_id)

        # Notify AMM monitor of newly quoted prices (for drift baseline)
        if _notify_amm_after_create:
            _buy_quoted = self._last_quoted_price.get("buy")
            _sell_quoted = self._last_quoted_price.get("sell")
            self.amm_monitor.notify_quoted_price(_buy_quoted, _sell_quoted)

        # Coin snapshot after creating offers (coins now locked into offers)
        if work_items and not created_any:
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
                log_event("info", "post_create_market_observed",
                          f"Observed market after creation: {fresh_mid:.8f} "
                          f"(keeping deployed quote baselines)")

    # -------------------------------------------------------------------
    # Coin management
    # -------------------------------------------------------------------

    def _handle_coins(self, active_buy_count: int, active_sell_count: int):
        """Handle coin counting, topup, and prep.

        Three-tier checking (V1 parity):
        1. needs_coin_prep() — TOTAL coins critically low → full prep
        2. needs_topup() — FREE coins low → lightweight split
        3. check_runtime_health() — every 5 loops, independent free coin check
        """
        if self._recovery_is_active():
            log_event("debug", "coin_ops_skip_recovery",
                      "Recovery mode active — skipping topup/prep until the book is healthy")
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
            print(f"   📊 Startup coin re-check: {readiness.get('overall_status', '?')}", flush=True)
            log_event("info", "startup_coin_recheck",
                      f"Re-checked coins after wallet settled: "
                      f"{readiness.get('overall_status', 'UNKNOWN')}")

        # Update coin counts + inventory every loop
        self.coin_manager.update_coin_counts()
        self._emit_coin_update("periodic")

        # V3: Periodic wallet reconciliation — authoritative sync every N loops
        # V4 FIX: Changed default from 5 to 2 — active trading causes rapid
        # coin churn (create/cancel destroys and recreates coins) so the DB
        # drifts quickly. Every 2 loops (~2 min) keeps it much tighter.
        self.coin_manager._reconcile_counter += 1
        reconcile_every = getattr(cfg, "RECONCILE_EVERY_N_LOOPS", 2)
        if self.coin_manager._reconcile_counter >= reconcile_every:
            self.coin_manager.reconcile_with_wallet()
            self.coin_manager._reconcile_counter = 0

        # Log coin inventory every 10 loops (gives a running picture)
        if self._loop_count % 10 == 0:
            self.coin_manager.log_inventory(reason="periodic")

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
                    log_event("info", "coin_settle_wait",
                              f"Waiting for wallet to settle ({int(settle_elapsed)}/"
                              f"{self._coin_settle_grace_secs}s) — skipping coin checks")
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
            log_event("warning", "coin_prep_trigger",
                      "TOTAL coins critically low — starting auto coin top-up")
            self.coin_manager.start_topup(active_buy_count, active_sell_count)
            return

        # Tier 2: Lightweight topup — free coins running low
        if self.coin_manager.needs_topup(active_buy_count, active_sell_count):
            log_event("info", "topup_trigger",
                      "Starting coin top-up to replenish free coins (existing offers stay active)...")
            self.coin_manager.start_topup(active_buy_count, active_sell_count)
            return

        # Tier 3: Runtime health check — every 5 loops, independent check
        if self.coin_manager.check_runtime_health(active_buy_count, active_sell_count):
            log_event("info", "health_topup_trigger",
                      "Runtime health check triggered coin top-up")
            self.coin_manager.start_topup(active_buy_count, active_sell_count)
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

        # Prune market intel fingerprints
        self.market_intel.prune_fingerprints()

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
            from database import cleanup_old_pool_snapshots, cleanup_old_trading_pace
            cleanup_old_pool_snapshots(days=30)
            cleanup_old_trading_pace(days=7)
        except Exception:
            pass

        # ---- Coin sanity check + orphan cleanup ----
        # Runs every housekeeping cycle (5 min). Catches:
        # - Locked coins whose offers were cancelled outside the bot
        # - Divergence between locked count and open offer count
        try:
            from database import (coin_sanity_check, cleanup_orphaned_locked_coins,
                                  get_open_offers as _hk_get_open_offers,
                                  batch_cancel_stale_offers as _hk_batch_cancel_stale_offers,
                                  get_locked_coin_ids_for_trade as _hk_get_locked_coin_ids_for_trade)

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
                                        _sid = _cid if _cid.startswith("0x") else "0x" + _cid
                                        _hk_wallet_locked.add(_sid)
                except Exception:
                    pass
                orphan_stats = cleanup_orphaned_locked_coins(
                    wallet_open_ids,
                    wallet_confirmed_locked=_hk_wallet_locked
                )
                if orphan_stats["total_freed"] > 0:
                    log_event("info", "housekeeping_orphan_cleanup",
                              f"Freed {orphan_stats['total_freed']} orphaned locked coins "
                              f"during housekeeping")

            # If the DB still has offers marked open that are not in the
            # wallet-open set and no longer have locked coins, retire those
            # stale rows so the dashboard stays aligned during long runs.
            stale_db_ids = []
            recent_ids = {
                tid for tid, ts in self.offer_manager._recently_created.items()
                if tid and (now - float(ts or 0)) < 45
            }
            for db_offer in db_open:
                tid = db_offer.get("trade_id", "")
                if not tid or tid in wallet_open_ids or tid in recent_ids:
                    continue
                if _hk_get_locked_coin_ids_for_trade(tid):
                    continue
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
            log_event("debug", "housekeeping_sanity_failed",
                      f"Coin sanity check failed: {hk_e}")

        # ---- Sage offer cleanup: delete completed/cancelled offers ----
        # Sage keeps ALL offers forever. Over time this bloats get_offers
        # and can push open offers out of the result window. Clean up
        # offers that are in terminal states (cancelled, completed, expired).
        try:
            from wallet import get_wallet_type
            if get_wallet_type() == "sage":
                from wallet import sage_delete_offer
                all_sage_offers = get_all_offers(include_completed=True,
                                                  start=0, end=500)
                if all_sage_offers:
                    TERMINAL_STATUSES = {
                        "CANCELLED", "COMPLETED", "EXPIRED", "FAILED",
                        "CONFIRMED", "SUCCEEDED",
                    }
                    # Also check integer statuses (Sage uses ints sometimes)
                    # Status > 1 typically means completed/cancelled in Sage
                    to_delete = []
                    now_ts = int(time.time())

                    for offer in all_sage_offers:
                        status_val = offer.get("status")
                        trade_id = offer.get("trade_id", "")
                        if not trade_id:
                            continue

                        is_terminal = False
                        if isinstance(status_val, str):
                            if status_val.upper() in TERMINAL_STATUSES:
                                is_terminal = True
                        elif isinstance(status_val, int):
                            if status_val >= 2:  # 2+ = completed/cancelled
                                is_terminal = True

                        local_offer = get_offer(trade_id)

                        # Also check time expiry
                        if not is_terminal:
                            valid_times = offer.get("valid_times") or {}
                            max_time = (valid_times.get("max_time", 0) or
                                       offer.get("max_time", 0) or 0)
                            if max_time and int(max_time) > 0:
                                if now_ts > int(max_time) + 300:
                                    is_terminal = True  # Expired >5min ago

                        if is_terminal:
                            to_delete.append((
                                trade_id,
                                map_sage_terminal_offer_status(
                                    status_val,
                                    sage_offer=offer,
                                    local_offer=local_offer,
                                    now_ts=now_ts,
                                ),
                            ))

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
                            log_event("info", "sage_offer_cleanup",
                                      f"Deleted {deleted}/{len(to_delete)} "
                                      f"terminal offers from Sage")
                            if status_updates > 0:
                                log_event("debug", "sage_offer_cleanup_db",
                                          f"Updated {status_updates} local offer "
                                          f"statuses during Sage cleanup")

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
                                f" at {price:.8f}"
                                if isinstance(price, float)
                                else ""
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
            log_event("debug", "sage_cleanup_failed",
                      f"Sage offer cleanup failed: {sage_e}")

        log_event("debug", "housekeeping", "Periodic housekeeping completed")

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
                db_offers = [{"trade_id": o.get("trade_id", ""),
                              "offer_bech32": None,
                              "dexie_id": None,
                              "side": o.get("side", "")} for o in all_open]

            # Split into: already posted (skip), have bech32 (fast), need RPC (slow)
            skip_count = 0
            fast_queue = []   # Have bech32 in DB, just need to post to Dexie
            slow_queue = []   # Missing bech32, need wallet RPC

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
                                self.dexie_manager._fingerprint(bech32))
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
                                self.splash_manager.queue_post(bech32, trade_id, force=True)
                            count += 1
                            # Save to DB for next time
                            try:
                                from database import update_offer_bech32
                                update_offer_bech32(trade_id, bech32)
                            except Exception:
                                pass
                            time.sleep(0.3)  # Gentle on wallet RPC
                    except Exception as e:
                        log_event("debug", "repost_error",
                                  f"Error getting bech32 for {trade_id[:16]}...: {e}")

            if count > 0:
                self.dexie_manager.flush_queue(flush_all=True)
                log_event("info", "dexie_repost_done",
                          f"Re-posted {count} offers to Dexie"
                          + (" in the background " if background else " ")
                          + f"({len(fast_queue)} fast + {len(slow_queue)} via RPC, "
                          f"{skip_count} already live)")
                # Also broadcast to Splash if enabled (V3)
                if getattr(cfg, "SPLASH_ENABLED", False):
                    self.splash_manager.flush_queue(flush_all=True)
                    log_event("info", "splash_repost_done",
                              f"Confirmed {count} offers over Splash"
                              + (" in the background" if background else ""))
            else:
                log_event("info", "dexie_repost_done",
                          f"All {skip_count} offers already live on Dexie — nothing to repost")

        except Exception as e:
            log_event("error", "dexie_repost_failed", f"Dexie repost failed: {e}")

    # -------------------------------------------------------------------
    # Health Monitor Thread (V1 parity)
    # -------------------------------------------------------------------

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
        startup_wallet_type = str(os.getenv("WALLET_TYPE", "sage") or "sage").strip().lower()
        monitor_label = "Sage wallet" if startup_wallet_type == "sage" else "Chia wallet/full node"
        log_event("info", "health_monitor", f"{monitor_label} health monitor started (checking every 15s)")

        while self._running:
            try:
                health = get_chia_health()

                wallet_info = health.get("wallet", {}) or {}
                node_info = health.get("node", {}) or {}
                wallet_type = str(
                    health.get("wallet_type")
                    or os.getenv("WALLET_TYPE", "sage")
                    or "sage"
                ).strip().lower()
                wallet_sync_state = str(wallet_info.get("sync_state") or "").strip().lower()
                sage_wallet_service_ok = (
                    wallet_type == "sage"
                    and bool(wallet_info.get("reachable"))
                    and wallet_sync_state in ("", "unknown", "synced")
                )
                effective_status = health.get("status", "unknown")
                effective_healthy = bool(health.get("healthy")) or sage_wallet_service_ok

                with self._chia_health_lock:
                    self._chia_health["status"] = effective_status
                    self._chia_health["wallet_sync_state"] = wallet_sync_state or "unknown"
                    self._chia_health["wallet_reachable"] = wallet_info.get("reachable", False)
                    self._chia_health["wallet_synced"] = wallet_info.get("synced", False)
                    self._chia_health["node_synced"] = node_info.get("synced", False)
                    self._chia_health["last_check"] = time.time()

                if effective_healthy:
                    if self._consecutive_unhealthy > 0:
                        was_down = self._consecutive_unhealthy * self._health_check_interval
                        service_label = "Sage wallet" if wallet_type == "sage" else "Chia services"
                        log_event("success", "chia_recovered",
                                  f"{service_label} recovered (was unhealthy for {was_down}s)")
                        self._emit("health", {"status": "recovered", "downtime_secs": was_down})
                    self._consecutive_unhealthy = 0
                    with self._chia_health_lock:
                        self._chia_health["consecutive_failures"] = 0
                else:
                    self._consecutive_unhealthy += 1
                    with self._chia_health_lock:
                        self._chia_health["consecutive_failures"] = self._consecutive_unhealthy
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
                    reason_str = " — " + ", ".join(reasons) if reasons else ""

                    if self._consecutive_unhealthy == 1:
                        health_label = "Sage wallet" if wallet_type == "sage" else "Chia health"
                        log_event("warning", "chia_unhealthy",
                                  f"{health_label}: {effective_status}{reason_str}")
                    elif self._consecutive_unhealthy >= 4 and self._consecutive_unhealthy % 4 == 0:
                        still_label = "Sage wallet" if wallet_type == "sage" else "Chia"
                        log_event("warning", "chia_still_unhealthy",
                                  f"{still_label} still unhealthy ({secs}s){reason_str}")

                    # After threshold, emit critical alert
                    if secs >= self._auto_restart_threshold:
                        time_since_last = time.time() - self._last_auto_restart_time
                        if time_since_last >= self._auto_restart_cooldown:
                            self._last_auto_restart_time = time.time()
                            self._consecutive_unhealthy = 0
                            restart_label = "Sage wallet" if wallet_type == "sage" else "Chia"
                            log_event("error", "chia_restart_needed",
                                      f"{restart_label} unhealthy for {secs}s ({', '.join(reasons) or 'unknown'}) "
                                      f"— manual restart recommended")
                            self._emit("health", {
                                "status": "restart_needed",
                                "unhealthy_secs": secs,
                                "reasons": reasons,
                            })

                    self._emit("health", {
                        "status": effective_status,
                        "consecutive_failures": self._consecutive_unhealthy,
                        "unhealthy_secs": secs,
                    })

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
            log_event("info", "watcher_disabled", "Price watcher disabled — no CAT_ASSET_ID")
            return

        log_event("info", "watcher_started",
                  f"Fast price watcher active (polling Tibet every {self._watcher_interval}s)")

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
                        log_event("debug", "watcher_error",
                                  f"Tibet API unreachable ({consecutive_errors} consecutive failures)")
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
                            token_change = abs(token_res - prev_token) / prev_token * 100
                            change_pct = max(xch_change, token_change)

                            if change_pct >= self._watcher_min_change_pct:
                                direction = "buy_pressure" if xch_res > prev_xch else "sell_pressure"

                                self._watcher_data["triggered"] = True
                                self._watcher_data["change_pct"] = change_pct
                                self._watcher_data["direction"] = direction
                                self._watcher_data["last_change_ts"] = time.time()
                                self._watcher_data["triggers"] += 1

                                swap_msg = (f"🔔 Tibet swap detected! Reserves moved {change_pct:.3f}% "
                                            f"({direction}) — waking bot for immediate requote")
                                print(swap_msg, flush=True)
                                log_event("warning", "tibet_swap_detected", swap_msg)

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
                for pair in pairs:
                    pair_asset = str(pair.get("short_name", "")).lower().strip()
                    pair_asset_id = str(pair.get("asset_id", "")).lower().strip()
                    if normalized in (pair_asset, pair_asset_id, pair_asset_id + "00"):
                        # API returns mojos — divide by 1e12 to match price_engine units
                        xch_res = float(pair.get("xch_reserve", 0)) / 1e12
                        token_res = float(pair.get("token_reserve", 0))
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
        log_event("info", "coin_watcher_started",
                  f"Coin watcher active (polling every {self._coin_watcher_interval}s)")

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
                xch_records = _extract_coin_records(xch_result) if xch_result else []

                cat_result = get_spendable_coins_rpc(cfg.CAT_WALLET_ID)
                cat_records = _extract_coin_records(cat_result) if cat_result else []

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

                # First poll — log the baseline so we know our starting point
                if not self._coin_snapshot:
                    xch_free = sum(1 for c in current_snapshot.values()
                                   if c["wallet_type"] == "xch" and c.get("source") == "wallet")
                    xch_locked = sum(1 for c in current_snapshot.values()
                                     if c["wallet_type"] == "xch" and c.get("source") == "db_locked")
                    cat_free = sum(1 for c in current_snapshot.values()
                                   if c["wallet_type"] == "cat" and c.get("source") == "wallet")
                    cat_locked = sum(1 for c in current_snapshot.values()
                                     if c["wallet_type"] == "cat" and c.get("source") == "db_locked")
                    xch_total_mojos = sum(c["amount"] for c in current_snapshot.values()
                                          if c["wallet_type"] == "xch")
                    cat_total_mojos = sum(c["amount"] for c in current_snapshot.values()
                                          if c["wallet_type"] == "cat")
                    log_event("info", "coin_watcher_baseline",
                              f"[CoinWatch] Baseline established: "
                              f"XCH {xch_free} free + {xch_locked} locked "
                              f"({xch_total_mojos / 1_000_000_000_000:.4f} XCH) | "
                              f"CAT {cat_free} free + {cat_locked} locked "
                              f"({cat_total_mojos} mojos) | "
                              f"{len(current_snapshot)} coins tracked",
                              data={"xch_free": xch_free, "xch_locked": xch_locked,
                                    "xch_total_mojos": xch_total_mojos,
                                    "cat_free": cat_free, "cat_locked": cat_locked,
                                    "cat_total_mojos": cat_total_mojos,
                                    "total_tracked": len(current_snapshot)})
                    self._coin_snapshot = current_snapshot
                    continue  # Skip change detection on first poll

                # Compare against previous snapshot
                changes = self._detect_coin_changes(
                    self._coin_snapshot, current_snapshot, db_state
                )

                with self._coin_watcher_lock:
                    self._coin_watcher_polls += 1
                    if changes:
                        self._coin_watcher_changes += len(changes)

                # Log each change with structured data
                for change in changes:
                    log_event(change["severity"], change["event_type"],
                              change["message"],
                              data=change.get("data"))

                # Update snapshot for next comparison
                self._coin_snapshot = current_snapshot

            except Exception as e:
                log_event("debug", "coin_watcher_error",
                          f"Coin watcher error: {e}")

            # Sleep in 1s increments for clean exit
            for _ in range(self._coin_watcher_interval):
                if not self._running:
                    break
                time.sleep(1)

        log_event("info", "coin_watcher_exit", "Coin watcher stopped")

    def _detect_coin_changes(self, old_snapshot: Dict, new_snapshot: Dict,
                              db_state: Dict) -> list:
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
                changes.append({
                    "severity": "info",
                    "event_type": "coin_watcher_new",
                    "message": (f"[CoinWatch] NEW {wt} coin {cid[:16]}... ({amt_str})"
                                f" | {desig}/{tier}"),
                    "data": {"coin_id": cid, "amount_mojos": info["amount"],
                             "wallet_type": info["wallet_type"],
                             "designation": desig, "assigned_tier": tier},
                })

        # Detect DISAPPEARED coins (in old snapshot, not in new)
        for cid, info in old_snapshot.items():
            if cid not in new_snapshot:
                wt = info["wallet_type"].upper()
                if info["wallet_type"] == "xch":
                    amt_str = f"{info['amount'] / 1_000_000_000_000:.4f} XCH"
                else:
                    amt_str = f"{info['amount']} mojos"
                changes.append({
                    "severity": "info",
                    "event_type": "coin_watcher_gone",
                    "message": (f"[CoinWatch] GONE {wt} coin {cid[:16]}... ({amt_str})"
                                f" — left the current wallet snapshot"),
                    "data": {"coin_id": cid, "amount_mojos": info["amount"],
                             "wallet_type": info["wallet_type"]},
                })

        # Detect STATUS CHANGES (coin exists in both but source changed)
        for cid in new_snapshot:
            if cid in old_snapshot:
                old_src = old_snapshot[cid].get("source", "")
                new_src = new_snapshot[cid].get("source", "")
                if old_src != new_src:
                    wt = new_snapshot[cid]["wallet_type"].upper()
                    if new_snapshot[cid]["wallet_type"] == "xch":
                        amt_str = f"{new_snapshot[cid]['amount'] / 1_000_000_000_000:.4f} XCH"
                    else:
                        amt_str = f"{new_snapshot[cid]['amount']} mojos"
                    changes.append({
                        "severity": "info",
                        "event_type": "coin_watcher_status",
                        "message": (f"[CoinWatch] {wt} coin {cid[:16]}... ({amt_str})"
                                    f" source changed: {old_src} → {new_src}"),
                        "data": {"coin_id": cid,
                                 "amount_mojos": new_snapshot[cid]["amount"],
                                 "wallet_type": new_snapshot[cid]["wallet_type"],
                                 "old_source": old_src, "new_source": new_src},
                    })

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

        log_event("info", "orphan_cleanup_start",
                  "Starting orphaned offer cleanup...")

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
                log_event("warning", "orphan_cleanup_failed",
                          "Could not fetch wallet offers")
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

            log_event("info", "orphan_cleanup_found",
                      f"Found {len(orphan_ids)} orphaned offers "
                      f"(wallet={len(wallet_ids)}, db={len(db_ids)})")

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
                batch = orphan_ids[i:i + BATCH_SIZE]
                done = min(i + BATCH_SIZE, total)
                log_event("info", "orphan_cancel_batch",
                          f"Cancelling orphan batch {done}/{total}...")

                batch_results = cancel_offers_batch(batch, secure=True)
                all_cancel_results.update(batch_results)

                # Summary for this batch
                successes = sum(1 for r in batch_results.values()
                                if r and r.get("success"))
                failures = len(batch_results) - successes
                log_event("info", "orphan_cancel_batch_result",
                          f"Batch {done}/{total}: {successes} confirmed, "
                          f"{failures} failed")

                if i + BATCH_SIZE < total:
                    time.sleep(BATCH_DELAY)

            result["cancel_results"] = {
                "total": total,
                "confirmed": sum(1 for r in all_cancel_results.values()
                                 if r and r.get("success")),
                "failed": sum(1 for r in all_cancel_results.values()
                              if not r or not r.get("success")),
            }

            log_event("info", "orphan_cleanup_done",
                      f"Orphan cleanup complete: "
                      f"{result['cancel_results']['confirmed']}/{total} "
                      f"confirmed cancelled, "
                      f"{result['cancel_results']['failed']} failed")

        except Exception as e:
            log_event("error", "orphan_cleanup_error", f"Cleanup failed: {e}")
            result["error"] = str(e)

        return result

    # -------------------------------------------------------------------
    # Graceful Config Migration (V2 — config-first, batched cancels)
    # -------------------------------------------------------------------

    def graceful_config_change(self, new_config: Dict = None) -> Dict:
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
            return {"status": "error", "message": "Bot not running — just restart with new config"}

        # Guard against concurrent graceful applies (e.g. rapid slider changes)
        if getattr(self, '_graceful_in_progress', False):
            return {"status": "skipped", "message": "Graceful apply already in progress"}
        self._graceful_in_progress = True

        log_event("info", "config_migration", "Starting graceful config migration...")

        try:
            # ── Step 1: Reload config FIRST ──
            # New spread/skew/sizing settings take effect immediately.
            # The next bot loop cycle will create new offers using the new config,
            # so there's no coverage gap.
            cfg.reload()
            log_event("info", "config_migration", "Config reloaded — new settings active")

            # ── Step 2: Sync current offers ──
            open_buys, open_sells, _ = self.offer_manager.sync_from_wallet()
            total = len(open_buys) + len(open_sells)

            if total == 0:
                log_event("info", "config_migration", "No active offers — config applied directly")
                self._graceful_in_progress = False
                return {"status": "ok", "message": "Config reloaded, no offers to migrate"}

            if total <= 4:
                log_event("info", "config_migration",
                          f"Only {total} offers — config applied, bot will requote naturally")
                self._graceful_in_progress = False
                return {"status": "ok", "message": "Config reloaded, few offers will requote naturally"}

            # ── Step 3: Sort by distance from mid price, keep 2 tightest per side ──
            mid_price = self._current_mid_price
            if mid_price <= 0:
                price_data = self.price_engine.get_price()
                if price_data:
                    mid_price = Decimal(str(price_data.get("mid_price", 0)))

            if mid_price <= 0:
                self._graceful_in_progress = False
                return {"status": "ok", "message": "No price available — config reloaded, will requote naturally"}

            keep_per_side = 2

            def sort_by_distance(offers):
                """Sort offers by distance from mid price (tightest first)."""
                cat_scale = 10 ** cfg.CAT_DECIMALS
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
                        offer_price = Decimal(str(xch_val / cat_val)) if cat_val > 0 else Decimal("999")
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

            retain_buy_ids = [o.get("trade_id") for o in retain_buy if o.get("trade_id")]
            retain_sell_ids = [o.get("trade_id") for o in retain_sell if o.get("trade_id")]
            cancel_ids = [o.get("trade_id") for o in cancel_buy + cancel_sell if o.get("trade_id")]

            log_event("info", "config_migration",
                      f"Keeping {len(retain_buy_ids)}b + {len(retain_sell_ids)}s core offers, "
                      f"queuing {len(cancel_ids)} outer offers for batched cancellation")

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
                log_event("info", "config_migration",
                          f"Background cancel started — {len(cancel_ids)} offers "
                          f"in batches of 5")
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
            return {"status": "partial", "message": f"Migration partially failed ({e}), config is reloaded"}

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

        log_event("info", "batched_cancel",
                  f"Starting batched cancel: {total} offers, "
                  f"batch size {BATCH_SIZE}, delay {BATCH_DELAY}s")

        try:
            for i in range(0, total, BATCH_SIZE):
                if not self._running:
                    log_event("warning", "batched_cancel",
                              f"Bot stopped — aborting cancel after {done}/{total}")
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
                    log_event("info", "batched_cancel",
                              f"Circuit breaker cleared after {_cb_wait}s — resuming cancel")

                batch = cancel_ids[i:i + BATCH_SIZE]
                batch_num = (i // BATCH_SIZE) + 1
                total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

                log_event("info", "batched_cancel",
                          f"Batch {batch_num}/{total_batches}: cancelling {len(batch)} offers")

                try:
                    results = self.offer_manager.cancel_offers(
                        batch, reason="config_migration"
                    )
                    batch_successes = sum(
                        1 for tid in batch
                        if results.get(tid) and results[tid].get("success")
                    )
                    batch_failures = len(batch) - batch_successes
                    done += batch_successes
                    failed += batch_failures
                except Exception as e:
                    log_event("warning", "batched_cancel",
                              f"Batch {batch_num} error: {e} — continuing with next batch")
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
            log_event("warning", "batched_cancel_done",
                      f"Batched cancel stopped early: {done} cancelled, {failed} failed, "
                      f"{remaining} left live out of {total} ({aborted_reason})")
        elif failed > 0:
            log_event("warning", "batched_cancel_done",
                      f"Batched cancel queued retries: {done} cancelled, {failed} failed "
                      f"out of {total}; waiting for retry confirmation before migration unlocks.")
        else:
            log_event("info", "batched_cancel_done",
                      f"Batched cancel complete: {done} cancelled, {failed} failed "
                      f"out of {total}. Waiting for wallet verification before "
                      f"migration unlocks.")

    # -------------------------------------------------------------------
    # State queries (for API/GUI)
    # -------------------------------------------------------------------

    def get_state(self) -> Dict:
        """Get full bot state for the GUI/API."""
        with self._state_lock:
            state = dict(self._bot_state)
        state["loop_duration"] = round(self._last_loop_duration, 2)
        state["loop_seconds"] = cfg.LOOP_SECONDS
        state["dry_run"] = cfg.DRY_RUN

        # Add module states
        state["coins"] = self.coin_manager.get_status()
        state["risk"] = self.risk_manager.get_inventory_state()
        state["dexie"] = self.dexie_manager.get_stats()
        try:
            from database import get_fills
            recent_fills = get_fills(cat_asset_id=cfg.CAT_ASSET_ID, limit=10)
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

        # Add Splash stats (V3 — P2P broadcasting)
        state["splash"] = self.splash_manager.get_stats()
        state["splash_node"] = self.splash_node.get_status()
        state["splash_receive"] = self.get_splash_receive_stats()

        # Add Coinset stats (V3 — fast coin queries)
        state["coinset"] = self.coinset_client.get_stats()
        try:
            state["recovery"] = dict(self._recovery_state)
        except RuntimeError:
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

    def get_price_info(self) -> Dict:
        """Get current price information."""
        return {
            "mid_price": str(self._current_mid_price),
            "last_quoted_buy": str(self._last_quoted_price.get("buy", "0")),
            "last_quoted_sell": str(self._last_quoted_price.get("sell", "0")),
        }
