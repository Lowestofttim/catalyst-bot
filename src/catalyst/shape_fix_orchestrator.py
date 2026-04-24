"""User-triggered multi-stage recovery flow for ladder shape drift

Coordinates a cancel -> wait-for-on-chain-confirmation -> coin-recheck ->
rebuild pipeline as a single orchestrated action, with progressive status
updates streamed to the GUI over SSE. Invoked by the operator when the
watchdog has reported shape drift that the incremental reaction strategy
cannot clear on its own.

Key responsibilities:
    - Drive the staged flow (CANCELLING, WAITING_FOR_CONFIRMATION,
      CHECKING_COINS, RESHAPING, REBUILDING, COMPLETE / HALTED)
    - Stream progress and final status out over SSE for the GUI
    - Honour abort requests at every checkpoint with no orphaned locks
    - Respect existing safety gates (position guard, storm protection)

Runs on a dedicated daemon thread so the main trading loop is never
blocked. Globally serialised — at most one flow can run anywhere at a
time (not one per side); re-entry is rejected with a clear reason.

Design principles
-----------------

1. **One flow at a time, globally serialised** (per user requirement).
   Keeps the coin-reservation story simple and avoids multi-side races
   against the main loop.

2. **Non-blocking for trading.** Runs on a dedicated daemon thread; the
   trading loop is never blocked waiting for a recovery to complete.

3. **Abort-safe.** The flow checks the abort flag at every checkpoint.
   A user-requested abort rolls forward to the next checkpoint and
   halts cleanly (no orphaned locks / reservations).

4. **Respects safety gates.** The orchestrator never bypasses the
   position guard, storm protection, or any other existing safety
   mechanism. If the rebuild can't proceed (later phase), the flow
   halts with the gate's reason for visibility.

5. **Fast path first.** The design anticipates that once the general
   coin-return reclassification fix is in place, reshape will rarely
   be needed — returned coins will already be in tier pools when the
   rebuild stage looks for them.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# State enums
# ---------------------------------------------------------------------------

class Stage(Enum):
    """A flow's current execution stage."""
    CANCELLING = "cancelling"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    CHECKING_COINS = "checking_coins"        # P2
    RESHAPING = "reshaping"                  # P3
    REBUILDING = "rebuilding"                # P2
    COMPLETE = "complete"
    HALTED = "halted"


class HaltReason(Enum):
    """Why a flow halted short of COMPLETE."""
    USER_ABORTED = "user_aborted"
    TIMEOUT_CONFIRMATION = "timeout_waiting_for_confirmation"
    TIMEOUT_RESHAPE = "timeout_reshape"
    POSITION_GUARD_BLOCKED = "position_guard_blocked"
    NO_TIER_COINS_POSSIBLE = "no_tier_correct_coins_possible"
    CANCEL_REJECTED = "cancel_rejected"
    INTERNAL_ERROR = "internal_error"


# Pipeline definition — the ordered list of stages a flow walks through.
# Later phases will extend this; keep the tuple here so the UI knows
# what the full roadmap looks like even mid-flow.
P1_PIPELINE: tuple = (
    Stage.CANCELLING,
    Stage.WAITING_FOR_CONFIRMATION,
)

P2_PIPELINE: tuple = (
    Stage.CANCELLING,
    Stage.WAITING_FOR_CONFIRMATION,
    Stage.CHECKING_COINS,
    Stage.REBUILDING,
)

# The pipeline the orchestrator currently uses. Bumping this constant
# after new stages are added unlocks them in the UI.
ACTIVE_PIPELINE: tuple = P2_PIPELINE


# ---------------------------------------------------------------------------
# Flow state
# ---------------------------------------------------------------------------

@dataclass
class FlowState:
    """Snapshot of a single recovery flow. Mutated by the orchestrator
    thread; rendered to dict for SSE emission."""
    flow_id: str
    side: str
    trade_ids: List[str]
    alert_id: str = ""
    stage: Stage = Stage.CANCELLING
    stages_completed: List[Stage] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    halt_reason: Optional[HaltReason] = None
    detail: str = ""
    cancelled_count: int = 0
    new_offer_count: int = 0
    total_requested: int = 0

    # Private execution plumbing (not serialised to UI)
    abort_flag: threading.Event = field(default_factory=threading.Event)

    def is_terminal(self) -> bool:
        return self.stage in (Stage.COMPLETE, Stage.HALTED)

    @property
    def status(self) -> str:
        if self.halt_reason is not None:
            return "halted"
        if self.stage == Stage.COMPLETE:
            return "complete"
        return "running"

    def to_dict(self, pipeline: tuple = None) -> dict:
        """Render to a SSE-friendly dict. Includes the full pipeline so
        the UI can render past/current/future stages consistently."""
        if pipeline is None:
            pipeline = ACTIVE_PIPELINE
        completed = [s.value for s in self.stages_completed]
        return {
            "flow_id": self.flow_id,
            "side": self.side,
            "stage": self.stage.value,
            "status": self.status,
            "detail": self.detail,
            "elapsed_ms": int((time.time() - self.started_at) * 1000),
            "pipeline": [s.value for s in pipeline],
            "stages_completed": completed,
            "halt_reason": self.halt_reason.value if self.halt_reason else None,
            "summary": {
                "cancelled_count": self.cancelled_count,
                "new_offer_count": self.new_offer_count,
                "total_requested": self.total_requested,
            },
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class ShapeFixOrchestrator:
    """Attach-to-bot singleton that manages recovery flows.

    Access from the Flask request thread:
        result = bot.shape_fix_orchestrator.start_flow(
            side="sell", trade_ids=[...], alert_id="...")

    Access the current flow's state for status endpoints:
        flow = bot.shape_fix_orchestrator.current_flow()
    """

    # P1 tuning — aggressive polling matches the user's "as fast as the
    # tech allows" requirement. The DB read is cheap (~ms), so 2s polls
    # give sub-3s detection latency without spinning the CPU.
    CONFIRMATION_POLL_INTERVAL_S: float = 2.0
    CONFIRMATION_TIMEOUT_S: float = 180.0   # 3 min — generous for slow mempool

    # P2 tuning — the rebuild wait is longer because we're waiting for:
    #   - the main loop's next requote tick (up to ~45s)
    #   - the bot to create_ladder(), posted to Dexie/Splash
    # If rebuild is blocked by the position guard, we detect that after
    # 2 min of no progress and halt with POSITION_GUARD_BLOCKED.
    REBUILD_POLL_INTERVAL_S: float = 3.0
    REBUILD_TIMEOUT_S: float = 600.0        # 10 min — covers guard stalemates
    POSITION_GUARD_SUSPICION_S: float = 120.0  # 2 min of no progress = guard

    def __init__(self, bot: Any, event_bus: Any):
        """:param bot: the BotLoop instance (needs offer_manager)
        :param event_bus: the api_server EventBus (for SSE emit + alert_store)
        """
        self._bot = bot
        self._events = event_bus
        self._lock = threading.Lock()
        # side -> FlowState. One slot per side (buy|sell), globally
        # serialised per user requirement (max 1 flow anywhere).
        self._active: Dict[str, FlowState] = {}
        self._threads: Dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_running(self, side: Optional[str] = None) -> bool:
        """True if a flow is active on the given side (or any side)."""
        with self._lock:
            if side is None:
                return bool(self._active)
            return side in self._active

    def current_flow(self, side: Optional[str] = None) -> Optional[FlowState]:
        """Snapshot of the active flow on the given side (or the only one)."""
        with self._lock:
            if side is not None:
                return self._active.get(side)
            if len(self._active) == 1:
                return next(iter(self._active.values()))
            return None

    def start_flow(self, side: str, trade_ids: List[str],
                   alert_id: str = "") -> Dict[str, Any]:
        """Begin a recovery flow.

        Returns a dict with:
            - accepted: bool
            - flow_id: str   (when accepted)
            - error: str     (when rejected)

        Rejects if any flow is already running (per user requirement:
        one side at a time, globally serialised).
        """
        if side not in ("buy", "sell"):
            return {"accepted": False, "error": f"invalid side '{side}'"}

        cleaned = [str(t).strip() for t in (trade_ids or []) if t]
        if not cleaned:
            return {"accepted": False, "error": "no trade_ids supplied"}

        with self._lock:
            if self._active:
                busy_side = next(iter(self._active.keys()))
                return {
                    "accepted": False,
                    "error": (
                        f"Another shape-fix is running (side={busy_side}). "
                        "Wait for it to finish before starting another."
                    ),
                }

            flow_id = f"sf_{int(time.time() * 1000)}_{side}"
            flow = FlowState(
                flow_id=flow_id,
                side=side,
                trade_ids=cleaned,
                alert_id=str(alert_id or ""),
                total_requested=len(cleaned),
            )
            self._active[side] = flow
            t = threading.Thread(
                target=self._run_flow,
                args=(flow,),
                name=f"shape_fix_{side}",
                daemon=True,
            )
            self._threads[side] = t
            t.start()

        return {"accepted": True, "flow_id": flow_id}

    def abort_flow(self, side: str) -> bool:
        """Signal the flow on ``side`` to abort at the next checkpoint.

        Returns True if a flow was running and the abort signal was
        sent. The flow transitions to HALTED (reason=USER_ABORTED)
        asynchronously — callers should not assume the flow has
        stopped by the time this returns.
        """
        with self._lock:
            flow = self._active.get(side)
        if flow is None:
            return False
        flow.abort_flag.set()
        return True

    # ------------------------------------------------------------------
    # Internal: event emission
    # ------------------------------------------------------------------

    def _emit(self, flow: FlowState) -> None:
        """Push the current flow state as a shape_fix_progress SSE event.

        Also mirrors to the log (INFO) so the event is visible in the
        Logs tab and the superlog file for post-hoc analysis.
        """
        flow.last_update = time.time()
        payload = flow.to_dict()
        try:
            self._events.emit("shape_fix_progress", payload)
        except Exception:
            # SSE is best-effort — never let a flaky subscriber kill a flow
            pass
        # Also log at INFO for auditability.
        try:
            from super_log import log_event
            log_event(
                "info",
                "shape_fix_progress",
                f"[{flow.side}] {flow.stage.value} "
                f"({flow.status}) — {flow.detail}",
                data={
                    "flow_id": flow.flow_id,
                    "side": flow.side,
                    "stage": flow.stage.value,
                    "status": flow.status,
                    "elapsed_ms": payload["elapsed_ms"],
                    "halt_reason": payload["halt_reason"],
                },
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: flow execution (runs on dedicated thread)
    # ------------------------------------------------------------------

    def _run_flow(self, flow: FlowState) -> None:
        """Main flow loop. Calls each stage method in order; bails on
        halt_reason set."""
        try:
            # Announce start
            flow.detail = (
                f"Recovery started — will cancel {flow.total_requested} "
                f"offer(s) on {flow.side} side"
            )
            self._emit(flow)

            # Stage 1: cancelling
            self._stage_cancelling(flow)
            if flow.halt_reason is not None:
                return

            # Stage 2: waiting for confirmation
            self._stage_waiting_for_confirmation(flow)
            if flow.halt_reason is not None:
                return

            # Stage 3 (P2): verify tier-correct coin availability.
            self._stage_checking_coins(flow)
            if flow.halt_reason is not None:
                return

            # Stage 4 (P2): wait for main-loop rebuild to complete.
            self._stage_rebuilding(flow)
            if flow.halt_reason is not None:
                return

            # Terminal: COMPLETE.
            flow.stage = Stage.COMPLETE
            flow.detail = (
                f"Recovery complete — {flow.new_offer_count} new offer(s) "
                f"on {flow.side} side"
            )
            self._emit(flow)

        except Exception as e:
            # Any uncaught exception halts the flow gracefully.
            flow.halt_reason = HaltReason.INTERNAL_ERROR
            flow.stage = Stage.HALTED
            flow.detail = f"Internal error: {type(e).__name__}: {e}"
            try:
                from super_log import log_event
                log_event(
                    "error", "shape_fix_internal_error",
                    f"Shape-fix flow crashed: {e}",
                )
            except Exception:
                pass
            self._emit(flow)
        finally:
            self._finalise(flow)

    # ------------------------------------------------------------------
    # Stages
    # ------------------------------------------------------------------

    def _stage_cancelling(self, flow: FlowState) -> None:
        flow.stage = Stage.CANCELLING
        flow.detail = f"Submitting cancel for {flow.total_requested} offer(s)"
        self._emit(flow)

        if flow.abort_flag.is_set():
            flow.halt_reason = HaltReason.USER_ABORTED
            flow.stage = Stage.HALTED
            flow.detail = "Aborted by user before cancel submitted"
            self._emit(flow)
            return

        try:
            result = self._bot.offer_manager.cancel_offers(
                flow.trade_ids, reason="shape_fix_recovery")
        except Exception as e:
            flow.halt_reason = HaltReason.INTERNAL_ERROR
            flow.stage = Stage.HALTED
            flow.detail = f"Cancel call raised: {type(e).__name__}: {e}"
            self._emit(flow)
            return

        if not isinstance(result, dict):
            flow.halt_reason = HaltReason.INTERNAL_ERROR
            flow.stage = Stage.HALTED
            flow.detail = "Cancel returned unexpected type"
            self._emit(flow)
            return

        # Storm-block check — cancel_offers returns {tid: {"success": False,
        # "error": "cancel_storm_blocked"}} when storm protection trips.
        if any(
            isinstance(r, dict) and r.get("error") == "cancel_storm_blocked"
            for r in result.values()
        ):
            flow.halt_reason = HaltReason.CANCEL_REJECTED
            flow.stage = Stage.HALTED
            flow.detail = (
                "Cancel refused by storm protection. This batch is too "
                "large to cancel in one shot; escalate manually."
            )
            self._emit(flow)
            return

        succeeded = [
            tid for tid, r in result.items()
            if isinstance(r, dict) and r.get("success")
        ]
        flow.cancelled_count = len(succeeded)
        flow.stages_completed.append(Stage.CANCELLING)
        flow.detail = (
            f"Cancel accepted by Sage: {flow.cancelled_count} of "
            f"{flow.total_requested} offer(s)"
        )
        self._emit(flow)

    def _stage_waiting_for_confirmation(self, flow: FlowState) -> None:
        """Poll the open-offer book until ``flow.trade_ids`` no longer
        appear. The bot's normal wallet_sync path updates the DB from
        Sage each cycle, so once the cancels confirm on-chain they'll
        disappear from the DB within one poll.

        Timeout = :attr:`CONFIRMATION_TIMEOUT_S` (default 3 min).
        """
        flow.stage = Stage.WAITING_FOR_CONFIRMATION
        flow.detail = "Waiting for cancel tx to confirm on-chain"
        self._emit(flow)

        target_tids = set(flow.trade_ids)
        deadline = time.time() + self.CONFIRMATION_TIMEOUT_S
        last_still_open = len(target_tids)
        last_emit_at = 0.0

        try:
            from database import get_open_offers
            from config import cfg
        except Exception as e:
            flow.halt_reason = HaltReason.INTERNAL_ERROR
            flow.stage = Stage.HALTED
            flow.detail = f"Could not import DB helpers: {e}"
            self._emit(flow)
            return

        while time.time() < deadline:
            if flow.abort_flag.is_set():
                flow.halt_reason = HaltReason.USER_ABORTED
                flow.stage = Stage.HALTED
                flow.detail = (
                    f"Aborted with {last_still_open} cancel(s) still "
                    "pending — on-chain cancels will complete regardless"
                )
                self._emit(flow)
                return

            try:
                rows = get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID) or []
                open_tids = {str(r.get("trade_id") or "") for r in rows}
                still_open = len(target_tids & open_tids)
            except Exception:
                # Transient DB hiccup — try again next poll
                still_open = last_still_open

            # Emit progress when the count changes, OR every 5s as a
            # keep-alive so the UI doesn't look frozen.
            now = time.time()
            if still_open != last_still_open or (now - last_emit_at) >= 5.0:
                confirmed = flow.total_requested - still_open
                flow.detail = (
                    f"{confirmed} of {flow.total_requested} cancel(s) "
                    f"confirmed ({still_open} still pending)"
                )
                self._emit(flow)
                last_emit_at = now
                last_still_open = still_open

            if still_open == 0:
                flow.stages_completed.append(Stage.WAITING_FOR_CONFIRMATION)
                flow.detail = (
                    f"All {flow.total_requested} cancels confirmed on-chain"
                )
                self._emit(flow)
                return

            time.sleep(self.CONFIRMATION_POLL_INTERVAL_S)

        flow.halt_reason = HaltReason.TIMEOUT_CONFIRMATION
        flow.stage = Stage.HALTED
        flow.detail = (
            f"Timed out after {int(self.CONFIRMATION_TIMEOUT_S)}s — "
            f"{last_still_open} cancel(s) still open. The on-chain tx "
            "may eventually land; check the wallet directly."
        )
        self._emit(flow)

    # ------------------------------------------------------------------
    # P2 stages: checking coins + rebuilding
    # ------------------------------------------------------------------

    def _stage_checking_coins(self, flow: FlowState) -> None:
        """Verify we have tier-correct coins in the free pool on the
        relevant side. Triggers a reconcile first so any just-returned
        coins from the cancel are picked up and classified.

        Informational for P2 — the main loop's create_ladder will use
        whatever coins are available (F70-guarded). If we find zero
        tier-correct coins, we log a warning but still proceed; the
        rebuild stage will halt with a clear reason if create_ladder
        can't make offers.

        P3 will promote this stage's output into a decision: if the
        inventory is insufficient, insert a RESHAPING stage before
        REBUILDING.
        """
        flow.stage = Stage.CHECKING_COINS
        flow.detail = "Reconciling returned coins with wallet"
        self._emit(flow)

        if flow.abort_flag.is_set():
            flow.halt_reason = HaltReason.USER_ABORTED
            flow.stage = Stage.HALTED
            flow.detail = "Aborted before coin check"
            self._emit(flow)
            return

        # AGGRESSIVE reconcile — Sage may be mid-sync when the cancel
        # first confirms; the new output coins might not appear until a
        # second or third reconcile call. We call up to 3 times with
        # short gaps to maximise the chance of catching them on this
        # stage, so the REBUILD stage that follows has fresh inventory.
        # Also reset the main-loop reconcile counter so the next cycle
        # reconciles too (belt-and-braces).
        try:
            for _attempt in range(3):
                if flow.abort_flag.is_set():
                    break
                try:
                    self._bot.coin_manager.reconcile_with_wallet()
                except Exception:
                    pass
                # Small pause lets Sage's coin-watcher pick up late arrivals
                time.sleep(2.0)
            # Trigger the main loop's next-cycle reconcile too
            try:
                if hasattr(self._bot.coin_manager, "_reconcile_counter"):
                    self._bot.coin_manager._reconcile_counter = 99
            except Exception:
                pass
        except Exception:
            pass

        # Inspect free-tier counts on the relevant side. 'sell' offers
        # lock CAT coins (we give CAT, receive XCH); 'buy' offers lock
        # XCH coins. So the returned coins and the tier pool we care
        # about mirror that.
        side = flow.side
        asset_label = "CAT" if side == "sell" else "XCH"
        try:
            cm = self._bot.coin_manager
            if side == "sell":
                inv = getattr(cm, "_cat_inventory", {}) or {}
            else:
                inv = getattr(cm, "_xch_inventory", {}) or {}

            def _bucket_count(key: str) -> int:
                b = inv.get(key)
                return len(b) if b is not None else 0

            inner = _bucket_count("inner")
            mid = _bucket_count("mid")
            outer = _bucket_count("outer")
            extreme = _bucket_count("extreme")
        except Exception as e:
            flow.stages_completed.append(Stage.CHECKING_COINS)
            flow.detail = f"Could not read inventory (non-fatal): {e}"
            self._emit(flow)
            return

        flow.stages_completed.append(Stage.CHECKING_COINS)
        flow.detail = (
            f"Free {asset_label} coins — inner {inner} | mid {mid} | "
            f"outer {outer} | extreme {extreme}"
        )
        self._emit(flow)

    def _stage_rebuilding(self, flow: FlowState) -> None:
        """Wait for the main loop to rebuild the ladder to the
        configured target count. We don't drive ``create_ladder``
        directly — the main loop already has the full context (probe-
        anchored mid, risk checks, price caps, slot sequencing). We
        just:

          1. Set ``_force_requote[side] = True`` so the next cycle
             prioritises this side's rebuild.
          2. Poll the open-offer count every ``REBUILD_POLL_INTERVAL_S``.
          3. Emit progress as new offers land on-chain.
          4. Detect the position-guard stalemate (no progress for
             ``POSITION_GUARD_SUSPICION_S``) and halt with a
             descriptive reason.
          5. Hit the overall timeout ``REBUILD_TIMEOUT_S`` and halt if
             the rebuild never completes.
        """
        flow.stage = Stage.REBUILDING
        flow.detail = "Signalling main loop to rebuild the ladder"
        self._emit(flow)

        if flow.abort_flag.is_set():
            flow.halt_reason = HaltReason.USER_ABORTED
            flow.stage = Stage.HALTED
            flow.detail = "Aborted before rebuild started"
            self._emit(flow)
            return

        # Set force_requote so the next cycle prioritises rebuild.
        # Note: this flag is consumed by bot_loop's requote path, which
        # clears it after running. Safe to set from another thread.
        try:
            fr = getattr(self._bot, "_force_requote", None)
            if isinstance(fr, dict):
                fr[flow.side] = True
        except Exception:
            pass

        try:
            from database import get_open_offers
            from config import cfg
        except Exception as e:
            flow.halt_reason = HaltReason.INTERNAL_ERROR
            flow.stage = Stage.HALTED
            flow.detail = f"Could not import DB helpers: {e}"
            self._emit(flow)
            return

        target_count = (
            int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 24) or 24)
            if flow.side == "buy"
            else int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 24) or 24)
        )

        def _count_side_offers() -> int:
            try:
                rows = get_open_offers(
                    side=flow.side, cat_asset_id=cfg.CAT_ASSET_ID) or []
                return len(rows)
            except Exception:
                return -1

        starting_count = max(0, _count_side_offers())
        last_emitted_count = starting_count
        last_progress_at = time.time()
        last_emit_at = 0.0
        deadline = time.time() + self.REBUILD_TIMEOUT_S

        while time.time() < deadline:
            if flow.abort_flag.is_set():
                flow.halt_reason = HaltReason.USER_ABORTED
                flow.stage = Stage.HALTED
                flow.detail = (
                    f"Aborted during rebuild — "
                    f"{max(0, last_emitted_count - starting_count)} "
                    f"new offer(s) created before abort"
                )
                self._emit(flow)
                return

            current_count = _count_side_offers()
            now = time.time()

            if current_count >= 0 and current_count != last_emitted_count:
                # Progress — either offers landed or (rare) some went away.
                created_so_far = max(0, current_count - starting_count)
                flow.new_offer_count = created_so_far
                flow.detail = (
                    f"{current_count}/{target_count} offers live on "
                    f"{flow.side} side ({created_so_far} new this recovery)"
                )
                self._emit(flow)
                last_emitted_count = current_count
                last_emit_at = now
                if current_count > starting_count:
                    # Actual forward progress — reset the guard-suspicion
                    # timer so we don't halt mid-rebuild.
                    last_progress_at = now
            elif (now - last_emit_at) >= 5.0:
                # Heart-beat — show the elapsed time's still ticking
                # even if the count hasn't changed.
                flow.detail = (
                    f"{last_emitted_count}/{target_count} — waiting for "
                    f"main loop's next cycle to create offers "
                    f"(elapsed {int(now - (flow.started_at or now))}s)"
                )
                self._emit(flow)
                last_emit_at = now

            # Success: we've hit (or got close to) the target count.
            # ±1 tolerance accounts for sniper probe slots and in-flight
            # creations the main loop hasn't committed yet.
            if current_count >= target_count - 1:
                flow.stages_completed.append(Stage.REBUILDING)
                flow.new_offer_count = max(0, current_count - starting_count)
                flow.detail = (
                    f"Ladder rebuilt: {current_count}/{target_count} "
                    f"offers live on {flow.side} side"
                )
                self._emit(flow)
                return

            # Detect the position-guard stalemate: if the count hasn't
            # moved upward for POSITION_GUARD_SUSPICION_S, bail with a
            # clear reason. The main loop logs position_hard_guard_blocked
            # at WARN each cycle it trips, so the Logs tab has detail.
            if (now - last_progress_at) > self.POSITION_GUARD_SUSPICION_S:
                flow.halt_reason = HaltReason.POSITION_GUARD_BLOCKED
                flow.stage = Stage.HALTED
                flow.detail = (
                    "Rebuild stalled — the position circuit breaker is "
                    "likely blocking new offers. Opposite-side fills "
                    "must unwind the position first. Check Activity for "
                    "'position_hard_guard_blocked' events."
                )
                self._emit(flow)
                return

            time.sleep(self.REBUILD_POLL_INTERVAL_S)

        # Outer timeout
        flow.halt_reason = HaltReason.TIMEOUT_CONFIRMATION
        flow.stage = Stage.HALTED
        flow.detail = (
            f"Timed out after {int(self.REBUILD_TIMEOUT_S)}s — only "
            f"{last_emitted_count}/{target_count} offers on "
            f"{flow.side} side"
        )
        self._emit(flow)

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    def _finalise(self, flow: FlowState) -> None:
        """Remove the flow from the active set and clear the source
        alert. Always runs, even after internal errors."""
        with self._lock:
            self._active.pop(flow.side, None)
            self._threads.pop(flow.side, None)

        # Clear the originating Recommendation alert so the panel
        # reflects the new state. The SSE alert_cleared event will
        # propagate to the frontend.
        if flow.alert_id:
            try:
                store = getattr(self._events, "_alert_store", None)
                if store is not None:
                    store.clear(flow.alert_id)
            except Exception:
                pass


__all__ = [
    "Stage",
    "HaltReason",
    "FlowState",
    "ShapeFixOrchestrator",
    "P1_PIPELINE",
    "P2_PIPELINE",
    "ACTIVE_PIPELINE",
]
