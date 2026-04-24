"""Adaptive AMM buffer sizing driven by recent sweep activity

Tracks arbitrage sweep events in a rolling in-memory window and returns
a multiplier (1.0x baseline up to 2.5x cap) that widens the configured
`AMM_BUFFER_BPS` when sweeps are frequent. `record_sweep()` is called by
`bot_loop` as sweep events drain, and `get_buffer()` is consumed by
`amm_monitor` when evaluating buffer proximity so offers are pushed
farther from the AMM curve during active arbitrage. State lives in a
module-level singleton guarded by a threading lock.

Key responsibilities:
    - Track sweep timestamps in a rolling window
    - Map sweep frequency to a buffer multiplier
    - Expose `record_sweep()` and `get_buffer()` entry points
    - Keep all state in-memory, lock-protected, and process-local
"""

from __future__ import annotations

import threading
import time
from collections import deque
from decimal import Decimal
from typing import Deque, Optional, Tuple


# ---------------------------------------------------------------------------
# Core class
# ---------------------------------------------------------------------------

class DynamicAMMBuffer:
    """Thread-safe rolling-window sweep tracker that returns widened buffer bps."""

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        # Each entry: (timestamp_monotonic, fill_count)
        self._sweeps: Deque[Tuple[float, int]] = deque()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_sweep(self, fill_count: int = 1) -> None:
        """Record that a sweep just occurred (call after SweepEvent fires)."""
        with self._lock:
            self._sweeps.append((time.monotonic(), fill_count))

    def get_effective_buffer_bps(self, base_bps) -> Decimal:
        """Return effective buffer bps, possibly widened by recent sweeps.

        base_bps may be int, float, str, or Decimal.
        """
        base = Decimal(str(base_bps))
        multiplier = self._get_multiplier()
        return (base * multiplier).quantize(Decimal("0.1"))

    def sweep_count_in_window(self) -> int:
        """Return the number of sweep events recorded in the current window."""
        with self._lock:
            self._prune_locked()
            return len(self._sweeps)

    def get_state(self) -> dict:
        """Diagnostic snapshot."""
        with self._lock:
            self._prune_locked()
            count = len(self._sweeps)
        multiplier = self._get_multiplier()
        try:
            from config import cfg
            base_bps = Decimal(str(getattr(cfg, "AMM_BUFFER_BPS", "30")))
            effective_bps = (base_bps * multiplier).quantize(Decimal("0.1"))
        except Exception:
            base_bps = effective_bps = Decimal("30")
        return {
            "sweep_count_in_window": count,
            "window_mins":           self._window_mins(),
            "multiplier":            float(multiplier),
            "base_bps":              str(base_bps),
            "effective_bps":         str(effective_bps),
            "enabled":               self._enabled(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_multiplier(self) -> Decimal:
        if not self._enabled():
            return Decimal("1")
        with self._lock:
            self._prune_locked()
            count = len(self._sweeps)
        try:
            from config import cfg
            med = Decimal(str(getattr(cfg, "DYNAMIC_BUFFER_MULTIPLIER_MED",  "1.5")))
            hi  = Decimal(str(getattr(cfg, "DYNAMIC_BUFFER_MULTIPLIER_HIGH", "2.0")))
            cap = Decimal(str(getattr(cfg, "DYNAMIC_BUFFER_MULTIPLIER_CAP",  "2.5")))
        except Exception:
            med, hi, cap = Decimal("1.5"), Decimal("2.0"), Decimal("2.5")

        if count == 0:   return Decimal("1")
        if count <= 2:   return med
        if count <= 5:   return hi
        return cap

    def _prune_locked(self) -> None:
        """Remove sweep entries older than the rolling window."""
        window_secs = self._window_mins() * 60
        cutoff = time.monotonic() - window_secs
        while self._sweeps and self._sweeps[0][0] < cutoff:
            self._sweeps.popleft()

    @staticmethod
    def _window_mins() -> float:
        try:
            from config import cfg
            return float(getattr(cfg, "DYNAMIC_BUFFER_WINDOW_MINS", 60))
        except Exception:
            return 60.0

    @staticmethod
    def _enabled() -> bool:
        try:
            from config import cfg
            return bool(getattr(cfg, "DYNAMIC_BUFFER_ENABLED", True))
        except Exception:
            return True


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_buffer: Optional[DynamicAMMBuffer] = None
_buffer_lock = threading.Lock()


def _get_buffer_instance() -> DynamicAMMBuffer:
    global _buffer
    if _buffer is None:
        with _buffer_lock:
            if _buffer is None:
                _buffer = DynamicAMMBuffer()
    return _buffer


def reset_buffer() -> None:
    """Replace the singleton (used by tests)."""
    global _buffer
    with _buffer_lock:
        _buffer = None


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def record_sweep(fill_count: int = 1) -> None:
    """Record a sweep event.  Call this after each SweepEvent fires."""
    _get_buffer_instance().record_sweep(fill_count)


def get_buffer(base_bps) -> Decimal:
    """Return effective buffer bps (possibly widened by recent sweeps)."""
    return _get_buffer_instance().get_effective_buffer_bps(base_bps)


def get_state() -> dict:
    """Return diagnostic state dict for the dynamic buffer."""
    return _get_buffer_instance().get_state()

