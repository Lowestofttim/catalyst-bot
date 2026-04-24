"""Persistent SQLite-backed capacity reservations for in-flight offer amounts

ReservationManager tracks aggregate XCH and CAT mojos that callers intend to
commit during parallel offer creation, so the bot can avoid over-allocating
its balance before those offers actually post. Unlike coin_reservations.py
(which locks specific coin IDs in memory), this module records capacity
totals in SQLite and survives restarts. Reservations are advisory: callers
inspect get_reserved_totals() and decide for themselves whether to proceed.

Key responsibilities:
    - try_acquire() to record an intended XCH/CAT capacity lease with a TTL
    - release() to clear a lease when the work completes or fails
    - expire_stale() to reap leases whose TTL has passed
    - get_reserved_totals() for callers to read current in-flight commitments
    - init_reservation_table() to create the schema lazily (called from database.py)

This is additive to the per-coin lock held via the coins table — it answers
the question "how much capacity is already spoken for right now?" rather
than "is this specific coin locked?". Callers that need hard enforcement
must combine this signal with their own pre-flight checks.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import get_connection, log_event


@dataclass(frozen=True, slots=True)
class ReservationResult:
    """Outcome of a reservation attempt."""
    success: bool
    reservation_id: str
    error: str = ""


# ---------------------------------------------------------------------------
# Schema — table created via init_reservation_table()
# ---------------------------------------------------------------------------
RESERVATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservation_leases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id  TEXT NOT NULL UNIQUE,
    purpose         TEXT NOT NULL,
    xch_mojos       INTEGER NOT NULL DEFAULT 0,
    cat_mojos       INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'completed', 'failed', 'expired')),
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    released_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservation_leases(status);
CREATE INDEX IF NOT EXISTS idx_reservations_expires ON reservation_leases(expires_at);
"""


def init_reservation_table():
    """Create the reservation_leases table if it doesn't exist.

    Called from database.init_database() during migration phase.
    """
    conn = get_connection()
    conn.executescript(RESERVATION_SCHEMA)
    conn.commit()


# Module-level singleton — avoids creating new instances every cycle
_singleton_lock = threading.Lock()
_singleton: Optional["ReservationManager"] = None


def get_reservation_manager() -> "ReservationManager":
    """Return the module-level ReservationManager singleton."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ReservationManager()
    return _singleton


class ReservationManager:
    """Thread-safe capacity reservation for parallel offer creation."""

    def __init__(self):
        self._lock = threading.Lock()

    def try_acquire(
        self,
        purpose: str,
        xch_mojos: int = 0,
        cat_mojos: int = 0,
        lease_secs: int = 120,
    ) -> ReservationResult:
        """Try to acquire a capacity reservation.

        Args:
            purpose: Human-readable reason (e.g., "create_buy_offer_tier_mid")
            xch_mojos: XCH capacity to reserve (in mojos)
            cat_mojos: CAT capacity to reserve (in mojos)
            lease_secs: How long the reservation lives (default 120s)

        Returns:
            ReservationResult with success=True and a reservation_id, or
            success=False with an error message.
        """
        if xch_mojos <= 0 and cat_mojos <= 0:
            return ReservationResult(
                success=False,
                reservation_id="",
                error="reservation requires positive xch_mojos or cat_mojos",
            )

        lease_secs = max(30, lease_secs)  # minimum 30s
        reservation_id = f"res_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_secs)

        with self._lock:
            try:
                conn = get_connection()
                now_iso = now.isoformat()

                # Expire stale leases and insert new one in same commit.
                # Python's sqlite3 auto-manages transactions — do NOT use
                # manual BEGIN/COMMIT (causes "cannot start transaction
                # within a transaction" with default isolation_level).
                conn.execute(
                    """UPDATE reservation_leases
                       SET status = 'expired', released_at = ?
                       WHERE status = 'active' AND expires_at <= ?""",
                    (now_iso, now_iso),
                )
                conn.execute(
                    """INSERT INTO reservation_leases
                       (reservation_id, purpose, xch_mojos, cat_mojos,
                        status, created_at, expires_at)
                       VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                    (reservation_id, purpose, xch_mojos, cat_mojos,
                     now_iso, expires_at.isoformat()),
                )
                conn.commit()

                return ReservationResult(
                    success=True,
                    reservation_id=reservation_id,
                )

            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return ReservationResult(
                    success=False,
                    reservation_id="",
                    error=f"reservation failed: {e}",
                )

    def release(self, reservation_id: str, status: str = "completed"):
        """Release a reservation.

        Args:
            reservation_id: The ID returned by try_acquire()
            status: Final status — "completed" or "failed"
        """
        if not reservation_id:
            return

        if status not in ("completed", "failed"):
            status = "completed"

        with self._lock:
            try:
                conn = get_connection()
                now_iso = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """UPDATE reservation_leases
                       SET status = ?, released_at = ?
                       WHERE reservation_id = ? AND status = 'active'""",
                    (status, now_iso, reservation_id),
                )
                conn.commit()
            except Exception as e:
                try:
                    log_event("warning", "reservation_release_error",
                              f"Failed to release {reservation_id}: {e}")
                except Exception:
                    pass

    def expire_stale(self) -> int:
        """Expire all active leases past their TTL. Returns count expired."""
        with self._lock:
            try:
                conn = get_connection()
                now_iso = datetime.now(timezone.utc).isoformat()
                cursor = conn.execute(
                    """UPDATE reservation_leases
                       SET status = 'expired', released_at = ?
                       WHERE status = 'active' AND expires_at <= ?""",
                    (now_iso, now_iso),
                )
                count = cursor.rowcount
                conn.commit()
                if count > 0:
                    try:
                        log_event("info", "reservation_expired",
                                  f"Expired {count} stale reservation(s)")
                    except Exception:
                        pass
                return count
            except Exception as e:
                print(f"  [ReservationManager] expire_stale error: {e}", flush=True)
                return 0

    def expire_all(self) -> int:
        """Expire ALL active leases (used on startup to clear previous runtime).

        Returns count expired.
        """
        with self._lock:
            try:
                conn = get_connection()
                now_iso = datetime.now(timezone.utc).isoformat()
                cursor = conn.execute(
                    """UPDATE reservation_leases
                       SET status = 'expired', released_at = ?
                       WHERE status = 'active'""",
                    (now_iso,),
                )
                count = cursor.rowcount
                conn.commit()
                return count
            except Exception as e:
                print(f"  [ReservationManager] expire_all error: {e}", flush=True)
                return 0

    def get_reserved_totals(self) -> dict:
        """Get total active reserved amounts.

        Returns:
            {"xch_mojos": int, "cat_mojos": int, "count": int}
        """
        with self._lock:
            try:
                conn = get_connection()
                now_iso = datetime.now(timezone.utc).isoformat()
                row = conn.execute(
                    """SELECT COALESCE(SUM(xch_mojos), 0) AS xch,
                              COALESCE(SUM(cat_mojos), 0) AS cat,
                              COUNT(*) AS cnt
                       FROM reservation_leases
                       WHERE status = 'active' AND expires_at > ?""",
                    (now_iso,),
                ).fetchone()
                return {
                    "xch_mojos": row["xch"],
                    "cat_mojos": row["cat"],
                    "count": row["cnt"],
                }
            except Exception as e:
                print(f"  [ReservationManager] get_reserved_totals error: {e}", flush=True)
                return {"xch_mojos": 0, "cat_mojos": 0, "count": 0}

    def list_active(self) -> list:
        """List all active reservations (for diagnostics)."""
        with self._lock:
            try:
                conn = get_connection()
                now_iso = datetime.now(timezone.utc).isoformat()
                rows = conn.execute(
                    """SELECT reservation_id, purpose, xch_mojos, cat_mojos,
                              created_at, expires_at
                       FROM reservation_leases
                       WHERE status = 'active' AND expires_at > ?
                       ORDER BY created_at""",
                    (now_iso,),
                ).fetchall()
                return [dict(r) for r in rows]
            except Exception as e:
                print(f"  [ReservationManager] list_active error: {e}", flush=True)
                return []

    def prune_old(self, retention_hours: int = 24):
        """Delete non-active leases older than retention period."""
        with self._lock:
            try:
                conn = get_connection()
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat()
                conn.execute(
                    """DELETE FROM reservation_leases
                       WHERE status != 'active' AND created_at < ?""",
                    (cutoff,),
                )
                conn.commit()
            except Exception as e:
                print(f"  [ReservationManager] prune_old error: {e}", flush=True)

