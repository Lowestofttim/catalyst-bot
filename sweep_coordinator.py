"""Groups same-block fills into sweep events and upgrades UNKNOWN classifications

When an arb bot sweeps several of our offers in one on-chain transaction, all
those fills share the same `spent_block_index`. `SweepCoordinator` collects
incoming fills within a short time window and groups co-block fills into a
single `SweepEvent` for downstream PnL / diagnostics attribution.

Key responsibilities:
    - Buffer fills over a `SWEEP_WINDOW_SECS` window keyed by block index
    - Emit finalised `SweepEvent` objects once the window closes
    - Upgrade UNKNOWN fills to DEXIE_COMBINED when at least
      `SWEEP_MIN_FILLS` share a block (medium confidence)
    - Remain thread-safe under concurrent fill arrivals

The coordinator never touches offer state or the wallet; it only
enriches fill metadata so other modules can reason about sweep episodes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SweepEntry:
    """One fill inside a sweep group."""
    fill_id:            int
    trade_id:           str
    classification:     str
    spent_block_index:  int
    taker_puzzle_hash:  Optional[str] = None
    # "buy" or "sell" — which side of our book was swept.
    # Stamped from FillClassification.side by fill_tracker so that
    # bot_loop can determine protected side without a DB lookup.
    side:               Optional[str] = None
    added_at:           float = field(default_factory=time.monotonic)


@dataclass
class SweepEvent:
    """A finalised group of fills swept in the same on-chain transaction."""
    sweep_group_id:     str
    spent_block_index:  int
    fills:              List[SweepEntry]
    finalised_at:       float = field(default_factory=time.monotonic)

    @property
    def fill_count(self) -> int:
        return len(self.fills)

    @property
    def trade_ids(self) -> List[str]:
        return [e.trade_id for e in self.fills]

    def __str__(self) -> str:
        return (
            f"SweepEvent(block={self.spent_block_index}, "
            f"fills={self.fill_count}, group={self.sweep_group_id})"
        )


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

# How long (seconds) to wait before finalising a sweep group.
# Fills at the same block height may arrive a few seconds apart as
# fill_tracker processes them sequentially.
_DEFAULT_WINDOW_SECS: float = 15.0

# Maximum number of sweep events to buffer before oldest are dropped.
_MAX_BUFFERED_EVENTS: int = 200


class SweepCoordinator:
    """Thread-safe collector that groups fills by spent_block_index."""

    def __init__(self, window_secs: float = _DEFAULT_WINDOW_SECS) -> None:
        self._window_secs = window_secs
        self._lock = threading.Lock()

        # block_index → list of SweepEntry
        self._pending: Dict[int, List[SweepEntry]] = {}

        # Finalised events waiting to be drained
        self._events: List[SweepEvent] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_fill(
        self,
        fill_id: int,
        classification,   # FillClassification instance
    ) -> Optional[str]:
        """Register a fill with the coordinator.

        If the fill has a spent_block_index, it is buffered.  When the
        window expires, fills sharing the same block index are finalised
        into a SweepEvent and UNKNOWN fills are upgraded to DEXIE_COMBINED.

        Returns the sweep_group_id if the fill was grouped, else None.
        """
        block_idx = classification.spent_block_index
        if block_idx is None:
            return None

        entry = SweepEntry(
            fill_id=fill_id,
            trade_id=classification.trade_id,
            classification=classification.classification,
            spent_block_index=block_idx,
            taker_puzzle_hash=classification.taker_puzzle_hash,
            side=getattr(classification, "side", None),
        )

        with self._lock:
            if block_idx not in self._pending:
                self._pending[block_idx] = []
            self._pending[block_idx].append(entry)

            # If this block already has >1 fill, it's already a sweep group —
            # return the anticipated group id even before finalisation.
            if len(self._pending.get(block_idx, [])) > 1:
                return f"sweep_{block_idx}"

        return None

    def tick(self) -> None:
        """Expire pending groups whose window has elapsed.

        Call this periodically (e.g., once per bot cycle) so that
        single-fill groups whose window has passed are also finalised.
        """
        with self._lock:
            self._expire_pending_locked()

    def drain_sweep_events(self) -> List[SweepEvent]:
        """Return and clear the list of finalised sweep events."""
        with self._lock:
            events, self._events = self._events, []
        return events

    def get_pending_summary(self) -> Dict:
        """Non-blocking snapshot of pending state (for diagnostics)."""
        with self._lock:
            return {
                "pending_block_groups": len(self._pending),
                "pending_fill_count": sum(
                    len(v) for v in self._pending.values()
                ),
                "buffered_events": len(self._events),
            }

    # ------------------------------------------------------------------
    # Internal helpers (must be called with _lock held)
    # ------------------------------------------------------------------

    def _expire_pending_locked(self) -> None:
        now = time.monotonic()
        expired_blocks: List[int] = []

        for block_idx, entries in self._pending.items():
            if not entries:
                expired_blocks.append(block_idx)
                continue
            oldest = min(e.added_at for e in entries)
            if now - oldest >= self._window_secs:
                expired_blocks.append(block_idx)
                self._finalise_group_locked(block_idx, entries)

        for b in expired_blocks:
            self._pending.pop(b, None)

    def _finalise_group_locked(
        self, block_idx: int, entries: List[SweepEntry]
    ) -> None:
        """Convert a list of entries into a SweepEvent (or discard if single)."""
        # Read min-fills threshold from config (default 3).
        # On liquid pairs, two fills in the same block are usually two retail
        # buyers, not a coordinated arb sweep.  Use SWEEP_MIN_FILLS=2 for
        # thin/illiquid pairs; 3 (default) or higher for liquid ones.
        try:
            from config import cfg as _cfg
            _min_fills = max(2, int(getattr(_cfg, "SWEEP_MIN_FILLS", 3) or 3))
        except Exception:
            _min_fills = 3

        if len(entries) < _min_fills:
            # Not enough fills to be a sweep — leave classification as-is.
            return

        group_id = f"sweep_{block_idx}"

        # Upgrade UNKNOWN fills with matching block index to DEXIE_COMBINED
        self._upgrade_unknown_fills_locked(entries, group_id)

        event = SweepEvent(
            sweep_group_id=group_id,
            spent_block_index=block_idx,
            fills=list(entries),
        )

        self._events.append(event)
        if len(self._events) > _MAX_BUFFERED_EVENTS:
            self._events.pop(0)

    def _upgrade_unknown_fills_locked(
        self, entries: List[SweepEntry], group_id: str
    ) -> None:
        """Persist DEXIE_COMBINED + sweep_group_id for UNKNOWN fills."""
        from fill_classifier import FillType

        for entry in entries:
            if entry.classification != FillType.UNKNOWN:
                # Already classified (ARB_SWEEP_*, DEXIE_COMBINED) — just
                # ensure the sweep_group_id is stamped.
                _set_sweep_group(entry.fill_id, group_id)
                continue

            # Upgrade UNKNOWN → DEXIE_COMBINED
            try:
                from database import get_connection
                conn = get_connection()
                conn.execute(
                    """UPDATE fills
                       SET fill_classification = ?,
                           sweep_group_id      = ?
                       WHERE fill_id = ?""",
                    (FillType.DEXIE_COMBINED, group_id, entry.fill_id),
                )
                conn.commit()
                entry.classification = FillType.DEXIE_COMBINED
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_coordinator: Optional[SweepCoordinator] = None
_coordinator_lock = threading.Lock()


def get_coordinator() -> SweepCoordinator:
    """Return the shared module-level SweepCoordinator instance."""
    global _coordinator
    if _coordinator is None:
        with _coordinator_lock:
            if _coordinator is None:
                window = _DEFAULT_WINDOW_SECS
                try:
                    from config import cfg
                    window = float(
                        getattr(cfg, "SWEEP_WINDOW_SECS", _DEFAULT_WINDOW_SECS)
                    )
                except Exception:
                    pass
                _coordinator = SweepCoordinator(window_secs=window)
    return _coordinator


def reset_coordinator() -> None:
    """Replace the singleton (used by tests)."""
    global _coordinator
    with _coordinator_lock:
        _coordinator = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_sweep_group(fill_id: int, group_id: str) -> None:
    """Stamp sweep_group_id on a fill without changing classification."""
    try:
        from database import get_connection
        conn = get_connection()
        conn.execute(
            "UPDATE fills SET sweep_group_id = ? WHERE fill_id = ?",
            (group_id, fill_id),
        )
        conn.commit()
    except Exception:
        pass

