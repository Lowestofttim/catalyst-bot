"""Coin reservation registry.

Eliminates races between offer creation, topup absorption, and
consolidation by giving each operation an explicit reservation on the
coins it intends to use.

Before this module: trade and topup both scanned ``_xch_inventory`` /
``_cat_inventory`` independently. If trade picked a coin at the same
moment topup was about to absorb it, whichever ran second either
silently skipped or attempted to spend a coin already consumed. This
produced transient "no coin found" failures and "ghost" coins.

After this module: every long-running operation (offer creation,
misfit absorb, consolidation, split) reserves coins BEFORE the RPC
call. Reservations are:

- **Atomic** — held behind a lock, so two concurrent reservations
  can't race for the same coin.
- **Short-lived** — default 30 second TTL. Expired reservations auto-
  release so a crash / timeout doesn't leak reservations forever.
- **Intentional** — caller must declare ``purpose`` (for logs and
  auditability) and opaque ``owner`` (to prevent one operation's
  release accidentally freeing another's reservations).

This module is intentionally tiny and dependency-free — it could live
in coin_manager.py, but separating it keeps the reservation contract
clean and testable in isolation.

Usage::

    from coin_reservations import ReservationRegistry

    registry = ReservationRegistry()

    # ── Offer creation path ─────────────────────────────────────────
    reserved = registry.reserve(
        coin_ids=["aaa...", "bbb..."],
        owner="offer-create-cycle-82",
        purpose="offer_create",
        ttl_seconds=45,
    )
    if reserved != 2:
        log_event("coin_contended", "some coins grabbed by another op")
        # use whatever was reserved, skip the rest
    try:
        submit_offers(reserved_coin_ids)
    finally:
        registry.release_by_owner("offer-create-cycle-82")

    # ── Topup path ──────────────────────────────────────────────────
    if not registry.is_reserved(coin_id):
        registry.reserve([coin_id], owner="topup-absorb", purpose="absorb")
        ...
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set


# Reservation entries in the registry — one per coin_id at a time.
@dataclass(frozen=True)
class Reservation:
    coin_id: str
    owner: str
    purpose: str
    created_at: float
    expires_at: float


class ReservationRegistry:
    """Thread-safe registry of coin reservations.

    All public methods acquire the internal lock briefly. Reservations
    time out and auto-release via :meth:`gc_expired` — call periodically
    from the bot's main loop (e.g. every 10 cycles).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # coin_id (lowercase hex) -> Reservation
        self._reservations: Dict[str, Reservation] = {}
        # Owner -> set of coin_ids held by that owner (for fast release_by_owner)
        self._by_owner: Dict[str, Set[str]] = {}
        # Counters for diagnostics
        self._total_reserved = 0
        self._total_released = 0
        self._total_expired = 0
        self._total_contested = 0  # attempts to reserve an already-reserved coin

    # --- public API --------------------------------------------------

    def reserve(
        self,
        coin_ids: Iterable[str],
        owner: str,
        purpose: str,
        ttl_seconds: float = 30.0,
    ) -> List[str]:
        """Reserve one or more coins for ``owner``.

        Returns the list of coin_ids that WERE actually reserved (a subset
        of the input — coins already reserved by someone else are silently
        skipped, and the caller should check the return length).

        If this owner already holds a reservation for a coin, the TTL is
        refreshed. This lets a long-running op re-reserve its own coins
        idempotently.
        """
        if not coin_ids or not owner:
            return []
        now = time.time()
        expires = now + max(1.0, float(ttl_seconds))
        reserved: List[str] = []
        with self._lock:
            # Opportunistically garbage-collect expired reservations so
            # we don't hold onto stale entries.
            self._gc_locked(now)
            owner_set = self._by_owner.setdefault(owner, set())
            for raw in coin_ids:
                cid = _normalise(raw)
                if not cid:
                    continue
                existing = self._reservations.get(cid)
                if existing and existing.owner != owner and existing.expires_at > now:
                    # Contested — skip this coin
                    self._total_contested += 1
                    continue
                # Free to reserve (or refresh our own)
                self._reservations[cid] = Reservation(
                    coin_id=cid,
                    owner=owner,
                    purpose=purpose,
                    created_at=now,
                    expires_at=expires,
                )
                owner_set.add(cid)
                reserved.append(cid)
                self._total_reserved += 1
        return reserved

    def release(self, coin_ids: Iterable[str], owner: str) -> int:
        """Release reservations held by ``owner``. Coins owned by someone
        else are not affected (silently skipped). Returns the count of
        reservations released."""
        if not coin_ids or not owner:
            return 0
        released = 0
        with self._lock:
            owner_set = self._by_owner.get(owner)
            if owner_set is None:
                return 0
            for raw in coin_ids:
                cid = _normalise(raw)
                if not cid:
                    continue
                r = self._reservations.get(cid)
                if r is not None and r.owner == owner:
                    del self._reservations[cid]
                    owner_set.discard(cid)
                    released += 1
                    self._total_released += 1
            if not owner_set:
                self._by_owner.pop(owner, None)
        return released

    def release_by_owner(self, owner: str) -> int:
        """Release ALL reservations held by ``owner``. Useful in finally
        blocks to guarantee cleanup even on exception paths."""
        if not owner:
            return 0
        with self._lock:
            owner_set = self._by_owner.pop(owner, None)
            if not owner_set:
                return 0
            released = 0
            for cid in list(owner_set):
                r = self._reservations.get(cid)
                if r is not None and r.owner == owner:
                    del self._reservations[cid]
                    released += 1
                    self._total_released += 1
            return released

    def is_reserved(self, coin_id: str, *, now: Optional[float] = None) -> bool:
        """True if the coin is currently reserved by ANY owner (and the
        reservation has not expired)."""
        cid = _normalise(coin_id)
        if not cid:
            return False
        if now is None:
            now = time.time()
        with self._lock:
            r = self._reservations.get(cid)
            if r is None:
                return False
            if r.expires_at <= now:
                # Lazy cleanup of this one expired entry
                self._expire_locked(cid, r)
                return False
            return True

    def is_reserved_by(self, coin_id: str, owner: str) -> bool:
        """True if the coin is reserved by this specific owner."""
        cid = _normalise(coin_id)
        if not cid:
            return False
        with self._lock:
            r = self._reservations.get(cid)
            return r is not None and r.owner == owner and r.expires_at > time.time()

    def filter_unreserved(self, coin_ids: Iterable[str]) -> List[str]:
        """Return the subset of ``coin_ids`` that are NOT currently
        reserved. Convenience for callers that want to scan an inventory
        without accidentally using a coin another operation is holding."""
        now = time.time()
        out: List[str] = []
        with self._lock:
            self._gc_locked(now)
            for raw in coin_ids:
                cid = _normalise(raw)
                if not cid:
                    continue
                if cid not in self._reservations:
                    out.append(cid)
        return out

    def gc_expired(self) -> int:
        """Release expired reservations. Call periodically. Returns count
        of reservations released."""
        with self._lock:
            return self._gc_locked(time.time())

    def stats(self) -> Dict[str, int]:
        """Diagnostic snapshot — useful for logging / dashboards."""
        with self._lock:
            return {
                "currently_reserved": len(self._reservations),
                "owners": len(self._by_owner),
                "total_reserved": self._total_reserved,
                "total_released": self._total_released,
                "total_expired": self._total_expired,
                "total_contested": self._total_contested,
            }

    # --- private helpers ---------------------------------------------

    def _gc_locked(self, now: float) -> int:
        """Must be called with self._lock held. Returns count expired."""
        expired_ids = [
            cid for cid, r in self._reservations.items() if r.expires_at <= now
        ]
        for cid in expired_ids:
            r = self._reservations.pop(cid)
            owner_set = self._by_owner.get(r.owner)
            if owner_set:
                owner_set.discard(cid)
                if not owner_set:
                    self._by_owner.pop(r.owner, None)
            self._total_expired += 1
        return len(expired_ids)

    def _expire_locked(self, cid: str, r: Reservation) -> None:
        """Lazy-expire a single entry. Must be called with lock held."""
        self._reservations.pop(cid, None)
        owner_set = self._by_owner.get(r.owner)
        if owner_set:
            owner_set.discard(cid)
            if not owner_set:
                self._by_owner.pop(r.owner, None)
        self._total_expired += 1


# ---------------------------------------------------------------------
# Normalisation — Chia coin IDs are 32-byte hex strings. The rest of
# the codebase mixes "0x"-prefixed and bare lowercase. Standardise on
# bare lowercase throughout this registry so equality is reliable.
# ---------------------------------------------------------------------

def _normalise(coin_id: str) -> str:
    if not coin_id:
        return ""
    c = str(coin_id).strip().lower()
    if c.startswith("0x"):
        c = c[2:]
    return c


__all__ = [
    "Reservation",
    "ReservationRegistry",
]
