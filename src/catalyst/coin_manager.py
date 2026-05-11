"""Central coin inventory, classification, and lifecycle orchestrator

CoinManager is the authoritative in-memory view of the bot's spendable XCH and
CAT coins. It keeps live counts, runs tier-aware classification, manages the
fee-coin pool, holds the in-flight reservation registry, drives smart topups,
and launches the coin_prep_worker subprocess when deeper reshaping is needed.
Wallet RPC results are folded back in via reconcile_with_wallet() so the DB
and live caches stay aligned with ground truth.

Key responsibilities:
    - Track XCH/CAT inventories and expose tier-bucketed counts
    - Classify coins into reserve / trading / small / locked roles
    - Own FeeCoinPool and the ReservationRegistry used across modules
    - Run smart topup (split reserves, consolidate smalls) between cycles
    - Spawn and monitor the coin_prep_worker subprocess
    - Reconcile live state with wallet RPC on demand

A module-level fast-reconcile signal (request_fast_reconcile() /
consume_fast_reconcile()) lets other modules nudge the next cycle to skip its
normal cadence and refresh immediately after an event that likely changed
coin state.
"""

import time
import threading
import subprocess
import json
import os
import hashlib
import sys
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from config import cfg
from database import log_event


# ---------------------------------------------------------------------------
# Fast-reconcile trigger (F75).
#
# Other modules (notably ``offer_manager.cancel_offers``) can call
# :func:`request_fast_reconcile` to flag "please run reconcile ASAP on
# the next loop tick, don't wait for the normal cadence". The main
# bot loop consults :func:`consume_fast_reconcile` when deciding
# whether to reconcile this cycle.
#
# Rationale: cancels create new output coins that the bot needs to see
# before attempting to rebuild the ladder. The normal 2-cycle reconcile
# cadence means the rebuild often races ahead of the reconcile and the
# new coins don't appear in tier pools until a cycle later. This flag
# closes that race — the reconcile runs on the very next cycle after a
# cancel confirms, so returned coins are in the right tier pool before
# create_ladder() looks for them.
#
# This is intentionally a module-level flag (not an instance attribute)
# so offer_manager can signal without needing a reference to the
# CoinManager instance. Matches the pattern used by
# ``_bot_cancelled_ids`` in offer_manager for the same reason.
# ---------------------------------------------------------------------------

_fast_reconcile_flag: bool = False
_fast_reconcile_lock: threading.Lock = threading.Lock()
_TOPUP_PENDING = "pending"


def _topup_event_log_level(event_type: str) -> str:
    """Return the log level for routine top-up progress events."""
    evt = str(event_type or "")
    routine_events = {
        "topup_inventory",
        "topup_inventory_changed",
        "topup_waiting",
    }
    routine_suffixes = (
        "_refetch",
        "_fee_coin_reserved",
        "_osstep_start",
        "_osstep_outputs_owned",
        "_osstep_wait",
    )
    if evt in routine_events or evt.endswith(routine_suffixes):
        return "debug"
    return "info"


def request_fast_reconcile(reason: str = "unspecified") -> None:
    """Request a reconcile on the next bot-loop cycle.

    Thread-safe. Idempotent — calling multiple times before the flag
    is consumed is equivalent to calling once.
    """
    global _fast_reconcile_flag
    with _fast_reconcile_lock:
        _fast_reconcile_flag = True
    # Log at debug so we can correlate requests with cycle-level reconciles
    try:
        log_event("debug", "fast_reconcile_requested",
                  f"Fast reconcile requested (reason={reason})")
    except Exception:
        pass


def consume_fast_reconcile() -> bool:
    """Return True once if a fast reconcile was requested, resetting
    the flag. Subsequent calls return False until the next request.
    """
    global _fast_reconcile_flag
    with _fast_reconcile_lock:
        was = _fast_reconcile_flag
        _fast_reconcile_flag = False
    return was
from tx_fees import (
    fee_pool_enabled,
    get_effective_transaction_fee_mojos,
    get_fee_coin_size_mojos,
    get_fee_coin_size_xch,
    get_fee_pool_count,
    get_fee_tier_name,
)
from wallet import (
    get_exact_spendable_coins_rpc,
    get_next_address, send_transaction,
    split_coins_rpc,
    get_wallet_type,
    WALLET_ID_XCH,
    get_owned_coins,
    get_owned_coins_detailed,
)
from win_subprocess import hidden_subprocess_kwargs


# Cooldowns
_TOPUP_COOLDOWN = 600            # 10 minutes between emergency topups (normal)
_TOPUP_BACKOFF_BASE = 300        # 5 minutes — first retry when nothing available
_TOPUP_BACKOFF_MAX = 3600        # 60 minutes — ceiling for exponential backoff
# Old fixed 2-hour constant removed. Backoff is now exponential:
# attempt 0 → 5 min, 1 → 10 min, 2 → 20 min, 3 → 40 min, 4+ → 60 min (capped)
_TOPUP_DRIP_INTERVAL = 90        # 90 seconds between proactive drip checks
_DRIP_SOURCE_NOTICE_INTERVAL = 3600  # 60 minutes between optional no-source notices


def _coin_prep_worker_command(worker_path: str) -> list[str]:
    """Return the command used to launch the coin prep worker.

    In a PyInstaller bundle, `coin_prep_worker.py` is not a normal Python
    source tree with its helper modules beside it. Re-enter the bundled
    Catalyst executable so imports resolve from the PyInstaller archive.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--coin-prep-worker"]
    return [sys.executable, worker_path]


def _set_sage_data_dir_from_cert_env(env: dict, cert_path: str) -> None:
    if (env.get("SAGE_DATA_DIR") or "").strip():
        return

    try:
        cert_dir = os.path.dirname(os.path.realpath(cert_path))
        if os.path.basename(cert_dir).lower() == "ssl":
            env["SAGE_DATA_DIR"] = os.path.dirname(cert_dir)
    except Exception:
        pass


def _log_coin_prep_sage_rpc_context(cert_path: str, source: str) -> None:
    try:
        cert_name = os.path.basename(cert_path) or "wallet.crt"
        log_event(
            "info",
            "coin_prep_sage_rpc_context",
            "Passing "
            f"{source} Sage RPC certificate to coin prep worker: "
            f"{cert_name} (path redacted)",
        )
    except Exception:
        pass


def _coin_prep_worker_environment(base_env: Optional[dict] = None) -> dict:
    """Return the environment used when launching the coin-prep worker.

    Sage's startup path can auto-detect the wallet TLS certificate at runtime
    without writing it to the user's .env. The prep worker is a fresh process,
    so make that detected RPC context explicit before spawning it.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["PYTHONIOENCODING"] = "utf-8"
    env["_CATALYST_PRESERVE_PROCESS_ENV"] = "1"

    wallet_type = (
        (env.get("WALLET_TYPE") or getattr(cfg, "WALLET_TYPE", "sage") or "sage")
        .strip()
        .lower()
    )
    if wallet_type != "sage":
        return env

    cert_path = (env.get("SAGE_CERT_PATH") or "").strip()
    key_path = (env.get("SAGE_KEY_PATH") or "").strip()
    if cert_path and key_path:
        _set_sage_data_dir_from_cert_env(env, cert_path)
        _log_coin_prep_sage_rpc_context(cert_path, "configured")
        return env

    try:
        from sage_node import detect_sage_cert_path
        detected_cert = detect_sage_cert_path()
    except Exception as exc:
        try:
            log_event(
                "warning",
                "coin_prep_sage_cert_detect_failed",
                f"Could not auto-detect Sage cert for coin prep worker: {exc}",
            )
        except Exception:
            pass
        return env

    if not detected_cert:
        return env

    detected_cert = os.path.realpath(detected_cert)
    detected_key = os.path.realpath(
        os.path.join(os.path.dirname(detected_cert), "wallet.key")
    )
    env["SAGE_CERT_PATH"] = detected_cert
    env["SAGE_KEY_PATH"] = detected_key
    _set_sage_data_dir_from_cert_env(env, detected_cert)

    _log_coin_prep_sage_rpc_context(detected_cert, "auto-detected")
    return env


class _TopupWalletDegraded(Exception):
    """Raised when wallet RPC becomes too degraded to continue topup safely."""


# -----------------------------------------------------------------------
# Fee Coin Pool — thread-safe reservation for concurrent operations
# -----------------------------------------------------------------------

class FeeCoinPool:
    """Thread-safe pool for reserving dedicated fee coins.

    Problem: when the bot fires multiple operations (creates + cancels)
    in the same cycle, Sage auto-picks fee coins for each one.  If the
    operations overlap, Sage may grab the *same* fee coin for two
    different transactions → MEMPOOL_CONFLICT / BAD_AGGREGATE_SIGNATURE.

    Solution: each operation reserves a specific fee coin from this pool
    *before* calling Sage, and passes it via the ``coin_ids`` parameter.
    Sage then uses the provided coin for the fee instead of auto-picking.

    Lifecycle:
      • ``refresh()`` is called once at the start of every bot cycle
        with the current fee-coin inventory.  This resets all prior
        reservations (coins that were successfully spent are gone from
        the inventory; coins that weren't are re-added automatically).
      • ``reserve()`` hands out one coin ID per call.
      • No explicit ``release()`` needed — the next ``refresh()`` resets.
    """

    def __init__(self):
        self._available: list = []   # [(coin_id, amount_mojos), ...]
        self._reserved: set = set()  # coin IDs handed out this cycle
        self._lock = threading.Lock()

    # ---- pool management ----

    def refresh(self, fee_coin_records: list):
        """Repopulate from inventory.  Resets all reservations."""
        with self._lock:
            self._available = []
            self._reserved = set()
            for rec in fee_coin_records:
                cid = _coin_id_from_record(rec)
                if cid:
                    amt = _coin_amount(rec)
                    self._available.append((cid.lower(), amt))

    def reserve(self) -> str | None:
        """Reserve one fee coin.  Returns coin_id or None if pool empty."""
        with self._lock:
            for cid, _amt in self._available:
                if cid not in self._reserved:
                    self._reserved.add(cid)
                    return cid
        return None

    # ---- introspection ----

    @property
    def available_count(self) -> int:
        with self._lock:
            return sum(1 for cid, _ in self._available if cid not in self._reserved)

    @property
    def total_count(self) -> int:
        with self._lock:
            return len(self._available)

    @property
    def reserved_count(self) -> int:
        with self._lock:
            return len(self._reserved)


# -----------------------------------------------------------------------
# Coin record helpers
# -----------------------------------------------------------------------

def _extract_coin_records(rpc_result) -> list:
    """Extract coin records from an RPC get_spendable_coins response.

    Handles both Chia and Sage wallet response formats.
    Detects RPC errors (connection failures, auth errors) and logs them
    rather than silently returning empty lists.
    """
    if not rpc_result or not isinstance(rpc_result, dict):
        return []

    # Check for RPC error responses (Sage returns these as dicts with 'error' key)
    if rpc_result.get("error") or rpc_result.get("success") is False:
        err = rpc_result.get("error", "unknown")
        log_event("warning", "rpc_error_in_coins",
                  f"Wallet RPC returned error instead of coins: {str(err)[:200]}")
        return []

    return rpc_result.get("confirmed_records") or rpc_result.get("records") or []


def _get_free_coins_rpc(wallet_id: int):
    """Get the exact currently free/selectable wallet coins.

    For Sage this bypasses the older owned+selectable merge workaround and
    returns the strict selectable view. For Chia the adapter aliases this to
    the normal spendable RPC, so the call stays backend-safe.
    """
    return get_exact_spendable_coins_rpc(wallet_id)


def _coin_amount(record: dict) -> int:
    """Get mojos amount from a coin record."""
    coin = record.get("coin", {})
    return coin.get("amount", 0)


def _chia_int_to_bytes(v: int) -> bytes:
    """Convert int to bytes using Chia's encoding (variable-length, signed).

    This matches chia.util.ints.int_to_bytes() exactly:
      byte_count = (v.bit_length() + 8) >> 3
      v.to_bytes(byte_count, "big", signed=True)

    Critical: Chia does NOT use fixed 8-byte encoding for coin IDs.
    Using fixed 8 bytes produces WRONG hashes for most amounts.
    """
    if v == 0:
        return b""
    byte_count = (v.bit_length() + 8) >> 3
    return v.to_bytes(byte_count, "big", signed=True)


def _coin_id_from_record(record: dict) -> str:
    """Get coin ID from a coin record.

    Strategy (in order of preference):
      1. Use a pre-computed ID field (Chia uses 'name', Sage uses 'coin_id')
      2. Compute SHA256(parent + puzzle_hash + int_to_bytes(amount))
         using Chia's variable-length int encoding
    """
    coin = record.get("coin", {})

    # ---- Strategy 1: Use wallet-provided ID ----
    # Chia wallet uses 'name', Sage wallet uses 'coin_id' at record level
    name = (coin.get("name", "")
            or record.get("name", "")
            or record.get("coin_id", "")
            or coin.get("coin_id", ""))
    if name:
        # Always normalize: lowercase + 0x prefix.
        # Sage wallet may return mixed-case hex; reconcile_coins_with_wallet
        # uses norm_coin_id() (lowercase). Without normalizing here, the
        # same physical coin gets two DB rows — one from upsert_coin (mixed
        # case, status='free') and one from reconcile (lowercase, status=
        # 'locked'). The free row then "wins" in queries.
        name = name.strip().lower()
        if not name.startswith("0x"):
            name = "0x" + name
        return name

    # ---- Strategy 2: Compute from fields ----
    parent = coin.get("parent_coin_info", "")
    puzzle = coin.get("puzzle_hash", "")
    amount = coin.get("amount", 0)
    if not parent or not puzzle:
        return ""
    try:
        p_bytes = bytes.fromhex(parent.replace("0x", ""))
        z_bytes = bytes.fromhex(puzzle.replace("0x", ""))
        a_bytes = _chia_int_to_bytes(amount)
        return "0x" + hashlib.sha256(p_bytes + z_bytes + a_bytes).hexdigest()
    except Exception:
        return ""


def _classify_coins(records: list, trading_size_mojos: int) -> Dict[str, list]:
    """Classify coin records into reserve / trading / small.

    Thresholds (relative to trading_size_mojos):
      RESERVE:  amount >= 2.0 × trading_size
      TRADING:  0.5 × trading_size <= amount < 2.0 × trading_size
      SMALL:    amount < 0.5 × trading_size
    """
    reserve = []
    trading = []
    small = []

    threshold_reserve = int(trading_size_mojos * 2.0)
    threshold_small = int(trading_size_mojos * 0.5)

    for rec in records:
        amt = _coin_amount(rec)
        if amt >= threshold_reserve:
            reserve.append(rec)
        elif amt >= threshold_small:
            trading.append(rec)
        else:
            small.append(rec)

    # Sort reserve by size descending (biggest first — best to split)
    reserve.sort(key=_coin_amount, reverse=True)
    # Sort small by size descending (for consolidation summary)
    small.sort(key=_coin_amount, reverse=True)

    return {"reserve": reserve, "trading": trading, "small": small}


def _load_advised_deposit_coin_ids() -> set:
    """Load deposit-advisory coin IDs that the user has already allocated."""
    try:
        from database import get_setting
        raw = get_setting("deposit_advisory_advised_coins", "") or ""
    except Exception:
        return set()
    return {
        str(part).strip().lower()
        for part in str(raw).split(",")
        if str(part).strip()
    }


def _filter_unallocated_deposit_sources(
    records: list,
    wallet_type: str,
    db_designations: dict,
    advised_coin_ids: set,
    threshold_mojos: int,
) -> tuple[list, int]:
    """Keep top-up from spending large external deposits before allocation."""
    if not records or int(threshold_mojos or 0) <= 0:
        return list(records or []), 0

    safe: list = []
    blocked = 0
    advised = {str(cid).lower() for cid in (advised_coin_ids or set())}
    designations = {
        str(cid).lower(): str(designation or "").lower()
        for cid, designation in (db_designations or {}).items()
    }

    for record in records:
        coin_id = str(_coin_id_from_record(record) or "").lower()
        designation = designations.get(coin_id, "")
        amount = int(_coin_amount(record) or 0)
        if (
            designation == "unknown"
            and coin_id not in advised
            and amount >= int(threshold_mojos)
        ):
            blocked += 1
            continue
        safe.append(record)

    return safe, blocked


def _deposit_advisory_source_threshold_mojos(
    is_cat: bool,
    fallback_tier_mojos: int = 0,
) -> int:
    """Threshold for deposit-sized unknown coins in top-up source selection."""
    smallest = 0
    try:
        sizes = get_tier_sizes_mojos_from_cfg(is_cat=is_cat)
        normal_sizes = [
            int(v)
            for tier, v in (sizes or {}).items()
            if tier in ("inner", "mid", "outer", "extreme") and int(v or 0) > 0
        ]
        if normal_sizes:
            smallest = min(normal_sizes)
    except Exception:
        smallest = 0

    if smallest <= 0:
        try:
            smallest = int(fallback_tier_mojos or 0)
        except Exception:
            smallest = 0
    if smallest <= 0:
        return 0
    return int(smallest * 10)


def _classify_coins_tiered(records: list, tier_sizes_mojos: Dict[str, int]) -> Dict[str, list]:
    """Classify coins into tier-specific buckets when TIER_ENABLED.

    LEGACY fallback — used by non-designation-aware code paths.
    The main path now uses CoinManager._classify_coins_by_designation().

    Each coin is matched to the nearest tier within ±20% tolerance.
    Coins too large for any tier go to 'reserve'.
    Coins too small for any tier go to 'small'.

    Args:
        records: List of coin records from RPC
        tier_sizes_mojos: {"inner": mojos, "mid": mojos, "outer": mojos, "extreme": mojos}

    Returns dict with keys: reserve, inner, mid, outer, extreme, small
    """
    result = {"reserve": [], "inner": [], "mid": [], "outer": [], "extreme": [], "small": []}
    if "sniper" in tier_sizes_mojos:
        result["sniper"] = []
    if "fees" in tier_sizes_mojos:
        result["fees"] = []

    # Sort tier sizes descending for classification
    tiers_sorted = sorted(tier_sizes_mojos.items(), key=lambda x: x[1], reverse=True)
    largest_tier_mojos = tiers_sorted[0][1] if tiers_sorted else 0
    smallest_tier_mojos = tiers_sorted[-1][1] if tiers_sorted else 0

    # Reserve threshold: 2× the largest tier size
    reserve_threshold = int(largest_tier_mojos * 2.0)
    # Small threshold: 0.5× the smallest tier size
    small_threshold = int(smallest_tier_mojos * 0.5)

    for rec in records:
        amt = _coin_amount(rec)

        if amt >= reserve_threshold:
            result["reserve"].append(rec)
            continue

        if amt < small_threshold:
            result["small"].append(rec)
            continue

        # Match to nearest tier within ±20% tolerance
        best_tier = None
        best_diff = float("inf")
        for tier_name, tier_mojos in tiers_sorted:
            low = int(tier_mojos * 0.8)
            high = int(tier_mojos * 1.2)
            if low <= amt <= high:
                diff = abs(amt - tier_mojos)
                if diff < best_diff:
                    best_tier = tier_name
                    best_diff = diff

        if best_tier:
            result[best_tier].append(rec)
        else:
            # Doesn't match any tier — assign to nearest tier anyway
            nearest = None
            nearest_diff = float("inf")
            for tier_name, tier_mojos in tiers_sorted:
                diff = abs(amt - tier_mojos)
                if diff < nearest_diff:
                    nearest = tier_name
                    nearest_diff = diff
            if nearest:
                result[nearest].append(rec)
            else:
                result["small"].append(rec)

    # Sort reserve by size descending
    result["reserve"].sort(key=_coin_amount, reverse=True)
    result["small"].sort(key=_coin_amount, reverse=True)

    return result


def _infer_designation_by_size(amt: int, tier_sizes_mojos: Dict[str, int]) -> Tuple[str, str]:
    """Infer a coin's designation from its amount (for NEW/UNKNOWN coins only).

    Used when a coin has no designation yet. Once designated, this is NOT
    called again — the DB designation is authoritative.

    Routes through the single-source-of-truth classifier in
    :mod:`coin_classifier`. This means the 2026-04-17 bug — where reconcile
    classified a 23.4k CAT coin as ``tier_spare/inner`` using loose ±20%
    bounds while the misfit absorber flagged it with stricter 0.98/1.5
    bounds — is now impossible. Both paths agree.

    Returns: ``(designation_str, assigned_tier_str)``
    """
    from coin_classifier import infer_designation_by_size as _cc_infer
    return _cc_infer(amt, tier_sizes_mojos)


def _effective_tier_size_drift_bounds(
    low_ratio: Optional[float],
    high_ratio: Optional[float],
) -> Tuple[float, float]:
    """Return the same coin-fit bounds used by tier offer selection."""
    try:
        from coin_classifier import DEFAULT_FLOOR_TOLERANCE
        default_low = float(DEFAULT_FLOOR_TOLERANCE)
    except Exception:
        default_low = 0.98
    try:
        default_high = float(getattr(cfg, "COIN_MAX_SIZE_RATIO", 1.5) or 1.5)
    except Exception:
        default_high = 1.5
    return (
        default_low if low_ratio is None else float(low_ratio),
        default_high if high_ratio is None else float(high_ratio),
    )


def check_tier_size_drift_standalone(
    low_ratio: Optional[float] = None,
    high_ratio: Optional[float] = None,
    min_sample: int = 2,
) -> List[Dict]:
    """Module-level mirror of CoinManager.check_tier_size_drift.

    Same logic, no instance required. Used by the coin-prep subprocess
    for its end-of-prep verification (the worker doesn't have a live
    CoinManager) and by api_server's pre-start gate. Returns the same
    list-of-finding-dicts shape: empty when all tier coins match.

    By default this uses the same usable coin bounds as live offer
    selection: the coin-classifier floor tolerance and COIN_MAX_SIZE_RATIO.
    That avoids telling the operator to re-prep while the bot still has
    perfectly usable oversize coins that live topup can reshape normally.
    """
    from database import get_coins_by_designation

    low_ratio, high_ratio = _effective_tier_size_drift_bounds(low_ratio, high_ratio)
    findings: List[Dict] = []
    if not cfg.TIER_ENABLED:
        return findings
    for wallet_type, is_cat in (("xch", False), ("cat", True)):
        try:
            live_sizes = get_tier_sizes_mojos_from_cfg(is_cat=is_cat)
        except Exception:
            continue
        if not live_sizes:
            continue
        for tier_name in ("inner", "mid", "outer", "extreme"):
            live_size = int(live_sizes.get(tier_name, 0) or 0)
            if live_size <= 0:
                continue
            try:
                coins = get_coins_by_designation(wallet_type, "tier_spare", tier_name)
            except Exception:
                continue
            amounts = sorted(
                int(c.get("amount_mojos") or 0)
                for c in coins
                if int(c.get("amount_mojos") or 0) > 0
                and str(c.get("status") or "free").lower() == "free"
            )
            if len(amounts) < max(1, int(min_sample)):
                continue
            n = len(amounts)
            median = (
                float(amounts[n // 2]) if n % 2 == 1
                else (amounts[n // 2 - 1] + amounts[n // 2]) / 2.0
            )
            ratio = float(median) / float(live_size)
            if ratio < low_ratio or ratio > high_ratio:
                findings.append({
                    "side": wallet_type,
                    "tier": tier_name,
                    "median_mojos": int(median),
                    "live_size_mojos": live_size,
                    "ratio": round(ratio, 3),
                    "coin_count": n,
                })
    return findings


def reclassify_tier_spare_coins() -> Dict[str, int]:
    """Re-stamp every existing tier_spare coin against the CURRENT tier
    sizes from cfg.

    Coin prep's diff-based designator only labels NEW coins it creates
    (coin_prep_worker._designate_new_tier_coins) — coins from previous
    prep runs keep their original assigned_tier even if Smart Settings
    has since changed the tier sizes. The drift check then sees stale
    labels and fires `tier_size_drift` immediately on bot start.

    This module-level function walks every tier_spare coin, runs
    classify_coin against the current tier sizes, and either:
      - re-stamps the coin to the tier its actual amount fits;
      - moves obvious dust into the dust bucket;
      - leaves the coin alone when the assignment is already correct.

    Returns a counter dict (``reclassified``, ``to_dust``, ``unchanged``,
    ``errors``) so callers can log churn. Module-level so the coin-prep
    subprocess can call it directly without instantiating CoinManager.
    """
    from coin_classifier import classify_coin, CoinDesignation, CoinFit
    from database import get_coins_by_designation, set_coin_designation

    moved = {"reclassified": 0, "to_dust": 0, "unchanged": 0, "errors": 0}
    if not cfg.TIER_ENABLED:
        return moved

    for wallet_type, is_cat in (("xch", False), ("cat", True)):
        try:
            live_sizes = get_tier_sizes_mojos_from_cfg(is_cat=is_cat)
        except Exception:
            continue
        if not live_sizes:
            continue
        try:
            coins = get_coins_by_designation(wallet_type, "tier_spare")
        except Exception:
            continue
        for c in coins:
            coin_id = c.get("coin_id") or c.get("id") or ""
            amount = int(c.get("amount_mojos") or 0)
            current_tier = (c.get("assigned_tier") or "").lower()
            if not coin_id or amount <= 0:
                continue
            # Skip non-standard tiers (sniper, fees) — `classify_coin`
            # only knows about inner/mid/outer/extreme via live_sizes.
            # Sniper coins are tiny relative to the main tiers and would
            # always be classified as DUST by the size-based classifier,
            # which destroys the sniper pool every reclassify pass and
            # forces an unnecessary topup absorption on every cycle. The
            # sniper/fees tiers manage their own sizing outside this
            # path; leaving these coins alone is correct.
            if current_tier in ("sniper", "fees"):
                moved["unchanged"] += 1
                continue
            try:
                cls = classify_coin(amount, live_sizes)
            except Exception:
                moved["errors"] += 1
                continue

            if cls.designation == CoinDesignation.DUST:
                new_designation = "dust"
                new_tier = None
            elif cls.best_tier and cls.fit in (CoinFit.EXACT, CoinFit.OVERSIZE_FIT):
                new_designation = "tier_spare"
                new_tier = cls.best_tier
            else:
                # Coin doesn't fit any tier cleanly — leave the existing
                # designation in place. The prep flow's consolidation step
                # will pick it up as stranded and may absorb it. Promoting
                # to dust here could orphan a coin that could still be
                # recombined by a later prep pass.
                moved["unchanged"] += 1
                continue

            if new_designation == "tier_spare" and new_tier == current_tier:
                moved["unchanged"] += 1
                continue

            try:
                set_coin_designation(coin_id, new_designation,
                                     assigned_tier=new_tier or "none")
                if new_designation == "dust":
                    moved["to_dust"] += 1
                else:
                    moved["reclassified"] += 1
            except Exception:
                moved["errors"] += 1

    return moved


def get_tier_sizes_mojos_from_cfg(is_cat: bool = False) -> Dict[str, int]:
    """Module-level helper that builds the tier_sizes_mojos dict from cfg
    without requiring a CoinManager instance.

    Used by :class:`OfferManager` (F70) to pass tier sizes into
    :func:`_select_coin_for_offer` for SSOT misfit rejection. Keeps the
    offer side from reaching into the CoinManager private API and lets
    the check run when an instance isn't readily available.

    Returns ``{"inner": mojos, "mid": mojos, "outer": mojos, "extreme": mojos}``
    where the keys are SIZE-tier (the labels coins carry in the DB
    designation system: a coin named ``tier_spare/inner`` is an inner-SIZE
    coin, regardless of which ladder POSITION it ends up backing). The F70
    misfit-rejection path pairs this dict with a SIZE-indexed
    ``preferred_tier`` from ``coin_size_tier_for_slot_position``, so both
    sides of the comparison must use the same indexing scheme.

    Under BUY_LADDER_REVERSED=True the buy storage is POSITION-indexed
    (Smart Defaults writes BUY_INNER_SIZE_XCH = small inner-position offer),
    but coin-size buckets follow ``_BUY_REVERSED_POSITION_TO_COIN_SIZE``
    where position-inner maps to bucket-extreme (smallest coin bucket).
    We apply that flip so the returned dict is coin-size-bucket-indexed
    and matches the ``preferred_tier`` produced by
    ``coin_size_tier_for_slot_position``. Without the flip, a 0.29 XCH
    coin would be labeled "inner" against a position-indexed dict while
    the selector asks for "extreme", rejecting every eligible coin.

    For CAT we derive sizes as ``(xch_tier_size / mid_price) * prep_headroom``
    scaled to CAT mojos. When mid_price is unknown, falls back to the
    XCH-denominated size directly.
    """
    if not cfg.TIER_ENABLED:
        return {}
    try:
        from config import get_buy_tier_size_xch, get_sell_tier_size_xch
    except Exception:
        return {}
    side = "sell" if is_cat else "buy"
    result_xch: Dict[str, Decimal] = {}

    if not is_cat and bool(getattr(cfg, "BUY_LADDER_REVERSED", False)):
        # Buy storage is POSITION-indexed; map each coin-size bucket back
        # to the POSITION whose offer size lives in that bucket, so the
        # returned dict is SIZE-bucket-indexed (matches the selector and
        # the coin_prep_worker CLI naming).
        for coin_tier in ("inner", "mid", "outer", "extreme"):
            pos_tier = _BUY_REVERSED_POSITION_TO_COIN_SIZE_INVERSE[coin_tier]
            attr = f"BUY_{pos_tier.upper()}_SIZE_XCH"
            val = Decimal(str(getattr(cfg, attr, 0) or 0))
            if val <= 0:
                # Legacy fallback (no per-side fields set). Legacy storage is
                # sell-shaped (inner=biggest), so the flip back to size-bucket
                # naming is just identity on the position we mapped to.
                val = Decimal(str(getattr(cfg, f"{pos_tier.upper()}_SIZE_XCH", 0) or 0))
            if val > 0:
                result_xch[coin_tier] = val
    else:
        get = get_sell_tier_size_xch if side == "sell" else get_buy_tier_size_xch
        for tier in ("inner", "mid", "outer", "extreme"):
            try:
                result_xch[tier] = Decimal(str(get(tier)))
            except Exception:
                continue

    # Apply prep headroom so the returned tier sizes match the actual coin
    # sizes created by coin_prep_worker. Prep sizes coins at tier_size ×
    # (1 + COIN_PREP_HEADROOM_PCT/100); classifier uses the same multiplier
    # so inner-sized coins classify as inner instead of drifting to mid.
    # Reading the non-existent `COIN_PREP_HEADROOM_MULT` key used to leave
    # prep_mult=1.0 and cause ~40 relabels per cycle.
    try:
        _raw = getattr(cfg, "COIN_PREP_HEADROOM_PCT", None)
        # Explicit None / missing → default 10%. A legitimate PCT=0 (zero
        # headroom) must pass through; the `or` short-circuit would treat
        # it as missing and substitute the default — wrong.
        headroom_pct = Decimal("10") if _raw is None else Decimal(str(_raw))
        if headroom_pct < 0:
            headroom_pct = Decimal("0")
        prep_mult = Decimal("1") + (headroom_pct / Decimal("100"))
    except Exception:
        prep_mult = Decimal("1.10")

    if is_cat:
        # Need a price to convert XCH-denominated tier sizes to CAT mojos.
        # Priority:
        #   1. _CLI_LIVE_PRICE env var (set by api_server when launching the
        #      coin_prep_worker subprocess via --live-price). This is the
        #      authoritative source inside the prep subprocess, where
        #      api_server.bot is None and the price-engine path below
        #      always falls through to the 0.0001 placeholder. Without
        #      this branch, the post-prep drift verification produces a
        #      false-positive `tier_size_post_prep_drift` ERROR every run
        #      (CAT side ratio = 0.0001 / live_price ≈ 0.5×).
        #   2. bot.price_engine cached last_price (live weighted Tibet+Dexie mid).
        #   3. bot._bot_state["mid_price"] (last published by cycle).
        #   4. Fresh price_engine.get_price() call (computes weighted mid).
        #   5. 0.0001 placeholder as an absolute last resort.
        # The old implementation looked for `api_server.bot_state` as a
        # MODULE-level attribute, which never existed (the real attribute
        # lives on the BotLoop INSTANCE as `bot._bot_state`). Every lookup
        # fell through to 0.0001, producing wrong CAT tier sizes and causing
        # F70 to reject legitimate extreme-sized CAT coins as misfits.
        price = None
        try:
            import os as _os
            _cli_price = (_os.getenv("_CLI_LIVE_PRICE") or "").strip()
            if _cli_price:
                try:
                    p = Decimal(_cli_price)
                    if p > 0:
                        price = p
                except Exception:
                    pass
        except Exception:
            pass
        if price is None or price <= 0:
            try:
                p = Decimal(str(getattr(cfg, "LAST_QUOTED_MID", 0) or 0))
                if p > 0:
                    price = p
            except Exception:
                pass
        if price is None or price <= 0:
            try:
                import api_server as _api
                _bot = getattr(_api, "bot", None)
                if _bot is not None:
                    pe = getattr(_bot, "price_engine", None)
                    if pe is not None:
                        try:
                            last = pe.get_last_price()
                            if last and last > 0:
                                price = Decimal(str(last))
                        except Exception:
                            pass
                    if price is None or price <= 0:
                        bs = getattr(_bot, "_bot_state", None) or {}
                        mid = bs.get("mid_price")
                        if mid:
                            try:
                                price = Decimal(str(mid))
                                if price <= 0:
                                    price = None
                            except Exception:
                                price = None
                    if (price is None or price <= 0) and pe is not None:
                        try:
                            fresh = pe.get_price()
                            if isinstance(fresh, dict):
                                p = fresh.get("mid_price") or fresh.get("mid") or fresh.get("price")
                            else:
                                p = fresh
                            if p:
                                price = Decimal(str(p))
                                if price <= 0:
                                    price = None
                        except Exception:
                            price = None
            except Exception:
                price = None
        if price is None or price <= 0:
            # Last-resort placeholder. Only happens before the bot is wired
            # up or when the price engine is completely unavailable. Note:
            # this produces wrong sizes that cause F70 misfit false
            # positives — avoid by keeping the price engine reachable.
            price = Decimal("0.0001")
        cat_scale = Decimal(10) ** Decimal(cfg.CAT_DECIMALS)
        out = {}
        for tier, xch_size in result_xch.items():
            cat_amount = (xch_size / price * prep_mult).quantize(Decimal("1"))
            out[tier] = int(cat_amount * cat_scale)
        return out

    xch_scale = Decimal("1000000000000")
    return {
        tier: int((xch_size * prep_mult) * xch_scale)
        for tier, xch_size in result_xch.items()
    }


# ──────────────────────────────────────────────────────────────────────────
# Slot position tier  →  coin SIZE tier translation (handles BUY_LADDER_REVERSED)
# ──────────────────────────────────────────────────────────────────────────
# A "slot position tier" is *where* an offer sits in the ladder relative to
# the mid price (inner = closest, extreme = furthest). The "coin size tier"
# is *which prepared coin size* that slot will spend. For the SELL ladder
# they are always the same. For the BUY ladder with BUY_LADDER_REVERSED=True
# they are flipped — buy_inner_position uses extreme-sized coins, etc.
#
# This module historically read BUY_*_TIER_COUNT directly as if those numbers
# referred to coin sizes. They don't — they refer to slot positions. Without
# applying the reversal flip, coin prep allocates the wrong number of coins
# for each tier and the live ladder runs out of coins it actually needs.
#
# All prep / readiness / topup planning that produces coin counts MUST go
# through `flip_position_tiers_to_coin_size_tiers()` so that a single source
# of truth (BUY_*_TIER_COUNT + BUY_LADDER_REVERSED) drives both prep and the
# live ladder.
# ──────────────────────────────────────────────────────────────────────────

# Slot-position → coin-size mapping when BUY_LADDER_REVERSED=True.
# Keys: "what the user calls the slot position". Values: "which coin size that
# slot actually spends". Mirrors the logic in risk_manager.get_tier_size() so
# the two stay in sync.
_BUY_REVERSED_POSITION_TO_COIN_SIZE = {
    "inner":   "extreme",
    "mid":     "outer",
    "outer":   "mid",
    "extreme": "inner",
}
# Inverse: coin-size bucket → position whose offer size lives in that bucket.
# The map is its own inverse (inner↔extreme, mid↔outer are involutions), but
# naming the constant makes the intent clear at each call site.
_BUY_REVERSED_POSITION_TO_COIN_SIZE_INVERSE = {
    v: k for k, v in _BUY_REVERSED_POSITION_TO_COIN_SIZE.items()
}


def flip_position_tiers_to_coin_size_tiers(
    position_counts: Dict[str, int],
    side: Optional[str],
) -> Dict[str, int]:
    """Translate slot-position counts into coin-SIZE counts.

    For sell side (or any side when BUY_LADDER_REVERSED=False) this is the
    identity. For buy side with reversal on, the inner↔extreme and mid↔outer
    counts swap so the result reflects how many coins of each *size tier*
    the buy ladder actually needs.

    Always returns a dict with all four base tier keys present (inner, mid,
    outer, extreme), defaulting to 0. Extra keys passed in (e.g. "sniper",
    "fees") are preserved untouched.
    """
    base_tiers = ("inner", "mid", "outer", "extreme")
    out: Dict[str, int] = {tn: int(position_counts.get(tn, 0) or 0) for tn in base_tiers}
    # Preserve any non-positional tiers (sniper, fees, etc.)
    for k, v in (position_counts or {}).items():
        if k not in base_tiers:
            out[k] = int(v or 0)

    side_norm = (side or "").lower()
    if side_norm not in ("buy", "xch"):
        return out

    if not getattr(cfg, "BUY_LADDER_REVERSED", False):
        return out

    flipped = {tn: 0 for tn in base_tiers}
    for position, coin_size in _BUY_REVERSED_POSITION_TO_COIN_SIZE.items():
        flipped[coin_size] += int(out.get(position, 0) or 0)

    # Re-attach non-positional tiers
    for k, v in out.items():
        if k not in base_tiers:
            flipped[k] = v
    return flipped


def coin_size_tier_for_slot_position(
    position_tier: str,
    side: Optional[str],
) -> str:
    """Return the coin SIZE tier that a slot at the given position will spend.

    Used by the offer creator's coin pre-selector so it requests the right
    prepared coin tier instead of one named after the slot position.
    """
    pt = (position_tier or "").lower()
    side_norm = (side or "").lower()
    if side_norm in ("buy", "xch") and getattr(cfg, "BUY_LADDER_REVERSED", False):
        return _BUY_REVERSED_POSITION_TO_COIN_SIZE.get(pt, pt)
    return pt


def get_tier_distribution(
    max_offers_per_side: int,
    tier_counts: Optional[Dict[str, int]] = None,
    side: Optional[str] = None,
) -> Dict[str, int]:
    """Calculate how many offers fall in each tier for one side.

    If explicit tier counts are configured, they define the ladder template
    from the inside out. Any remaining slots fall into the extreme tier, and
    smaller ladders simply truncate the outer tiers first.

    `side` selects which per-side knob to read when `tier_counts` isn't given:
      - 'xch' / 'buy'  -> BUY_*_TIER_COUNT
      - 'cat' / 'sell' -> SELL_*_TIER_COUNT
      - None (default) -> per-tier MAX of buy and sell (used by planners that
        prep a single shared inventory).

    Otherwise this falls back to the legacy ratio-based split:
      ratio < 0.1  → inner
      ratio < 0.4  → mid
      ratio < 0.7  → outer
      ratio >= 0.7 → extreme

    Returns {"inner": count, "mid": count, "outer": count, "extreme": count}
    """
    dist = {"inner": 0, "mid": 0, "outer": 0, "extreme": 0}
    if max_offers_per_side <= 0:
        return dist

    if tier_counts is not None:
        configured = tier_counts
    else:
        side_norm = (side or "").lower()
        if side_norm in ("xch", "buy"):
            prefix = "BUY_"
        elif side_norm in ("cat", "sell"):
            prefix = "SELL_"
        else:
            prefix = None

        if prefix is None:
            # Max of both sides — used by prep planners that produce one shared
            # tier_counts dict. Runtime layers (needs_topup, coin_readiness)
            # should always pass an explicit side instead.
            configured = {
                tier: max(
                    int(getattr(cfg, f"BUY_{tier.upper()}_TIER_COUNT", 0) or 0),
                    int(getattr(cfg, f"SELL_{tier.upper()}_TIER_COUNT", 0) or 0),
                )
                for tier in ("inner", "mid", "outer", "extreme")
            }
        else:
            configured = {
                tier: int(getattr(cfg, f"{prefix}{tier.upper()}_TIER_COUNT", 0) or 0)
                for tier in ("inner", "mid", "outer", "extreme")
            }
    configured = {
        tier: max(0, int(configured.get(tier, 0) or 0))
        for tier in ("inner", "mid", "outer", "extreme")
    }

    if any(configured.values()):
        remaining = max_offers_per_side
        for tier in ("inner", "mid", "outer", "extreme"):
            take = min(configured[tier], remaining)
            dist[tier] = take
            remaining -= take
        if remaining > 0:
            dist["extreme"] += remaining
        # Translate from slot-position counts to coin-SIZE counts so that
        # callers (coin prep, topup, readiness) get the actual number of
        # coins of each size needed when BUY_LADDER_REVERSED is active.
        return flip_position_tiers_to_coin_size_tiers(dist, side=side)

    for slot in range(max_offers_per_side):
        ratio = slot / max_offers_per_side
        if ratio < 0.1:
            dist["inner"] += 1
        elif ratio < 0.4:
            dist["mid"] += 1
        elif ratio < 0.7:
            dist["outer"] += 1
        else:
            dist["extreme"] += 1

    return flip_position_tiers_to_coin_size_tiers(dist, side=side)


def get_tier_spare_distribution(
    spare_counts: Optional[Dict[str, int]] = None,
    side: Optional[str] = None,
) -> Dict[str, int]:
    """Return explicit spare counts per tier, if configured.

    `side` selects which per-side knob to read:
      - 'xch' / 'buy'  -> BUY_*_TIER_SPARE_COUNT  (XCH coins fund buy offers)
      - 'cat' / 'sell' -> SELL_*_TIER_SPARE_COUNT (CAT coins fund sell offers)
      - None (default) -> per-tier MAX of buy and sell (used by planners that
        prep a single shared inventory).

    A value of 0 means "no explicit override" for that tier. When every tier is
    zero, callers should fall back to the recommended weighted spare logic.
    """
    if spare_counts is not None:
        configured = spare_counts
    else:
        side_norm = (side or "").lower()
        if side_norm in ("xch", "buy"):
            prefix = "BUY_"
        elif side_norm in ("cat", "sell"):
            prefix = "SELL_"
        else:
            prefix = None

        if prefix is None:
            # Max of both sides — used by prep planners that produce one shared
            # tier_counts dict. The runtime layer (needs_topup, coin_readiness)
            # should always pass an explicit side instead.
            configured = {
                tier: max(
                    int(getattr(cfg, f"BUY_{tier.upper()}_TIER_SPARE_COUNT", 0) or 0),
                    int(getattr(cfg, f"SELL_{tier.upper()}_TIER_SPARE_COUNT", 0) or 0),
                )
                for tier in ("inner", "mid", "outer", "extreme")
            }
        else:
            configured = {
                tier: int(getattr(cfg, f"{prefix}{tier.upper()}_TIER_SPARE_COUNT", 0) or 0)
                for tier in ("inner", "mid", "outer", "extreme")
            }
    sanitized = {
        tier: max(0, int(configured.get(tier, 0) or 0))
        for tier in ("inner", "mid", "outer", "extreme")
    }
    # Same flip as get_tier_distribution: spare counts are configured in
    # slot-position space but consumed in coin-size space.
    return flip_position_tiers_to_coin_size_tiers(sanitized, side=side)


def _clamp_coin_prep_multiplier(multiplier_raw) -> float:
    """Clamp the user coin-prep multiplier to the supported range.

    Floor is 1.0 — below this the spare allocation rounds to zero for some
    tiers, leaving no buffer at all.  Smart Defaults also enforces 1.0 as its
    minimum.  Ceiling is 3.0 (prep time beyond this exceeds practical benefit).
    """
    try:
        multiplier = float(multiplier_raw)
    except Exception:
        multiplier = 1.0
    return max(1.0, min(3.0, multiplier))


def _get_tier_size_weights(
    tier_sizes_xch: Optional[Dict[str, Decimal]] = None,
) -> Dict[str, float]:
    """Return relative spare-weighting per tier based on fill frequency.

    Sell side (normal): larger offers sit closer to mid price and fill most
    often, so inner > mid > outer > extreme weighting is correct.

    Buy side with BUY_LADDER_REVERSED: the tier *positions* are flipped —
    inner position uses extreme-sized offers (smallest, closest to mid, fills
    most), extreme position uses inner-sized offers (largest, furthest, rarely
    fills). So buy-side fill frequency is the inverse: extreme > outer > mid >
    inner weighting.

    Since both sides run simultaneously, the combined spare weight is the
    average of sell-side and buy-side weights.  Without reversal, both sides
    are identical so the average equals the normal sell-side weighting.
    """
    sizes = tier_sizes_xch or {
        "inner": getattr(cfg, "INNER_SIZE_XCH", Decimal("0")),
        "mid": getattr(cfg, "MID_SIZE_XCH", Decimal("0")),
        "outer": getattr(cfg, "OUTER_SIZE_XCH", Decimal("0")),
        "extreme": getattr(cfg, "EXTREME_SIZE_XCH", Decimal("0")),
    }
    normalized = {}
    for tier_name in ("inner", "mid", "outer", "extreme"):
        try:
            normalized[tier_name] = max(Decimal("0"), Decimal(str(sizes.get(tier_name, 0) or 0)))
        except Exception:
            normalized[tier_name] = Decimal("0")

    positive_sizes = [value for value in normalized.values() if value > 0]
    if normalized.get("mid", Decimal("0")) > 0:
        reference = normalized["mid"]
    elif positive_sizes:
        reference = max(positive_sizes)
    else:
        reference = Decimal("1")

    # Sell-side weights: proportional to offer size (inner = largest = highest weight)
    sell_weights: Dict[str, float] = {}
    for tier_name, size in normalized.items():
        if size <= 0 or reference <= 0:
            sell_weights[tier_name] = 0.0
        else:
            sell_weights[tier_name] = float(size / reference)

    buy_ladder_reversed = getattr(cfg, "BUY_LADDER_REVERSED", False)
    if not buy_ladder_reversed:
        return sell_weights

    # Buy-side weights with reversal: inner position fills most but uses the
    # extreme-sized coin — so extreme coins churn fastest on buys.
    # Flip inner↔extreme and mid↔outer to reflect reversed fill order.
    reversed_sizes = {
        "inner":   normalized.get("extreme", Decimal("0")),
        "mid":     normalized.get("outer",   Decimal("0")),
        "outer":   normalized.get("mid",     Decimal("0")),
        "extreme": normalized.get("inner",   Decimal("0")),
    }
    buy_weights: Dict[str, float] = {}
    for tier_name, size in reversed_sizes.items():
        if size <= 0 or reference <= 0:
            buy_weights[tier_name] = 0.0
        else:
            buy_weights[tier_name] = float(size / reference)

    # Average both sides — coins serve both sell and buy simultaneously
    weights: Dict[str, float] = {}
    for tier_name in ("inner", "mid", "outer", "extreme"):
        weights[tier_name] = (sell_weights.get(tier_name, 0.0) + buy_weights.get(tier_name, 0.0)) / 2.0
    return weights


def get_weighted_tier_prep_counts(
    max_offers_per_side: int,
    multiplier_raw,
    tier_counts: Optional[Dict[str, int]] = None,
    tier_sizes_xch: Optional[Dict[str, Decimal]] = None,
    spare_counts: Optional[Dict[str, int]] = None,
    side: Optional[str] = None,
) -> Dict[str, int]:
    """Return prepared coin counts per tier for one asset wallet.

    Base active counts always follow the live ladder distribution. Any spare
    budget from the coin-prep multiplier is then redistributed toward larger
    live tiers, so inner/mid tiers keep more spare coins than outer/extreme.
    """
    dist = get_tier_distribution(max_offers_per_side, tier_counts=tier_counts, side=side)
    counts = {tier_name: int(count or 0) for tier_name, count in dist.items()}
    total_slots = sum(counts.values())
    if total_slots <= 0:
        return counts

    explicit_spares = get_tier_spare_distribution(spare_counts=spare_counts, side=side)
    if any(explicit_spares.values()):
        return {
            tier_name: counts.get(tier_name, 0) + explicit_spares.get(tier_name, 0)
            for tier_name in ("inner", "mid", "outer", "extreme")
        }

    multiplier = _clamp_coin_prep_multiplier(multiplier_raw)
    # 1.0 means one weighted spare layer across the live ladder.
    spare_budget = max(0, int(round(total_slots * multiplier)))
    if spare_budget <= 0:
        return counts

    tier_order = ("inner", "mid", "outer", "extreme")
    size_weights = _get_tier_size_weights(tier_sizes_xch=tier_sizes_xch)

    weighted_slots = {}
    total_weight = 0.0
    for tier_name in tier_order:
        slot_count = counts.get(tier_name, 0)
        if slot_count <= 0:
            continue
        tier_weight = max(0.0, float(size_weights.get(tier_name, 0.0) or 0.0))
        weight_points = float(slot_count) * (tier_weight if tier_weight > 0 else 1.0)
        weighted_slots[tier_name] = weight_points
        total_weight += weight_points

    if total_weight <= 0:
        return counts

    extras = {tier_name: 0 for tier_name in tier_order}
    remaining = spare_budget
    remainders = []
    for tier_name in tier_order:
        weight_points = weighted_slots.get(tier_name, 0.0)
        if weight_points <= 0:
            continue
        raw_extra = (spare_budget * weight_points) / total_weight
        whole_extra = int(raw_extra)
        extras[tier_name] = whole_extra
        remaining -= whole_extra
        remainders.append(
            {
                "tier": tier_name,
                "fraction": raw_extra - whole_extra,
                "weight": float(size_weights.get(tier_name, 0.0) or 0.0),
                "slots": counts.get(tier_name, 0),
            }
        )

    remainders.sort(
        key=lambda item: (
            -item["fraction"],
            -item["weight"],
            -item["slots"],
            tier_order.index(item["tier"]),
        )
    )
    for item in remainders[:remaining]:
        extras[item["tier"]] += 1

    return {
        tier_name: counts.get(tier_name, 0) + extras.get(tier_name, 0)
        for tier_name in tier_order
    }


def get_recommended_tier_spare_counts(
    max_offers_per_side: int,
    multiplier_raw,
    tier_counts: Optional[Dict[str, int]] = None,
    tier_sizes_xch: Optional[Dict[str, Decimal]] = None,
) -> Dict[str, int]:
    """Return only the spare portion of the prepared tier counts."""
    dist = get_tier_distribution(max_offers_per_side, tier_counts=tier_counts)
    prepared = get_weighted_tier_prep_counts(
        max_offers_per_side,
        multiplier_raw,
        tier_counts=tier_counts,
        tier_sizes_xch=tier_sizes_xch,
        spare_counts={},
    )
    return {
        tier_name: max(0, int(prepared.get(tier_name, 0) or 0) - int(dist.get(tier_name, 0) or 0))
        for tier_name in ("inner", "mid", "outer", "extreme")
    }


def _format_amount_xch(mojos: int) -> str:
    """Format mojos as human-readable XCH amount."""
    return f"{Decimal(mojos) / Decimal('1000000000000'):.4f}"


def _format_amount_cat(mojos: int, decimals: int) -> str:
    """Format CAT mojos as human-readable token amount."""
    scale = Decimal(10 ** decimals)
    return f"{Decimal(mojos) / scale:.2f}"


class CoinManager:
    """Manages coin health and preparation for offer creation.

    Maintains a running coin inventory, classifying every spendable coin
    by its role (reserve/trading/small). Uses this to make smart decisions
    about when and how to split or consolidate.
    """

    def __init__(self):
        # Current coin counts (free/spendable only)
        self._xch_coins: int = 0
        self._cat_coins: int = 0

        # Locked coin tracking — coins locked in active offers
        self._xch_locked_coins: int = 0
        self._cat_locked_coins: int = 0
        self._xch_locked_amount: int = 0   # mojos
        self._cat_locked_amount: int = 0   # mojos
        self._xch_total_coins: int = 0     # free + locked
        self._cat_total_coins: int = 0     # free + locked

        # Coin inventory — last snapshot of classified coins
        # When TIER_ENABLED, this also has inner/mid/outer/extreme keys
        self._xch_inventory: Dict[str, list] = {"reserve": [], "trading": [], "small": []}
        self._cat_inventory: Dict[str, list] = {"reserve": [], "trading": [], "small": []}

        # Coin prep state
        self._prep_running: bool = False
        self._topup_running: bool = False
        self._topup_thread: Optional[threading.Thread] = None

        # F71: Coin reservation registry (see coin_reservations.py).
        # Short-lived reservations prevent trade-create and topup-reshape
        # from racing on the same coin. Every long-running operation
        # (offer create, absorb, consolidate, split) reserves coins before
        # its RPC call and releases them in a finally block.
        # Reservations are TTL'd so a crash can't leak them forever.
        from coin_reservations import ReservationRegistry
        self.reservations = ReservationRegistry()

        # Backoff state
        self._no_coins_backoff: bool = False
        self._no_coins_backoff_count: int = 0   # consecutive "nothing to work with" hits
        self._last_topup_time: float = 0
        self._last_drip_time: float = 0          # last proactive drip check timestamp
        self._topup_is_drip: bool = False        # current run was drip-triggered (not emergency)
        self._topup_budget_backoff_until: float = 0
        self._topup_budget_backoff_count: int = 0
        self._topup_budget_backoff_probe: Optional[Dict[str, int | bool | str]] = None
        self._recent_absorb_submissions: Dict[str, float] = {}
        self._recent_topup_split_submissions: Dict[str, float] = {}
        self._recent_consolidate_submissions: Dict[str, float] = {}
        self._last_consolidate_not_submitted: bool = False
        self._last_drip_source_unavailable_log: Dict[str, float] = {}

        # Warning throttle
        self._last_low_coin_warning: float = 0

        # Coin prep worker process
        self._prep_process: Optional[subprocess.Popen] = None

        # Lock for thread safety
        self._lock = threading.Lock()

        # Worker cancelled IDs
        self._worker_cancelled_ids: set = set()
        self._topup_abort_logged: bool = False
        self._topup_stop_requested: bool = False

        # Fingerprint for CLI commands — auto-detect if not in config
        self._fingerprint = self._resolve_fingerprint()

        # Loop counter for runtime health checks
        self._health_check_counter: int = 0

        # Coin change tracking — stores coin IDs from last snapshot
        self._prev_xch_coin_ids: Optional[set] = None
        self._prev_cat_coin_ids: Optional[set] = None

        # ---- Designation-based tracking (V3 adaptive system) ----
        # Replaces pure amount-based classification with explicit role tracking.
        # Reserve coins are reserve because we SAY they are, not because
        # they happen to be big enough.
        self._reserve_ids_xch: set = set()    # Coin IDs designated as XCH reserve
        self._reserve_ids_cat: set = set()    # Coin IDs designated as CAT reserve
        self._tier_spares: Dict[str, Dict[str, int]] = {
            "xch": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0, "sniper": 0, "fees": 0},
            "cat": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0, "sniper": 0, "fees": 0},
        }
        # Pre-populate from DB so needs_topup/needs_prep have valid data before
        # the first update_coin_counts() cycle completes.
        try:
            from database import get_tier_spare_counts
            for _wt in ("xch", "cat"):
                _spares = get_tier_spare_counts(_wt)
                if _spares:
                    self._tier_spares[_wt] = _spares
        except Exception:
            pass  # DB may not be ready yet — zeros are safe defaults
        self._trading_pace: str = "normal"    # Current pace: slow/normal/busy
        self._last_pace_calc: float = 0       # Timestamp of last pace calculation
        self._reconcile_counter: int = 0      # Counts loops between reconciliations

        # ---- Fee Coin Pool (concurrent operation support) ----
        # Each operation (create / cancel) reserves a dedicated fee coin
        # from this pool so Sage doesn't auto-pick the same one for two
        # concurrent transactions.  Refreshed at start of each cycle.
        self.fee_pool = FeeCoinPool()

    def refresh_fee_pool_from_wallet(self):
        """Quick-refresh fee pool from current wallet state.

        Call this after cancel operations complete but before creates start.
        Ensures the pool only contains coins Sage hasn't already claimed
        for pending cancel transactions.

        Much lighter than a full update_coin_counts() — one RPC call,
        filtered to fee-sized coins only.
        """
        try:
            fee_size = get_fee_coin_size_mojos()
            if fee_size <= 0:
                return
            result = get_exact_spendable_coins_rpc(WALLET_ID_XCH)
            if not result or not result.get("success"):
                return  # can't refresh — keep existing pool
            records = _extract_coin_records(result)
            low = int(fee_size * 0.8)
            high = int(fee_size * 1.2)
            fee_records = [
                r for r in records
                if low <= int((r.get("coin") or {}).get("amount", 0)) <= high
            ]
            self.fee_pool.refresh(fee_records)
        except Exception:
            pass  # non-fatal — keep existing pool

    def _sniper_pool_enabled(self) -> bool:
        """Whether the dedicated sniper pool should be prepared and maintained.

        The sniper is an arb-discovery tool that fires a simultaneous buy +
        sell probe, so it only works in two-sided mode. Under any single-
        sided ``LIQUIDITY_MODE`` the sniper pool is forced off even if the
        ``SNIPER_ENABLED`` flag is still set — stops stale config from
        preparing coins the bot will never use.
        """
        # Single-sided mode → arb snipe cannot operate; skip pool entirely.
        _mode = (getattr(cfg, "LIQUIDITY_MODE", "two_sided") or "two_sided").lower()
        if _mode in ("buy_only", "sell_only"):
            return False
        try:
            sniper_size = Decimal(str(getattr(cfg, "SNIPER_SIZE_XCH", "0") or "0"))
        except Exception:
            sniper_size = Decimal("0")
        return (
            bool(getattr(cfg, "TIER_ENABLED", False))
            and bool(getattr(cfg, "SNIPER_ENABLED", False))
            and int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0) > 0
            and sniper_size > 0
        )

    def _optional_topup_source_available(self, wallet_type: str, target_size_mojos: int = 0) -> bool:
        """Return True when a drip-only optional pool has a cheap source to split.

        Optional pools (sniper/fee buffers) are useful, but they should not
        start a topup worker over and over when the matching wallet has no
        reserve or useful small coins. Active offer tiers can still trigger
        normal emergency topup paths.
        """
        inventory = self._cat_inventory if wallet_type == "cat" else self._xch_inventory
        reserve = inventory.get("reserve") or []
        if reserve:
            if target_size_mojos <= 0:
                return True
            if any(_coin_amount(record) >= target_size_mojos for record in reserve):
                return True
        small = inventory.get("small") or []
        if len(small) < 2:
            return False
        if target_size_mojos <= 0:
            return True
        small_total = sum(_coin_amount(record) for record in small)
        return small_total >= target_size_mojos * 2

    def _log_drip_source_unavailable(self, key: str, message: str) -> None:
        """Rate-limit calm no-source notices for optional drip pools."""
        self._log_source_unavailable("drip_source_unavailable", key, message)

    def _log_topup_source_unavailable(self, key: str, message: str) -> None:
        """Rate-limit calm no-source notices for emergency topups."""
        self._log_source_unavailable("topup_source_unavailable", key, message)

    def _log_source_unavailable(self, event_type: str, key: str, message: str) -> None:
        """Rate-limit repeated no-source notices for the same pool."""
        now = time.time()
        rate_key = f"{event_type}:{key}"
        last = self._last_drip_source_unavailable_log.get(rate_key, 0)
        if now - last < _DRIP_SOURCE_NOTICE_INTERVAL:
            return
        self._last_drip_source_unavailable_log[rate_key] = now
        log_event("info", event_type, message)

    def _configured_tier_names(self, include_sniper: bool = True) -> List[str]:
        tiers = ["inner", "mid", "outer", "extreme"]
        if include_sniper and self._sniper_pool_enabled():
            tiers.append("sniper")
        return tiers

    def _configured_tier_sizes_xch(self, include_sniper: bool = True,
                                    side: str = "sell") -> Dict[str, Decimal]:
        """Return {tier: size_xch} for prepping coins on a given side.

        F62 (2026-04-09): side-aware. Buy offers and sell offers can now
        have different tier sizes so each side can consume its own balance
        independently. Defaults to "sell" to preserve the historical
        behaviour of callers that don't pass a side.
        """
        from config import get_buy_tier_size_xch, get_sell_tier_size_xch
        if (side or "sell").strip().lower() == "buy":
            _get = get_buy_tier_size_xch
        else:
            _get = get_sell_tier_size_xch
        sizes = {
            "inner":   Decimal(str(_get("inner") or 0)),
            "mid":     Decimal(str(_get("mid") or 0)),
            "outer":   Decimal(str(_get("outer") or 0)),
            "extreme": Decimal(str(_get("extreme") or 0)),
        }
        if include_sniper and self._sniper_pool_enabled():
            sizes["sniper"] = Decimal(str(getattr(cfg, "SNIPER_SIZE_XCH", Decimal("0"))))
        return sizes

    def _configured_xch_prep_sizes(self) -> Dict[str, Decimal]:
        sizes = dict(self._configured_tier_sizes_xch())
        if self._fee_pool_enabled():
            sizes[get_fee_tier_name()] = Decimal(str(get_fee_coin_size_xch()))
        return sizes

    def _configured_prep_counts(self, wallet_type: str) -> Dict[str, int]:
        """Expected prepared-coin counts per tier for the current config."""
        if not bool(getattr(cfg, "TIER_ENABLED", False)):
            return {}

        max_buy = int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25) or 25)
        max_sell = int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25) or 25)
        wallet_norm = (wallet_type or "").strip().lower()
        if wallet_norm in ("xch", "buy"):
            max_offers = max_buy
        elif wallet_norm in ("cat", "sell"):
            max_offers = max_sell
        else:
            max_offers = max(max_buy, max_sell)

        multiplier = getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0"))
        tier_counts = get_weighted_tier_prep_counts(
            max_offers, multiplier, side=wallet_type)

        if self._sniper_pool_enabled():
            tier_counts["sniper"] = int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0)

        if wallet_type == "xch" and self._fee_pool_enabled():
            tier_counts[get_fee_tier_name()] = int(get_fee_pool_count() or 0)

        return {k: v for k, v in tier_counts.items() if int(v or 0) > 0}

    def _fee_pool_enabled(self) -> bool:
        return bool(getattr(cfg, "ENABLE_COIN_PREP", False)) and fee_pool_enabled()

    def _tx_fee_mojos(self) -> int:
        return get_effective_transaction_fee_mojos()

    def _wallet_rpc_failed(self, rpc_result) -> bool:
        return (
            rpc_result is None
            or not isinstance(rpc_result, dict)
            or bool(rpc_result.get("error"))
            or rpc_result.get("success") is False
        )

    def _looks_like_wallet_rpc_degradation(self, detail) -> bool:
        text = str(detail or "").strip().lower()
        if not text:
            return True
        return any(token in text for token in (
            "timed out",
            "timeout",
            "connection",
            "rpc",
            "http",
            "ssl",
            "empty",
            "none",
        ))

    def _spacescan_coin_state(self, coin_id: str) -> Optional[Dict]:
        if not coin_id:
            return None
        try:
            from spacescan import is_coin_spent
            return is_coin_spent(coin_id)
        except Exception:
            return None

    def _spacescan_self_send_confirmed(self, coin_id: str, expected_address: str,
                                       tag: str) -> bool:
        state = self._spacescan_coin_state(coin_id)
        if not state or not state.get("spent"):
            return False
        receiver = str(state.get("receiver_address") or "").strip()
        try:
            from spacescan import is_known_wallet_address
        except Exception:
            is_known_wallet_address = None
        if expected_address and receiver == expected_address:
            log_event(
                "info",
                f"{tag}_spacescan_self_send",
                f"Spacescan confirms source coin {coin_id[:12]}... spent to "
                f"{receiver[:16]}... even though Sage response was weak",
            )
            return True
        if receiver and is_known_wallet_address and is_known_wallet_address(receiver, {expected_address}):
            log_event(
                "info",
                f"{tag}_spacescan_self_send_known_wallet",
                f"Spacescan confirms source coin {coin_id[:12]}... spent to "
                f"known own address {receiver[:16]}... even though Sage response was weak",
            )
            return True
        return False

    def _spacescan_coin_spent_confirmed(self, coin_id: str, tag: str,
                                        label: str) -> bool:
        state = self._spacescan_coin_state(coin_id)
        if not state or not state.get("spent"):
            return False
        receiver = str(state.get("receiver_address") or "").strip()
        suffix = f" to {receiver[:16]}..." if receiver else ""
        log_event(
            "info",
            f"{tag}_{label}_onchain",
            f"Spacescan confirms coin {coin_id[:12]}... was spent on-chain{suffix}",
        )
        return True

    def _abort_topup_for_wallet_degradation(self, reason: str,
                                            event_type: str = "topup_wallet_degraded"):
        if not self._topup_abort_logged:
            log_event("warning", event_type, reason)
            self._topup_abort_logged = True
        raise _TopupWalletDegraded(reason)

    @staticmethod
    def _extract_sage_transaction_ids(result) -> List[str]:
        """Extract normalized Sage transaction ids from a submit response."""
        if result is None:
            return []

        tx_ids = []
        if isinstance(result, dict):
            raw_ids = result.get("transaction_ids")
            if isinstance(raw_ids, list):
                tx_ids.extend(raw_ids)
            single = result.get("transaction_id") or result.get("tx_id")
            if single:
                tx_ids.append(single)
            nested = result.get("transaction") or result.get("tx")
            if isinstance(nested, dict):
                nested_single = nested.get("transaction_id")
                if nested_single:
                    tx_ids.append(nested_single)
                nested_ids = nested.get("transaction_ids")
                if isinstance(nested_ids, list):
                    tx_ids.extend(nested_ids)

        normalized = []
        seen = set()
        for tx_id in tx_ids:
            clean = str(tx_id or "").strip().lower()
            if not clean:
                continue
            if not clean.startswith("0x"):
                clean = "0x" + clean
            if clean not in seen:
                seen.add(clean)
                normalized.append(clean)
        return normalized

    @staticmethod
    def _coin_id_compare_key(coin_id) -> str:
        clean = str(coin_id or "").strip().lower()
        if clean.startswith("0x"):
            clean = clean[2:]
        return clean

    def _sage_pending_transaction_count(self) -> Optional[int]:
        try:
            from wallet_sage import get_pending_transactions
            pending = get_pending_transactions()
            if pending is not None:
                return len(pending or [])
        except Exception:
            pass
        return None

    def _selectable_source_coin_ids(
        self,
        wallet_id: int,
        name: str,
        coin_ids: List[str],
    ) -> List[str]:
        source_keys = {
            self._coin_id_compare_key(cid)
            for cid in (coin_ids or [])
            if self._coin_id_compare_key(cid)
        }
        if not source_keys:
            return []
        selectable = self._get_strict_selectable_coin_id_set(wallet_id, name) or set()
        selectable_keys = {
            self._coin_id_compare_key(cid)
            for cid in selectable
            if self._coin_id_compare_key(cid)
        }
        return sorted(source_keys.intersection(selectable_keys))

    def _combine_no_txid_submission_state(
        self,
        wallet_id: int,
        name: str,
        coin_ids: List[str],
    ) -> Dict[str, object]:
        source_keys = {
            self._coin_id_compare_key(cid)
            for cid in (coin_ids or [])
            if self._coin_id_compare_key(cid)
        }
        source_count = len(source_keys)
        grace_secs = max(
            0,
            int(getattr(cfg, "TOPUP_COMBINE_NO_TXID_GRACE_SECS", 45) or 0),
        )
        poll_interval = max(
            1,
            int(getattr(cfg, "TOPUP_COMBINE_NO_TXID_POLL_SECS", 4) or 4),
        )
        started = time.time()
        pending_count: Optional[int] = None
        selectable_inputs: List[str] = []

        while True:
            pending_count = self._sage_pending_transaction_count()
            selectable_inputs = self._selectable_source_coin_ids(
                wallet_id,
                name,
                coin_ids,
            )
            elapsed = int(time.time() - started)
            if source_count > 0 and len(selectable_inputs) == source_count:
                return {
                    "state": "not_submitted",
                    "pending_count": pending_count,
                    "selectable_count": len(selectable_inputs),
                    "source_count": source_count,
                    "elapsed": elapsed,
                }
            if elapsed >= grace_secs or self._topup_should_stop():
                break
            time.sleep(min(poll_interval, max(0, grace_secs - elapsed)))

        state = "unverified_no_pending" if pending_count == 0 else "unverified"
        return {
            "state": state,
            "pending_count": pending_count,
            "selectable_count": len(selectable_inputs),
            "source_count": source_count,
            "elapsed": int(time.time() - started),
        }

    def _get_transaction_confirmation_state(self, tx_ids: List[str]) -> Dict[str, object]:
        """Summarize Sage transaction confirmation state for runtime top-up."""
        tx_ids = [tid for tid in (tx_ids or []) if tid]
        if not tx_ids or get_wallet_type() != "sage":
            return {"known": False, "confirmed": False, "confirmed_count": 0, "total": 0, "height": 0}

        try:
            from wallet_sage import get_transaction
        except Exception:
            return {"known": False, "confirmed": False, "confirmed_count": 0, "total": len(tx_ids), "height": 0}

        confirmed_count = 0
        best_height = 0
        any_known = False
        for tx_id in tx_ids:
            try:
                tx_info = get_transaction(tx_id)
            except Exception:
                tx_info = None
            if not tx_info or not isinstance(tx_info, dict):
                continue
            any_known = True
            confirmed = bool(tx_info.get("confirmed", False))
            height = int(tx_info.get("confirmed_at_height", 0) or 0)
            if confirmed or height > 0:
                confirmed_count += 1
                best_height = max(best_height, height)

        total = len(tx_ids)
        return {
            "known": any_known,
            "confirmed": (confirmed_count > 0 if total == 1 else confirmed_count == total),
            "confirmed_count": confirmed_count,
            "total": total,
            "height": best_height,
        }

    def _get_owned_coin_amount_map(self, wallet_id: int, name: str) -> Dict[str, int]:
        """Return owned wallet coins as {coin_id: amount_mojos}."""
        try:
            owned_result = get_owned_coins(wallet_id) or {}
            owned_map = {}
            if isinstance(owned_result, dict):
                for cid, amount in owned_result.items():
                    clean = str(cid or "").strip().lower()
                    if not clean:
                        continue
                    if not clean.startswith("0x"):
                        clean = "0x" + clean
                    owned_map[clean] = int(amount or 0)
            return owned_map
        except Exception as e:
            log_event("warning", f"{name}_owned_unavailable",
                      f"Owned coin view unavailable: {str(e)[:160]}")
            # Safe lower-bound fallback: if Sage's owned view flakes out but the
            # strict selectable view is still healthy, treat selectable coins as
            # the minimum owned set instead of silently reporting "0 owned".
            try:
                selectable_result = _get_free_coins_rpc(wallet_id)
                if self._wallet_rpc_failed(selectable_result):
                    return {}
                fallback_map = {}
                for record in _extract_coin_records(selectable_result):
                    cid = _coin_id_from_record(record)
                    if cid:
                        fallback_map[cid] = _coin_amount(record)
                if fallback_map:
                    log_event("info", f"{name}_owned_fallback_selectable",
                              f"Using selectable coin view as owned lower-bound "
                              f"({len(fallback_map)} coins)")
                return fallback_map
            except Exception:
                return {}

    def _get_strict_selectable_coin_id_set(self, wallet_id: int, name: str) -> set:
        """Return strict selectable/free coin ids for a wallet."""
        try:
            if get_wallet_type() == "sage":
                from wallet_sage import get_selectable_coins_only
                result = get_selectable_coins_only(wallet_id)
            else:
                result = _get_free_coins_rpc(wallet_id)
        except Exception as e:
            log_event("warning", f"{name}_selectable_unavailable",
                      f"Selectable coin view unavailable: {str(e)[:160]}")
            return set()

        if self._wallet_rpc_failed(result):
            return set()

        selectable_ids = set()
        for record in _extract_coin_records(result):
            cid = _coin_id_from_record(record)
            if cid:
                selectable_ids.add(cid)
        return selectable_ids

    # -------------------------------------------------------------------
    # Fingerprint resolution
    # -------------------------------------------------------------------

    def _resolve_fingerprint(self) -> str:
        """Get wallet fingerprint — config first, then RPC auto-detect.

        coin_prep_worker had a bug where empty WALLET_FINGERPRINT in .env
        caused CLI splits to fail with 'Invalid value for -f'. This method
        mirrors coin_prep_worker's robust approach.
        """
        # Try config first
        fp = str(getattr(cfg, "WALLET_FINGERPRINT", "") or "").strip()
        if fp and fp.isdigit():
            log_event("info", "coin_mgr_fingerprint",
                      f"Using fingerprint from config: {fp}")
            return fp

        # Auto-detect via RPC
        wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()

        if wallet_type == "sage":
            # Sage: use get_current_key() instead of get_logged_in_fingerprint.
            # Retry up to 4 times with short delays — Sage may still be starting
            # up when the bot initializes (e.g. user closed Sage and let the app
            # launch it fresh). Non-fatal: fingerprint is re-resolved later if needed.
            from wallet_sage import get_current_key
            for _attempt in range(4):
                try:
                    key = get_current_key()
                    if key and key.get("fingerprint"):
                        fp_str = str(key["fingerprint"])
                        log_event("info", "coin_mgr_fingerprint",
                                  f"Auto-detected Sage fingerprint: {fp_str}")
                        return fp_str
                except Exception as e:
                    if _attempt < 3:
                        import time as _time
                        _time.sleep(3)
                    else:
                        log_event("warning", "coin_mgr_fingerprint_rpc_fail",
                                  f"Sage fingerprint detection failed after 4 attempts: {e}")
        else:
            # Chia: use get_logged_in_fingerprint RPC
            try:
                from wallet import rpc
                result = rpc("get_logged_in_fingerprint", {})
                rpc_fp = result.get("fingerprint") if result else None
                if rpc_fp:
                    fp_str = str(rpc_fp)
                    log_event("info", "coin_mgr_fingerprint",
                              f"Auto-detected fingerprint via RPC: {fp_str}")
                    return fp_str
            except Exception as e:
                log_event("warning", "coin_mgr_fingerprint_rpc_fail",
                          f"RPC fingerprint detection failed: {e}")

            # CLI fallback (Chia only)
            try:
                import subprocess as sp
                proc = sp.run(
                    ["chia", "keys", "show"],
                    capture_output=True, text=True, timeout=30
                )
                for line in proc.stdout.splitlines():
                    if "fingerprint:" in line.lower():
                        parts = line.split(":")
                        if len(parts) >= 2:
                            maybe_fp = parts[-1].strip()
                            if maybe_fp.isdigit():
                                log_event("info", "coin_mgr_fingerprint",
                                          f"Auto-detected fingerprint via CLI: {maybe_fp}")
                                return maybe_fp
            except Exception as e:
                log_event("warning", "coin_mgr_fingerprint_cli_fail",
                          f"CLI fingerprint detection failed: {e}")

        log_event(
            "info",
            "coin_mgr_no_fingerprint",
            "Wallet fingerprint is not available yet; waiting for wallet selection or RPC readiness",
        )
        return ""

    # -------------------------------------------------------------------
    # V3: Designation-based coin classification (replaces amount-only)
    # -------------------------------------------------------------------

    def _classify_coins_by_designation(self, records: list, wallet_type: str,
                                        tier_sizes_mojos: Dict[str, int]) -> Dict[str, list]:
        """Classify coins using DB designations first, size-inference as fallback.

        This is the HEART of the V3 adaptive system. Instead of classifying
        coins purely by their amount (which breaks when reserve coins get split
        below the threshold), we check each coin's DESIGNATED ROLE in the DB.

        Workflow:
        1. Coins with existing designations → keep them (reserve stays reserve)
        2. NEW coins (designation='unknown') → infer by size, persist to DB
        3. Returns same dict structure as _classify_coins_tiered() for backward compat

        Args:
            records: Raw coin records from wallet RPC
            wallet_type: 'xch' or 'cat'
            tier_sizes_mojos: {"inner": mojos, "mid": mojos, ...}

        Returns dict with keys: reserve, inner, mid, outer, extreme,
        optional sniper, and small
        """
        result = {
            "reserve": [],
            "inner": [],
            "mid": [],
            "outer": [],
            "extreme": [],
            "sniper": [],
            "fees": [],
            "small": [],
        }

        try:
            from database import get_free_coins, get_locked_coins, set_coin_designation

            # Build a lookup of DB designations for this wallet
            db_coins = get_free_coins(wallet_type)
            db_desig_map = {}  # coin_id → (designation, assigned_tier)
            for dc in db_coins:
                cid = dc.get("coin_id", "")
                desig = dc.get("designation", "unknown") or "unknown"
                atier = dc.get("assigned_tier", "none") or "none"
                db_desig_map[cid] = (desig, atier)

            # Rebalance duplicate-size tiers like XCH sniper/fees. If the DB
            # lost the distinction across a restart, stale designations can put
            # every same-sized free coin into the first matching tier.
            #
            # Tolerance MUST be tight (~1%) — the original ±20% window was wide
            # enough to chain asymmetric sell tiers (inner=19.5k, mid=16.25k,
            # outer=13.65k CAT) into one "duplicate group", then reassign coins
            # by coin_id order, which scrambles prep's amount-based assignment.
            # Example symptom: prep made 9 outer CAT coins at 13,650 CAT each,
            # but they landed in inner (26 inner, 0 outer) because inner/mid/
            # outer were 20%-adjacent and got grouped. We only want to dedupe
            # TRULY identical sizes here (sniper/fees when both are 0.001 XCH).
            _DUPLICATE_TIER_TOLERANCE = Decimal("0.01")  # ±1%
            expected_counts = self._configured_prep_counts(wallet_type)
            locked_by_tier = {}
            for locked in get_locked_coins(wallet_type):
                desig = (locked.get("designation") or "unknown").strip()
                atier = (locked.get("assigned_tier") or "none").strip()
                if desig in ("tier_active", "tier_spare") and atier in expected_counts:
                    locked_by_tier[atier] = locked_by_tier.get(atier, 0) + 1

            duplicate_tier_groups = []
            for tier_name, amount in tier_sizes_mojos.items():
                amount_int = int(amount or 0)
                if amount_int <= 0 or tier_name not in expected_counts:
                    continue

                matched_group = None
                for group in duplicate_tier_groups:
                    ref_amount = group["amount"]
                    slack = max(1, int(ref_amount * _DUPLICATE_TIER_TOLERANCE))
                    if abs(amount_int - ref_amount) <= slack:
                        matched_group = group
                        break

                if matched_group is None:
                    duplicate_tier_groups.append({
                        "amount": amount_int,
                        "tiers": [tier_name],
                    })
                else:
                    matched_group["tiers"].append(tier_name)
                    matched_group["amount"] = min(matched_group["amount"], amount_int)

            for group in duplicate_tier_groups:
                tiers_for_amount = group["tiers"]
                if len(tiers_for_amount) < 2:
                    continue

                matching_coin_ids = []
                for rec in records:
                    cid = _coin_id_from_record(rec)
                    if not cid:
                        continue
                    amt = _coin_amount(rec)
                    for tier_name in tiers_for_amount:
                        tier_amount = int(tier_sizes_mojos.get(tier_name, 0) or 0)
                        if tier_amount <= 0:
                            continue
                        # Same tight tolerance as the tier-grouping above —
                        # a loose window here would also sweep in non-matching
                        # coins and scramble amount-based assignments.
                        slack = max(1, int(tier_amount * _DUPLICATE_TIER_TOLERANCE))
                        if abs(amt - tier_amount) <= slack:
                            matching_coin_ids.append(cid)
                            break
                if not matching_coin_ids:
                    continue

                matching_coin_ids = sorted(set(matching_coin_ids))
                free_targets = {
                    tier_name: max(
                        0,
                        int(expected_counts.get(tier_name, 0) or 0)
                        - int(locked_by_tier.get(tier_name, 0) or 0),
                    )
                    for tier_name in tiers_for_amount
                }

                reassigned = {}
                cursor = 0
                for tier_name in tiers_for_amount:
                    take = min(max(0, len(matching_coin_ids) - cursor), free_targets.get(tier_name, 0))
                    if take <= 0:
                        continue
                    for cid in matching_coin_ids[cursor:cursor + take]:
                        reassigned[cid] = tier_name
                    cursor += take

                if cursor < len(matching_coin_ids):
                    leftovers = matching_coin_ids[cursor:]
                    for cid in leftovers:
                        prior_tier = db_desig_map.get(cid, ("unknown", "none"))[1]
                        fallback_tier = prior_tier if prior_tier in tiers_for_amount else tiers_for_amount[-1]
                        reassigned[cid] = fallback_tier

                for cid, tier_name in reassigned.items():
                    set_coin_designation(cid, "tier_spare", tier_name)
                    db_desig_map[cid] = ("tier_spare", tier_name)

            # Track reserve IDs for this wallet
            reserve_ids = set()
            skipped_no_id = 0

            for rec in records:
                cid = _coin_id_from_record(rec)
                if not cid:
                    skipped_no_id += 1
                    continue
                amt = _coin_amount(rec)

                # Check DB designation first
                db_info = db_desig_map.get(cid)
                if db_info and db_info[0] not in ('unknown', None):
                    desig, atier = db_info
                else:
                    # New/unknown coin — infer by size
                    desig, atier = _infer_designation_by_size(amt, tier_sizes_mojos)
                    # Persist the inferred designation so it survives restarts
                    set_coin_designation(cid, desig, atier)

                # Place into the appropriate bucket
                if desig == 'reserve':
                    result["reserve"].append(rec)
                    reserve_ids.add(cid)
                elif desig == 'dust':
                    result["small"].append(rec)
                elif desig in ('tier_spare', 'tier_active'):
                    bucket = atier if atier in result else "small"
                    result[bucket].append(rec)
                else:
                    # 'unknown' or unexpected — infer by size for bucket placement
                    _, inferred_tier = _infer_designation_by_size(amt, tier_sizes_mojos)
                    if inferred_tier in result:
                        result[inferred_tier].append(rec)
                    else:
                        result["small"].append(rec)

            # Warn if coins were skipped (missing coin_id — likely wallet format issue)
            if skipped_no_id > 0:
                log_event("warning", "coins_skipped_no_id",
                          f"{skipped_no_id} {wallet_type} coins skipped — "
                          f"could not determine coin_id (check wallet response format)")

            # Update in-memory reserve tracking
            if wallet_type == 'xch':
                self._reserve_ids_xch = reserve_ids
            else:
                self._reserve_ids_cat = reserve_ids

            # Final startup/restart cleanup for tiers whose configured sizes are
            # close enough that Sage split dust/fees can blur them together
            # (notably XCH sniper vs fees). If DB designations are stale or the
            # exact amounts drift a little, rebalance buckets to expected counts.
            if wallet_type == "xch" and result.get("sniper") and self._fee_pool_enabled():
                fee_target = int(expected_counts.get("fees", 0) or 0)
                sniper_target = int(expected_counts.get("sniper", 0) or 0)
                fee_have = len(result.get("fees", []))
                sniper_have = len(result.get("sniper", []))
                need_fees = max(0, fee_target - fee_have)
                excess_sniper = max(0, sniper_have - sniper_target)
                if need_fees > 0 and excess_sniper > 0:
                    fee_size = int(tier_sizes_mojos.get("fees", 0) or 0)
                    sniper_bucket = result.get("sniper", [])
                    sniper_bucket.sort(key=lambda rec: abs(_coin_amount(rec) - fee_size))
                    move_count = min(need_fees, excess_sniper, len(sniper_bucket))
                    moved = []
                    for _ in range(move_count):
                        rec = sniper_bucket.pop(0)
                        cid = _coin_id_from_record(rec)
                        if cid:
                            set_coin_designation(cid, "tier_spare", "fees")
                            db_desig_map[cid] = ("tier_spare", "fees")
                        moved.append(rec)
                    if moved:
                        result.setdefault("fees", []).extend(moved)

            # Update spare counts from DB
            try:
                from database import get_tier_spare_counts
                self._tier_spares[wallet_type] = get_tier_spare_counts(wallet_type)
            except Exception as e:
                log_event("warning", "tier_spare_counts_failed",
                          f"Could not update tier spare counts for {wallet_type}: {e}")

        except Exception as e:
            # Fallback when DB is unavailable — use SSOT size inference so the
            # old ±20% bounds from _classify_coins_tiered never reintroduce the
            # 2026-04-17 misfit-as-inner bug on DB hiccups.
            log_event("warning", "designation_fallback",
                      f"Designation classification failed ({e}), using SSOT size-inference fallback")
            result = {k: [] for k in ["reserve", "inner", "mid", "outer", "extreme", "small"]}
            for tier_name in tier_sizes_mojos:
                if tier_name not in result:
                    result[tier_name] = []
            for rec in records:
                amt = _coin_amount(rec)
                desig, atier = _infer_designation_by_size(amt, tier_sizes_mojos)
                if desig == "reserve":
                    result["reserve"].append(rec)
                elif desig == "tier_spare" and atier in result:
                    result[atier].append(rec)
                else:
                    result["small"].append(rec)

        # Sort reserve and small by size descending
        result["reserve"].sort(key=_coin_amount, reverse=True)
        result["small"].sort(key=_coin_amount, reverse=True)

        return result

    def _ensure_reserve_exists(self, wallet_type: str, records: list):
        """Make sure at least one coin is designated as reserve.

        The reserve coin is just whatever's left after tier pools are created.
        It doesn't need a minimum size — it's organic. If no reserve exists,
        we promote the largest undesignated/free coin.

        Called after classification. If the previous reserve was spent/gone,
        finds and promotes the next largest free coin.
        """
        try:
            from database import (get_reserve_coins, designate_reserve,
                                   get_coins_by_designation)

            existing = get_reserve_coins(wallet_type)
            if existing:
                # Reserve exists — nothing to do
                return

            # No reserve — find the largest free undesignated coin
            # Prefer 'unknown' coins, then 'dust' (consolidation material)
            unknowns = get_coins_by_designation(wallet_type, 'unknown')
            if unknowns:
                best = unknowns[0]  # Already sorted by amount DESC
                designate_reserve(best['coin_id'], wallet_type, best['amount_mojos'])
                if wallet_type == 'xch':
                    self._reserve_ids_xch.add(best['coin_id'])
                else:
                    self._reserve_ids_cat.add(best['coin_id'])
                return

            # No unknown coins — check if we have any large dust
            dust = get_coins_by_designation(wallet_type, 'dust')
            if dust:
                best = dust[0]
                designate_reserve(best['coin_id'], wallet_type, best['amount_mojos'])
                if wallet_type == 'xch':
                    self._reserve_ids_xch.add(best['coin_id'])
                else:
                    self._reserve_ids_cat.add(best['coin_id'])
                return

            # No coins to promote — reserve is empty (will be created from
            # freed coins when offers fill/cancel, or consolidated by Strategy 3)
            log_event("debug", "no_reserve_available",
                      f"No {wallet_type.upper()} reserve coin — will create from freed coins")

        except Exception as e:
            log_event("warning", "ensure_reserve_error",
                      f"Failed to ensure {wallet_type} reserve: {e}")

    # -------------------------------------------------------------------
    # V3: Trading pace and adaptive thresholds
    # -------------------------------------------------------------------

    def get_trading_pace(self) -> str:
        """Returns 'slow', 'normal', or 'busy' based on recent fill rate.

        Caches the result for 5 minutes to avoid hitting the DB every loop.
        """
        now = time.time()
        if now - self._last_pace_calc < 300:  # Cache for 5 min
            return self._trading_pace

        try:
            from database import get_current_pace
            pace = get_current_pace()
            self._trading_pace = pace
            self._last_pace_calc = now
        except Exception as e:
            log_event("debug", "trading_pace_fetch_failed",
                      f"Trading pace DB fetch failed (keeping previous value '{self._trading_pace}'): {e}")

        return self._trading_pace

    def get_startup_advisory(self) -> Dict:
        """Calculate and return collateral allocation advice at startup.

        Tells the user: "You have X XCH. Here's how it'll be allocated
        across tiers and reserve." Helps set expectations before trading.

        Returns dict with:
          - total_available: total confirmed XCH (free + offer-locked)
          - spendable_available: currently free/selectable XCH
          - per_tier_needs: coins needed per tier (active + spares)
          - reserve_size: what's left after tier allocation
          - assessment: 'EXCESS' / 'ADEQUATE' / 'LOW' / 'CRITICAL'
          - message: human-readable advice string
        """
        try:
            from wallet import get_wallet_balance

            # Sage returns confirmed = total held (including offer-locked),
            # spendable = currently free/selectable. On a resumed live book,
            # the advisory must use confirmed balance for "do we have enough
            # capital overall?" and spendable balance only for current headroom.
            xch_bal = get_wallet_balance(cfg.WALLET_ID_XCH)
            wb = xch_bal.get("wallet_balance") or xch_bal if xch_bal else {}
            spendable_mojos = wb.get("spendable_balance", 0) or 0
            confirmed_mojos = wb.get("confirmed_wallet_balance", spendable_mojos) or spendable_mojos
            if isinstance(spendable_mojos, str):
                spendable_mojos = int(spendable_mojos)
            if isinstance(confirmed_mojos, str):
                confirmed_mojos = int(confirmed_mojos)

            spendable_xch = Decimal(str(spendable_mojos)) / Decimal("1000000000000")
            total_xch = Decimal(str(confirmed_mojos)) / Decimal("1000000000000")
            locked_xch = max(Decimal("0"), total_xch - spendable_xch)
            has_locked_collateral = locked_xch > Decimal("0.000001")

            multiplier = getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0"))
            max_buy_offers = int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25) or 25)
            max_sell_offers = int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25) or 25)
            max_per_side = max(max_buy_offers, max_sell_offers)

            if cfg.TIER_ENABLED:
                tier_dist = get_tier_distribution(max_buy_offers, side="xch")
                prepared_counts = get_weighted_tier_prep_counts(
                    max_buy_offers, multiplier, side="xch")
                per_tier_needs = {}
                total_tier_xch = Decimal("0")
                # F62 (2026-04-09): this block computes XCH wallet needs
                # (side="xch"). Under reverse-buy, both `tier_dist` and
                # `prepared_counts` come back SIZE-INDEXED (the flip is
                # applied inside `get_tier_distribution`), but the per-side
                # helpers `get_buy_tier_size_xch(tier)` are POSITION-semantic.
                # Multiplying a size-indexed count by a position-indexed size
                # mismatches buckets under reverse-buy and inflates the
                # computed pool by ~2x (the old "need 116.8 XCH" advisory).
                #
                # The fix is to build a SIZE-indexed buy sizes dict here —
                # same pattern as the coin-prep launcher. Under reverse-buy,
                # the "inner" slot (biggest XCH coin) uses position-extreme's
                # size, etc.
                from config import get_buy_tier_size_xch
                _buy_ladder_reversed = bool(getattr(cfg, "BUY_LADDER_REVERSED", False))
                if _buy_ladder_reversed:
                    _size_indexed_buy = {
                        "inner":   get_buy_tier_size_xch("extreme"),  # biggest coin = pos extreme
                        "mid":     get_buy_tier_size_xch("outer"),
                        "outer":   get_buy_tier_size_xch("mid"),
                        "extreme": get_buy_tier_size_xch("inner"),    # smallest coin = pos inner
                    }
                else:
                    _size_indexed_buy = {
                        t: get_buy_tier_size_xch(t) for t in ("inner", "mid", "outer", "extreme")
                    }
                for t, _slots in tier_dist.items():
                    coins_needed = int(prepared_counts.get(t, 0) or 0)
                    tier_size = Decimal(str(_size_indexed_buy.get(t) or cfg.MID_SIZE_XCH))
                    tier_xch = tier_size * coins_needed
                    per_tier_needs[t] = {
                        'coins': coins_needed,
                        'xch_each': float(tier_size),
                        'xch_total': float(tier_xch)
                    }
                    total_tier_xch += tier_xch

                reserve_xch = total_xch - total_tier_xch
                # F62: the largest XCH coin we might prep is the max over
                # buy-side sizes (XCH wallet only funds buy offers). Sell
                # side is CAT so it doesn't affect XCH pool sizing.
                largest_tier = max(
                    Decimal(str(get_buy_tier_size_xch("inner") or 0)),
                    Decimal(str(get_buy_tier_size_xch("mid") or 0)),
                    Decimal(str(get_buy_tier_size_xch("outer") or 0)),
                    Decimal(str(get_buy_tier_size_xch("extreme") or 0)),
                    Decimal(str(getattr(cfg, "SNIPER_SIZE_XCH", Decimal("0")) or "0")),
                )

                # Two-tier reserve breakdown:
                # (a) configured_reserve — the XCH_RESERVE amount; untouchable,
                #     never split by topup, kept as a hard safety margin
                # (b) topup_pool — whatever remains after configured_reserve;
                #     large unbroken coins the topup worker splits when tier
                #     coins run low.  Topup cannot function if this is too thin.
                configured_reserve = max(Decimal("0"),
                    Decimal(str(getattr(cfg, "XCH_RESERVE", Decimal("0")) or "0")))
                topup_pool = max(Decimal("0"), reserve_xch - configured_reserve)

                total_str = f"{float(total_xch):.1f}"
                spendable_str = f"{float(spendable_xch):.1f}"
                needed_str = f"{float(total_tier_xch):.1f}"
                reserve_str = f"{float(reserve_xch):.1f}"
                configured_str = f"{float(configured_reserve):.1f}"
                topup_pool_str = f"{float(topup_pool):.2f}"

                # Build reserve detail suffix for messages
                if configured_reserve > 0:
                    _reserve_detail = (
                        f"{reserve_str} XCH reserve "
                        f"({configured_str} configured + {topup_pool_str} topup pool)"
                    )
                else:
                    _reserve_detail = (
                        f"{reserve_str} XCH reserve ({topup_pool_str} available for topup splits)"
                    )

                if has_locked_collateral:
                    resume_prefix = (
                        f"{total_str} XCH total, {spendable_str} XCH currently free. "
                        f"Configured tiers need {needed_str} XCH total"
                    )
                else:
                    resume_prefix = (
                        f"{total_str} XCH available. "
                        f"Tiers need {needed_str}"
                    )

                if reserve_xch < 0:
                    assessment = 'CRITICAL'
                    if has_locked_collateral:
                        msg = (
                            f"{resume_prefix}, so the wallet is short by "
                            f"{float(abs(reserve_xch)):.1f} XCH overall. "
                            f"Existing offers may still be live, but the configured book "
                            f"cannot be fully rebuilt at these settings."
                        )
                    else:
                        msg = (f"Need {needed_str} XCH for tiers but only "
                               f"have {total_str} XCH. Reduce tier sizes or "
                               f"number of offers.")
                elif topup_pool < largest_tier * 2:
                    # Warn when the topup pool (not just total reserve) is too thin
                    # to fund a full split of the largest tier.
                    # A topup pool smaller than 2× largest_tier can't reliably
                    # replenish both buy and sell sides in a single split pass.
                    assessment = 'LOW'
                    if has_locked_collateral:
                        msg = (
                            f"{resume_prefix}, leaving {_reserve_detail} (topup pool low). "
                            f"Locked balance is funding live offers; "
                            f"{spendable_str} XCH is the free headroom. "
                            f"Topup pool needs ≥{float(largest_tier * 2):.1f} XCH "
                            f"to reliably replenish the largest tier."
                        )
                    else:
                        msg = (f"{total_str} XCH available. "
                               f"Tiers need {needed_str}, leaving {_reserve_detail} (topup pool low). "
                               f"Topup needs ≥{float(largest_tier * 2):.1f} XCH to split the largest tier.")
                elif reserve_xch > largest_tier * 3:
                    assessment = 'EXCESS'
                    if has_locked_collateral:
                        msg = (
                            f"{resume_prefix}, leaving {_reserve_detail} (plenty). "
                            f"Existing live offers are already using the locked balance; "
                            f"{spendable_str} XCH remains free for topups."
                        )
                    else:
                        msg = (f"{total_str} XCH available. "
                               f"Tiers need {needed_str}, leaving {_reserve_detail} (plenty).")
                else:
                    assessment = 'ADEQUATE'
                    if has_locked_collateral:
                        msg = (
                            f"{resume_prefix}, leaving {_reserve_detail}. "
                            f"Existing live offers are using the locked balance; "
                            f"{spendable_str} XCH remains free for topups."
                        )
                    else:
                        msg = (f"{total_str} XCH available. "
                               f"Tiers need {needed_str}, leaving {_reserve_detail}.")

                return {
                    'total_available': float(total_xch),
                    'spendable_available': float(spendable_xch),
                    'locked_estimate': float(locked_xch),
                    'per_tier_needs': per_tier_needs,
                    'total_tier_xch': float(total_tier_xch),
                    'reserve_size': float(reserve_xch),
                    'configured_reserve': float(configured_reserve),
                    'topup_pool': float(topup_pool),
                    'assessment': assessment,
                    'message': msg
                }
            else:
                # Non-tiered mode — simpler calculation
                target_coins = int(max_per_side * 2 * multiplier)
                coin_size = self.get_target_xch_coin_size()
                needed_xch = coin_size * target_coins
                reserve_xch = total_xch - needed_xch

                if reserve_xch < 0:
                    assessment = 'CRITICAL'
                elif reserve_xch < coin_size * 2:
                    assessment = 'LOW'
                else:
                    assessment = 'ADEQUATE'

                return {
                    'total_available': float(total_xch),
                    'spendable_available': float(spendable_xch),
                    'locked_estimate': float(locked_xch),
                    'target_coins': target_coins,
                    'coin_size': float(coin_size),
                    'needed_xch': float(needed_xch),
                    'reserve_size': float(reserve_xch),
                    'assessment': assessment,
                    'message': (f"{float(total_xch):.1f} XCH available. "
                                f"Need {float(needed_xch):.1f} for {target_coins} coins, "
                                f"leaving {float(reserve_xch):.1f} reserve. "
                                f"Status: {assessment}")
                }

        except Exception as e:
            return {
                'total_available': 0,
                'assessment': 'UNKNOWN',
                'message': f"Could not calculate advisory: {e}"
            }

    def reconcile_with_wallet(self):
        """Authoritative sync: wallet RPC wins any disagreement.

        Called every N loops (configurable), and should be called on startup.

        Uses Sage's filter_mode="owned" (all held coins) and "selectable"
        (free coins only) to determine which coins exist and their status.
        Locked coins = owned - selectable.

        Catches:
          - Coins that vanished (mark gone)
          - Coins that appeared (add as 'unknown' for next classification)
          - Status mismatches (free↔locked sync)
          - Reserve coins that disappeared (promote next largest)
        """
        try:
            from database import (reconcile_coins_with_wallet,
                                   get_reserve_coins)
            from wallet import get_wallet_type

            wallet_type_str = get_wallet_type()
            is_sage = wallet_type_str == "sage"
            wallet_open_offers = None
            wallet_confirmed_locked = set()

            for wt, wallet_id in [("xch", cfg.WALLET_ID_XCH),
                                   ("cat", cfg.CAT_WALLET_ID)]:
                offer_id_map = {}
                selectable_records = None

                if is_sage:
                    # Sage V5 FIX: Use get_owned_coins_detailed() which returns
                    # the offer_id for each coin. This tells us EXACTLY which coins
                    # are locked by offers — no more unreliable set-difference guessing.
                    #
                    # From Sage source (migrations/0002_options.sql):
                    #   owned_coins = wallet_coins WHERE spent IS NULL (includes locked)
                    #   selectable_coins = same + offer_hash IS NULL (free only)
                    # The CoinRecord includes offer_id for locked coins.
                    try:
                        detailed_snapshot = self._get_sage_owned_coin_snapshot(wallet_id)
                    except Exception:
                        detailed_snapshot = None

                    if detailed_snapshot is not None:
                        # Build owned_map and selectable_map from detailed data
                        # This is more accurate than two separate RPC calls because
                        # the data is from a single query — no race condition between
                        # the owned and selectable calls.
                        owned_map = dict(detailed_snapshot["owned_map"])
                        selectable_map = dict(detailed_snapshot["selectable_map"])
                        selectable_records = list(detailed_snapshot["selectable_records"])
                        offer_id_map = dict(detailed_snapshot["offer_id_map"])
                        wallet_confirmed_locked.update(detailed_snapshot["locked_ids"])
                    else:
                        # Fallback: use old owned+selectable approach
                        from wallet import get_owned_coins, get_selectable_coins_map
                        owned_map = get_owned_coins(wallet_id)
                        selectable_map = get_selectable_coins_map(wallet_id)

                    if owned_map is None or selectable_map is None:
                        log_event("warning", "reconcile_skip",
                                  f"Could not fetch {wt} coins from Sage — skipping")
                        continue

                    # Run the full reconciliation
                    stats = reconcile_coins_with_wallet(
                        wallet_selectable=selectable_map,
                        wallet_owned=owned_map,
                        wallet_type=wt
                    )
                    total = (stats["added"] + stats.get("reappeared", 0)
                             + stats["marked_gone"] + stats["freed"] + stats["locked"])
                    if total > 0:
                        print(f"  [CoinMgr] {wt.upper()} reconcile: "
                              f"+{stats['added']} new, "
                              f"{stats.get('reappeared', 0)} reappeared, "
                              f"-{stats['marked_gone']} gone, "
                              f"{stats['locked']} locked, {stats['freed']} freed, "
                              f"{stats['already_ok']} ok", flush=True)

                    # V5 FIX: Use offer_id from wallet to directly link coins.
                    # The offer_id field from Sage tells us exactly which offer
                    # locked each coin — no more unreliable amount-based matching.
                    # This replaces the old link_offers_to_locked_coins() approach.
                    #
                    # V6 FIX (2026-04-08): Wallet-authoritative audit pass.
                    # Always run the audit when offer_id_map is available — not
                    # just when stats["locked"] > 0. The previous gating left
                    # three drift conditions unfixed:
                    #   (a) coins already locked from a prior cycle with NULL
                    #       trade_id (no longer caught after the first cycle)
                    #   (b) coins already locked with a STALE trade_id
                    #       (offer was replaced; coin was reused under a
                    #       different offer)
                    #   (c) coins still flagged status='locked' in DB whose
                    #       offer no longer exists in the wallet at all
                    #       (orphan locks — happen when reconcile_coins_with
                    #       _wallet's free-transition is blocked by a stale
                    #       trade_id at line 1994).
                    # The audit fixes (a)+(b)+(c) in one pass, with the wallet
                    # as the single source of truth.
                    if offer_id_map is not None:
                        try:
                            from database import get_connection, _now
                            conn = get_connection()
                            now = _now()
                            audit_linked = 0     # NULL → trade_id set
                            audit_relinked = 0   # stale trade_id overwritten
                            audit_freed = 0      # orphan lock freed
                            # Get all open offers from wallet to map offer_hash → trade_id
                            if wallet_open_offers is None:
                                from wallet import get_all_offers
                                wallet_open_offers = get_all_offers(
                                    include_completed=False,
                                    start=0,
                                    end=500,
                                ) or []
                            all_open = wallet_open_offers
                            # Build {offer_hash: trade_id} mapping. For Sage,
                            # the offer's trade_id IS the offer_hash, so this
                            # is mostly an identity map; the lookup is here so
                            # we only ever assign a trade_id we've actually
                            # seen as an open offer (defends against junk).
                            hash_to_trade = {}
                            open_trade_ids = set()
                            if all_open:
                                for o in all_open:
                                    tid = o.get("trade_id", "")
                                    if tid:
                                        hash_to_trade[tid.lower()] = tid
                                        open_trade_ids.add(tid)

                            # ---- Build the wallet-authoritative locked map ----
                            # For every coin that Sage says is locked, derive
                            # the canonical trade_id we should record.
                            wallet_locked_to_trade = {}
                            for cid, oid in offer_id_map.items():
                                store_id = cid if cid.startswith("0x") else "0x" + cid
                                trade_id = hash_to_trade.get(oid, oid)
                                if trade_id:
                                    wallet_locked_to_trade[store_id] = trade_id

                            # ---- Pass 1: Reconcile each wallet-locked coin ----
                            # The DB row should be (status='locked',
                            # trade_id=canonical). Apply the minimum update.
                            for store_id, canonical_tid in wallet_locked_to_trade.items():
                                row = conn.execute(
                                    "SELECT status, trade_id FROM coins "
                                    "WHERE coin_id=?",
                                    (store_id,)
                                ).fetchone()
                                if row is None:
                                    # Coin not in DB yet — reconcile_coins_with_wallet
                                    # should have inserted it above. If it's
                                    # still missing, something is wrong with
                                    # the upsert; skip silently here so we
                                    # don't double-commit.
                                    continue
                                cur_status = row['status']
                                cur_tid = row['trade_id'] or ""
                                if cur_status != "locked":
                                    conn.execute(
                                        "UPDATE coins SET status='locked', "
                                        "trade_id=?, last_seen=? WHERE coin_id=?",
                                        (canonical_tid, now, store_id)
                                    )
                                    audit_relinked += 1
                                elif not cur_tid:
                                    conn.execute(
                                        "UPDATE coins SET trade_id=?, "
                                        "last_seen=? WHERE coin_id=?",
                                        (canonical_tid, now, store_id)
                                    )
                                    audit_linked += 1
                                elif cur_tid != canonical_tid:
                                    conn.execute(
                                        "UPDATE coins SET trade_id=?, "
                                        "last_seen=? WHERE coin_id=?",
                                        (canonical_tid, now, store_id)
                                    )
                                    audit_relinked += 1

                            # ---- Pass 2: Free orphan locks ----
                            # Any DB row that says status='locked' for THIS
                            # wallet type but does NOT appear in the wallet's
                            # locked map is a stale lock. Either the offer is
                            # gone (cancelled/filled and confirmed on-chain)
                            # or the trade_id was bogus to begin with.
                            #
                            # Guard: don't touch coins locked very recently
                            # (last 60s). The offer_manager may have just
                            # locked a coin in the gap between our wallet
                            # snapshot and now — we don't want the audit to
                            # race-clobber a fresh lock.
                            stale_threshold_secs = 60
                            db_locked_rows = conn.execute(
                                "SELECT coin_id, trade_id, last_seen "
                                "FROM coins "
                                "WHERE wallet_type=? AND status='locked'",
                                (wt,)
                            ).fetchall()
                            for row in db_locked_rows:
                                cid_db = row['coin_id']
                                cid_norm = (cid_db if cid_db.startswith("0x")
                                            else "0x" + cid_db).lower()
                                if cid_norm in wallet_locked_to_trade:
                                    continue  # Wallet still locks it — fine
                                # Wallet doesn't lock it. Is it freshly
                                # locked by us in the last 60s?
                                last_seen = row['last_seen'] or 0
                                try:
                                    age = now - int(last_seen)
                                except Exception:
                                    age = 999999
                                if age < stale_threshold_secs:
                                    continue  # Too fresh to touch
                                # Also: if the row's trade_id is in our open
                                # offers list, the wallet just hasn't reported
                                # the lock yet (rare RPC race); leave it alone.
                                cur_tid = row['trade_id'] or ""
                                if cur_tid and cur_tid in open_trade_ids:
                                    continue
                                # Genuine orphan lock — free it.
                                conn.execute(
                                    "UPDATE coins SET status='free', "
                                    "trade_id=NULL, last_seen=? "
                                    "WHERE coin_id=?",
                                    (now, cid_db)
                                )
                                audit_freed += 1

                            conn.commit()

                            if audit_linked or audit_relinked or audit_freed:
                                log_event("info", "reconcile_audit",
                                          f"{wt.upper()} wallet audit: "
                                          f"linked={audit_linked}, "
                                          f"relinked={audit_relinked}, "
                                          f"freed_orphans={audit_freed}")
                        except Exception as audit_e:
                            log_event("warning", "reconcile_audit_failed",
                                      f"Wallet audit failed for {wt}: {audit_e}")

                    # Fallback: amount-based linking ONLY when direct offer_id
                    # linking is not available (i.e. no offer_id_map from Sage)
                    if stats["locked"] > 0 and not offer_id_map:
                        try:
                            from database import link_offers_to_locked_coins
                            if wallet_open_offers is None:
                                from wallet import get_all_offers
                                wallet_open_offers = get_all_offers(
                                    include_completed=False,
                                    start=0,
                                    end=500,
                                ) or []
                            all_open = wallet_open_offers
                            if all_open:
                                link_stats = link_offers_to_locked_coins(
                                    all_open, cfg.CAT_ASSET_ID)
                                linked = link_stats.get("linked", 0)
                                if linked > 0:
                                    log_event("debug", "reconcile_link_offers",
                                              f"{wt.upper()} linked {linked} coins to offers "
                                              f"after reconcile (amount-based fallback)")
                        except Exception as link_e:
                            log_event("debug", "reconcile_link_failed",
                                      f"Post-reconcile offer linking failed: {link_e}")

                else:
                    # Chia wallet: original approach using spendable-only RPC
                    from database import (get_free_coins, get_locked_coins,
                                           mark_coins_gone, mark_coin_spent,
                                           free_coin, upsert_coin, get_open_offers)

                    rpc_result = self._get_coins_fast(wallet_id)
                    rpc_records = _extract_coin_records(rpc_result)
                    rpc_ids = set()
                    for rec in rpc_records:
                        cid = _coin_id_from_record(rec)
                        if cid:
                            rpc_ids.add(cid)

                    db_free = get_free_coins(wt)
                    db_ids = {c["coin_id"] for c in db_free}

                    gone = db_ids - rpc_ids
                    if gone:
                        mark_coins_gone(list(gone))

                    new_coins = rpc_ids - db_ids
                    for rec in rpc_records:
                        cid = _coin_id_from_record(rec)
                        if cid and cid in new_coins:
                            amt = _coin_amount(rec)
                            upsert_coin(cid, wt, amt)

                # Check if reserve disappeared
                if selectable_records is not None:
                    rpc_records = list(selectable_records)
                else:
                    rpc_result = self._get_coins_fast(wallet_id)
                    rpc_records = _extract_coin_records(rpc_result)
                reserves = get_reserve_coins(wt)
                if not reserves:
                    self._ensure_reserve_exists(wt, rpc_records)

            # ---- Chia-only: Reconcile locked coins (Sage handles this above) ----
            if not is_sage:
                try:
                    from database import (get_locked_coins, get_open_offers,
                                           free_coin, mark_coin_spent)
                    open_offers_list = get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
                    open_trade_ids = {o["trade_id"] for o in open_offers_list}

                    xch_rpc = self._get_coins_fast(cfg.WALLET_ID_XCH)
                    cat_rpc = self._get_coins_fast(cfg.CAT_WALLET_ID)
                    wallet_spendable = set()
                    for rec in _extract_coin_records(xch_rpc) + _extract_coin_records(cat_rpc):
                        cid = _coin_id_from_record(rec)
                        if cid:
                            wallet_spendable.add(cid)

                    db_locked = get_locked_coins()
                    reconciled = 0
                    for coin in db_locked:
                        cid = coin["coin_id"]
                        linked_trade = coin.get("trade_id", "")
                        if cid in wallet_spendable:
                            free_coin(cid)
                            reconciled += 1
                        elif linked_trade and linked_trade not in open_trade_ids:
                            mark_coin_spent(cid)
                            reconciled += 1
                    if reconciled > 0:
                        log_event("info", "reconcile_locked",
                                  f"Reconciled {reconciled} stale locked coins")
                except Exception as lock_e:
                    log_event("debug", "reconcile_locked_error",
                              f"Locked coin reconciliation failed: {lock_e}")

            # ---- Link offers to their locked coins (assign trade_ids) ----
            try:
                from database import link_offers_to_locked_coins

                # Get all active offers with normalized summaries
                if wallet_open_offers is None:
                    from wallet import get_all_offers
                    wallet_open_offers = get_all_offers(
                        include_completed=False,
                        start=0,
                        end=500,
                    ) or []
                all_offers = wallet_open_offers
                if all_offers and isinstance(all_offers, list):
                    active = [o for o in all_offers
                              if o.get("status") in ("active", "PENDING_ACCEPT")]
                    if active:
                        link_stats = link_offers_to_locked_coins(
                            active, cfg.CAT_ASSET_ID
                        )
                        if link_stats["linked"] > 0:
                            print(f"  [CoinMgr] Linked {link_stats['linked']} offers to coins "
                                  f"({link_stats['already_linked']} already linked, "
                                  f"{link_stats['unmatched_offers']} unmatched offers, "
                                  f"{link_stats['unmatched_coins']} unmatched coins)",
                                  flush=True)
            except Exception as link_e:
                log_event("debug", "offer_link_error",
                          f"Offer-to-coin linking failed: {link_e}")

            # ---- Orphaned locked coin cleanup (V5 FIX) ----
            # Free locked coins whose trade_id no longer matches an open offer.
            #
            # CRITICAL FIX: For Sage wallets, pass the set of coin IDs that
            # the wallet confirms are offer-locked (have offer_id set).
            # The cleanup function will NOT free these coins even if they
            # lack a trade_id in our DB — the wallet is authoritative.
            # This breaks the tug-of-war: reconcile locks → orphan frees → repeat.
            try:
                from database import cleanup_orphaned_locked_coins, get_open_offers
                db_open = get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)
                wallet_open_ids = {o.get("trade_id", "") for o in db_open
                                   if o.get("trade_id")}

                orphan_stats = cleanup_orphaned_locked_coins(
                    wallet_open_ids,
                    wallet_confirmed_locked=wallet_confirmed_locked
                )
                if orphan_stats["total_freed"] > 0:
                    print(f"  [CoinMgr] Freed {orphan_stats['total_freed']} orphaned locked coins",
                          flush=True)
            except Exception as orphan_e:
                log_event("debug", "orphan_cleanup_error",
                          f"Orphan cleanup during reconcile failed: {orphan_e}")

            log_event("debug", "reconcile_done",
                      "Wallet reconciliation complete")

        except Exception as e:
            log_event("warning", "reconcile_error",
                      f"Wallet reconciliation failed: {e}")

    # -------------------------------------------------------------------
    # V3: Coinset-aware coin query (fast cloud API with wallet fallback)
    # -------------------------------------------------------------------

    def _get_coins_fast(self, wallet_id: int):
        """Get spendable coins — tries Coinset first, falls back to wallet RPC.

        This is the V3 fast path. If a CoinsetClient is available and
        initialized, we use it for ~100ms queries instead of 2-5s wallet RPC.
        Falls back transparently if Coinset is unavailable.

        Returns the same record shape as the wallet coin RPC helpers.
        """
        # Check if we have a coinset client (injected by bot_loop at startup)
        coinset = getattr(self, "_coinset_client", None)
        if coinset and getattr(cfg, "COINSET_ENABLED", True):
            result = coinset.get_spendable_coins(wallet_id)
            if result is not None:
                return result

        # Default: exact currently free/selectable wallet RPC
        return _get_free_coins_rpc(wallet_id)

    @staticmethod
    def _make_simple_coin_record(coin_id: str, amount: int) -> Dict:
        """Build the minimal coin-record shape used by local classification code."""
        cid = str(coin_id or "").strip().lower()
        if cid and not cid.startswith("0x"):
            cid = "0x" + cid
        return {
            "coin_id": cid,
            "coin": {"amount": int(amount or 0)},
        }

    def _get_sage_owned_coin_snapshot(self, wallet_id: int) -> Optional[Dict]:
        """Fetch one Sage owned-coin view and derive owned/selectable state.

        Sage's owned coin records include `offer_id` for offer-locked coins, so a
        single `filter_mode="owned"` query can tell us every owned coin, which
        subset is selectable, and which coins are confirmed locked by offers.
        """
        if get_wallet_type() != "sage":
            return None

        try:
            detailed_map = get_owned_coins_detailed(wallet_id)
        except Exception:
            return None

        if detailed_map is None:
            return None

        owned_map = {}
        selectable_map = {}
        selectable_records = []
        offer_id_map = {}
        locked_ids = set()

        for raw_coin_id, info in (detailed_map or {}).items():
            coin_id = str(raw_coin_id or "").strip().lower()
            if not coin_id:
                continue
            if not coin_id.startswith("0x"):
                coin_id = "0x" + coin_id

            info = info or {}
            amount = int(info.get("amount", 0) or 0)
            owned_map[coin_id] = amount

            offer_id = info.get("offer_id")
            if isinstance(offer_id, str):
                offer_id = offer_id.lower()

            if offer_id:
                offer_id_map[coin_id] = offer_id
                locked_ids.add(coin_id)
            else:
                selectable_map[coin_id] = amount
                selectable_records.append(self._make_simple_coin_record(coin_id, amount))

        return {
            "owned_map": owned_map,
            "selectable_map": selectable_map,
            "selectable_records": selectable_records,
            "owned_ids": set(owned_map.keys()),
            "locked_ids": locked_ids,
            "offer_id_map": offer_id_map,
        }

    # -------------------------------------------------------------------
    # CLI-based coin splitting (reliable — RPC split doesn't broadcast)
    # -------------------------------------------------------------------

    def _split_via_cli(self, wallet_id: int, coin_id: str,
                       num_coins: int, coin_size: Decimal,
                       name: str = "topup") -> bool:
        """Split a coin into smaller coins.

        Uses Sage RPC /split when WALLET_TYPE=sage, or the Chia CLI
        `chia wallet coins split` when WALLET_TYPE=chia.

        Args:
            wallet_id: Wallet ID to split in
            coin_id: The coin ID to split (hex, with or without 0x prefix)
            num_coins: Number of new coins to create
            coin_size: Size of each new coin (in XCH or CAT token amount)
            name: Label for logging

        Returns:
            True if split confirmed (or partially confirmed)
        """
        is_cat = (wallet_id != WALLET_ID_XCH)

        log_event("info", f"split_cli_{name}",
                  f"Split: {num_coins} coins of {coin_size} "
                  f"{'CAT tokens' if is_cat else 'XCH'} (display units) "
                  f"[source: {coin_id[:16]}...]")

        # Get starting coin count for confirmation polling
        start_result = _get_free_coins_rpc(wallet_id)
        start_records = _extract_coin_records(start_result)
        start_count = len(start_records)

        # --- Dispatch to Sage RPC or Chia CLI ---
        wallet_type = get_wallet_type()

        if wallet_type == "sage":
            # Sage native /split endpoint — uses output_count, auto-sizes
            # Sage splits evenly (no amount param needed), so we just specify
            # the number of outputs. The coin is divided equally.
            log_event("info", f"split_sage_{name}",
                      f"Using Sage /split RPC for {num_coins} outputs")
            try:
                # output_count = num_coins + 1 because we want N trading coins
                # plus a remainder. Sage splits the coin into output_count pieces.
                # If we want 6 coins of 3.2 XCH from a 26.98 XCH coin, we can't
                # control individual sizes — Sage divides equally. So we request
                # the total number we need and accept even splits.
                result = split_coins_rpc(
                    wallet_id=wallet_id,
                    target_coin_id=coin_id,
                    num_coins=num_coins + 1,  # +1 for remainder
                    amount_per_coin=0,  # Sage ignores this, splits evenly
                    fee_mojos=self._tx_fee_mojos(),
                    is_cat=is_cat,
                )
                if result is None:
                    log_event("warning", f"split_sage_{name}_fail",
                              "Sage /split returned None")
                    return False
                # Check for error in response
                if isinstance(result, dict) and result.get("error"):
                    log_event("warning", f"split_sage_{name}_fail",
                              f"Sage /split error: {result['error']}")
                    return False
                log_event("info", f"split_sage_{name}",
                          "Sage /split submitted successfully")
            except Exception as e:
                log_event("warning", f"split_sage_{name}_error",
                          f"Sage /split error: {e}")
                return False
        else:
            # Chia CLI split (reliable — broadcasts to network every time)
            # NOTE: CLI `-a` takes DISPLAY UNITS (XCH or CAT tokens), NOT mojos.
            bare_coin_id = coin_id.replace("0x", "")

            # Lazy-resolve fingerprint if it was empty at init time
            if not self._fingerprint or not self._fingerprint.strip():
                self._fingerprint = self._resolve_fingerprint()

            cmd = [
                "chia", "wallet", "coins", "split",
                "-f", self._fingerprint,
                "-i", str(wallet_id),
                "-n", str(num_coins),
                "-a", str(coin_size),
                "-t", bare_coin_id,
                "-m", "0"
            ]

            try:
                import subprocess as sp
                process = sp.Popen(
                    cmd,
                    stdin=sp.PIPE,
                    stdout=sp.PIPE,
                    stderr=sp.PIPE,
                    text=True
                )
                stdout, stderr = process.communicate(input="y\n", timeout=60)
                output = stdout + stderr

                if "submitted to" in output.lower() or "transaction" in output.lower():
                    log_event("info", f"split_cli_{name}",
                              "CLI split submitted successfully")
                else:
                    log_event("warning", f"split_cli_{name}_fail",
                              f"CLI split failed: {output[:200]}")
                    return False

            except Exception as e:
                log_event("warning", f"split_cli_{name}_error",
                          f"CLI split error: {e}")
                return False

        # --- Wait for confirmation via coin count polling ---
        expected_count = start_count + num_coins
        confirmed = False
        poll_start = time.time()
        max_wait = 180  # 3 minutes (test showed ~61s)

        while (time.time() - poll_start) < max_wait:
            time.sleep(5)
            result = _get_free_coins_rpc(wallet_id)
            records = _extract_coin_records(result)
            current_count = len(records)
            elapsed = int(time.time() - poll_start)

            if current_count >= expected_count:
                log_event("info", f"split_{name}",
                          f"Split confirmed ({current_count} coins, {elapsed}s)")
                confirmed = True
                break

            if elapsed % 30 == 0 and elapsed > 0:
                log_event("info", f"split_{name}_wait",
                          f"Waiting for split... ({current_count}/{expected_count} coins, {elapsed}s)")

        if not confirmed:
            # Check if at least some coins were created
            final_result = _get_free_coins_rpc(wallet_id)
            final_records = _extract_coin_records(final_result)
            final_count = len(final_records)
            new_coins = final_count - start_count
            if new_coins > 0:
                log_event("info", f"split_{name}",
                          f"Partial split: {new_coins}/{num_coins} coins created after {max_wait}s")
                confirmed = True
            else:
                log_event("warning", f"split_{name}_timeout",
                          f"Split not confirmed after {max_wait}s (still {final_count} coins)")

        return confirmed

    # -------------------------------------------------------------------
    # Coin counting & inventory
    # -------------------------------------------------------------------

    def update_coin_counts(self) -> Tuple[int, int]:
        """Count and classify ALL coins for XCH and CAT wallets.

        Updates:
          - Spendable coin counts and inventory (reserve/trading/small/tiers)
          - Locked coin counts and amounts (coins held in active offers)
          - Total coin counts (free + locked)

        The locked coin data comes from comparing get_all_coins_for_wallet()
        (returns everything) with the exact selectable/free coin view.
        """
        if self._prep_running or self._topup_running:
            # During topup/prep: do a lightweight DB-only count update so the
            # bot sees newly-created coins without interfering with the worker.
            try:
                from database import get_all_coins_state
                _db_coins = get_all_coins_state()
                if _db_coins is not None:
                    _xch_free = sum(1 for c in _db_coins if c.get("wallet_type") == "xch" and c.get("status") == "free")
                    _cat_free = sum(1 for c in _db_coins if c.get("wallet_type") == "cat" and c.get("status") == "free")
                    if _xch_free != self._xch_coins or _cat_free != self._cat_coins:
                        self._xch_coins = _xch_free
                        self._cat_coins = _cat_free
            except Exception:
                pass
            return (self._xch_coins, self._cat_coins)

        try:
            # XCH — spendable coins (for inventory classification)
            # V3: uses Coinset fast path if available, falls back to wallet RPC
            wallet_type = get_wallet_type()
            xch_owned_snapshot = (
                self._get_sage_owned_coin_snapshot(cfg.WALLET_ID_XCH)
                if wallet_type == "sage" else None
            )
            if xch_owned_snapshot is not None:
                xch_records = list(xch_owned_snapshot["selectable_records"])
            else:
                xch_result = self._get_coins_fast(cfg.WALLET_ID_XCH)
                xch_records = _extract_coin_records(xch_result)

            # RETRY: If wallet returned 0 coins, try once more after a short wait.
            # Sage wallet sometimes returns empty on the first call after startup.
            if len(xch_records) == 0:
                import time as _time
                log_event("warning", "coin_count_retry",
                          "XCH wallet returned 0 coins — retrying in 3s...")
                _time.sleep(3)
                if wallet_type == "sage":
                    xch_owned_snapshot = self._get_sage_owned_coin_snapshot(cfg.WALLET_ID_XCH)
                    xch_records = list((xch_owned_snapshot or {}).get("selectable_records", []))
                else:
                    xch_result = self._get_coins_fast(cfg.WALLET_ID_XCH)
                    xch_records = _extract_coin_records(xch_result)
                if len(xch_records) > 0:
                    log_event("info", "coin_count_retry_ok",
                              f"Retry succeeded: {len(xch_records)} XCH coins found")

            self._xch_coins = len(xch_records)

            # CAT — spendable coins
            # V3: uses Coinset fast path if available, falls back to wallet RPC
            cat_owned_snapshot = (
                self._get_sage_owned_coin_snapshot(cfg.CAT_WALLET_ID)
                if wallet_type == "sage" else None
            )
            if cat_owned_snapshot is not None:
                cat_records = list(cat_owned_snapshot["selectable_records"])
            else:
                cat_result = self._get_coins_fast(cfg.CAT_WALLET_ID)
                cat_records = _extract_coin_records(cat_result)

            # RETRY: Same retry for CAT wallet
            if len(cat_records) == 0:
                import time as _time
                log_event("warning", "coin_count_retry",
                          "CAT wallet returned 0 coins — retrying in 3s...")
                _time.sleep(3)
                if wallet_type == "sage":
                    cat_owned_snapshot = self._get_sage_owned_coin_snapshot(cfg.CAT_WALLET_ID)
                    cat_records = list((cat_owned_snapshot or {}).get("selectable_records", []))
                else:
                    cat_result = self._get_coins_fast(cfg.CAT_WALLET_ID)
                    cat_records = _extract_coin_records(cat_result)
                if len(cat_records) > 0:
                    log_event("info", "coin_count_retry_ok",
                              f"Retry succeeded: {len(cat_records)} CAT coins found")

            self._cat_coins = len(cat_records)

            # ---- Step 1: Persist coins to database FIRST ----
            # This ensures DB rows exist so set_coin_designation() UPDATE works
            # during classification. Without this, new coins get designated in
            # memory but the DB designation is lost (UPDATE hits 0 rows).

            # For Sage wallet: Also fetch "owned" coins to distinguish between
            # truly gone coins vs Sage-hidden receive-side coins in offers
            xch_owned_ids = set()
            cat_owned_ids = set()
            if wallet_type == "sage":
                if xch_owned_snapshot is not None:
                    xch_owned_ids = set(xch_owned_snapshot["owned_ids"])
                else:
                    xch_owned = get_owned_coins(cfg.WALLET_ID_XCH)
                    if xch_owned:
                        xch_owned_ids = set(xch_owned.keys())

                if cat_owned_snapshot is not None:
                    cat_owned_ids = set(cat_owned_snapshot["owned_ids"])
                else:
                    cat_owned = get_owned_coins(cfg.CAT_WALLET_ID)
                    if cat_owned:
                        cat_owned_ids = set(cat_owned.keys())

            self._persist_coins_to_db(xch_records, "xch", {}, xch_owned_ids)
            self._persist_coins_to_db(cat_records, "cat", {}, cat_owned_ids)

            # ---- Step 2: Classify coins (reads+writes designations in DB) ----
            if cfg.TIER_ENABLED:
                xch_tier_mojos = self._get_tier_sizes_mojos(is_cat=False)
                self._xch_inventory = self._classify_coins_by_designation(
                    xch_records, "xch", xch_tier_mojos)
                self._ensure_reserve_exists("xch", xch_records)

                cat_tier_mojos = self._get_tier_sizes_mojos(is_cat=True)
                self._cat_inventory = self._classify_coins_by_designation(
                    cat_records, "cat", cat_tier_mojos)
                self._ensure_reserve_exists("cat", cat_records)
            else:
                xch_trading_mojos = int(self.get_target_xch_coin_size() * Decimal("1000000000000"))
                self._xch_inventory = _classify_coins(xch_records, xch_trading_mojos)

                cat_scale = Decimal(10) ** Decimal(cfg.CAT_DECIMALS)
                cat_trading_mojos = int(self.get_target_cat_coin_size() * cat_scale)
                self._cat_inventory = _classify_coins(cat_records, cat_trading_mojos)

            # ---- Locked coin tracking ----
            # Get ALL coins (free + locked) and subtract spendable to find locked
            self._update_locked_coins(xch_records, cat_records)

            # ---- Refresh fee coin pool for this cycle ----
            # Must happen AFTER classification so _xch_inventory["fees"] is current.
            self.fee_pool.refresh(self._xch_inventory.get("fees", []))

        except Exception as e:
            log_event("warning", "coin_count_failed", f"Failed to count coins: {e}")

        return (self._xch_coins, self._cat_coins)

    def _persist_coins_to_db(self, records: list, wallet_type: str,
                                inventory: Dict[str, list], owned_ids: set = None):
        """Persist all spendable coins to the coins table in the database.

        For each coin in the current snapshot:
          - Upsert it (insert new or update last_seen)
          - Classify its tier from the inventory dict

        After upserting, any DB coins that were 'free' but weren't seen
        in this snapshot get marked as 'gone' (they vanished from the wallet).

        Sage wallet fix: If a coin is missing from selectable but exists in owned_ids
        (from filter_mode="owned"), it's not gone — it's just hidden because it's the
        receive-side of an active offer. Only mark truly missing coins as gone.

        SAFETY: If the wallet RPC returned 0 coins but the DB has coins,
        this is almost certainly a transient RPC failure — NOT all coins
        vanishing. We skip the mark-gone step to preserve designations.

        Args:
            records: Raw coin records from the RPC
            wallet_type: 'xch' or 'cat'
            inventory: Classified inventory dict (from _classify_coins or _classify_coins_tiered)
            owned_ids: Set of coin IDs from filter_mode="owned" (Sage wallet only, None for Chia)
        """
        try:
            from database import upsert_coin, get_free_coins, mark_coins_gone

            # Build a coin_id → tier lookup from the inventory classification
            coin_tier_map = {}
            for tier_name, tier_records in inventory.items():
                for rec in tier_records:
                    cid = _coin_id_from_record(rec)
                    if cid:
                        coin_tier_map[cid] = tier_name

            # Upsert all current coins
            seen_ids = set()
            for rec in records:
                cid = _coin_id_from_record(rec)
                if not cid:
                    continue
                amt = _coin_amount(rec)
                tier = coin_tier_map.get(cid, "unknown")
                upsert_coin(cid, wallet_type, amt, tier)
                seen_ids.add(cid)

            # Mark coins that vanished — were 'free' in DB but not in current snapshot
            # Normalize DB coin IDs to match the format from _coin_id_from_record()
            from database import norm_coin_id
            db_free = get_free_coins(wallet_type)
            missing_ids = [c["coin_id"] for c in db_free
                           if norm_coin_id(c["coin_id"]) not in seen_ids]
            gone_count = 0
            sage_hidden_count = 0
            if missing_ids:
                # SAFETY GUARD: If wallet returned 0 coins but DB has many,
                # this is a wallet RPC failure, not mass disappearance.
                # Don't nuke the entire coin DB — it destroys prep designations.
                if len(seen_ids) == 0 and len(missing_ids) > 5:
                    log_event("warning", "coin_persist_skip_gone",
                              f"Wallet returned 0 {wallet_type} coins but DB has "
                              f"{len(missing_ids)} free — skipping mark-gone "
                              f"(likely RPC failure, not mass disappearance)")
                else:
                    # SAGE FIX: Check if coins are missing from selectable but present
                    # in owned_ids. If so, they're just hidden (receive-side of offer),
                    # not gone. Only mark truly missing coins as gone.
                    truly_gone_ids = []
                    if owned_ids is not None:
                        # Sage wallet case: have owned coin IDs from filter_mode="owned"
                        for coin_id in missing_ids:
                            normalized = norm_coin_id(coin_id).lower()
                            if normalized in owned_ids:
                                # Coin is in owned but not selectable → hidden by Sage
                                sage_hidden_count += 1
                                log_event("debug", "sage_receive_side_hidden",
                                          f"{wallet_type.upper()} coin {normalized[:12]}... "
                                          f"hidden (receive-side of offer)")
                            else:
                                # Truly gone
                                truly_gone_ids.append(coin_id)
                        gone_count = mark_coins_gone(truly_gone_ids) if truly_gone_ids else 0
                    else:
                        # Chia wallet case: no owned_ids available, use original behavior
                        gone_count = mark_coins_gone(missing_ids)

            # Log sync summary with structured data
            # (individual coin events already logged by database.py)
            summary_msg = (f"{wallet_type.upper()} sync: {len(seen_ids)} in wallet, "
                          f"{gone_count} gone, {len(db_free)} were free in DB")
            if sage_hidden_count > 0:
                summary_msg += f", {sage_hidden_count} hidden by Sage"
            log_event("debug", "coin_sync_summary",
                      summary_msg,
                      data={"wallet_type": wallet_type,
                            "coins_in_wallet": len(seen_ids),
                            "gone_count": gone_count,
                            "sage_hidden_count": sage_hidden_count,
                            "db_free_before": len(db_free)})

        except Exception as e:
            log_event("warning", "coin_persist_failed",
                      f"Failed to persist {wallet_type} coins to DB: {e}")

    def _update_locked_coins(self, xch_spendable: list, cat_spendable: list):
        """Calculate locked coins using BOTH the coins table AND the offers table.

        Strategy:
          1. Check coins table for locked coins (populated by lock_coin() calls)
          2. ALWAYS also check the offers table for open offers
          3. Use whichever source reports MORE locked coins (handles the case where
             the coins table hasn't been populated yet for pre-existing offers)

        This hybrid approach ensures:
          - New offers (created with the coins table code) use coins-table data
          - Old offers (from before coins table existed) still show as locked
          - On restart, locked counts are correct immediately
        """
        try:
            # ---- Source 1: Coins table ----
            coins_xch_locked = 0
            coins_xch_locked_mojos = 0
            coins_cat_locked = 0
            coins_cat_locked_mojos = 0
            try:
                from database import get_coin_summary
                summary = get_coin_summary()
                coins_xch_locked = summary.get('xch_locked_count', 0)
                coins_xch_locked_mojos = summary.get('xch_locked_mojos', 0)
                coins_cat_locked = summary.get('cat_locked_count', 0)
                coins_cat_locked_mojos = summary.get('cat_locked_mojos', 0)
            except Exception as e:
                log_event("warning", "coin_summary_fetch_failed",
                          f"Coin summary DB fetch failed (locked counts will be zero): {e}")

            # ---- Source 2: Offers table (always check) ----
            from database import get_open_offers
            open_offers = get_open_offers(cat_asset_id=cfg.CAT_ASSET_ID)

            buy_offers = [o for o in open_offers if o.get("side") == "buy"]
            offers_xch_locked = len(buy_offers)
            offers_xch_locked_mojos = 0
            for o in buy_offers:
                try:
                    size_xch = Decimal(str(o.get("size_xch", 0)))
                    offers_xch_locked_mojos += int(size_xch * Decimal("1000000000000"))
                except Exception as e:
                    log_event("debug", "buy_offer_mojo_calc_failed",
                              f"XCH locked mojo calc failed for offer {o.get('trade_id','?')[:12]}: {e}")

            sell_offers = [o for o in open_offers if o.get("side") == "sell"]
            offers_cat_locked = len(sell_offers)
            offers_cat_locked_mojos = 0
            cat_scale = Decimal(10) ** Decimal(cfg.CAT_DECIMALS)
            for o in sell_offers:
                try:
                    size_cat = Decimal(str(o.get("size_cat", 0)))
                    offers_cat_locked_mojos += int(size_cat * cat_scale)
                except Exception as e:
                    log_event("debug", "sell_offer_mojo_calc_failed",
                              f"CAT locked mojo calc failed for offer {o.get('trade_id','?')[:12]}: {e}")

            # ---- Use the OFFERS table as the authoritative source ----
            # The offers table knows exactly which side each offer locks:
            #   buy offers lock XCH coins, sell offers lock CAT coins.
            # The coins table may be inflated because Sage's "non-selectable"
            # includes coins on BOTH sides of an offer, not just the offered side.
            # Fall back to coins table only if offers table is empty (shouldn't happen).
            self._xch_locked_coins = offers_xch_locked if offers_xch_locked > 0 else coins_xch_locked
            self._xch_locked_amount = offers_xch_locked_mojos if offers_xch_locked > 0 else coins_xch_locked_mojos
            self._cat_locked_coins = offers_cat_locked if offers_cat_locked > 0 else coins_cat_locked
            self._cat_locked_amount = offers_cat_locked_mojos if offers_cat_locked > 0 else coins_cat_locked_mojos

            # Total = free (from RPC) + locked
            self._xch_total_coins = len(xch_spendable) + self._xch_locked_coins
            self._cat_total_coins = len(cat_spendable) + self._cat_locked_coins

            # Fix label drift caused by the serial-mode selector
            # fallback (offer_manager._create_one). When Sage picks a
            # mismatched-size coin to back a slot, the coin retains its
            # old tier label even though its ACTUAL size now matches a
            # different tier. Over successive fill bursts this starves
            # the topup splitter (which reads labels) even when the
            # wallet has plenty of XCH to re-split. Normalising labels
            # to match actual sizes here means topup's view matches
            # reality so it can move XCH back into under-stocked tiers.
            try:
                self._normalize_tier_labels()
            except Exception as _norm_err:
                log_event("warning", "tier_normalize_failed",
                          f"Tier label normalisation pass failed: {_norm_err}",
                          data={"exc_type": type(_norm_err).__name__,
                                "exc_msg": str(_norm_err)})

        except Exception as e:
            log_event("warning", "locked_coin_count_failed",
                      f"Failed to count locked coins: {e}")

    def _normalize_tier_labels(self) -> Dict[str, int]:
        """Re-classify free tier_spare coins by SIZE and fix label drift.

        The serial-mode offer-selector fallback lets Sage pick any free
        coin when pre-selection fails. That picked coin may be sized for
        a DIFFERENT tier than the slot. The offer works, but when the
        coin returns to the wallet (fill change or cancel) the DB still
        carries the old label. Over time labels desynchronise from sizes
        and topup starves despite plentiful XCH.

        For each free tier_spare coin:
          - classify by actual size via ``classify_coin``
          - if best_tier is set but differs from assigned_tier, relabel
          - if classifier says reserve / dust / misfit, demote to
            ``designation='reserve'`` (reserve) or ``'unknown'`` (others)
            so topup can use it as raw split material.

        Idempotent — writes nothing when labels already match sizes.
        """
        log_event("debug", "tier_normalize_entry",
                  "Running tier label normalisation pass")
        try:
            from coin_classifier import classify_coin, CoinDesignation as _CD  # noqa: F401
        except Exception as _imp_err:
            log_event("debug", "tier_normalize_import_failed",
                      f"classify_coin import failed: {_imp_err}")
            return {"relabeled": 0, "demoted": 0}
        from database import get_connection, _now

        summary = {"relabeled": 0, "demoted_reserve": 0,
                   "demoted_unknown": 0}
        # Diagnostic counters so we can see why nothing fires even
        # when drift is present.
        scanned = {"xch": 0, "cat": 0}
        conn = get_connection()

        for wt in ("xch", "cat"):
            try:
                tier_sizes = get_tier_sizes_mojos_from_cfg(is_cat=(wt == "cat"))
            except Exception as _ts_err:
                log_event("debug", "tier_normalize_sizes_failed",
                          f"get_tier_sizes_mojos_from_cfg({wt}) failed: {_ts_err}")
                tier_sizes = {}
            if not tier_sizes:
                log_event("debug", "tier_normalize_no_sizes",
                          f"No tier sizes available for {wt} — skipping")
                continue
            rows = conn.execute(
                "SELECT coin_id, amount_mojos, assigned_tier "
                "FROM coins "
                "WHERE wallet_type=? "
                "  AND status='free' "
                "  AND designation='tier_spare' "
                "  AND assigned_tier IN ('inner','mid','outer','extreme')",
                (wt,),
            ).fetchall()
            scanned[wt] = len(rows)

            for r in rows:
                amt = int(r["amount_mojos"] or 0)
                if amt <= 0:
                    continue
                try:
                    cls = classify_coin(amt, tier_sizes)
                except Exception:
                    continue

                current = (r["assigned_tier"] or "").lower()
                best = (cls.best_tier or "").lower() if cls.best_tier else ""

                # Compare by VALUE (strings) not identity — enum
                # identity comparison fails in the bot's running process
                # even though sys.modules normally dedupes (root cause
                # not fully understood, observed during 2026-04-23
                # session — .value-based comparison is the robust fix).
                _des_val = getattr(cls.designation, "value", str(cls.designation))

                if _des_val == "tier_spare" and best and best != current:
                    conn.execute(
                        "UPDATE coins SET assigned_tier=?, last_seen=? "
                        "WHERE coin_id=?",
                        (best, _now(), r["coin_id"]),
                    )
                    summary["relabeled"] += 1
                elif _des_val == "reserve":
                    conn.execute(
                        "UPDATE coins SET designation='reserve', "
                        "assigned_tier='none', last_seen=? "
                        "WHERE coin_id=?",
                        (_now(), r["coin_id"]),
                    )
                    summary["demoted_reserve"] += 1
                elif _des_val in ("dust", "unknown"):
                    conn.execute(
                        "UPDATE coins SET designation='unknown', "
                        "assigned_tier='none', last_seen=? "
                        "WHERE coin_id=?",
                        (_now(), r["coin_id"]),
                    )
                    summary["demoted_unknown"] += 1

        # Overstock harvest — demote slack tier coins to reserve so the
        # topup splitter has split-material when other tiers run short.
        # Without this, change coins from fills accumulate in whichever
        # tier their size matches, and tiers whose coins got consumed
        # stay empty because there's no reserve to split from. Promoting
        # overstock keeps the reserve pool fed automatically.
        #
        # Rule: for each tier, if free_count > target, demote the
        # (free_count − target) largest coins to reserve. Largest-first
        # because bigger coins have more split potential.
        summary["demoted_overstock"] = 0
        for wt in ("xch", "cat"):
            try:
                targets = self._configured_prep_counts(wt)
            except Exception:
                targets = {}
            if not targets:
                continue
            for tier in ("inner", "mid", "outer", "extreme"):
                target = int(targets.get(tier, 0) or 0)
                if target <= 0:
                    continue
                rows = conn.execute(
                    "SELECT coin_id, amount_mojos FROM coins "
                    "WHERE wallet_type=? AND status='free' "
                    "  AND designation='tier_spare' "
                    "  AND assigned_tier=? "
                    "ORDER BY amount_mojos DESC",
                    (wt, tier),
                ).fetchall()
                if len(rows) <= target:
                    continue
                # Excess = rows.length - target. Demote the LARGEST
                # first (they have the most split potential).
                excess = len(rows) - target
                for r in rows[:excess]:
                    conn.execute(
                        "UPDATE coins SET designation='reserve', "
                        "assigned_tier='none', last_seen=? "
                        "WHERE coin_id=?",
                        (_now(), r["coin_id"]),
                    )
                    summary["demoted_overstock"] += 1

        total_changes = (summary["relabeled"] + summary["demoted_reserve"]
                         + summary["demoted_unknown"]
                         + summary["demoted_overstock"])
        log_event("debug", "tier_normalize_exit",
                  f"scanned xch={scanned['xch']} cat={scanned['cat']} "
                  f"relabeled={summary['relabeled']} "
                  f"→reserve={summary['demoted_reserve']} "
                  f"→unknown={summary['demoted_unknown']} "
                  f"overstock→reserve={summary['demoted_overstock']} "
                  f"total_changes={total_changes}")
        if total_changes > 0:
            try:
                conn.commit()
                # Steady-state suppression: if the SAME coins keep being
                # re-normalised every cycle (same counts) there's a deeper
                # convention mismatch elsewhere — the classifier writes the
                # "correct" label but something else re-writes it back. Log
                # that once, then DEBUG; the log was noisy otherwise.
                prev = getattr(self, "_last_normalize_summary", None)
                stable = (
                    prev is not None
                    and prev.get("relabeled") == summary["relabeled"]
                    and prev.get("demoted_reserve") == summary["demoted_reserve"]
                    and prev.get("demoted_unknown") == summary["demoted_unknown"]
                    and prev.get("demoted_overstock") == summary["demoted_overstock"]
                )
                log_event(
                    "debug" if stable else "info",
                    "tier_labels_normalized",
                    f"Tier label normalisation: "
                    f"relabeled={summary['relabeled']} "
                    f"→reserve={summary['demoted_reserve']} "
                    f"→unknown={summary['demoted_unknown']} "
                    f"overstock→reserve={summary['demoted_overstock']}",
                    data=summary,
                )
                self._last_normalize_summary = dict(summary)
            except Exception as _commit_err:
                log_event("warning", "tier_normalize_commit_failed",
                          f"Commit failed: {_commit_err}")
        return summary

    def get_inventory_summary(self) -> Dict:
        """Get a human-readable summary of the coin inventory."""
        xch_inv = self._xch_inventory
        cat_inv = self._cat_inventory

        xch_reserve_total = sum(_coin_amount(r) for r in xch_inv.get("reserve", []))
        cat_reserve_total = sum(_coin_amount(r) for r in cat_inv.get("reserve", []))
        xch_small_total = sum(_coin_amount(r) for r in xch_inv.get("small", []))
        cat_small_total = sum(_coin_amount(r) for r in cat_inv.get("small", []))

        summary = {
            "xch_reserve": len(xch_inv.get("reserve", [])),
            "xch_reserve_total": _format_amount_xch(xch_reserve_total),
            "xch_small": len(xch_inv.get("small", [])),
            "xch_small_total": _format_amount_xch(xch_small_total),
            "cat_reserve": len(cat_inv.get("reserve", [])),
            "cat_reserve_total": _format_amount_cat(cat_reserve_total, cfg.CAT_DECIMALS),
            "cat_small": len(cat_inv.get("small", [])),
            "cat_small_total": _format_amount_cat(cat_small_total, cfg.CAT_DECIMALS),
            "tier_enabled": cfg.TIER_ENABLED,
        }

        if cfg.TIER_ENABLED:
            # Tier-aware: show per-tier counts
            tier_names = self._configured_tier_names()
            for tier in tier_names:
                summary[f"xch_{tier}"] = len(xch_inv.get(tier, []))
                summary[f"cat_{tier}"] = len(cat_inv.get(tier, []))
            summary["xch_fees"] = len(xch_inv.get("fees", []))
            summary["cat_fees"] = len(cat_inv.get("fees", []))
            # Total trading = sum of all tier buckets
            summary["xch_trading"] = sum(summary[f"xch_{t}"] for t in tier_names)
            summary["cat_trading"] = sum(summary[f"cat_{t}"] for t in tier_names)
        else:
            summary["xch_trading"] = len(xch_inv.get("trading", []))
            summary["cat_trading"] = len(cat_inv.get("trading", []))
            summary["xch_fees"] = 0
            summary["cat_fees"] = 0

        # Locked coin data — coins held in active offers
        summary["xch_locked_coins"] = self._xch_locked_coins
        summary["xch_locked_amount"] = _format_amount_xch(self._xch_locked_amount)
        summary["xch_locked_amount_raw"] = self._xch_locked_amount
        summary["cat_locked_coins"] = self._cat_locked_coins
        summary["cat_locked_amount"] = _format_amount_cat(self._cat_locked_amount, cfg.CAT_DECIMALS)
        summary["cat_locked_amount_raw"] = self._cat_locked_amount
        summary["xch_total_coins"] = self._xch_total_coins
        summary["cat_total_coins"] = self._cat_total_coins

        return summary

    def check_tier_size_drift(
        self,
        low_ratio: Optional[float] = None,
        high_ratio: Optional[float] = None,
        min_sample: int = 2,
    ) -> List[Dict]:
        """Detect when prepared tier-coin sizes drift outside usable bounds.

        Smart Settings sizes coins for the price at prep time. If the live
        mid drifts, the same offer ladder needs differently-sized coins.
        Once the median prepped coin sits outside the selector-compatible
        band, live topup should reshape reserve and misfit coins into the
        new pool sizes. A full Coin Prep is only needed if the wallet cannot
        self-heal from existing reserves.

        Returns a list of finding dicts — one per (side, tier) that has
        drifted — with the median amount, live target size, ratio, and
        sample count. Empty list when everything is in range.
        """
        if not cfg.TIER_ENABLED:
            return []
        try:
            from database import get_coins_by_designation
        except Exception:
            return []

        low_ratio, high_ratio = _effective_tier_size_drift_bounds(low_ratio, high_ratio)
        findings: List[Dict] = []
        for wallet_type, is_cat in (("xch", False), ("cat", True)):
            try:
                live_sizes = get_tier_sizes_mojos_from_cfg(is_cat=is_cat)
            except Exception:
                continue
            if not live_sizes:
                continue
            for tier_name in ("inner", "mid", "outer", "extreme"):
                live_size = int(live_sizes.get(tier_name, 0) or 0)
                if live_size <= 0:
                    continue
                try:
                    coins = get_coins_by_designation(
                        wallet_type, "tier_spare", tier_name
                    )
                except Exception:
                    continue
                amounts = sorted(
                    int(c.get("amount_mojos") or 0)
                    for c in coins
                    if int(c.get("amount_mojos") or 0) > 0
                    and str(c.get("status") or "free").lower() == "free"
                )
                if len(amounts) < max(1, int(min_sample)):
                    continue
                n = len(amounts)
                if n % 2 == 1:
                    median = float(amounts[n // 2])
                else:
                    median = (amounts[n // 2 - 1] + amounts[n // 2]) / 2.0
                ratio = float(median) / float(live_size)
                if ratio < low_ratio or ratio > high_ratio:
                    findings.append({
                        "side": wallet_type,
                        "tier": tier_name,
                        "median_mojos": int(median),
                        "live_size_mojos": live_size,
                        "ratio": round(ratio, 3),
                        "coin_count": n,
                    })
        return findings

    def get_free_coin_counts(self, active_buy_count: int = 0,
                              active_sell_count: int = 0) -> Dict[str, int]:
        """Get truly free coins (spendable minus active offers).

        Includes active reservation totals as informational fields so callers
        and the GUI can see how much capacity is currently reserved by
        in-flight offer creation attempts across threads.

        Wallet-type guard: under Sage, ``self._xch_coins`` is already the
        count of UNLOCKED coins — Sage's owned-coin snapshot puts coins
        with an ``offer_id`` into a separate ``locked_ids`` set, and
        ``selectable_records`` (the source of ``self._xch_coins``)
        excludes them. Subtracting ``active_buy_count`` then double-counts
        the lock and produces ``free_xch = 0`` whenever the bot has more
        active offers than spare coins — exactly the steady state of a
        live ladder. That triggers a false-positive "Coin headroom is low"
        alert in runtime_monitor every cycle. Detect that condition
        (active count > spendable count is only possible when spendable
        already excluded the locks) and skip the subtraction. The legacy
        chia-full-wallet path, where spendable did include offer-locked
        coins, still uses the subtraction.
        """
        if active_buy_count > self._xch_coins:
            free_xch = self._xch_coins
        else:
            free_xch = max(0, self._xch_coins - active_buy_count)
        if active_sell_count > self._cat_coins:
            free_cat = self._cat_coins
        else:
            free_cat = max(0, self._cat_coins - active_sell_count)

        # Fetch active reservation totals (mojos held by in-flight creates).
        # Fail-open: reservation data is additive, not critical path.
        reserved_xch_mojos = 0
        reserved_cat_mojos = 0
        try:
            from reservation_manager import ReservationManager as _RM
            _totals = _RM().get_reserved_totals()
            reserved_xch_mojos = _totals.get("xch", 0)
            reserved_cat_mojos = _totals.get("cat", 0)
        except Exception:
            pass

        return {
            "xch_spendable": self._xch_coins,
            "cat_spendable": self._cat_coins,
            "xch_free": free_xch,
            "cat_free": free_cat,
            "active_buy": active_buy_count,
            "active_sell": active_sell_count,
            # Active reservation amounts (in mojos) — informational only.
            # These represent capacity held by in-flight offer creation threads.
            "reserved_xch_mojos": reserved_xch_mojos,
            "reserved_cat_mojos": reserved_cat_mojos,
        }

    def coin_readiness_report(self) -> Dict:
        """Produce a detailed coin readiness report showing per-tier availability
        vs requirements. Called at startup so the bot knows exactly what coins
        are available before creating any offers.

        Returns a dict with:
          - per-tier status (available, needed, active_slots, spare, status)
          - overall_ready: True if all tiers have enough coins for active offers
          - overall_status: "READY", "SPARE_BUFFER_LOW", or "CRITICAL"
        """
        report = {"tiers": {}, "overall_ready": True, "overall_status": "READY"}

        if not cfg.TIER_ENABLED:
            # Non-tiered: simple check
            xch_trading = len(self._xch_inventory.get("trading", []))
            cat_trading = len(self._cat_inventory.get("trading", []))
            target_xch = cfg.MAX_ACTIVE_BUY_OFFERS if cfg.ENABLE_BUY else 0
            target_cat = cfg.MAX_ACTIVE_SELL_OFFERS if cfg.ENABLE_SELL else 0
            report["xch_trading"] = xch_trading
            report["cat_trading"] = cat_trading
            report["xch_needed"] = target_xch
            report["cat_needed"] = target_cat
            if xch_trading < target_xch or cat_trading < target_cat:
                report["overall_ready"] = False
                report["overall_status"] = "LOW"
            log_event("info", "coin_readiness",
                      f"COIN READINESS: XCH {xch_trading}/{target_xch} trading, "
                      f"CAT {cat_trading}/{target_cat} trading — "
                      f"Status: {report['overall_status']}")
            return report

        # ---- Tiered readiness ----
        max_buy_offers = int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25) or 25)
        max_sell_offers = int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25) or 25)
        # Per-side distributions: buy and sell can now have independent
        # live tier counts (BUY_*_TIER_COUNT vs SELL_*_TIER_COUNT).
        xch_dist = get_tier_distribution(max_buy_offers, side="xch")
        cat_dist = get_tier_distribution(max_sell_offers, side="cat")
        # Shared dist (max of both sides) is what callers reading
        # tier_info["slots_per_side"] expect for display purposes.
        tier_dist = {
            t: max(int(xch_dist.get(t, 0) or 0), int(cat_dist.get(t, 0) or 0))
            for t in ("inner", "mid", "outer", "extreme")
        }
        multiplier = getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0"))
        prepared_xch = get_weighted_tier_prep_counts(
            max_buy_offers, multiplier, side="xch")
        prepared_cat = get_weighted_tier_prep_counts(
            max_sell_offers, multiplier, side="cat")

        any_critical = False
        any_low = False

        # Pre-compute locked-coin counts per tier so the status check can
        # treat "all coins locked in active offers" as healthy deployment
        # instead of flagging EMPTY. Without this, the startup re-check
        # fires CRITICAL the moment the first ladder goes out.
        _locked_by_tier: Dict[str, Dict[str, int]] = {"xch": {}, "cat": {}}
        try:
            from database import get_connection as _get_conn
            _conn = _get_conn()
            for _wt in ("xch", "cat"):
                for _tn in ("inner", "mid", "outer", "extreme"):
                    _locked_by_tier[_wt][_tn] = int(_conn.execute(
                        "SELECT COUNT(*) FROM coins "
                        "WHERE status='locked' AND wallet_type=? AND assigned_tier=?",
                        (_wt, _tn),
                    ).fetchone()[0] or 0)
        except Exception:
            pass

        for tier_name in ["inner", "mid", "outer", "extreme"]:
            slots_per_side = tier_dist.get(tier_name, 0)

            # XCH coins are for BUY offers, CAT coins are for SELL offers.
            # Each asset uses ITS OWN side's tier distribution.
            xch_needed = int(xch_dist.get(tier_name, 0) or 0) if cfg.ENABLE_BUY else 0
            cat_needed = int(cat_dist.get(tier_name, 0) or 0) if cfg.ENABLE_SELL else 0
            xch_target = int(prepared_xch.get(tier_name, 0) or 0) if cfg.ENABLE_BUY else 0
            cat_target = int(prepared_cat.get(tier_name, 0) or 0) if cfg.ENABLE_SELL else 0
            xch_spare = xch_target - xch_needed
            cat_spare = cat_target - cat_needed
            active_needed = xch_needed + cat_needed  # Total for summary

            xch_have = len(self._xch_inventory.get(tier_name, []))
            cat_have = len(self._cat_inventory.get(tier_name, []))
            xch_locked = int(_locked_by_tier.get("xch", {}).get(tier_name, 0))
            cat_locked = int(_locked_by_tier.get("cat", {}).get(tier_name, 0))

            # F62: Status per asset — compare the free pool against the SPARE
            # BUFFER target, not the slot count. The old check compared `have`
            # to `needed` (slot count), which meant every tier flipped to [LOW]
            # the moment the ladder deployed — because after deployment, the
            # slot coins are locked in offers and only the spare buffer is in
            # _xch_inventory. The correct "healthy" state is: the spare buffer
            # is still intact. Pre-deploy `have` is the full pool (>= spare
            # target, so READY). Post-deploy `have` is the spare buffer (>=
            # spare target iff buffer is intact).
            xch_spare_target = max(0, xch_target - xch_needed)
            cat_spare_target = max(0, cat_target - cat_needed)

            def _compute_status(have: int, locked: int, slot_need: int,
                                spare_tgt: int, enabled: bool) -> str:
                if not enabled or slot_need == 0:
                    return "READY"
                if spare_tgt <= 0:
                    # No spares configured — coin count == slot count. Count
                    # locked coins too, otherwise the tier flips EMPTY the
                    # moment the ladder deploys (all coins move to locked).
                    covered = have + locked
                    if covered >= slot_need:
                        return "READY"
                    return "LOW" if covered > 0 else "EMPTY"
                if have >= spare_tgt:
                    return "READY"
                if have > 0:
                    return "LOW"
                return "EMPTY"

            xch_status = _compute_status(xch_have, xch_locked, xch_needed,
                                         xch_spare_target, cfg.ENABLE_BUY)
            cat_status = _compute_status(cat_have, cat_locked, cat_needed,
                                         cat_spare_target, cfg.ENABLE_SELL)

            # F62: Spare buffer remaining — after a full deployment, the entire
            # free pool IS the spare buffer (slot coins are locked). Before
            # deployment, `have` also includes the slot coins, so we subtract
            # them. Detect which phase by comparing `have` against the total
            # target: if have >= target, we haven't deployed yet.
            if xch_have >= xch_target and xch_target > 0:
                xch_spare_remaining = max(0, xch_have - xch_needed)
            else:
                xch_spare_remaining = xch_have
            if cat_have >= cat_target and cat_target > 0:
                cat_spare_remaining = max(0, cat_have - cat_needed)
            else:
                cat_spare_remaining = cat_have

            tier_info = {
                "slots_per_side": slots_per_side,
                "active_needed": active_needed,
                "total_prepped": xch_target + cat_target,
                "spare_target": xch_spare + cat_spare,
                "xch_available": xch_have,
                "xch_needed": xch_needed,
                "cat_available": cat_have,
                "cat_needed": cat_needed,
                "xch_spare_remaining": xch_spare_remaining,
                "cat_spare_remaining": cat_spare_remaining,
                "xch_status": xch_status,
                "cat_status": cat_status,
            }
            report["tiers"][tier_name] = tier_info

            if xch_status == "EMPTY" or cat_status == "EMPTY":
                any_critical = True
            elif xch_status == "LOW" or cat_status == "LOW":
                any_low = True

            # F62: Show free-pool size vs total prep target (not vs slot count).
            # Post-deploy, the slot coins are locked in offers, so `have` drops
            # to just the spare buffer. Showing `have/target` (e.g. 1/4) is
            # still a bit terse — so we also print an explicit spare summary
            # that makes the buffer state unambiguous.
            log_event("debug", "coin_readiness",
                      f"  {tier_name.upper():>8}: "
                      f"XCH {xch_have:>3}/{xch_target} [{xch_status}] | "
                      f"CAT {cat_have:>3}/{cat_target} [{cat_status}] | "
                      f"Spares: XCH {xch_spare_remaining}/{xch_spare_target}, "
                      f"CAT {cat_spare_remaining}/{cat_spare_target}")

        if self._sniper_pool_enabled():
            sniper_target = int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0)
            sniper_xch_have = len(self._xch_inventory.get("sniper", []))
            sniper_cat_have = len(self._cat_inventory.get("sniper", []))
            # F67: Count locked sniper coins too — a sniper coin locked in an
            # active offer is still part of the pool (just doing its job). The
            # old check only counted FREE coins, so firing 1 probe dropped the
            # count to SNIPER_PREP_COUNT-1 and triggered a false LOW_SPARES.
            try:
                from database import get_connection as _get_conn
                _conn = _get_conn()
                _locked_sniper_xch = _conn.execute(
                    "SELECT COUNT(*) FROM coins WHERE status='locked' AND assigned_tier='sniper' AND wallet_type='xch'"
                ).fetchone()[0]
                _locked_sniper_cat = _conn.execute(
                    "SELECT COUNT(*) FROM coins WHERE status='locked' AND assigned_tier='sniper' AND wallet_type='cat'"
                ).fetchone()[0]
            except Exception:
                _locked_sniper_xch = 0
                _locked_sniper_cat = 0
            sniper_xch_have += int(_locked_sniper_xch or 0)
            sniper_cat_have += int(_locked_sniper_cat or 0)
            sniper_xch_needed = sniper_target if cfg.ENABLE_BUY else 0
            # Snipers are XCH-only (opportunistic buys) — no CAT sniper coins needed
            sniper_cat_needed = 0
            sniper_xch_status = "READY" if sniper_xch_have >= sniper_xch_needed else ("LOW" if sniper_xch_have > 0 else "EMPTY")
            sniper_cat_status = "READY" if sniper_cat_have >= sniper_cat_needed else ("LOW" if sniper_cat_have > 0 else "EMPTY")
            report["tiers"]["sniper"] = {
                "slots_per_side": 0,
                "active_needed": 0,
                "total_prepped": sniper_target * 2,
                "spare_target": sniper_target * 2,
                "xch_available": sniper_xch_have,
                "xch_needed": sniper_xch_needed,
                "cat_available": sniper_cat_have,
                "cat_needed": sniper_cat_needed,
                "xch_spare_remaining": sniper_xch_have,
                "cat_spare_remaining": sniper_cat_have,
                "xch_status": sniper_xch_status if cfg.ENABLE_BUY else "READY",
                "cat_status": sniper_cat_status if cfg.ENABLE_SELL else "READY",
            }
            if (cfg.ENABLE_BUY and sniper_xch_status == "EMPTY") or (cfg.ENABLE_SELL and sniper_cat_status == "EMPTY"):
                any_critical = True
            elif (cfg.ENABLE_BUY and sniper_xch_status == "LOW") or (cfg.ENABLE_SELL and sniper_cat_status == "LOW"):
                any_low = True
            log_event("debug", "coin_readiness",
                      f"  {'SNIPER':>8}: "
                      f"XCH {sniper_xch_have:>3}/{sniper_xch_needed} [{sniper_xch_status}] | "
                      f"CAT {sniper_cat_have:>3}/{sniper_cat_needed} [{sniper_cat_status}] | "
                      f"Dedicated pool")

        if self._fee_pool_enabled():
            fee_target = get_fee_pool_count()
            fee_have = len(self._xch_inventory.get("fees", []))
            # F67: Count locked fee coins too — same as snipers, a fee coin
            # locked in an active offer is still part of the pool.
            try:
                from database import get_connection as _get_conn
                _locked_fees = _get_conn().execute(
                    "SELECT COUNT(*) FROM coins WHERE status='locked' AND assigned_tier='fees' AND wallet_type='xch'"
                ).fetchone()[0]
            except Exception:
                _locked_fees = 0
            fee_have += int(_locked_fees or 0)
            fee_status = "READY" if fee_have >= fee_target else ("LOW" if fee_have > 0 else "EMPTY")
            report["tiers"]["fees"] = {
                "slots_per_side": 0,
                "active_needed": 0,
                "total_prepped": fee_target,
                "spare_target": fee_target,
                "xch_available": fee_have,
                "xch_needed": fee_target,
                "cat_available": 0,
                "cat_needed": 0,
                "xch_spare_remaining": fee_have,
                "cat_spare_remaining": 0,
                "xch_status": fee_status,
                "cat_status": "READY",
            }
            if fee_status == "EMPTY":
                any_critical = True
            elif fee_status == "LOW":
                any_low = True
            log_event(
                "debug",
                "coin_readiness",
                f"  {'FEES':>8}: XCH {fee_have:>3}/{fee_target} [{fee_status}] | "
                f"Fee coins at {str(get_fee_coin_size_xch())} XCH each",
            )

        if any_critical:
            report["overall_ready"] = False
            report["overall_status"] = "CRITICAL"
        elif any_low:
            # Low free spares means the buffer is thin, not that active offer
            # slots are unusable. Topup/drip can replenish this in-session, so
            # keep startup readiness true and report it as buffer state.
            report["overall_ready"] = True
            report["overall_status"] = "SPARE_BUFFER_LOW"

        # Summary line
        total_xch = sum(t["xch_available"] for t in report["tiers"].values())
        total_cat = sum(t["cat_available"] for t in report["tiers"].values())
        total_needed = sum(t["active_needed"] for t in report["tiers"].values())
        total_target = sum(t["total_prepped"] for t in report["tiers"].values())

        log_event("info", "coin_readiness",
                  f"COIN READINESS: {total_xch} XCH + {total_cat} CAT trading coins | "
                  f"Active slots: {total_needed} | Target (with spares): {total_target} | "
                  f"Multiplier: {multiplier}x | Status: {report['overall_status']}")

        return report

    def log_inventory(self, reason: str = "periodic"):
        """Log the current coin inventory to the console/SSE.

        Args:
            reason: What triggered this log — used to tag the event so we
                    can see the coin state at every stage of the lifecycle.
                    Values: 'startup', 'offer_created', 'offer_cancelled',
                            'offer_filled', 'coin_prep', 'topup', 'periodic'
        """
        inv = self.get_inventory_summary()

        # Locked coin summary line (always shown)
        locked_line = (
            f" || LOCKED: XCH {inv.get('xch_locked_coins', 0)} coins "
            f"({inv.get('xch_locked_amount', '0')}), "
            f"CAT {inv.get('cat_locked_coins', 0)} coins "
            f"({inv.get('cat_locked_amount', '0')})"
        )

        if inv.get("tier_enabled"):
            # V3: Show per-tier spare counts from designation system
            xch_spares = self._tier_spares.get("xch", {})
            cat_spares = self._tier_spares.get("cat", {})
            pace = self.get_trading_pace()

            xch_tier_detail = " | ".join(
                f"{t}: {inv.get(f'xch_{t}', 0)} total, {xch_spares.get(t, 0)} spare"
                for t in self._configured_tier_names())
            cat_tier_detail = " | ".join(
                f"{t}: {inv.get(f'cat_{t}', 0)} total, {cat_spares.get(t, 0)} spare"
                for t in self._configured_tier_names())
            fee_detail = ""
            if self._fee_pool_enabled():
                fee_detail = f" | fees: {inv.get('xch_fees', 0)} total, {xch_spares.get('fees', 0)} spare"

            log_event("info", "coin_inventory",
                      f"[{reason.upper()}] XCH: topup_pool={inv['xch_reserve']} "
                      f"({inv['xch_reserve_total']} XCH) | {xch_tier_detail}{fee_detail} | "
                      f"dust={inv['xch_small']} | "
                      f"CAT: topup_pool={inv['cat_reserve']} | {cat_tier_detail} | "
                      f"dust={inv['cat_small']} | pace={pace}"
                      f"{locked_line}")
        else:
            log_event("info", "coin_inventory",
                      f"[{reason.upper()}] FREE — "
                      f"XCH: {inv['xch_reserve']} topup_pool ({inv['xch_reserve_total']} XCH), "
                      f"{inv['xch_trading']} trading, {inv['xch_small']} small ({inv['xch_small_total']} XCH) | "
                      f"CAT: {inv['cat_reserve']} topup_pool ({inv['cat_reserve_total']}), "
                      f"{inv['cat_trading']} trading, {inv['cat_small']} small ({inv['cat_small_total']})"
                      f"{locked_line}")

    def snapshot_coins(self, reason: str = "check") -> Dict:
        """Take a fresh coin snapshot, classify, and log with reason tag.

        This is the main entry point for lifecycle tracking — call it
        whenever something happens that changes the coin state:
          - Bot startup → snapshot_coins("startup")
          - After creating offers → snapshot_coins("offer_created")
          - After detecting fills → snapshot_coins("offer_filled")
          - After cancelling offers → snapshot_coins("offer_cancelled")
          - After coin prep/topup → snapshot_coins("coin_prep")

        Detects and logs which specific coins appeared or disappeared since
        the last snapshot, giving full visibility into coin state changes.

        Returns the inventory summary dict.
        """
        # Don't re-scan during active topup/prep (would interfere)
        if not (self._prep_running or self._topup_running):
            self.update_coin_counts()

        # --- Coin change detection ---
        self._detect_coin_changes(reason)

        self.log_inventory(reason=reason)
        return self.get_inventory_summary()

    def _detect_coin_changes(self, reason: str):
        """Compare current coin IDs to previous snapshot and log changes.

        Tracks which coins were created (new IDs) and destroyed (missing IDs)
        since the last call. This gives full visibility into the UTXO lifecycle.
        """
        # Build current coin ID sets
        current_xch_ids = set()
        current_cat_ids = set()

        for _category, records in self._xch_inventory.items():
            for rec in records:
                coin_id = _coin_id_from_record(rec)
                if coin_id:
                    current_xch_ids.add(coin_id)

        for _category, records in self._cat_inventory.items():
            for rec in records:
                coin_id = _coin_id_from_record(rec)
                if coin_id:
                    current_cat_ids.add(coin_id)

        # Compare to previous snapshot — consolidated into a single log line
        # to avoid noisy per-coin logging that clutters the system log
        # during price adjustments (multiple snapshot_coins calls per cycle).
        _changes = []
        if hasattr(self, '_prev_xch_coin_ids') and self._prev_xch_coin_ids is not None:
            new_xch = current_xch_ids - self._prev_xch_coin_ids
            gone_xch = self._prev_xch_coin_ids - current_xch_ids
            if new_xch or gone_xch:
                _changes.append(
                    f"XCH +{len(new_xch)}/-{len(gone_xch)} "
                    f"({len(self._prev_xch_coin_ids)}→{len(current_xch_ids)})"
                )

        if hasattr(self, '_prev_cat_coin_ids') and self._prev_cat_coin_ids is not None:
            new_cat = current_cat_ids - self._prev_cat_coin_ids
            gone_cat = self._prev_cat_coin_ids - current_cat_ids
            if new_cat or gone_cat:
                _changes.append(
                    f"CAT +{len(new_cat)}/-{len(gone_cat)} "
                    f"({len(self._prev_cat_coin_ids)}→{len(current_cat_ids)})"
                )

        if _changes:
            # Use debug for routine requote snapshots, info for fills/startup
            _level = "info" if reason in ("startup", "offer_filled", "coin_prep") else "debug"
            log_event(_level, "coin_state_change",
                      f"[{reason.upper()}] {' | '.join(_changes)}")

        # Save for next comparison
        self._prev_xch_coin_ids = current_xch_ids
        self._prev_cat_coin_ids = current_cat_ids

    # -------------------------------------------------------------------
    # Coin prep threshold checks
    # -------------------------------------------------------------------

    def needs_coin_prep(self, active_buy_count: int = 0,
                        active_sell_count: int = 0) -> bool:
        """Check if FULL coin prep needed (total coins critically low).

        Target is calculated dynamically — same logic as start_coin_prep() —
        so the 10% threshold is always relative to what was actually prepped,
        not a stale XCH_TARGET_COINS value from .env (which Smart Defaults
        never updates).
        """
        if not cfg.ENABLE_COIN_PREP:
            return False
        if self._prep_running or self._topup_running:
            return False

        est_xch_total = self._xch_coins + active_buy_count
        est_cat_total = self._cat_coins + active_sell_count

        # --- Side-scope awareness ---
        # LIQUIDITY_MODE controls which sides the bot actually quotes.
        # buy_only → XCH coins are used, CAT coins are not.
        # sell_only → CAT coins are used, XCH coins are not.
        # Without this check the bot would keep trying to prep full
        # inventory on a disabled side, failing, and re-firing needs_prep
        # every cycle — wasting wallet operations and log noise.
        buy_enabled = bool(getattr(cfg, "ENABLE_BUY", True))
        sell_enabled = bool(getattr(cfg, "ENABLE_SELL", True))

        # --- Dynamic target: mirrors start_coin_prep() logic ---
        max_buy = int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25))
        max_sell = int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25))
        multiplier = getattr(cfg, "COIN_PREP_MULTIPLIER", 1.0)
        if cfg.TIER_ENABLED:
            xch_tier_counts = get_weighted_tier_prep_counts(
                max_buy, multiplier, side="xch")
            cat_tier_counts = get_weighted_tier_prep_counts(
                max_sell, multiplier, side="cat")
            target_xch = max(cfg.XCH_TARGET_COINS, sum(xch_tier_counts.values()))
            target_cat = max(cfg.CAT_TARGET_COINS, sum(cat_tier_counts.values()))
        else:
            computed = int((max_buy + max_sell) * float(multiplier))
            computed = max(computed, max_buy + max_sell)
            # Fall back to cfg value if it's larger (user may have over-ridden)
            target_xch = max(cfg.XCH_TARGET_COINS, computed)
            target_cat = max(cfg.CAT_TARGET_COINS, computed)

        # Zero out the target for a disabled side so needs_coin_prep()
        # does not keep reporting that it "needs" inventory we will never
        # use. A small positive floor for the enabled side is left intact
        # below via the `if target > 0` guard.
        if not buy_enabled:
            target_xch = 0
        if not sell_enabled:
            target_cat = 0

        needs_xch = (buy_enabled and target_xch > 0
                     and est_xch_total < int(target_xch * 0.1))
        needs_cat = (sell_enabled and target_cat > 0
                     and est_cat_total < int(target_cat * 0.1))

        if needs_xch or needs_cat:
            log_event("warning", "low_coins_total",
                      f"LOW COINS! XCH: {self._xch_coins} spendable + {active_buy_count} in offers = "
                      f"{est_xch_total}/{target_xch}, CAT: {self._cat_coins} spendable + "
                      f"{active_sell_count} in offers = {est_cat_total}/{target_cat}")
        return needs_xch or needs_cat

    def needs_topup(self, active_buy_count: int = 0,
                    active_sell_count: int = 0) -> bool:
        """Check if live coin top-up should run (free coins low).

        V3 ADAPTIVE: Uses trading pace to adjust the trigger threshold.
        Busy market → trigger earlier (50% spares). Slow → later (20%).
        """
        self._topup_needed_wallet_types = set()

        # F9 fix (2026-04-08): topup worker heartbeat watchdog. If the
        # worker thread crashed mid-run (uncaught exception OUTSIDE the
        # outer try/except), `_topup_running` would stay True forever and
        # `needs_topup()` would return False on every subsequent call —
        # silent stall. We now also check that the thread is actually
        # alive; if it's dead but the flag is still True, we clear the
        # flag, log a critical alert, and let the next call decide.
        if self._topup_running:
            try:
                _t = getattr(self, "_topup_thread", None)
                if _t is not None and not _t.is_alive():
                    with self._lock:
                        self._topup_running = False
                        self._topup_thread = None
                    log_event(
                        "error",
                        "topup_worker_zombie_cleared",
                        "CRITICAL: topup worker flag was True but thread was "
                        "dead — clearing flag so topups can resume. The worker "
                        "likely crashed without unwinding its outer try/except. "
                        "Investigate the previous superlog for an unhandled "
                        "exception in coin-topup thread."
                    )
                    # Fall through — re-evaluate cooldown and trigger fresh
                else:
                    return False
            except Exception:
                # If the watchdog itself errors, fall back to old behaviour
                # (return False) rather than risk firing two concurrent topups
                return False
        if self._prep_running:
            return False
        if self._topup_budget_backoff_active():
            return False

        # Cooldown — exponential when no coins are available
        if self._no_coins_backoff:
            cooldown = min(
                _TOPUP_BACKOFF_MAX,
                _TOPUP_BACKOFF_BASE * (2 ** self._no_coins_backoff_count),
            )
        else:
            cooldown = _TOPUP_COOLDOWN
        _emergency_ready = (time.time() - self._last_topup_time >= cooldown)
        _drip_ready = (time.time() - self._last_drip_time >= _TOPUP_DRIP_INTERVAL)
        if not _emergency_ready and not _drip_ready:
            return False

        # V4: Per-tier trigger percentages (source of truth = final settings).
        # Each tier has its own trigger % relative to its configured spare
        # pool size. Inner has the biggest pool and is hit most often →
        # triggers earliest. Extreme has the smallest pool and is hit least
        # often → tolerates the deepest drawdown before triggering.
        pace = self.get_trading_pace()
        if getattr(cfg, "TIER_TRIGGER_PACE_SCALE", True):
            if pace == 'busy':
                pace_scale = 1.4
            elif pace == 'slow':
                pace_scale = 0.7
            else:
                pace_scale = 1.0
        else:
            pace_scale = 1.0

        # The trigger percentages are defined by SLOT POSITION (= how often
        # that ladder position gets hit), NOT by coin size. For the sell side
        # and for non-reversed buy, slot position == coin size tier. For a
        # reversed buy ladder the two are swapped (inner slot uses extreme
        # coins, extreme slot uses inner coins, etc.). We use the same flip
        # helper that the rest of the codebase already uses so everything
        # stays consistent.
        def _position_pct(position_tier: str) -> float:
            base = {
                "inner":   getattr(cfg, "TIER_TRIGGER_PCT_INNER", 50),
                "mid":     getattr(cfg, "TIER_TRIGGER_PCT_MID", 40),
                "outer":   getattr(cfg, "TIER_TRIGGER_PCT_OUTER", 25),
                "extreme": getattr(cfg, "TIER_TRIGGER_PCT_EXTREME", 15),
                "sniper":  getattr(cfg, "TIER_TRIGGER_PCT_SNIPER", 40),
                "fees":    getattr(cfg, "TIER_TRIGGER_PCT_FEES", 30),
            }.get(position_tier, 30)
            scaled = (float(base) / 100.0) * pace_scale
            # Clamp to [0.05, 0.95] to avoid degenerate settings
            return max(0.05, min(0.95, scaled))

        def _pct_for_coin_size_tier(coin_size_tier: str, wallet_side: str) -> float:
            """Return the trigger pct for a given coin-size pool, using the
            slot-position semantics of the side. On reversed buy this
            translates the coin-size pool name back to its slot position
            via the shared flip helper.
            """
            # wallet_side: "xch" (buy-funded) or "cat" (sell-funded)
            ladder_side = "buy" if wallet_side == "xch" else "sell"
            try:
                # coin_size_tier_for_slot_position is its own inverse because
                # the reversed-buy map is a symmetric swap (inner↔extreme,
                # mid↔outer). So applying it to a coin-size tier yields the
                # slot position that uses that coin size.
                position_tier = coin_size_tier_for_slot_position(
                    coin_size_tier, side=ladder_side
                )
            except Exception:
                position_tier = coin_size_tier
            return _position_pct(position_tier)

        # Kept for legacy logging / fee fallback (still position-based)
        spare_keep_pct = _position_pct("inner")

        multiplier = getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0"))

        if cfg.TIER_ENABLED:
            # V4: Check per-tier spare counts using per-tier percentages.
            needs_any = False
            trigger_wallet_types: set[str] = set()
            trigger_log: list = []
            trigger_source_checks: list[tuple[str, str, int, int, int]] = []
            max_buy_offers = int(getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25) or 25)
            max_sell_offers = int(getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25) or 25)
            xch_dist = get_tier_distribution(max_buy_offers, side="xch")
            cat_dist = get_tier_distribution(max_sell_offers, side="cat")
            prepared_xch = get_weighted_tier_prep_counts(
                max_buy_offers, multiplier, side="xch")
            prepared_cat = get_weighted_tier_prep_counts(
                max_sell_offers, multiplier, side="cat")

            for tier_name in ("inner", "mid", "outer", "extreme"):
                # XCH coins serve buy offers, CAT coins serve sell offers.
                # Each wallet uses ITS OWN side's live tier count.
                xch_slots = int(xch_dist.get(tier_name, 0) or 0)
                cat_slots = int(cat_dist.get(tier_name, 0) or 0)
                xch_prepped = int(prepared_xch.get(tier_name, 0) or 0)
                cat_prepped = int(prepared_cat.get(tier_name, 0) or 0)
                xch_spare_target = max(0, xch_prepped - xch_slots)
                cat_spare_target = max(0, cat_prepped - cat_slots)

                # Current spares from DB (only free tier_spare coins)
                xch_spares_now = self._tier_spares.get("xch", {}).get(tier_name, 0)
                cat_spares_now = self._tier_spares.get("cat", {}).get(tier_name, 0)

                # Per-tier trigger pct is slot-position-based. On a reversed
                # buy ladder the buy (xch) side translates coin-size back to
                # slot position so high-traffic positions get the high
                # trigger pct regardless of which coin-size pool serves them.
                xch_pct = _pct_for_coin_size_tier(tier_name, "xch")
                cat_pct = _pct_for_coin_size_tier(tier_name, "cat")
                # Threshold is relative to THIS tier's starting pool size.
                # If the pool size is 0 (tier not configured), skip it.
                xch_threshold = max(1, int(round(xch_spare_target * xch_pct))) if xch_spare_target > 0 else 0
                cat_threshold = max(1, int(round(cat_spare_target * cat_pct))) if cat_spare_target > 0 else 0

                xch_trip = (xch_threshold > 0 and xch_spares_now < xch_threshold and cfg.ENABLE_BUY)
                cat_trip = (cat_threshold > 0 and cat_spares_now < cat_threshold and cfg.ENABLE_SELL)

                if xch_trip or cat_trip:
                    needs_any = True
                    if xch_trip:
                        trigger_wallet_types.add("xch")
                        trigger_source_checks.append((
                            "xch", tier_name, xch_spares_now,
                            xch_spare_target, xch_threshold,
                        ))
                    if cat_trip:
                        trigger_wallet_types.add("cat")
                        trigger_source_checks.append((
                            "cat", tier_name, cat_spares_now,
                            cat_spare_target, cat_threshold,
                        ))
                    trigger_log.append(
                        f"{tier_name} "
                        f"xch@{int(xch_pct*100)}%={xch_spares_now}/{xch_spare_target}(<{xch_threshold}) "
                        f"cat@{int(cat_pct*100)}%={cat_spares_now}/{cat_spare_target}(<{cat_threshold})"
                    )
                    # Don't break — collect all trips for better log context
                    continue

            if not needs_any and self._sniper_pool_enabled():
                sniper_target = int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0)
                # Sniper is its own pool — not flipped by reverse-buy.
                sniper_pct = _position_pct("sniper")
                sniper_threshold = max(1, int(round(sniper_target * sniper_pct))) if sniper_target > 0 else 0
                sniper_xch_now = self._tier_spares.get("xch", {}).get("sniper", 0)
                sniper_cat_now = self._tier_spares.get("cat", {}).get("sniper", 0)
                if sniper_threshold > 0 and (
                    (cfg.ENABLE_BUY and sniper_xch_now < sniper_threshold) or
                    (cfg.ENABLE_SELL and sniper_cat_now < sniper_threshold)
                ):
                    needs_any = True
                    if cfg.ENABLE_BUY and sniper_xch_now < sniper_threshold:
                        trigger_wallet_types.add("xch")
                        trigger_source_checks.append((
                            "xch", "sniper", sniper_xch_now,
                            sniper_target, sniper_threshold,
                        ))
                    if cfg.ENABLE_SELL and sniper_cat_now < sniper_threshold:
                        trigger_wallet_types.add("cat")
                        trigger_source_checks.append((
                            "cat", "sniper", sniper_cat_now,
                            sniper_target, sniper_threshold,
                        ))
                    trigger_log.append(
                        f"sniper@{int(sniper_pct*100)}% "
                        f"xch={sniper_xch_now}/{sniper_target}(<{sniper_threshold}) "
                        f"cat={sniper_cat_now}/{sniper_target}(<{sniper_threshold})"
                    )

            if not needs_any and self._fee_pool_enabled():
                fee_target = get_fee_pool_count()
                # Fee pool is its own XCH-only pool — not flipped by reverse-buy.
                fee_pct = _position_pct("fees")
                fee_threshold = max(1, int(round(fee_target * fee_pct))) if fee_target > 0 else 0
                fee_xch_now = self._tier_spares.get("xch", {}).get("fees", 0)
                if fee_threshold > 0 and fee_xch_now < fee_threshold:
                    needs_any = True
                    trigger_wallet_types.add("xch")
                    trigger_source_checks.append((
                        "xch", "fees", fee_xch_now,
                        fee_target, fee_threshold,
                    ))
                    trigger_log.append(
                        f"fees@{int(fee_pct*100)}% "
                        f"xch={fee_xch_now}/{fee_target}(<{fee_threshold})"
                    )

            if needs_any and _emergency_ready:
                source_ready_wallets: set[str] = set()
                for wallet_type, tier_name, spares_now, spare_target, threshold in (
                    trigger_source_checks
                ):
                    try:
                        if tier_name == get_fee_tier_name():
                            target_size_mojos = int(get_fee_coin_size_mojos())
                        else:
                            target_size_mojos = int(
                                self._get_tier_sizes_mojos(
                                    is_cat=(wallet_type == "cat")
                                ).get(tier_name, 0) or 0
                            )
                    except Exception:
                        target_size_mojos = 0
                    if self._optional_topup_source_available(
                        wallet_type,
                        target_size_mojos,
                    ):
                        source_ready_wallets.add(wallet_type)
                        continue
                    label = wallet_type.upper()
                    self._log_topup_source_unavailable(
                        f"{wallet_type}:{tier_name}",
                        f"{label} {tier_name} topup waiting: "
                        f"{spares_now}/{spare_target} spares "
                        f"(emergency threshold <{threshold}) but no "
                        f"{label} reserve or useful small coins are "
                        "available to split",
                    )

                if trigger_source_checks:
                    trigger_wallet_types.intersection_update(source_ready_wallets)
                    if not trigger_wallet_types:
                        return False

                log_event("info", "low_coins_adaptive",
                          f"Per-tier trigger fired (pace={pace}, scale={pace_scale:.2f}x). "
                          f"Trips: {'; '.join(trigger_log) if trigger_log else 'n/a'}")
                self._topup_is_drip = False
                self._topup_needed_wallet_types = set(trigger_wallet_types)
                return True

            # Drip check — proactively replenish tiers trending toward emergency.
            # If emergency topup is on cooldown, low emergency-tier counts
            # above are remembered in trigger_log but must not bypass the
            # emergency cooldown just because the drip clock is ready.
            # Uses its own _TOPUP_DRIP_INTERVAL cooldown (90s) independent of
            # the emergency gate. Only runs if emergency check found nothing.
            if _drip_ready:
                drip_pct = max(0.05, min(1.0, getattr(cfg, "TIER_DRIP_PCT", 100) / 100.0))
                for tier_name in ("inner", "mid", "outer", "extreme"):
                    _xch_slots = int(xch_dist.get(tier_name, 0) or 0)
                    _cat_slots = int(cat_dist.get(tier_name, 0) or 0)
                    _xch_sp_tgt = max(0, int(prepared_xch.get(tier_name, 0) or 0) - _xch_slots)
                    _cat_sp_tgt = max(0, int(prepared_cat.get(tier_name, 0) or 0) - _cat_slots)
                    _xch_sp = self._tier_spares.get("xch", {}).get(tier_name, 0)
                    _cat_sp = self._tier_spares.get("cat", {}).get(tier_name, 0)
                    _xch_drip = int(round(_xch_sp_tgt * drip_pct)) if _xch_sp_tgt > 0 else 0
                    _cat_drip = int(round(_cat_sp_tgt * drip_pct)) if _cat_sp_tgt > 0 else 0
                    _xch_low = _xch_drip > 0 and _xch_sp < _xch_drip and cfg.ENABLE_BUY
                    _cat_low = _cat_drip > 0 and _cat_sp < _cat_drip and cfg.ENABLE_SELL
                    if _xch_low or _cat_low:
                        if _xch_low:
                            try:
                                _xch_mojos = self._get_tier_sizes_mojos(is_cat=False).get(tier_name, 0)
                            except Exception:
                                _xch_mojos = 0
                            if not self._optional_topup_source_available("xch", _xch_mojos):
                                _xch_low = False
                                self._log_drip_source_unavailable(
                                    f"xch:{tier_name}",
                                    f"XCH {tier_name} drip waiting: {_xch_sp}/{_xch_drip} threshold "
                                    "but no XCH reserve or useful small coins are available to split",
                                )
                        if _cat_low:
                            try:
                                _cat_mojos = self._get_tier_sizes_mojos(is_cat=True).get(tier_name, 0)
                            except Exception:
                                _cat_mojos = 0
                            if not self._optional_topup_source_available("cat", _cat_mojos):
                                _cat_low = False
                                self._log_drip_source_unavailable(
                                    f"cat:{tier_name}",
                                    f"CAT {tier_name} drip waiting: {_cat_sp}/{_cat_drip} threshold "
                                    "but no CAT reserve or useful small coins are available to split",
                                )
                        if not (_xch_low or _cat_low):
                            continue
                        self._last_drip_time = time.time()
                        self._topup_is_drip = True
                        self._topup_needed_wallet_types = {
                            wallet_type
                            for wallet_type, low in (
                                ("xch", _xch_low),
                                ("cat", _cat_low),
                            )
                            if low
                        }
                        log_event("info", "drip_trigger",
                                  f"Proactive drip: {tier_name} "
                                  f"xch={_xch_sp}/{_xch_drip} (tgt {_xch_sp_tgt}) "
                                  f"cat={_cat_sp}/{_cat_drip} (tgt {_cat_sp_tgt})")
                        return True
                # Sniper pool drip (XCH + CAT, separate from trading tiers).
                if self._sniper_pool_enabled():
                    _sniper_target = int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0)
                    if _sniper_target > 0:
                        _sniper_drip_tgt = int(round(_sniper_target * drip_pct))
                        _sniper_xch_sp = self._tier_spares.get("xch", {}).get("sniper", 0)
                        _sniper_cat_sp = self._tier_spares.get("cat", {}).get("sniper", 0)
                        _sniper_xch_low = (
                            _sniper_drip_tgt > 0
                            and cfg.ENABLE_BUY
                            and _sniper_xch_sp < _sniper_drip_tgt
                        )
                        _sniper_cat_low = (
                            _sniper_drip_tgt > 0
                            and cfg.ENABLE_SELL
                            and _sniper_cat_sp < _sniper_drip_tgt
                        )
                        if _sniper_xch_low:
                            try:
                                _sniper_xch_dec = self._configured_tier_sizes_xch().get("sniper", Decimal("0"))
                                _sniper_xch_mojos = int(_sniper_xch_dec * Decimal("1000000000000"))
                            except Exception:
                                _sniper_xch_mojos = 0
                            if not self._optional_topup_source_available("xch", _sniper_xch_mojos):
                                _sniper_xch_low = False
                                self._log_drip_source_unavailable(
                                    "xch:sniper",
                                    f"XCH sniper drip waiting: {_sniper_xch_sp}/{_sniper_drip_tgt} spares "
                                    "but no XCH reserve or useful small coins are available to split",
                                )
                        if _sniper_cat_low:
                            try:
                                _sniper_cat_mojos = self._get_tier_sizes_mojos(is_cat=True).get("sniper", 0)
                            except Exception:
                                _sniper_cat_mojos = 0
                            if not self._optional_topup_source_available("cat", _sniper_cat_mojos):
                                _sniper_cat_low = False
                                self._log_drip_source_unavailable(
                                    "cat:sniper",
                                    f"CAT sniper drip waiting: {_sniper_cat_sp}/{_sniper_drip_tgt} spares "
                                    "but no CAT reserve or useful small coins are available to split",
                                )
                        if _sniper_xch_low or _sniper_cat_low:
                            self._last_drip_time = time.time()
                            self._topup_is_drip = True
                            self._topup_needed_wallet_types = {
                                wallet_type
                                for wallet_type, low in (
                                    ("xch", _sniper_xch_low),
                                    ("cat", _sniper_cat_low),
                                )
                                if low
                            }
                            log_event("info", "drip_trigger",
                                      f"Proactive drip: sniper "
                                      f"xch={_sniper_xch_sp}/{_sniper_drip_tgt} "
                                      f"cat={_sniper_cat_sp}/{_sniper_drip_tgt} "
                                      f"(tgt {_sniper_target})")
                            return True
                # Fee pool drip (XCH-only pool, separate from trading tiers).
                if self._fee_pool_enabled():
                    _fee_target = get_fee_pool_count()
                    if _fee_target > 0:
                        _fee_drip_tgt = int(round(_fee_target * drip_pct))
                        _fee_sp = self._tier_spares.get("xch", {}).get("fees", 0)
                        if _fee_drip_tgt > 0 and _fee_sp < _fee_drip_tgt:
                            self._last_drip_time = time.time()
                            self._topup_is_drip = True
                            self._topup_needed_wallet_types = {"xch"}
                            log_event("info", "drip_trigger",
                                      f"Proactive drip: fees "
                                      f"xch={_fee_sp}/{_fee_drip_tgt} (tgt {_fee_target})")
                            return True
                # Orphan reclaim: fire the topup worker (which runs the
                # misfit absorber as Step 0) when the 'small' bucket has
                # accumulated enough dust/misfit material to be worth
                # consolidating. Without this trigger, orphans sit idle
                # forever once all tiers are full — no topup reason ever
                # fires. Threshold: total small >= smallest trading tier
                # size, so the reclaimed reserve can immediately fund a
                # split if needed.
                try:
                    _orphan_wallets = []
                    if cfg.ENABLE_BUY:
                        _xch_tier = self._get_tier_sizes_mojos(is_cat=False)
                        _xch_small_total = sum(
                            _coin_amount(r) for r in self._xch_inventory.get("small", [])
                        )
                        _xch_smallest = min(
                            (v for k, v in _xch_tier.items()
                             if k in ("inner", "mid", "outer", "extreme") and v > 0),
                            default=0,
                        )
                        if (_xch_smallest > 0
                                and _xch_small_total >= _xch_smallest
                                and len(self._xch_inventory.get("small", [])) >= 2):
                            _orphan_wallets.append(
                                f"xch small×{len(self._xch_inventory.get('small', []))}="
                                f"{_format_amount_xch(_xch_small_total)}"
                            )
                    if cfg.ENABLE_SELL:
                        _cat_tier = self._get_tier_sizes_mojos(is_cat=True)
                        _cat_small_total = sum(
                            _coin_amount(r) for r in self._cat_inventory.get("small", [])
                        )
                        _cat_smallest = min(
                            (v for k, v in _cat_tier.items()
                             if k in ("inner", "mid", "outer", "extreme") and v > 0),
                            default=0,
                        )
                        if (_cat_smallest > 0
                                and _cat_small_total >= _cat_smallest
                                and len(self._cat_inventory.get("small", [])) >= 2):
                            _orphan_wallets.append(
                                f"cat small×{len(self._cat_inventory.get('small', []))}="
                                f"{_format_amount_cat(_cat_small_total, cfg.CAT_DECIMALS)}"
                            )
                    if _orphan_wallets:
                        self._last_drip_time = time.time()
                        self._topup_is_drip = True
                        self._topup_needed_wallet_types = {
                            "xch" if entry.startswith("xch ") else "cat"
                            for entry in _orphan_wallets
                        }
                        log_event(
                            "info", "drip_trigger",
                            "Proactive drip: orphan reclaim — " +
                            ", ".join(_orphan_wallets)
                        )
                        return True
                except Exception as _orphan_err:
                    log_event(
                        "debug", "orphan_reclaim_check_failed",
                        f"Orphan reclaim trigger check failed: {_orphan_err}"
                    )

                self._last_drip_time = time.time()  # nothing to drip — reset timer
            return False
        else:
            # Non-tiered: original logic with adaptive threshold
            free_xch = max(0, self._xch_coins - active_buy_count)
            free_cat = max(0, self._cat_coins - active_sell_count)

            xch_spare = int(cfg.MAX_ACTIVE_BUY_OFFERS * multiplier)
            cat_spare = int(cfg.MAX_ACTIVE_SELL_OFFERS * multiplier)
            target_free_xch = max(3, int(xch_spare * spare_keep_pct))
            target_free_cat = max(2, int(cat_spare * spare_keep_pct))

            needs_xch = free_xch < target_free_xch and cfg.ENABLE_BUY
            needs_cat = free_cat < target_free_cat and cfg.ENABLE_SELL

            if needs_xch or needs_cat:
                self._topup_needed_wallet_types = {
                    wallet_type
                    for wallet_type, low in (
                        ("xch", needs_xch),
                        ("cat", needs_cat),
                    )
                    if low
                }
                log_event("warning", "low_coins_free",
                          f"Low FREE coins! XCH: {free_xch} free (threshold {target_free_xch}), "
                          f"CAT: {free_cat} free (threshold {target_free_cat}) "
                          f"[pace={pace}, spare buffer at {spare_keep_pct*100:.0f}%, "
                          f"multiplier={multiplier}x]")
            return needs_xch or needs_cat

    def check_runtime_health(self, active_buy_count: int = 0,
                              active_sell_count: int = 0) -> bool:
        """Runtime coin health check — every 5 loops, independent."""
        if not getattr(cfg, "ENABLE_RUNTIME_COIN_HEALTH", False):
            return False
        if self._topup_running or self._prep_running:
            return False

        self._health_check_counter += 1
        if self._health_check_counter % 5 != 0:
            return False

        free_xch = max(0, self._xch_coins - active_buy_count)
        free_cat = max(0, self._cat_coins - active_sell_count)
        # Same spare-aware threshold as needs_topup()
        multiplier = float(getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0")))
        xch_spare = int(cfg.MAX_ACTIVE_BUY_OFFERS * multiplier)
        cat_spare = int(cfg.MAX_ACTIVE_SELL_OFFERS * multiplier)
        target_free_xch = max(3, int(xch_spare * 0.2))
        target_free_cat = max(2, int(cat_spare * 0.2))

        needs_xch = free_xch < target_free_xch and cfg.ENABLE_BUY
        needs_cat = free_cat < target_free_cat and cfg.ENABLE_SELL

        if needs_xch or needs_cat:
            cooldown = min(_TOPUP_BACKOFF_MAX, _TOPUP_BACKOFF_BASE * (2 ** self._no_coins_backoff_count)) if self._no_coins_backoff else _TOPUP_COOLDOWN
            if time.time() - self._last_topup_time < cooldown:
                if time.time() - self._last_low_coin_warning > 600:
                    remaining = int((cooldown - (time.time() - self._last_topup_time)) / 60)
                    log_event("info", "coin_health_cooldown",
                              f"Low coins but topup on cooldown ({remaining}m remaining)")
                    self._last_low_coin_warning = time.time()
                return False

            log_event("info", "coin_health_trigger",
                      f"[COIN HEALTH] XCH: {free_xch} free (need {target_free_xch}), "
                      f"CAT: {free_cat} free (need {target_free_cat})")
            return True

        if self._health_check_counter % 50 == 0:
            log_event("debug", "coin_health_ok",
                      f"Coin health OK: XCH={free_xch} free, CAT={free_cat} free")
        return False

    # -------------------------------------------------------------------
    # Live coin top-up (background thread)
    # -------------------------------------------------------------------

    def start_topup(self, active_buy_count: int = 0,
                    active_sell_count: int = 0,
                    is_drip: Optional[bool] = None) -> bool:
        """Start a live coin top-up in a background thread.

        ``is_drip`` lets the caller force the drip/emergency classification
        explicitly. needs_topup() always writes self._topup_is_drip before
        returning True (False at the emergency entry, True in each drip
        branch), so when the caller doesn't pass is_drip we preserve that
        value. Emergency call sites that DON'T flow through needs_topup
        (needs_coin_prep, health checks) MUST pass is_drip=False explicitly
        so a stale True from a previous drip cycle doesn't leak through and
        bypass the emergency cooldown semantics.
        """
        with self._lock:
            if self._topup_running:
                return False
            self._topup_running = True
            self._topup_stop_requested = False
            if is_drip is not None:
                self._topup_is_drip = bool(is_drip)
            # else: preserve the value set by needs_topup() (or by a prior
            # explicit caller). Reset-to-False here would silently downgrade
            # drip invocations to emergency-threshold behaviour, defeating
            # the proactive buffer (drip predicate targets the visible spare
            # buffer while emergency thresholds are lower), so the worker no-ops.
        # Emergency runs stamp _last_topup_time to enforce the full cooldown.
        # Drip runs already stamped _last_drip_time in needs_topup() — leave
        # _last_topup_time unchanged so the emergency gate is unaffected.
        if not self._topup_is_drip:
            self._last_topup_time = time.time()

        self._topup_thread = threading.Thread(
            target=self._topup_worker,
            args=(active_buy_count, active_sell_count),
            daemon=True,
            name="coin-topup"
        )
        try:
            self._topup_thread.start()
        except Exception as e:
            with self._lock:
                self._topup_running = False
            log_event("error", "topup_thread_start_failed", f"Failed to start topup thread: {e}")
            return False

        log_event("info", "topup_started",
                  "Live coin top-up started (existing offers stay active)")
        return True

    def stop_topup(self, wait_secs: float = 10.0) -> bool:
        """Request any running top-up worker to stop.

        ESCAPE HATCH: when the worker is wedged on a wallet RPC call, the
        join() times out and the thread stays alive. Previously we left
        ``_topup_running`` at True in that case, which made every
        subsequent is_busy() check return True and permanently locked the
        topup/prep path behind the zombie until the operator restarted
        the bot. Now we clear the running flag once the join timeout
        elapses and emit a critical log so the operator knows a zombie
        topup thread may be mutating the wallet in the background.
        The underlying thread is a daemon, so it terminates at process
        exit regardless.
        """
        thread = None
        with self._lock:
            if not self._topup_running:
                return False
            self._topup_stop_requested = True
            thread = self._topup_thread
        log_event("info", "topup_stop_requested", "Stopping background coin top-up")
        if thread and thread.is_alive() and wait_secs > 0:
            thread.join(timeout=wait_secs)
        if thread and thread.is_alive():
            log_event(
                "critical",
                "topup_stop_zombie",
                f"Coin top-up worker {thread.name!r} did not stop within "
                f"{wait_secs:.0f}s. Clearing the busy latch so future topup/"
                f"prep paths are not locked out behind this zombie; the "
                f"thread is a daemon and will die when its blocking RPC "
                f"returns or at process exit.",
                data={"thread": thread.name, "wait_secs": wait_secs},
            )
        with self._lock:
            # Force-release the busy latch even if the worker is still
            # alive. A new topup cannot be queued while the old worker is
            # still mutating the wallet (the wallet RPCs themselves
            # serialise), but is_busy() will now correctly report idle so
            # the rest of the bot does not wedge waiting on the latch.
            self._topup_running = False
            self._topup_stop_requested = False
            self._topup_thread = None
        return True

    def _topup_should_stop(self) -> bool:
        """Whether a running top-up worker has been asked to stop."""
        return bool(getattr(self, "_topup_stop_requested", False))

    def _topup_offer_deficits_by_tier(
        self,
        xch_dist: Dict[str, int],
        cat_dist: Dict[str, int],
    ) -> Dict[str, Dict[str, int]]:
        """Return active-offer deficits mapped to the coin-size tier pools.

        Runtime topup mostly thinks in free spare coins, but when offers are
        actually missing the refill priority must flip: restore the book first,
        then tidy misfits/spares. Buy offers spend XCH coins, sell offers
        spend CAT coins. On reversed buy ladders the offer tier is a slot
        position, so translate it to the coin-size tier before comparing with
        the XCH target distribution.
        """
        tiers = ("inner", "mid", "outer", "extreme")
        deficits = {
            "xch": {tier: 0 for tier in tiers},
            "cat": {tier: 0 for tier in tiers},
        }
        try:
            from database import get_open_offers

            for side, wallet_type, target in (
                ("buy", "xch", xch_dist),
                ("sell", "cat", cat_dist),
            ):
                if side == "buy" and not cfg.ENABLE_BUY:
                    continue
                if side == "sell" and not cfg.ENABLE_SELL:
                    continue

                active = {tier: 0 for tier in tiers}
                for offer in get_open_offers(
                    side=side,
                    cat_asset_id=getattr(cfg, "CAT_ASSET_ID", None),
                ) or []:
                    position_tier = str(offer.get("tier") or "mid").lower()
                    coin_tier = coin_size_tier_for_slot_position(position_tier, side=side)
                    if coin_tier in active:
                        active[coin_tier] += 1

                for tier in tiers:
                    expected = int(target.get(tier, 0) or 0)
                    deficits[wallet_type][tier] = max(0, expected - active.get(tier, 0))
        except Exception as exc:
            log_event(
                "debug",
                "topup_offer_deficit_scan_failed",
                f"Could not scan active offer deficits for topup priority: {exc}",
            )
        return deficits

    def _topup_worker(self, active_buy: int, active_sell: int):
        """Smart topup worker — classifies coins and decides strategy.

        Decision tree:
          1. Reserve coin exists → split it directly
          2. Many small coins → consolidate then split
          3. Nothing available → back off
        """
        try:
            self._topup_abort_logged = False
            if self._topup_should_stop():
                log_event("info", "topup_stopped", "Coin top-up stopped before work began")
                return

            # ---- Pre-check: is wallet synced? ----
            # After creating many offers rapidly, the wallet can be briefly
            # unsynced. get_spendable_coins_rpc() returns 0 during this window,
            # which would cause a false "no coins" backoff of 2 hours.
            # Wait briefly for sync before proceeding.
            wallet_ready = False
            wallet_backend = str(os.getenv("WALLET_TYPE", "sage") or "sage").strip().lower()
            for sync_attempt in range(6):  # Up to 30 seconds
                try:
                    from wallet import get_wallet_sync_status
                    sync_info = get_wallet_sync_status()
                    sync_state = str(sync_info.get("sync_state") or "").strip().lower() if sync_info else ""
                    if sync_info and sync_info.get("synced", False):
                        wallet_ready = True
                        break
                    elif (
                        wallet_backend == "sage"
                        and sync_info
                        and sync_info.get("reachable")
                        and sync_state in ("", "unknown")
                        and not sync_info.get("syncing", False)
                    ):
                        wallet_ready = True
                        break
                    else:
                        if sync_attempt == 0:
                            log_event("info", "topup_wait_sync",
                                      "Wallet not synced — waiting before topup scan...")
                        time.sleep(5)
                except Exception as e:
                    # get_wallet_sync_status may not exist for some backends.
                    # Only treat as ready if this is the first attempt — if we've
                    # been waiting, repeated exceptions suggest a real problem.
                    if sync_attempt == 0:
                        wallet_ready = True
                        break
                    else:
                        log_event("warning", "topup_sync_check_error",
                                  f"Sync check threw on attempt {sync_attempt}: {e}")
                        time.sleep(5)

            if not wallet_ready:
                log_event("warning", "topup_wallet_unsynced",
                          "Wallet still not synced after 30s — short cooldown, will retry next cycle")
                self._last_topup_time = time.time()
                # DON'T set _no_coins_backoff — this is temporary, not "no coins"
                return

            # ---- Fresh coin inventory ----
            xch_result = _get_free_coins_rpc(cfg.WALLET_ID_XCH)
            cat_result = _get_free_coins_rpc(cfg.CAT_WALLET_ID)

            # Detect RPC errors: if the wallet returned an error (not just empty),
            # don't treat it as "no coins" and enter backoff — short cooldown instead.
            xch_is_error = (isinstance(xch_result, dict) and
                            (xch_result.get("error") or xch_result.get("success") is False))
            cat_is_error = (isinstance(cat_result, dict) and
                            (cat_result.get("error") or cat_result.get("success") is False))
            if xch_is_error or cat_is_error or xch_result is None or cat_result is None:
                log_event("warning", "topup_rpc_error",
                          "Wallet RPC returned error during topup — short cooldown, will retry next cycle")
                self._last_topup_time = time.time()
                return

            xch_records = _extract_coin_records(xch_result)
            cat_records = _extract_coin_records(cat_result)

            # ---- Sanity check: if we got 0 coins, wallet may still be catching up ----
            # The wallet can report synced=true but still not show coins for a few seconds
            # after creating 50+ offers. If we see 0 total coins but had active offers,
            # this is almost certainly a transient state — don't back off.
            total_records = len(xch_records) + len(cat_records)
            total_active = active_buy + active_sell
            if total_records == 0 and total_active > 0:
                log_event("info", "topup_zero_transient",
                          f"Wallet returned 0 spendable coins but {total_active} offers are active — "
                          f"wallet likely still catching up. Short cooldown, will retry.")
                self._last_topup_time = time.time()
                return

            # ---- Classify coins (V3: designation-based) ----
            if cfg.TIER_ENABLED:
                xch_tier_mojos = self._get_tier_sizes_mojos(is_cat=False)
                cat_tier_mojos = self._get_tier_sizes_mojos(is_cat=True)
                xch_inv = self._classify_coins_by_designation(xch_records, "xch", xch_tier_mojos)
                cat_inv = self._classify_coins_by_designation(cat_records, "cat", cat_tier_mojos)
                if self._fee_pool_enabled():
                    self.fee_pool.refresh(xch_inv.get("fees", []))

                # Log tier breakdown
                tier_names = self._configured_tier_names()
                xch_tier_counts = ", ".join(
                    f"{t}={len(xch_inv.get(t, []))}" for t in tier_names)
                cat_tier_counts = ", ".join(
                    f"{t}={len(cat_inv.get(t, []))}" for t in tier_names)
                log_event(_topup_event_log_level("topup_inventory"), "topup_inventory",
                          f"Topup inventory (tiered) — "
                          f"XCH: {len(xch_inv['reserve'])} reserve, {xch_tier_counts}, "
                          f"{len(xch_inv['small'])} small | "
                          f"CAT: {len(cat_inv['reserve'])} reserve, {cat_tier_counts}, "
                          f"{len(cat_inv['small'])} small")
            else:
                xch_trading_mojos = int(self.get_target_xch_coin_size() * Decimal("1000000000000"))
                cat_scale = Decimal(10) ** Decimal(cfg.CAT_DECIMALS)
                cat_trading_mojos = int(self.get_target_cat_coin_size() * cat_scale)
                xch_inv = _classify_coins(xch_records, xch_trading_mojos)
                cat_inv = _classify_coins(cat_records, cat_trading_mojos)

                log_event(_topup_event_log_level("topup_inventory"), "topup_inventory",
                          f"Topup inventory — "
                          f"XCH: {len(xch_inv['reserve'])} reserve, {len(xch_inv['trading'])} trading, "
                          f"{len(xch_inv['small'])} small | "
                          f"CAT: {len(cat_inv['reserve'])} reserve, {len(cat_inv['trading'])} trading, "
                          f"{len(cat_inv['small'])} small")

            any_tier_needed = False   # tracks whether any tier was below its threshold
            did_anything = False
            topup_offer_deficits = {
                "xch": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0},
                "cat": {"inner": 0, "mid": 0, "outer": 0, "extreme": 0},
            }
            offer_deficit_total = 0
            offer_deficit_summary = ""
            spare_deficit_total = 0
            spare_deficit_summary = ""
            if cfg.TIER_ENABLED:
                max_buy_for_priority = int(
                    getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25) or 25)
                max_sell_for_priority = int(
                    getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25) or 25)
                xch_dist_for_priority = get_tier_distribution(
                    max_buy_for_priority, side="xch"
                )
                cat_dist_for_priority = get_tier_distribution(
                    max_sell_for_priority, side="cat"
                )
                topup_offer_deficits = self._topup_offer_deficits_by_tier(
                    xch_dist_for_priority,
                    cat_dist_for_priority,
                )
                parts = []
                for wallet_type, side_label in (("xch", "buy"), ("cat", "sell")):
                    for tier_name in ("inner", "mid", "outer", "extreme"):
                        count = int(
                            topup_offer_deficits.get(wallet_type, {}).get(tier_name, 0)
                            or 0
                        )
                        if count > 0:
                            offer_deficit_total += count
                            parts.append(f"{side_label}/{tier_name}={count}")
                offer_deficit_summary = ", ".join(parts)

                multiplier_for_priority = getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0"))
                buy_sizes_for_priority = self._configured_tier_sizes_xch(side="buy")
                sell_sizes_for_priority = self._configured_tier_sizes_xch(side="sell")
                prepared_xch_for_priority = get_weighted_tier_prep_counts(
                    max_buy_for_priority,
                    multiplier_for_priority,
                    tier_sizes_xch=buy_sizes_for_priority,
                    side="xch",
                )
                prepared_cat_for_priority = get_weighted_tier_prep_counts(
                    max_sell_for_priority,
                    multiplier_for_priority,
                    tier_sizes_xch=sell_sizes_for_priority,
                    side="cat",
                )

                if getattr(cfg, "TIER_TRIGGER_PACE_SCALE", True):
                    pace_for_priority = self.get_trading_pace()
                    pace_scale_for_priority = (
                        1.4 if pace_for_priority == "busy"
                        else (0.7 if pace_for_priority == "slow" else 1.0)
                    )
                else:
                    pace_scale_for_priority = 1.0
                drip_pct_for_priority = max(
                    0.05,
                    min(1.0, float(getattr(cfg, "TIER_DRIP_PCT", 100)) / 100.0),
                )

                def _priority_tier_pct(tier_name: str, wallet_side: str) -> float:
                    if bool(getattr(self, "_topup_is_drip", False)):
                        return drip_pct_for_priority
                    pct_map = {
                        "inner": getattr(cfg, "TIER_TRIGGER_PCT_INNER", 50),
                        "mid": getattr(cfg, "TIER_TRIGGER_PCT_MID", 40),
                        "outer": getattr(cfg, "TIER_TRIGGER_PCT_OUTER", 25),
                        "extreme": getattr(cfg, "TIER_TRIGGER_PCT_EXTREME", 15),
                    }
                    base = pct_map.get(tier_name, 30)
                    ladder_side = "buy" if wallet_side == "xch" else "sell"
                    try:
                        position_tier = coin_size_tier_for_slot_position(
                            tier_name, side=ladder_side
                        )
                        base = pct_map.get(position_tier, base)
                    except Exception:
                        pass
                    return max(
                        0.05,
                        min(0.95, (float(base) / 100.0) * pace_scale_for_priority),
                    )

                spare_parts = []
                for tier_name in ("inner", "mid", "outer", "extreme"):
                    xch_slots = int(xch_dist_for_priority.get(tier_name, 0) or 0)
                    cat_slots = int(cat_dist_for_priority.get(tier_name, 0) or 0)
                    xch_spare_target = max(
                        0,
                        int(prepared_xch_for_priority.get(tier_name, 0) or 0)
                        - xch_slots,
                    )
                    cat_spare_target = max(
                        0,
                        int(prepared_cat_for_priority.get(tier_name, 0) or 0)
                        - cat_slots,
                    )
                    xch_have = len(xch_inv.get(tier_name, []))
                    cat_have = len(cat_inv.get(tier_name, []))
                    xch_threshold = (
                        max(
                            1,
                            int(round(
                                xch_spare_target
                                * _priority_tier_pct(tier_name, "xch")
                            )),
                        )
                        if xch_spare_target > 0 else 0
                    )
                    cat_threshold = (
                        max(
                            1,
                            int(round(
                                cat_spare_target
                                * _priority_tier_pct(tier_name, "cat")
                            )),
                        )
                        if cat_spare_target > 0 else 0
                    )
                    if xch_threshold > 0 and xch_have < xch_threshold and cfg.ENABLE_BUY:
                        spare_deficit_total += xch_threshold - xch_have
                        spare_parts.append(f"buy/{tier_name}={xch_have}/{xch_threshold}")
                    if cat_threshold > 0 and cat_have < cat_threshold and cfg.ENABLE_SELL:
                        spare_deficit_total += cat_threshold - cat_have
                        spare_parts.append(f"sell/{tier_name}={cat_have}/{cat_threshold}")
                spare_deficit_summary = ", ".join(spare_parts)

            # ---- Step 0: Absorb misfit tier_spare coins into reserve ----
            # Coins returned as change from filled offers frequently land
            # between tier boundaries (too large for one tier, too small for
            # the next). With COIN_MAX_SIZE_RATIO capping selection, these
            # coins sit idle indefinitely. Fold them back into the reserve so
            # their value is recaptured for the next targeted split cycle.
            if cfg.TIER_ENABLED and offer_deficit_total <= 0 and spare_deficit_total <= 0:
                _xch_absorbed = self._absorb_misfits_to_reserve(
                    "XCH", cfg.WALLET_ID_XCH, xch_inv,
                    xch_tier_mojos, is_cat=False,
                )
                _cat_absorbed = (
                    self._absorb_misfits_to_reserve(
                        "CAT", cfg.CAT_WALLET_ID, cat_inv,
                        cat_tier_mojos, is_cat=True,
                    )
                    if cfg.ENABLE_SELL
                    else False
                )
                if _xch_absorbed:
                    # XCH reserve consumed — block ALL further ops this cycle.
                    # Sage returns pending coins as selectable, so without this
                    # gate a drip 40s later would pick the same (pending) reserve
                    # and submit a second TX → double-spend → one TX fails
                    # → misfits return → infinite absorb-defer loop.
                    did_anything = True
                    self._last_drip_time = time.time()
                    log_event("info", "topup_absorb_defer",
                              "XCH misfits absorbed into reserve — tier splits "
                              "deferred to next cycle (awaiting on-chain confirmation)")
                    return
                if _cat_absorbed:
                    # CAT reserve consumed — CAT tier splits are deferred this
                    # cycle (the pending absorption coin must confirm first).
                    # XCH is unaffected (separate reserve coin), so fall through
                    # to the XCH tier split section below rather than returning.
                    # The CAT split section will find no valid reserve and skip
                    # gracefully without double-spending the pending coin.
                    did_anything = True
                    log_event("info", "topup_cat_absorb_continue_xch",
                              "CAT misfits absorbed into reserve — proceeding with "
                              "XCH tier splits this cycle (CAT splits deferred)")

            elif cfg.TIER_ENABLED and offer_deficit_total > 0:
                log_event(
                    "info",
                    "topup_missing_offers_prioritized",
                    "Skipping misfit absorption because active offer slots are "
                    f"missing ({offer_deficit_summary}); restoring those tier "
                    "coins first.",
                )
            elif cfg.TIER_ENABLED:
                log_event(
                    "info",
                    "topup_floor_spares_prioritized",
                    "Skipping misfit absorption because tier spares are below "
                    f"priority thresholds ({spare_deficit_summary}); "
                    "replenishing floor-nearest spares first.",
                )

            if cfg.TIER_ENABLED:
                # ---- Tier-aware topup: check each tier ----
                max_buy_for_topup = int(
                    getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25) or 25)
                max_sell_for_topup = int(
                    getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25) or 25)
                # Per-side distributions: BUY_*_TIER_COUNT shapes XCH topup,
                # SELL_*_TIER_COUNT shapes CAT topup. They no longer have to
                # match — reverse-buy ladders are typically asymmetric.
                xch_dist = get_tier_distribution(max_buy_for_topup, side="xch")
                cat_dist = get_tier_distribution(max_sell_for_topup, side="cat")

                # V3 Adaptive threshold: spare buffer % based on trading pace
                multiplier = getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0"))
                pace = self.get_trading_pace()

                # Per-tier action thresholds that mirror the trigger's TIER_TRIGGER_PCT_*
                # config, including reversed-buy slot-position translation.
                # Previously the worker used a flat TOPUP_NORMAL_PCT=30% for all tiers
                # while the trigger used per-tier values (50/40/25/15%). With small spare
                # targets the int() truncation made the worker's threshold lower than the
                # trigger's, so the worker concluded "no action needed" after the trigger
                # had already fired — producing a misleading "coins locked" log message
                # even when the reserve coin was free to split.
                if getattr(cfg, "TIER_TRIGGER_PACE_SCALE", True):
                    _topup_pace_scale = 1.4 if pace == 'busy' else (0.7 if pace == 'slow' else 1.0)
                else:
                    _topup_pace_scale = 1.0

                # Drip invocations want the worker to act at the drip predicate
                # threshold (TIER_DRIP_PCT, default 100%) so the proactive buffer
                # is actually maintained. Without this, the drip trigger fires
                # every cycle but the worker no-ops because TIER_TRIGGER_PCT_*
                # is much lower (e.g. sniper trigger=75%, worker=40% → buffer
                # never restored until the harder reactive threshold is hit).
                _is_drip_invocation = bool(getattr(self, "_topup_is_drip", False))
                _drip_pct_norm = max(0.05, min(1.0, float(getattr(cfg, "TIER_DRIP_PCT", 100)) / 100.0))

                def _topup_tier_pct(tier_name: str, wallet_side: str) -> float:
                    """Return the action-threshold pct for this tier/side. For
                    drip invocations this is TIER_DRIP_PCT so the worker mirrors
                    the drip predicate; for reactive topups it's the per-tier
                    TIER_TRIGGER_PCT_* value (lower, urgent-action threshold)."""
                    if _is_drip_invocation:
                        return _drip_pct_norm
                    _pct_map = {
                        "inner":   getattr(cfg, "TIER_TRIGGER_PCT_INNER", 50),
                        "mid":     getattr(cfg, "TIER_TRIGGER_PCT_MID", 40),
                        "outer":   getattr(cfg, "TIER_TRIGGER_PCT_OUTER", 25),
                        "extreme": getattr(cfg, "TIER_TRIGGER_PCT_EXTREME", 15),
                        "sniper":  getattr(cfg, "TIER_TRIGGER_PCT_SNIPER", 40),
                        "fees":    getattr(cfg, "TIER_TRIGGER_PCT_FEES", 30),
                    }
                    _base = _pct_map.get(tier_name, 30)
                    # Reversed-buy: translate coin-size tier → slot position for pct
                    _ladder = "buy" if wallet_side == "xch" else "sell"
                    try:
                        _pos = coin_size_tier_for_slot_position(tier_name, side=_ladder)
                        _base = _pct_map.get(_pos, _base)
                    except Exception:
                        pass
                    return max(0.05, min(0.95, (float(_base) / 100.0) * _topup_pace_scale))

                xch_scale = Decimal("1000000000000")
                # F62 (2026-04-09): topup worker needs to know each side's
                # own tier sizes so it can split to the right target. The
                # XCH wallet replenishes coins at BUY sizes; the CAT wallet
                # at SELL sizes. Pre-F62 these were forced to be the same.
                buy_tier_sizes_xch  = self._configured_tier_sizes_xch(side="buy")
                sell_tier_sizes_xch = self._configured_tier_sizes_xch(side="sell")
                # Backward-compat alias used by downstream code paths below
                # (they read `live_tier_sizes_xch` for the XCH-side split
                # size when replenishing XCH coins).
                live_tier_sizes_xch = buy_tier_sizes_xch
                prepared_xch_counts = get_weighted_tier_prep_counts(
                    max_buy_for_topup,
                    multiplier,
                    tier_sizes_xch=buy_tier_sizes_xch,
                    side="xch",
                )
                prepared_cat_counts = get_weighted_tier_prep_counts(
                    max_sell_for_topup,
                    multiplier,
                    tier_sizes_xch=sell_tier_sizes_xch,
                    side="cat",
                )

                # Floor-nearest positions first: on a reversed buy ladder the
                # extreme coin pool serves the inner (near-mid, highest-traffic)
                # positions and should be replenished before outer/mid/inner pools.
                # Sell side is never reversed so always inner → extreme.
                _buy_reversed = getattr(cfg, "BUY_LADDER_REVERSED", False)
                _default_tier_order = (["extreme", "outer", "mid", "inner"]
                                       if _buy_reversed else
                                       ["inner", "mid", "outer", "extreme"])

                # EMPTY-FIRST priority: a tier with zero free coins blocks any
                # offer on that slot. Single-action topup means only one split
                # per cycle, so if inner=3 and mid=0 with inner iterated first,
                # mid has to wait the full drip interval. Sort so empty tiers
                # (on any side that has active slots) are attempted first; ties
                # fall back to the default floor-priority order.
                def _empty_first_key(tier_name: str) -> tuple:
                    xch_slots_n = int(xch_dist.get(tier_name, 0) or 0)
                    cat_slots_n = int(cat_dist.get(tier_name, 0) or 0)
                    xch_have_n = len(xch_inv.get(tier_name, []))
                    cat_have_n = len(cat_inv.get(tier_name, []))
                    xch_offer_deficit_n = int(
                        topup_offer_deficits.get("xch", {}).get(tier_name, 0) or 0
                    )
                    cat_offer_deficit_n = int(
                        topup_offer_deficits.get("cat", {}).get(tier_name, 0) or 0
                    )
                    offer_deficit_rank = 0 if (
                        xch_offer_deficit_n > 0 or cat_offer_deficit_n > 0
                    ) else 1
                    xch_empty = xch_slots_n > 0 and xch_have_n == 0
                    cat_empty = cat_slots_n > 0 and cat_have_n == 0
                    empty_rank = 0 if (xch_empty or cat_empty) else 1
                    try:
                        default_idx = _default_tier_order.index(tier_name)
                    except ValueError:
                        default_idx = 99
                    return (offer_deficit_rank, empty_rank, default_idx)

                _tier_order = sorted(_default_tier_order, key=_empty_first_key)
                _xch_split_done = False  # True only for real splits, not pool rebuilds
                for tier_name in _tier_order:
                    if self._topup_should_stop():
                        log_event("info", "topup_stopped", "Coin top-up stopped during tier replenishment")
                        return
                    xch_slots = int(xch_dist.get(tier_name, 0) or 0)
                    cat_slots = int(cat_dist.get(tier_name, 0) or 0)
                    if xch_slots == 0 and cat_slots == 0:
                        continue

                    # Runtime top-up replenishes free spare inventory only.
                    xch_prepped = int(prepared_xch_counts.get(tier_name, 0) or 0)
                    cat_prepped = int(prepared_cat_counts.get(tier_name, 0) or 0)
                    xch_spare_target = xch_prepped - xch_slots
                    cat_spare_target = cat_prepped - cat_slots

                    # XCH: check if this tier needs coins (XCH = buy side)
                    xch_have = len(xch_inv.get(tier_name, []))
                    xch_topup_threshold = max(1, int(round(xch_spare_target * _topup_tier_pct(tier_name, "xch")))) if xch_spare_target > 0 else 0
                    xch_offer_deficit = int(
                        topup_offer_deficits.get("xch", {}).get(tier_name, 0) or 0
                    )
                    xch_needs_spares = xch_spare_target > 0 and xch_have < xch_topup_threshold
                    target_full = max(0, xch_spare_target, xch_offer_deficit)
                    xch_needs_offer_rebuild = (
                        xch_offer_deficit > 0 and xch_have < target_full
                    )
                    if (xch_needs_offer_rebuild or xch_needs_spares) and cfg.ENABLE_BUY:
                        any_tier_needed = True
                        xch_tier_size = int(xch_tier_mojos.get(tier_name, 0) or 0)
                        if xch_tier_size <= 0:
                            xch_tier_size = int(
                                Decimal(str(live_tier_sizes_xch.get(tier_name, cfg.MID_SIZE_XCH)))
                                * self._get_coin_prep_headroom_multiplier()
                                * xch_scale
                            )
                        # Buffer: 25% of spare allocation, min 1, max 2.
                        # Scales with tier depth rather than being flat +2 for all
                        # tiers (flat +2 over-splits small-spare tiers like outer/extreme).
                        _buf_base = max(1, xch_spare_target, xch_offer_deficit)
                        _buf = max(1, min(2, int(_buf_base * 0.25)))
                        deficit = max(0, target_full - xch_have) + _buf
                        if xch_needs_offer_rebuild:
                            topup_reason = (
                                f"missing buy offers: {xch_have}/{target_full} rebuild target "
                                f"({xch_offer_deficit} missing, threshold {xch_topup_threshold})"
                            )
                        else:
                            topup_reason = (
                                f"spare buffer low: {xch_have}/{xch_topup_threshold} threshold"
                            )
                        log_event("info", f"topup_xch_{tier_name}",
                                  f"XCH {tier_name} {topup_reason} "
                                  f"(target {target_full}) — "
                                  f"need {deficit} at {_format_amount_xch(xch_tier_size)} each")
                        result = self._smart_topup_wallet(
                            f"XCH-{tier_name}", cfg.WALLET_ID_XCH,
                            xch_inv, xch_tier_size, deficit,
                            is_cat=False,
                            tier_is_empty=(xch_have == 0),
                            soft_budget_bypass_reason=(
                                "missing buy offer"
                                if xch_offer_deficit > 0
                                else (
                                    "floor-nearest buy slot"
                                    if tier_name == _default_tier_order[0]
                                    else None
                                )
                            ),
                        )
                        if result is True:
                            did_anything = True
                            _xch_split_done = True
                            # SINGLE-ACTION: one split per cycle keeps is_busy() short.
                            # The drip timer (90s) gives the TX time to confirm before
                            # the next cycle re-evaluates. Break out of the tier loop.
                            break
                        elif result:
                            # Pool rebuild submitted (not a tier split) — mark activity
                            # but do NOT break. Pool rebuilds only consolidate coins;
                            # the new reserve won't be available until next cycle.
                            # CAT still needs its turn this cycle, so fall through.
                            did_anything = True
                        else:
                            # Split failed — re-fetch before trying the next tier so we
                            # don't hit the same locked coin again.
                            time.sleep(3)
                            fresh = _get_free_coins_rpc(cfg.WALLET_ID_XCH)
                            fresh_records = _extract_coin_records(fresh)
                            xch_inv = self._classify_coins_by_designation(fresh_records, "xch", self._get_tier_sizes_mojos(is_cat=False))

                    if _xch_split_done:
                        break  # propagate single-action exit only for real XCH splits

                    # CAT: check if this tier needs coins (CAT = sell side)
                    cat_have = len(cat_inv.get(tier_name, []))
                    cat_topup_threshold = max(1, int(round(cat_spare_target * _topup_tier_pct(tier_name, "cat")))) if cat_spare_target > 0 else 0
                    cat_offer_deficit = int(
                        topup_offer_deficits.get("cat", {}).get(tier_name, 0) or 0
                    )
                    cat_needs_spares = cat_spare_target > 0 and cat_have < cat_topup_threshold
                    target_full = max(0, cat_spare_target, cat_offer_deficit)
                    cat_needs_offer_rebuild = (
                        cat_offer_deficit > 0 and cat_have < target_full
                    )
                    if (cat_needs_offer_rebuild or cat_needs_spares) and cfg.ENABLE_SELL:
                        any_tier_needed = True
                        cat_tier_mojos_val = self._get_tier_sizes_mojos(is_cat=True).get(tier_name, 0)
                        if cat_tier_mojos_val > 0:
                            _buf_base = max(1, cat_spare_target, cat_offer_deficit)
                            _buf = max(1, min(2, int(_buf_base * 0.25)))
                            deficit = max(0, target_full - cat_have) + _buf
                            cat_size_display = _format_amount_cat(cat_tier_mojos_val, cfg.CAT_DECIMALS)
                            if cat_needs_offer_rebuild:
                                topup_reason = (
                                    f"missing sell offers: {cat_have}/{target_full} rebuild target "
                                    f"({cat_offer_deficit} missing, threshold {cat_topup_threshold})"
                                )
                            else:
                                topup_reason = (
                                    f"spare buffer low: {cat_have}/{cat_topup_threshold} threshold"
                                )
                            log_event("info", f"topup_cat_{tier_name}",
                                      f"CAT {tier_name} {topup_reason} "
                                      f"(target {target_full}) — "
                                      f"need {deficit} at {cat_size_display} each")
                            result = self._smart_topup_wallet(
                                f"CAT-{tier_name}", cfg.CAT_WALLET_ID,
                                cat_inv, cat_tier_mojos_val, deficit,
                                is_cat=True,
                                tier_is_empty=(cat_have == 0),
                                soft_budget_bypass_reason=(
                                    "missing sell offer"
                                    if cat_offer_deficit > 0
                                    else (
                                        "floor-nearest sell slot"
                                        if tier_name == "inner"
                                        else None
                                    )
                                ),
                            )
                            if result:
                                did_anything = True
                                # SINGLE-ACTION: one split per cycle.
                                break
                            # Split failed — re-fetch before trying next tier.
                            time.sleep(3)
                            fresh = _get_free_coins_rpc(cfg.CAT_WALLET_ID)
                            fresh_records = _extract_coin_records(fresh)
                            cat_inv = self._classify_coins_by_designation(fresh_records, "cat", self._get_tier_sizes_mojos(is_cat=True))

                # SINGLE-ACTION: skip sniper/fee checks if a tier split already ran.
                if not did_anything and self._sniper_pool_enabled():
                    if self._topup_should_stop():
                        log_event("info", "topup_stopped", "Coin top-up stopped during sniper replenishment")
                        return
                    sniper_target = int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0)
                    sniper_threshold = max(1, int(round(sniper_target * _topup_tier_pct("sniper", "xch"))))
                    sniper_xch_size_dec = live_tier_sizes_xch.get("sniper", Decimal("0"))
                    sniper_cat_mojos_val = self._get_tier_sizes_mojos(is_cat=True).get("sniper", 0)

                    sniper_xch_have = len(xch_inv.get("sniper", []))
                    if sniper_xch_have < sniper_threshold and cfg.ENABLE_BUY and sniper_xch_size_dec > 0:
                        sniper_xch_size = int(sniper_xch_size_dec * self._get_coin_prep_headroom_multiplier() * xch_scale)
                        if _is_drip_invocation and not self._optional_topup_source_available("xch", sniper_xch_size):
                            self._log_drip_source_unavailable(
                                "xch:sniper",
                                f"XCH sniper drip waiting: {sniper_xch_have}/{sniper_threshold} threshold "
                                "but no XCH reserve or useful small coins are available to split",
                            )
                        else:
                            any_tier_needed = True
                            deficit = (sniper_target - sniper_xch_have) + 2
                            log_event("info", "topup_xch_sniper",
                                      f"XCH sniper pool low: {sniper_xch_have}/{sniper_threshold} threshold "
                                      f"(target {sniper_target}) — need {deficit} at {_format_amount_xch(sniper_xch_size)} each")
                            result = self._smart_topup_wallet(
                                "XCH-sniper", cfg.WALLET_ID_XCH,
                                xch_inv, sniper_xch_size, deficit,
                                is_cat=False
                            )
                            if result:
                                did_anything = True
                            if not did_anything:
                                time.sleep(3)
                                fresh = _get_free_coins_rpc(cfg.WALLET_ID_XCH)
                                fresh_records = _extract_coin_records(fresh)
                                xch_inv = self._classify_coins_by_designation(fresh_records, "xch", self._get_tier_sizes_mojos(is_cat=False))

                    if not did_anything:
                        sniper_cat_have = len(cat_inv.get("sniper", []))
                        if sniper_cat_have < sniper_threshold and cfg.ENABLE_SELL and sniper_cat_mojos_val > 0:
                            if _is_drip_invocation and not self._optional_topup_source_available("cat", sniper_cat_mojos_val):
                                self._log_drip_source_unavailable(
                                    "cat:sniper",
                                    f"CAT sniper drip waiting: {sniper_cat_have}/{sniper_threshold} threshold "
                                    "but no CAT reserve or useful small coins are available to split",
                                )
                            else:
                                any_tier_needed = True
                                deficit = (sniper_target - sniper_cat_have) + 2
                                sniper_cat_display = _format_amount_cat(sniper_cat_mojos_val, cfg.CAT_DECIMALS)
                                log_event("info", "topup_cat_sniper",
                                          f"CAT sniper pool low: {sniper_cat_have}/{sniper_threshold} threshold "
                                          f"(target {sniper_target}) — need {deficit} at {sniper_cat_display} each")
                                result = self._smart_topup_wallet(
                                    "CAT-sniper", cfg.CAT_WALLET_ID,
                                    cat_inv, sniper_cat_mojos_val, deficit,
                                    is_cat=True,
                                )
                                if result:
                                    did_anything = True

                # SINGLE-ACTION: skip fee check if a split already ran.
                if not did_anything and self._fee_pool_enabled():
                    if self._topup_should_stop():
                        log_event("info", "topup_stopped", "Coin top-up stopped during fee replenishment")
                        return
                    fee_target = get_fee_pool_count()
                    fee_threshold = max(1, int(round(fee_target * _topup_tier_pct("fees", "xch"))))
                    fee_xch_mojos = get_fee_coin_size_mojos()
                    fee_xch_have = len(xch_inv.get("fees", []))
                    if fee_xch_have < fee_threshold and fee_xch_mojos > 0:
                        any_tier_needed = True
                        deficit = (fee_target - fee_xch_have) + 2
                        log_event(
                            "info",
                            "topup_xch_fees",
                            f"XCH fee pool low: {fee_xch_have}/{fee_threshold} threshold "
                            f"(target {fee_target}) — need {deficit} at {_format_amount_xch(fee_xch_mojos)} each",
                        )
                        result = self._smart_topup_wallet(
                            "XCH-fees", cfg.WALLET_ID_XCH,
                            xch_inv, fee_xch_mojos, deficit,
                            is_cat=False
                        )
                        if result:
                            did_anything = True

            else:
                # ---- Non-tiered: original uniform topup ----
                free_xch_trading = max(0, len(xch_inv["trading"]) - active_buy)
                free_cat_trading = max(0, len(cat_inv["trading"]) - active_sell)

                target_free_xch = max(3, int(cfg.MAX_ACTIVE_BUY_OFFERS * 0.3))
                target_free_cat = max(2, int(cfg.MAX_ACTIVE_SELL_OFFERS * 0.3))

                if free_xch_trading < target_free_xch and cfg.ENABLE_BUY:
                    needed = target_free_xch - free_xch_trading + 5
                    xch_trading_mojos = int(self.get_target_xch_coin_size() * Decimal("1000000000000"))
                    xch_result = self._smart_topup_wallet(
                        "XCH", cfg.WALLET_ID_XCH,
                        xch_inv, xch_trading_mojos, needed,
                        is_cat=False
                    )
                    if xch_result:
                        did_anything = True

                if free_cat_trading < target_free_cat and cfg.ENABLE_SELL:
                    if did_anything:
                        time.sleep(5)
                    needed = target_free_cat - free_cat_trading + 3
                    cat_scale_val = Decimal(10) ** Decimal(cfg.CAT_DECIMALS)
                    cat_trading_mojos = int(self.get_target_cat_coin_size() * cat_scale_val)
                    cat_result = self._smart_topup_wallet(
                        "CAT", cfg.CAT_WALLET_ID,
                        cat_inv, cat_trading_mojos, needed,
                        is_cat=True
                    )
                    if cat_result:
                        did_anything = True

            if not did_anything:
                # Three distinct reasons we get here — log each accurately:
                #
                # 1. any_tier_needed=True  → a tier was below threshold but
                #    _smart_topup_wallet refused to split (reserve guard, reserve
                #    too small, no small coins). Reason already logged there.
                #
                # 2. any_tier_needed=False, wallet has balance → all tiers are
                #    above their action thresholds; coins deployed in offers.
                #    This is the normal steady-state. Previously this was
                #    misreported as "All coins locked in offers" even when the
                #    reserve coin was free — the threshold mismatch between
                #    trigger (per-tier %) and worker (flat 30%) caused the worker
                #    to think no tier needed help. Fixed by aligning thresholds.
                #
                # 3. Wallet balance ≤ reserve+1 XCH → genuinely nothing to split.
                #
                # F48 (2026-04-09) BUG FIX: confirmed_wallet_balance is nested
                # inside "wallet_balance", not at the top level. Fixed by reading
                # from the nested dict.
                from wallet import get_wallet_balance
                try:
                    xch_bal = get_wallet_balance(cfg.WALLET_ID_XCH)
                    wb_nested = (xch_bal or {}).get("wallet_balance") or {}
                    xch_total = Decimal(str(wb_nested.get("confirmed_wallet_balance", 0))) / Decimal("1000000000000")
                except Exception:
                    xch_total = Decimal("0")

                _is_drip = self._topup_is_drip
                if any_tier_needed:
                    # Tier was low but the split either timed out, was refused by the
                    # XCH_RESERVE/budget guard, or had no suitable source coin this cycle.
                    # The specific reason was already logged inside _smart_topup_wallet.
                    # Coins may have landed on chain anyway (slow block) — next cycle will
                    # re-evaluate and skip if tiers are now adequate.
                    if _is_drip:
                        self._last_drip_time = time.time()
                    else:
                        self._last_topup_time = time.time()
                        # A failed emergency attempt should also pause the
                        # proactive drip path. Otherwise the drip gate can
                        # immediately re-enter the same impossible split while
                        # the emergency cooldown is doing its job.
                        self._last_drip_time = time.time()
                    log_event("info", "topup_split_blocked",
                              f"{'Drip' if _is_drip else 'Topup'} needed but split did not complete "
                              f"this cycle ({xch_total:.4f} XCH in wallet) — will re-evaluate next cycle")
                    return
                elif xch_total > cfg.XCH_RESERVE + Decimal("1"):
                    # All tiers above the current invocation threshold.
                    if _is_drip:
                        self._last_drip_time = time.time()
                        log_event("info", "drip_adequate",
                                  f"Drip: all tiers at or above refill threshold ({xch_total:.4f} XCH) "
                                  f"— next drip in {_TOPUP_DRIP_INTERVAL}s")
                    else:
                        self._last_topup_time = time.time()
                        log_event("info", "topup_tiers_adequate",
                                  f"All tiers above emergency threshold ({xch_total:.4f} XCH in wallet) "
                                  f"— coins deployed in active offers (normal state)")
                    return
                else:
                    if _is_drip:
                        # Drip found no reserve coins — back off the drip interval,
                        # don't set emergency backoff (reserve shortage is expected during
                        # a full ladder; emergency topup handles it when truly needed).
                        self._last_drip_time = time.time()
                        log_event("info", "drip_no_coins",
                                  f"Drip: no reserve coins available to split "
                                  f"— next drip in {_TOPUP_DRIP_INTERVAL}s")
                    else:
                        self._no_coins_backoff = True
                        self._no_coins_backoff_count += 1
                        self._last_topup_time = time.time()
                        backoff_secs = min(
                            _TOPUP_BACKOFF_MAX,
                            _TOPUP_BACKOFF_BASE * (2 ** self._no_coins_backoff_count),
                        )
                        log_event("info", "topup_no_action",
                                  f"No coins available to split or consolidate — "
                                  f"backing off {backoff_secs//60:.0f} min "
                                  f"(attempt {self._no_coins_backoff_count}, "
                                  f"resets on fills or successful topup)")
                    return

            # SINGLE-ACTION success path: skip the long confirmation poll.
            # Set the drip timer to now so the next cycle fires in ~90s —
            # enough time for the TX to confirm on-chain. If confirmation
            # doesn't arrive in 90s, the next cycle will re-evaluate:
            # either the tier is satisfied (coins arrived) or the locked
            # source coin means _smart_topup_wallet returns False again
            # (handled gracefully by the "split blocked" path above).
            if did_anything:
                if self._topup_is_drip:
                    self._last_drip_time = time.time()
                else:
                    self._last_topup_time = time.time()
                self._no_coins_backoff = False
                self._no_coins_backoff_count = 0
                self._clear_topup_budget_backoff("topup action succeeded")
                log_event("info", "topup_single_action_done",
                          f"Single-action topup complete — "
                          f"next check in ~{_TOPUP_DRIP_INTERVAL}s for TX confirmation")
                return

            # No split happened — run the confirmation poll in case a previous
            # split is still pending (this path should be rare with single-action).
            pre_xch = len(xch_records)
            pre_cat = len(cat_records)
            self._poll_for_confirmation(pre_xch, pre_cat)

            # Success — reset appropriate cooldown timer and backoff counter
            if self._topup_is_drip:
                self._last_drip_time = 0   # check again soon for more drip work
            else:
                self._last_topup_time = 0  # immediate re-evaluation next cycle
            self._no_coins_backoff = False
            self._no_coins_backoff_count = 0
            self._clear_topup_budget_backoff("topup confirmation succeeded")

            # V3: Re-check reserve after topup (in case reserve was split)
            if cfg.TIER_ENABLED:
                self._ensure_reserve_exists("xch", xch_records)
                self._ensure_reserve_exists("cat", cat_records)

        except _TopupWalletDegraded:
            self._last_topup_time = time.time()
            self._no_coins_backoff = False
        except Exception as e:
            # F9 fix (2026-04-08): log full traceback so the operator can
            # actually diagnose the crash. Bare repr() loses the call site.
            try:
                import traceback as _tb
                _trace = _tb.format_exc()
            except Exception:
                _trace = ""
            log_event("error", "topup_error",
                      f"Topup worker error: {e}",
                      data={"traceback": _trace[:2000]})
        finally:
            # F9 fix (2026-04-08): the original finally cleared the running
            # flag and then called update_coin_counts() + log_inventory()
            # OUTSIDE its own try/except. If either of those raised, the
            # `finally` block itself would re-raise out of the worker
            # thread. The flag would still be cleared (good), but the
            # exception would surface as an uncaught thread crash with no
            # log. Wrap each cleanup step independently so a failure in
            # one doesn't break the others.
            try:
                with self._lock:
                    self._topup_running = False
                    self._topup_stop_requested = False
            except Exception as _flag_err:
                log_event("error", "topup_flag_clear_failed",
                          f"Failed to clear _topup_running flag: {_flag_err}")
            try:
                self.update_coin_counts()
            except Exception as _count_err:
                log_event("warning", "topup_post_count_failed",
                          f"update_coin_counts after topup failed: {_count_err}")
            try:
                self.log_inventory()
            except Exception as _inv_err:
                log_event("warning", "topup_post_inv_failed",
                          f"log_inventory after topup failed: {_inv_err}")

    def _smart_topup_wallet(self, name: str, wallet_id: int,
                             inventory: Dict[str, list],
                             trading_size_mojos: int, needed: int,
                             is_cat: bool = False,
                             tier_is_empty: bool = False,
                             soft_budget_bypass_reason: Optional[str] = None) -> bool:
        """Smart topup for one wallet. Returns True if an action was taken.

        Two-step process (mirrors coin_prep_worker):
          Step 1: Send-to-self to create an intermediate coin of exact size
                  (num_coins × coin_size mojos)
          Step 2: Track the new coin ID via snapshot/diff, then split it

        Fallback strategies:
          Strategy 1: Use a reserve coin as the funding source
          Strategy 2: Consolidate small coins first
        """
        reserve_coins = inventory["reserve"]
        small_coins = inventory["small"]
        pool_rebuild_extra_records = []

        # ---- Strategy 1: Use a reserve coin to create trading coins ----
        if reserve_coins:
            # Re-fetch FRESH coins (IDs may be stale after other transactions)
            _refetch_event = f"topup_{name.lower()}_refetch"
            log_event(_topup_event_log_level(_refetch_event), _refetch_event,
                      f"Re-fetching fresh {name} coins before split...")

            fresh_result = _get_free_coins_rpc(wallet_id)
            fresh_records = _extract_coin_records(fresh_result)
            fresh_records_by_id = {}
            for _fresh_rec in fresh_records:
                _fresh_id = _coin_id_from_record(_fresh_rec)
                if _fresh_id:
                    fresh_records_by_id[str(_fresh_id).lower()] = _fresh_rec
            # Use a loose threshold: any coin >= 1x trading_size can fund a split.
            # The old 2x threshold was too strict for the inner tier — the reserve
            # coin (e.g. 11.56 XCH) was smaller than 2 × 6.09 XCH = 12.18 XCH,
            # causing the re-fetch to see "no reserve" and silently abort.
            fresh_inv = _classify_coins(fresh_records, trading_size_mojos // 2 or 1)

            # Sieve out coins that DB already designated as tier_spare or
            # tier_active — otherwise the refetch path, which only uses
            # size-based classification, would happily pick a healthy
            # inner-tier spare as the funding pool for an outer-tier
            # split, degrading the other tier's coin inventory. DB
            # designations are authoritative: only `reserve`, `unknown`,
            # or empty designations are safe to use as a topup funding
            # source. Fall back to raw classification if the DB query
            # fails (fail-open) so a DB blip can't block every topup.
            try:
                from database import get_connection as _db_conn_fresh
                _wallet_type_str = "cat" if is_cat else "xch"
                _rows_fresh = _db_conn_fresh().execute(
                    "SELECT coin_id, designation, assigned_tier FROM coins "
                    "WHERE wallet_type=? AND status IN ('free', 'locked')",
                    (_wallet_type_str,),
                ).fetchall()
                _db_designations = {
                    str(_r["coin_id"]).lower(): str(
                        _r["designation"] or "unknown"
                    ).lower()
                    for _r in _rows_fresh
                    if _r["coin_id"]
                }
                _reserved_for_tiers = {
                    str(_r["coin_id"]).lower()
                    for _r in _rows_fresh
                    if (_r["designation"] or "").lower() in ("tier_spare", "tier_active")
                }
                if _reserved_for_tiers:
                    _safe_reserve = []
                    for _c in fresh_inv["reserve"]:
                        _cid = _coin_id_from_record(_c)
                        if str(_cid or "").lower() not in _reserved_for_tiers:
                            _safe_reserve.append(_c)
                    if len(_safe_reserve) < len(fresh_inv["reserve"]):
                        log_event(
                            "debug",
                            f"topup_{name.lower()}_reserve_tier_sieve",
                            f"Refetch: excluded "
                            f"{len(fresh_inv['reserve']) - len(_safe_reserve)} "
                            f"coins already designated as tier_spare/tier_active "
                            f"to avoid poaching another tier's stock",
                        )
                    fresh_inv["reserve"] = _safe_reserve

                _deposit_threshold_mojos = _deposit_advisory_source_threshold_mojos(
                    is_cat=is_cat,
                    fallback_tier_mojos=trading_size_mojos,
                )
                _safe_reserve, _blocked_deposits = _filter_unallocated_deposit_sources(
                    fresh_inv["reserve"],
                    wallet_type=_wallet_type_str,
                    db_designations=_db_designations,
                    advised_coin_ids=_load_advised_deposit_coin_ids(),
                    threshold_mojos=_deposit_threshold_mojos,
                )
                if _blocked_deposits:
                    log_event(
                        "info",
                        f"topup_{name.lower()}_unallocated_deposit_wait",
                        f"{name} topup found {_blocked_deposits} "
                        f"unallocated deposit-sized coin(s); waiting for "
                        f"the deposit allocation prompt before using them.",
                    )
                fresh_inv["reserve"] = _safe_reserve
            except Exception:
                # Fail open: if DB is unreachable, fall back to local sizing.
                # The worst-case is the legacy poaching behaviour, which is
                # still no worse than refusing every topup on a DB blip.
                pass

            if not fresh_inv["reserve"]:
                # INFO not WARNING: this is a benign race when the pool
                # coin was just consumed by another concurrent split, or
                # when the designated reserve is now too small to fund this
                # tier by itself. If it is still selectable, Strategy 3 may
                # combine it with excess spares to rebuild a usable pool.
                for _reserve_rec in reserve_coins:
                    _reserve_id = _coin_id_from_record(_reserve_rec)
                    if not _reserve_id:
                        continue
                    _fresh_reserve = fresh_records_by_id.get(str(_reserve_id).lower())
                    if _fresh_reserve is not None:
                        pool_rebuild_extra_records.append(_fresh_reserve)
                log_event("info", f"topup_{name.lower()}_reserve_gone",
                          f"{name} topup pool coin is no longer large enough "
                          f"or selectable for a direct split — checking pool "
                          f"rebuild options.")
                small_coins = fresh_inv["small"]
            else:
                largest = fresh_inv["reserve"][0]
                largest_amount = _coin_amount(largest)
                source_coin_id = _coin_id_from_record(largest)

                if is_cat:
                    amt_str = _format_amount_cat(largest_amount, cfg.CAT_DECIMALS)
                    size_str = _format_amount_cat(trading_size_mojos, cfg.CAT_DECIMALS)
                else:
                    amt_str = _format_amount_xch(largest_amount)
                    size_str = _format_amount_xch(trading_size_mojos)

                max_possible = largest_amount // trading_size_mojos
                num_to_create = min(needed, max_possible, 15)

                # Auto-scale request to what the remaining topup pool
                # budget can fund. When TOPUP_POOL_* is smaller than a full
                # refill (common when Smart Settings was last run before
                # the tier deficit grew), a full-ask would fail the budget
                # guard entirely and the tier would stay permanently short.
                # Instead, take the min of needed/source/safety/budget so
                # a partial refill makes forward progress. When the tier is
                # empty, the guard's empty_tier_bypass lets us overshoot —
                # skip the pre-clamp so the bypass path still works.
                # Same skip applies when the wallet has enough excess
                # unlocked spare coins to back the request: the soft
                # budget exists to stop unbounded spend, but with excess
                # spares already on hand the spend is bounded by what
                # we already hold (hard reserve still applies inside
                # _check_topup_reserve_guards).
                bypass_pre_clamp = tier_is_empty or bool(soft_budget_bypass_reason)
                if not bypass_pre_clamp:
                    requested_pool_mojos = num_to_create * trading_size_mojos
                    excess_mojos_pre = self._unlocked_excess_spare_mojos(
                        inventory=inventory,
                        is_cat=is_cat,
                    )
                    if excess_mojos_pre >= requested_pool_mojos:
                        bypass_pre_clamp = True
                if not bypass_pre_clamp:
                    budget_cap = self._max_coins_within_topup_budget(
                        is_cat=is_cat,
                        trading_size_mojos=trading_size_mojos,
                    )
                    if budget_cap is not None and budget_cap < num_to_create:
                        if budget_cap < 1:
                            self._mark_topup_budget_backoff(
                                name=name,
                                is_cat=is_cat,
                                trading_size_mojos=trading_size_mojos,
                            )
                            log_event(
                                "info",
                                f"topup_{name.lower()}_budget_empty_skip",
                                f"{name} topup pool budget exhausted — skipping "
                                f"split (would fund 0 coins). Re-run Smart "
                                f"Settings to replenish the budget."
                            )
                            num_to_create = 0
                        else:
                            log_event(
                                "info",
                                f"topup_{name.lower()}_budget_scaled",
                                f"{name} request scaled {num_to_create} → "
                                f"{budget_cap} to fit remaining topup pool "
                                f"budget. Deficit will be refilled over "
                                f"multiple cycles."
                            )
                            num_to_create = budget_cap

                if num_to_create < 1:
                    log_event("info", f"topup_{name.lower()}_skip",
                              f"{name} topup skipped — "
                              f"{'pool coin ({}) too small for even 1 trading coin ({})'.format(amt_str, size_str) if largest_amount < trading_size_mojos else 'budget cap reached'}")
                else:
                    # Calculate exact intermediate coin size
                    pool_amount_mojos = num_to_create * trading_size_mojos

                    if is_cat:
                        pool_str = _format_amount_cat(pool_amount_mojos, cfg.CAT_DECIMALS)
                    else:
                        pool_str = _format_amount_xch(pool_amount_mojos)

                    # F49 (2026-04-09): two-tier reserve enforcement.
                    #
                    # Check 1: HARD GUARD against the user's untouchable
                    # reserve. If splitting this pool would drop the
                    # wallet's total below XCH_RESERVE / CAT_RESERVE,
                    # refuse unconditionally. The user said "do not
                    # touch this amount no matter what" — we honour it.
                    #
                    # Check 2: TOPUP POOL BUDGET. If Smart Settings has
                    # set an explicit topup pool (TOPUP_POOL_XCH /
                    # TOPUP_POOL_CAT), track running spending in
                    # bot_settings and refuse when the budget is used up.
                    # Operators replenish by re-running Smart Settings.
                    guard_ok = self._check_topup_reserve_guards(
                        name=name,
                        wallet_id=wallet_id,
                        pool_amount_mojos=pool_amount_mojos,
                        is_cat=is_cat,
                        tier_is_empty=tier_is_empty,
                        inventory=inventory,
                        soft_budget_bypass_reason=soft_budget_bypass_reason,
                    )

                    if not guard_ok:
                        # Guard refused the split. Reason already logged
                        # inside the helper. Fall through to Strategy 2
                        # (small-coin consolidation) which is a safer
                        # refill source that doesn't risk the reserve.
                        small_coins = fresh_inv["small"]
                    else:
                        log_event("info", f"topup_{name.lower()}_start",
                                  f"Creating {name} pool coin ({pool_str}) from topup pool coin "
                                  f"({amt_str}) → will split into {num_to_create} × {size_str} "
                                  f"[source: {source_coin_id[:12]}...]")

                        success = self._two_step_split(
                            name=name,
                            wallet_id=wallet_id,
                            source_coin_id=source_coin_id,
                            pool_amount_mojos=pool_amount_mojos,
                            num_to_create=num_to_create,
                            trading_size_mojos=trading_size_mojos,
                            is_cat=is_cat,
                        )

                        if success is True:
                            # F49: record successful spend against the topup
                            # pool budget so the next cycle knows what's left.
                            try:
                                self._record_topup_pool_spend(
                                    is_cat=is_cat,
                                    amount_mojos=pool_amount_mojos,
                                )
                            except Exception as _budget_err:
                                log_event("debug", f"topup_{name.lower()}_budget_record_failed",
                                          f"Failed to record topup pool spend: {_budget_err}")

                            log_event("success", f"topup_{name.lower()}_split_ok",
                                      f"{name} topup complete: {num_to_create} new trading coins")
                            return True
                        elif success == _TOPUP_PENDING:
                            log_event("info", f"topup_{name.lower()}_split_wait",
                                      f"{name} split already submitted — waiting for "
                                      "wallet/chain state to settle")
                            return False
                        else:
                            log_event("info", f"topup_{name.lower()}_split_fail",
                                      f"{name} two-step split failed — will retry next cycle")
                            # Fall through to strategy 2

        # ---- Strategy 2: Consolidate small coins ----
        # Trigger with ≥2 small coins (down from 3) so price-shift misfits
        # and post-fill dust don't sit idle indefinitely with just 1–2 coins.
        if len(small_coins) >= 2:
            total_small = sum(_coin_amount(r) for r in small_coins)

            if total_small >= trading_size_mojos * 2:
                if is_cat:
                    total_str = _format_amount_cat(total_small, cfg.CAT_DECIMALS)
                else:
                    total_str = _format_amount_xch(total_small)

                log_event("info", f"topup_{name.lower()}_consolidate",
                          f"Consolidating {len(small_coins)} small {name} coins "
                          f"({total_str} total) into one coin for splitting")

                # Explicit coin IDs prevent Sage from picking coins from other
                # tiers (e.g. freshly-created tier_spare coins) when building
                # this consolidation transaction.
                _small_ids = [
                    _coin_id_from_record(r) for r in small_coins
                ]
                _small_ids = [cid for cid in _small_ids if cid]
                success = self._consolidate_coins(
                    name, wallet_id, total_small, is_cat,
                    source_coin_ids=_small_ids or None,
                )
                if success is True:
                    log_event("success", f"topup_{name.lower()}_consolidate_ok",
                              f"{name} consolidation submitted — will split after confirmation")
                    return True
                elif success == _TOPUP_PENDING:
                    log_event("info", f"topup_{name.lower()}_consolidate_wait",
                              f"{name} consolidation already submitted — waiting for "
                              "wallet/chain state to settle")
                    return False
                elif self._last_consolidate_not_submitted:
                    log_event(
                        "info",
                        f"topup_{name.lower()}_consolidate_not_submitted",
                        f"{name} consolidation was not submitted by Sage — will retry next topup cycle",
                    )
                    return False
                else:
                    log_event("warning", f"topup_{name.lower()}_consolidate_fail",
                              f"{name} consolidation failed")
            else:
                if is_cat:
                    total_str = _format_amount_cat(total_small, cfg.CAT_DECIMALS)
                    need_str = _format_amount_cat(trading_size_mojos * 2, cfg.CAT_DECIMALS)
                else:
                    total_str = _format_amount_xch(total_small)
                    need_str = _format_amount_xch(trading_size_mojos * 2)
                log_event("info", f"topup_{name.lower()}_small_insufficient",
                          f"{len(small_coins)} small {name} coins total {total_str} "
                          f"(need at least {need_str} to consolidate)")

        # ---- Strategy 3: Reconstitute topup pool from excess tier_spare coins ----
        #
        # Triggered when: (a) no reserve / pool coin exists, and (b) not enough
        # small coins to consolidate via Strategy 2.
        #
        # Root cause: the topup pool coin (a large "reserve" coin) was consumed to
        # split coins for a tier (e.g. extreme), leaving no funding source for future
        # splits. Coins that return from filled / cancelled / expired offers land in
        # tier buckets at exactly trading-size — too large for Strategy 2's "small"
        # threshold and too small to be classified as "reserve" on their own.
        #
        # Fix: collect all tier_spare coins across every tier bucket, keep a small
        # safety buffer (POOL_REBUILD_KEEP_PER_TIER) so existing offers can settle,
        # then consolidate the remainder into one large coin. That coin will be
        # classified as "reserve" on the next inventory scan and Strategy 1 can
        # split it for whichever tier is low.
        _POOL_REBUILD_KEEP = 1   # spare coins to leave in each tier bucket
        _POOL_REBUILD_MIN  = 2   # minimum excess coins required to attempt rebuild

        # Compute per-tier targets so we never poach from a tier that is
        # itself below target. Previously the rebuild treated any coins
        # beyond `_POOL_REBUILD_KEEP` as excess, which meant a freshly-
        # refilled inner tier (say 5/10) could be stripped to build
        # mid — undoing the split we just did. A coin is only "excess"
        # if the tier already has >= its target count.
        try:
            _rebuild_max_offers = int(
                getattr(
                    cfg,
                    "MAX_ACTIVE_SELL_OFFERS" if is_cat else "MAX_ACTIVE_BUY_OFFERS",
                    25,
                ) or 25
            )
            _rebuild_multiplier = getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0"))
            _rebuild_side_key = "cat" if is_cat else "xch"
            _rebuild_tier_sizes = (
                self._configured_tier_sizes_xch(side="sell")
                if is_cat else
                self._configured_tier_sizes_xch(side="buy")
            )
            _per_tier_targets = get_weighted_tier_prep_counts(
                _rebuild_max_offers, _rebuild_multiplier,
                tier_sizes_xch=_rebuild_tier_sizes,
                side=_rebuild_side_key,
            )
        except Exception:
            # Fail-open: if we can't compute targets, fall back to the
            # original behaviour (treat >_POOL_REBUILD_KEEP as excess).
            _per_tier_targets = {}

        _pool_candidates = []
        # Deliberately exclude "sniper" and "fees" from rebuild candidates:
        # those tiers use different coin sizes and won't be replenished by the
        # trading-tier split that follows. Consuming them here destroys the
        # sniper/fee pools with no recovery path.
        for _tname in ["inner", "mid", "outer", "extreme"]:
            _bucket = inventory.get(_tname, [])
            if not _bucket:
                continue
            _have = len(_bucket)
            _target = int(_per_tier_targets.get(_tname, 0) or 0)
            if _target > 0 and _have < _target:
                # Tier is below its own target — do NOT poach. Rebuilding
                # another tier from this would create an infinite
                # split-then-steal loop. Skip silently; _pool_rebuild_none
                # below will log if no candidates remain.
                continue
            # Keep the safety buffer AND never go below target.
            _floor = max(_POOL_REBUILD_KEEP, _target)
            _take = max(0, _have - _floor)
            _pool_candidates.extend(_bucket[:_take])

        if pool_rebuild_extra_records:
            _seen_candidate_ids = {
                str(_coin_id_from_record(_rec) or "").lower()
                for _rec in _pool_candidates
            }
            for _extra_rec in pool_rebuild_extra_records:
                _extra_id = _coin_id_from_record(_extra_rec)
                _extra_key = str(_extra_id or "").lower()
                if _extra_key and _extra_key not in _seen_candidate_ids:
                    _pool_candidates.append(_extra_rec)
                    _seen_candidate_ids.add(_extra_key)

        _priority_rebuild = bool(tier_is_empty or soft_budget_bypass_reason)
        if _priority_rebuild and sum(_coin_amount(r) for r in _pool_candidates) < (
            trading_size_mojos * 2
        ):
            _requested_tier = ""
            if "-" in name:
                _requested_tier = name.split("-", 1)[1].lower()
            _tier_priority = (
                ["extreme", "outer", "mid", "inner"]
                if (not is_cat and getattr(cfg, "BUY_LADDER_REVERSED", False))
                else ["inner", "mid", "outer", "extreme"]
            )
            try:
                _borrow_order = _tier_priority[
                    _tier_priority.index(_requested_tier) + 1:
                ]
            except ValueError:
                _borrow_order = []

            if _borrow_order:
                _seen_candidate_ids = {
                    str(_coin_id_from_record(_rec) or "").lower()
                    for _rec in _pool_candidates
                }
                _pool_total_now = sum(_coin_amount(r) for r in _pool_candidates)
                _borrowed_count = 0
                for _tname in _borrow_order:
                    _available = []
                    for _rec in inventory.get(_tname, []):
                        _cid = str(_coin_id_from_record(_rec) or "").lower()
                        if _cid and _cid not in _seen_candidate_ids:
                            _available.append((_cid, _rec))
                    _take_limit = max(0, len(_available) - _POOL_REBUILD_KEEP)
                    for _cid, _rec in _available[:_take_limit]:
                        _pool_candidates.append(_rec)
                        _seen_candidate_ids.add(_cid)
                        _pool_total_now += _coin_amount(_rec)
                        _borrowed_count += 1
                        if _pool_total_now >= trading_size_mojos * 2:
                            break
                    if _pool_total_now >= trading_size_mojos * 2:
                        break

                if _borrowed_count:
                    log_event(
                        "info",
                        f"topup_{name.lower()}_pool_rebuild_priority_borrow",
                        f"Borrowing {_borrowed_count} lower-priority spare "
                        f"coin(s) to rebuild the {name} topup pool "
                        f"({soft_budget_bypass_reason or 'empty tier'}).",
                    )

        if len(_pool_candidates) >= _POOL_REBUILD_MIN:
            _pool_total = sum(_coin_amount(r) for r in _pool_candidates)
            # Only worth consolidating if the result would be at least 2× a
            # trading coin (otherwise it can't fund even a single split next cycle)
            if _pool_total >= trading_size_mojos * 2:
                if is_cat:
                    _pool_str = _format_amount_cat(_pool_total, cfg.CAT_DECIMALS)
                else:
                    _pool_str = _format_amount_xch(_pool_total)

                log_event("info", f"topup_{name.lower()}_pool_rebuild",
                          f"No {name} reserve coin — rebuilding topup pool from "
                          f"{len(_pool_candidates)} excess spare coins "
                          f"({_pool_str} total). Pool will be available after "
                          f"on-chain confirmation.")

                # Pass explicit coin IDs so Sage only spends the intended
                # excess spare coins — not freshly-created coins from concurrent
                # topup splits that haven't been registered in the DB yet.
                _pool_candidate_ids = [
                    _coin_id_from_record(r) for r in _pool_candidates
                ]
                _pool_candidate_ids = [cid for cid in _pool_candidate_ids if cid]
                _rebuild_ok = self._consolidate_coins(
                    name, wallet_id, _pool_total, is_cat,
                    source_coin_ids=_pool_candidate_ids or None,
                )
                if _rebuild_ok is True:
                    log_event("success", f"topup_{name.lower()}_pool_rebuild_ok",
                              f"{name} topup pool rebuild submitted — new reserve coin "
                              f"will be classified and split on the next topup cycle.")
                    # Excess tier_spare coins are value returning to the topup
                    # pool, same as misfit absorption. Credit the soft-budget
                    # counter back now so a successful pool rebuild does not
                    # leave the budget guard thinking those coins are still
                    # permanently spent.
                    self._record_topup_pool_refund(is_cat, int(_pool_total))
                    # Return "rebuild" (not True) so the caller can distinguish a
                    # pool consolidation from an actual tier split. Pool rebuilds
                    # should NOT consume the single-action slot — the opposite side
                    # (CAT vs XCH) may still have splits to do this cycle, and
                    # blocking it for a consolidation causes CAT inner to starve.
                    return "rebuild"
                elif _rebuild_ok == _TOPUP_PENDING:
                    log_event("info", f"topup_{name.lower()}_pool_rebuild_wait",
                              f"{name} topup pool rebuild already submitted — "
                              "waiting for wallet/chain state to settle")
                    return False
                elif self._last_consolidate_not_submitted:
                    log_event(
                        "info",
                        f"topup_{name.lower()}_pool_rebuild_not_submitted",
                        f"{name} topup pool rebuild was not submitted by Sage — will retry next topup cycle",
                    )
                    return False
                else:
                    log_event("warning", f"topup_{name.lower()}_pool_rebuild_fail",
                              f"{name} topup pool rebuild consolidation failed")
            else:
                if is_cat:
                    _pool_str = _format_amount_cat(_pool_total, cfg.CAT_DECIMALS)
                    _need_str = _format_amount_cat(trading_size_mojos * 2, cfg.CAT_DECIMALS)
                else:
                    _pool_str = _format_amount_xch(_pool_total)
                    _need_str = _format_amount_xch(trading_size_mojos * 2)
                log_event("info", f"topup_{name.lower()}_pool_rebuild_insufficient",
                          f"{len(_pool_candidates)} candidate coins total {_pool_str} "
                          f"(need at least {_need_str} to rebuild pool)")

        log_event("info", f"topup_{name.lower()}_none",
                  f"No {name} reserve or consolidatable coins available")
        return False

    # -------------------------------------------------------------------
    # F49 (2026-04-09): Two-tier reserve enforcement helpers
    #
    # The bot's topup worker splits large wallet coins to replenish
    # trading tiers. Two rules protect the user's capital:
    #
    #   (1) HARD RESERVE (XCH_RESERVE / CAT_RESERVE)
    #       Absolute floor set by the user in step 1 of settings.
    #       NEVER split if doing so would drop the wallet below this.
    #       No exceptions, no configuration bypass.
    #
    #   (2) TOPUP POOL BUDGET (TOPUP_POOL_XCH / TOPUP_POOL_CAT)
    #       Working allocation set by Smart Settings. This is the
    #       budget the topup worker is permitted to consume across
    #       the current session. Once spent, further splits are
    #       refused until the operator re-runs Smart Settings (which
    #       resets the spend counter as part of saving new values).
    #
    # Budget tracking lives in bot_settings under the keys
    # `topup_pool_xch_spent_mojos` and `topup_pool_cat_spent_mojos`.
    # The counters reset on each successful `cfg.update()` call that
    # writes TOPUP_POOL_* (the frontend does this whenever Smart
    # Settings saves new values).
    # -------------------------------------------------------------------

    def _check_topup_reserve_guards(self, name: str, wallet_id: int,
                                    pool_amount_mojos: int,
                                    is_cat: bool,
                                    tier_is_empty: bool = False,
                                    inventory: Optional[Dict[str, list]] = None,
                                    soft_budget_bypass_reason: Optional[str] = None) -> bool:
        """Return True if a split of `pool_amount_mojos` is permitted.

        Checks (in order):
          1. Hard reserve guard — wallet total after split must remain
             at or above XCH_RESERVE / CAT_RESERVE.
          2. Topup pool budget — running session spend + this split
             must not exceed TOPUP_POOL_XCH / TOPUP_POOL_CAT when those
             values are configured (> 0). If configured to 0, the
             budget is treated as "unlimited" and only the hard reserve
             guard applies.

        Two budget escape hatches (the hard reserve guard is never
        bypassed — capital protection always applies):

          a. ``tier_is_empty=True`` — every offer on this slot is
             already blocked, which is worse than overspending the soft
             budget.
          b. ``soft_budget_bypass_reason`` — the caller has identified
             this as the floor-nearest live slot. The soft budget should
             not starve that slot while the hard reserve guard still
             passes.
          c. ``inventory`` shows excess unlocked spare coins across
             OTHER trading tiers that could fund this refill. As long
             as the wallet has spare capacity it should be allowed to
             use it; the soft budget exists to prevent unbounded spend
             when no excess exists. Sniper/fees coins, plus tiers
             below their own target, are excluded from the excess pool
             so we never poach a healthy tier to refill another.

        Logs a structured refusal event on block and returns False.
        On any unexpected error, logs a warning and returns True so
        the topup worker falls back to its legacy behaviour (fail open
        on the budget check — the hard reserve guard is still enforced
        separately before the split RPC is issued).
        """
        try:
            from wallet import get_wallet_balance
        except ImportError:
            return True

        # ---- Gate 1: Hard reserve guard ----
        # FAIL CLOSED on balance query errors. Previously a wallet RPC
        # flake let total_mojos fall through as None and the reserve check
        # silently skipped — the user's hard XCH/CAT_RESERVE floor could
        # be blown through exactly when Sage was already degraded. Refuse
        # the split until a fresh balance can be read.
        try:
            bal_raw = get_wallet_balance(wallet_id)
            wb = (bal_raw or {}).get("wallet_balance") or {}
            total_mojos = int(wb.get("confirmed_wallet_balance", 0) or 0)
        except Exception as exc:
            log_event(
                "warning",
                f"topup_{name.lower()}_balance_query_failed",
                f"Could not query wallet {wallet_id} balance for reserve guard: "
                f"{exc}. Refusing split — retry on next cycle once wallet RPC "
                f"is responsive. (fail-closed replaces former fail-open path.)",
            )
            return False

        if is_cat:
            scale = Decimal(10) ** Decimal(str(getattr(cfg, "CAT_DECIMALS", 3)))
            reserve_mojos = int(
                Decimal(str(getattr(cfg, "CAT_RESERVE", 0) or 0)) * scale
            )
            reserve_label = "CAT_RESERVE"
        else:
            reserve_mojos = int(
                Decimal(str(getattr(cfg, "XCH_RESERVE", 0) or 0))
                * Decimal("1000000000000")
            )
            reserve_label = "XCH_RESERVE"

        # "After split" = current total minus the tx fee we'll spend on
        # the split. The split itself keeps the total unchanged (coin
        # value is preserved across the spend bundle), so the only
        # actual decrement is the fee. We conservatively assume a
        # worst-case double-fee (pool creation + split). Use the
        # configured tx fee as a floor.
        #
        # UNITS: fees are paid from the XCH wallet even for CAT topups,
        # so the CAT balance is unaffected by the XCH fee. Subtracting
        # XCH-mojos from a CAT-mojos total used to produce false reserve
        # refusals near the CAT reserve floor (sell-side tier starvation
        # during otherwise healthy operation).
        if is_cat:
            fee_budget_mojos = 0
        else:
            try:
                from wallet_sage import get_effective_transaction_fee_mojos as _est_fee
                single_fee_mojos = int(_est_fee() or 0)
            except Exception:
                single_fee_mojos = int(
                    Decimal(str(getattr(cfg, "TRANSACTION_FEE_XCH", "0.000001") or 0))
                    * Decimal("1000000000000")
                )
            fee_budget_mojos = max(1, single_fee_mojos) * 2

        post_split_total = total_mojos - fee_budget_mojos
        if reserve_mojos > 0 and post_split_total < reserve_mojos:
            # Would drop below the user's hard floor — REFUSE.
            unit = "CAT" if is_cat else "XCH"
            scale_display = float(scale) if is_cat else 1e12
            log_event(
                "warning",
                f"topup_{name.lower()}_blocked_by_reserve",
                f"{name} split refused: splitting would drop wallet to "
                f"{post_split_total / scale_display:.6f} {unit} which is below "
                f"{reserve_label}={reserve_mojos / scale_display:.4f} {unit}. "
                f"User-configured hard reserve honoured. Increase "
                f"wallet balance or lower {reserve_label} to allow splits.",
            )
            return False

        # ---- Gate 2: Topup pool budget ----
        try:
            if is_cat:
                scale = Decimal(10) ** Decimal(str(getattr(cfg, "CAT_DECIMALS", 3)))
                budget_mojos = int(
                    Decimal(str(getattr(cfg, "TOPUP_POOL_CAT", 0) or 0)) * scale
                )
                budget_label = "TOPUP_POOL_CAT"
                spent_key = "topup_pool_cat_spent_mojos"
            else:
                budget_mojos = int(
                    Decimal(str(getattr(cfg, "TOPUP_POOL_XCH", 0) or 0))
                    * Decimal("1000000000000")
                )
                budget_label = "TOPUP_POOL_XCH"
                spent_key = "topup_pool_xch_spent_mojos"

            if budget_mojos > 0:
                # Budget explicitly set — track against it
                from database import get_setting as _get_setting
                try:
                    spent_raw = _get_setting(spent_key, "0")
                    spent_mojos = int(str(spent_raw) or "0")
                except Exception:
                    spent_mojos = 0

                projected = spent_mojos + pool_amount_mojos
                if projected > budget_mojos:
                    # CAT mojos have CAT_DECIMALS precision (default 3),
                    # XCH mojos have 12. Using /1e12 for both printed CAT
                    # amounts as 0.0001 when actual values were thousands.
                    _display_scale = float(
                        Decimal(10) ** Decimal(str(getattr(cfg, "CAT_DECIMALS", 3)))
                    ) if is_cat else 1e12
                    _unit = "CAT" if is_cat else "XCH"

                    if tier_is_empty:
                        # Empty tier = every offer on this slot is blocked.
                        # Letting the budget stop us here trades one protected
                        # number for a dead trading slot. The hard reserve
                        # above is the real capital guard — it already passed.
                        # INFO-level: this is the expected recovery path when
                        # the budget was sized smaller than one split. The
                        # warning severity was too loud for a designed bypass.
                        log_event(
                            "info",
                            f"topup_{name.lower()}_budget_bypass_empty_tier",
                            f"{name} tier is empty (0 free coins) — bypassing "
                            f"topup pool budget ({spent_mojos / _display_scale:.4f} "
                            f"spent + {pool_amount_mojos / _display_scale:.4f} "
                            f"requested > {budget_mojos / _display_scale:.4f} "
                            f"{_unit} {budget_label}). Hard reserve guard still "
                            f"honoured.",
                        )
                        return True

                    if soft_budget_bypass_reason:
                        log_event(
                            "info",
                            f"topup_{name.lower()}_budget_bypass_floor_priority",
                            f"{name} bypassing topup pool budget for "
                            f"{soft_budget_bypass_reason} "
                            f"({spent_mojos / _display_scale:.4f} spent + "
                            f"{pool_amount_mojos / _display_scale:.4f} "
                            f"requested > {budget_mojos / _display_scale:.4f} "
                            f"{_unit} {budget_label}). Hard reserve guard still "
                            f"honoured.",
                        )
                        return True

                    # Excess-spares bypass: the soft budget exists to stop
                    # unbounded spend, but if the wallet already holds
                    # enough excess unlocked spare coins (in tiers above
                    # their own target, excluding sniper/fees) to back this
                    # refill, the spend isn't unbounded — it's a normal
                    # working rotation. Allow the split and let normal
                    # rebuild paths replenish the pool. The hard reserve
                    # guard above already protects untouchable capital.
                    excess_mojos = self._unlocked_excess_spare_mojos(
                        inventory=inventory,
                        is_cat=is_cat,
                    )
                    if excess_mojos >= pool_amount_mojos:
                        log_event(
                            "info",
                            f"topup_{name.lower()}_budget_bypass_excess_spares",
                            f"{name} bypassing topup pool budget — wallet has "
                            f"{excess_mojos / _display_scale:.4f} {_unit} of excess "
                            f"unlocked spare coins, enough to back the requested "
                            f"{pool_amount_mojos / _display_scale:.4f} {_unit} split "
                            f"({spent_mojos / _display_scale:.4f} spent + "
                            f"{pool_amount_mojos / _display_scale:.4f} > "
                            f"{budget_mojos / _display_scale:.4f} {budget_label}). "
                            f"Hard reserve guard still honoured.",
                        )
                        return True

                    log_event(
                        "info",
                        f"topup_{name.lower()}_blocked_by_budget",
                        f"{name} split refused: topup pool budget exhausted "
                        f"({spent_mojos / _display_scale:.4f} spent + "
                        f"{pool_amount_mojos / _display_scale:.4f} requested > "
                        f"{budget_mojos / _display_scale:.4f} {_unit}, "
                        f"{budget_label}) and no excess unlocked spare coins to "
                        f"cover it. Re-run Smart Settings to replenish the topup pool.",
                    )
                    return False
        except Exception as exc:
            log_event("debug", f"topup_{name.lower()}_budget_check_failed",
                      f"Budget check raised {exc} — allowing split (hard reserve "
                      f"guard still enforced).")

        return True

    def _unlocked_excess_spare_mojos(self, inventory: Optional[Dict[str, list]],
                                     is_cat: bool) -> int:
        """Sum the mojos of unlocked tier_spare coins that exceed each
        tier's own target. Used by the budget guard's excess-spares
        bypass to know whether the wallet can cover a refill from
        already-held coins (without needing the operator to refill the
        soft topup pool budget).

        Excludes ``sniper`` and ``fees`` because those tiers use coin
        sizes that the trading-tier topup path can't replenish, so
        consuming them would strand those pools. Tiers below their own
        target are also excluded — poaching from them creates a
        split-then-steal loop. Same exclusion logic as the existing
        Strategy 3 (pool rebuild from excess spares).
        """
        if not inventory:
            return 0

        try:
            max_offers = int(
                getattr(
                    cfg,
                    "MAX_ACTIVE_SELL_OFFERS" if is_cat else "MAX_ACTIVE_BUY_OFFERS",
                    25,
                ) or 25
            )
            multiplier = getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0"))
            side_key = "cat" if is_cat else "xch"
            tier_sizes = (
                self._configured_tier_sizes_xch(side="sell")
                if is_cat else
                self._configured_tier_sizes_xch(side="buy")
            )
            per_tier_targets = get_weighted_tier_prep_counts(
                max_offers, multiplier,
                tier_sizes_xch=tier_sizes,
                side=side_key,
            )
        except Exception:
            per_tier_targets = {}

        KEEP = 1  # safety buffer (matches Strategy 3 _POOL_REBUILD_KEEP)
        total = 0
        for tname in ("inner", "mid", "outer", "extreme"):
            bucket = inventory.get(tname, []) or []
            if not bucket:
                continue
            have = len(bucket)
            target = int(per_tier_targets.get(tname, 0) or 0)
            if target > 0 and have < target:
                # Below own target — don't poach.
                continue
            floor = max(KEEP, target)
            take = max(0, have - floor)
            if take <= 0:
                continue
            for rec in bucket[:take]:
                total += int(_coin_amount(rec) or 0)
        return total

    def _max_coins_within_topup_budget(self, is_cat: bool,
                                       trading_size_mojos: int) -> Optional[int]:
        """Return the largest number of trading coins the remaining topup
        budget can fund, or None when no budget is configured (unlimited).

        Used to auto-scale refill requests when `TOPUP_POOL_*` is smaller
        than what a full deficit refill would cost. Without this, the guard
        refuses every request and the tier stays permanently short even
        while the budget has partial headroom.

        Returns 0 when the budget is exhausted — caller should skip the
        split entirely and let the existing guard log the refusal.
        """
        if trading_size_mojos <= 0:
            return None
        try:
            if is_cat:
                scale = Decimal(10) ** Decimal(str(getattr(cfg, "CAT_DECIMALS", 3)))
                budget = Decimal(str(getattr(cfg, "TOPUP_POOL_CAT", 0) or 0))
                budget_mojos = int(budget * scale)
                spent_key = "topup_pool_cat_spent_mojos"
            else:
                budget = Decimal(str(getattr(cfg, "TOPUP_POOL_XCH", 0) or 0))
                budget_mojos = int(budget * Decimal("1000000000000"))
                spent_key = "topup_pool_xch_spent_mojos"
        except Exception:
            return None

        if budget_mojos <= 0:
            return None  # unlimited — no cap

        try:
            from database import get_setting
            spent_mojos = int(str(get_setting(spent_key, "0") or "0"))
        except Exception:
            spent_mojos = 0

        remaining = max(0, budget_mojos - spent_mojos)
        return remaining // trading_size_mojos

    def _mark_topup_budget_backoff(self, name: str, is_cat: bool,
                                   trading_size_mojos: int) -> None:
        """Back off when the soft topup budget cannot fund one more coin."""
        now = time.time()
        self._topup_budget_backoff_count += 1
        backoff_secs = min(
            _TOPUP_BACKOFF_MAX,
            _TOPUP_BACKOFF_BASE * (2 ** max(0, self._topup_budget_backoff_count - 1)),
        )
        self._topup_budget_backoff_until = now + backoff_secs
        self._topup_budget_backoff_probe = {
            "name": str(name),
            "is_cat": bool(is_cat),
            "trading_size_mojos": int(trading_size_mojos),
        }
        # The drip gate used to let this same budget refusal run every bot cycle.
        # Stamp both clocks so the next retry follows the explicit backoff window.
        self._last_topup_time = now
        self._last_drip_time = now
        log_event(
            "info",
            "topup_budget_backoff",
            f"{name} topup budget cannot fund another coin; backing off "
            f"{int(backoff_secs // 60)} min. Re-run Smart Settings to replenish "
            f"the budget, or wait for fills/refunds to restore headroom.",
        )

    def _clear_topup_budget_backoff(self, reason: str) -> None:
        if self._topup_budget_backoff_until <= 0 and self._topup_budget_backoff_count == 0:
            return
        self._topup_budget_backoff_until = 0
        self._topup_budget_backoff_count = 0
        self._topup_budget_backoff_probe = None
        log_event("debug", "topup_budget_backoff_reset",
                  f"Topup budget backoff reset: {reason}")

    def _topup_budget_backoff_active(self) -> bool:
        """Return True while a budget-exhausted topup is cooling down.

        If Smart Settings has replenished the budget in the meantime, the
        stored probe sees that at least one coin can now be funded and clears
        the backoff immediately.
        """
        until = float(getattr(self, "_topup_budget_backoff_until", 0) or 0)
        if until <= 0:
            return False
        now = time.time()
        if now >= until:
            self._topup_budget_backoff_until = 0
            return False

        probe = getattr(self, "_topup_budget_backoff_probe", None)
        if probe:
            try:
                cap = self._max_coins_within_topup_budget(
                    is_cat=bool(probe.get("is_cat")),
                    trading_size_mojos=int(probe.get("trading_size_mojos") or 0),
                )
                if cap is None or cap >= 1:
                    self._clear_topup_budget_backoff("budget headroom returned")
                    return False
            except Exception:
                pass

        if now - self._last_low_coin_warning > 600:
            remaining = max(1, int((until - now) // 60))
            log_event(
                "info",
                "topup_budget_backoff_active",
                f"Topup budget exhausted; skipping retry for ~{remaining} min",
            )
            self._last_low_coin_warning = now
        return True

    def _record_topup_pool_spend(self, is_cat: bool, amount_mojos: int) -> None:
        """Persist an incremental topup pool spend to bot_settings.

        Idempotent across restarts: the counter is only cleared when
        Smart Settings writes new TOPUP_POOL_* values (the config
        writer clears it). No-op if the amount is zero or negative.
        """
        if amount_mojos <= 0:
            return
        try:
            from database import get_setting, set_setting
            key = "topup_pool_cat_spent_mojos" if is_cat else "topup_pool_xch_spent_mojos"
            current = int(str(get_setting(key, "0") or "0"))
            set_setting(key, str(current + int(amount_mojos)))
        except Exception as exc:
            log_event("debug", "topup_pool_spend_record_failed",
                      f"Could not record topup pool spend ({amount_mojos} mojos, "
                      f"is_cat={is_cat}): {exc}")

    def _record_topup_pool_refund(self, is_cat: bool, amount_mojos: int) -> None:
        """Credit an amount back to the topup pool spend counter.

        Mirror of _record_topup_pool_spend for the reverse direction:
        when coins are returned to the reserve (misfit absorption), the
        cumulative-spend counter must decrement so the budget guard
        continues to reflect the *net* amount carved from the pool. Without
        this, spent drifts permanently higher than reality — coins return
        to the reserve but the counter still treats them as committed —
        and tier refills get refused even while the reserve holds plenty
        of coins.

        Clamped at zero so an over-refund (e.g. manually-added coins
        joining a misfit absorption) can't go negative.
        """
        if amount_mojos <= 0:
            return
        try:
            from database import get_setting, set_setting
            key = "topup_pool_cat_spent_mojos" if is_cat else "topup_pool_xch_spent_mojos"
            current = int(str(get_setting(key, "0") or "0"))
            updated = max(0, current - int(amount_mojos))
            set_setting(key, str(updated))
        except Exception as exc:
            log_event("debug", "topup_pool_refund_record_failed",
                      f"Could not record topup pool refund ({amount_mojos} mojos, "
                      f"is_cat={is_cat}): {exc}")

    def _stamp_topup_output_designations(self, name: str, wallet_id: int,
                                         output_coin_ids: List[str],
                                         owned_amounts: Dict[str, int],
                                         is_cat: bool) -> None:
        """Persist intended tier designations for freshly split topup outputs.

        Sage one-step splits surface output IDs before the normal inventory scan
        has inserted/classified them. If sniper/fee-sized outputs reach that scan
        as unknown coins, the generic size classifier can route them to small/dust
        and the misfit absorber folds them back into reserve. Stamp the intended
        tier immediately while the split context still tells us what these coins
        were created for.
        """
        label = str(name or "").strip().lower()
        tier_name = label.rsplit("-", 1)[-1] if "-" in label else label
        valid_tiers = {"inner", "mid", "outer", "extreme", "sniper", get_fee_tier_name()}
        if tier_name not in valid_tiers:
            return

        wallet_type = "cat" if is_cat else "xch"
        stamped = 0
        try:
            from database import upsert_coin, set_coin_designation
            for raw_cid in output_coin_ids or []:
                cid = str(raw_cid or "").strip()
                if not cid:
                    continue
                amount = int((owned_amounts or {}).get(raw_cid)
                             or (owned_amounts or {}).get(cid)
                             or 0)
                if amount <= 0:
                    continue
                upsert_coin(
                    coin_id=cid,
                    wallet_type=wallet_type,
                    amount_mojos=amount,
                    tier=tier_name,
                    designation="tier_spare",
                    assigned_tier=tier_name,
                )
                set_coin_designation(cid, "tier_spare", tier_name)
                stamped += 1
        except Exception as exc:
            log_event("warning", f"topup_{label}_stamp_failed",
                      f"Could not stamp {wallet_type.upper()} topup outputs "
                      f"as {tier_name}: {exc}")
            return

        if stamped:
            log_event("debug", f"topup_{label}_outputs_stamped",
                      f"Stamped {stamped} {wallet_type.upper()} topup output "
                      f"coin(s) as tier_spare/{tier_name}")

    def _sage_one_step_split(self, name: str, wallet_id: int,
                              source_coin_id: str, num_to_create: int,
                              trading_size_mojos: int, is_cat: bool) -> bool:
        """Single-step topup split for Sage wallets using /create_transaction.

        /send_xch does not honour coin_ids hints (the SendXch Rust struct has no
        such field), so the classic two-step send-to-self → split would spend
        whatever free coins Sage picks — which are the tier_spare coins rather
        than the designated topup pool coin.

        /create_transaction DOES honour selected_coin_ids, so a single RPC call:
          1. Spends ONLY source_coin_id (the topup pool coin)
          2. Creates num_to_create outputs of exactly trading_size_mojos each
          3. Returns change to the wallet automatically

        The caller's confirmation logic is identical to the split-wait in
        _two_step_split step 2 — poll for num_to_create new owned coins that
        match trading_size_mojos.
        """
        from wallet_sage import sage_topup_split

        tag = f"topup_{name.lower()}"

        if self._topup_should_stop():
            log_event("info", f"{tag}_stopped",
                      f"{name} top-up stopped before one-step split")
            return False

        if not hasattr(self, "_recent_topup_split_submissions"):
            self._recent_topup_split_submissions = {}
        split_key = (
            f"{'cat' if is_cat else 'xch'}:{wallet_id}:"
            f"{str(source_coin_id or '').lower()}"
        )
        debounce_secs = int(getattr(cfg, "TOPUP_SPLIT_DEBOUNCE_SECS", 600) or 600)
        if debounce_secs > 0:
            now = time.time()
            self._recent_topup_split_submissions = {
                k: ts for k, ts in self._recent_topup_split_submissions.items()
                if now - ts < debounce_secs
            }
            last_submitted = self._recent_topup_split_submissions.get(split_key)
            if last_submitted is not None and now - last_submitted < debounce_secs:
                elapsed = int(now - last_submitted)
                log_event(
                    "info",
                    f"{tag}_osstep_debounce",
                    f"Skipping duplicate {name} one-step split from source "
                    f"{str(source_coin_id or '')[:12]}... submitted {elapsed}s ago; "
                    f"waiting for wallet/chain state to settle",
                )
                return _TOPUP_PENDING

        # --- Get own address for send-to-self outputs ---
        # Use the module-level get_next_address (from wallet adapter) so tests
        # can patch it and so the Chia backend also works if ever needed.
        try:
            addr_result = get_next_address(wallet_id=wallet_id, new_address=False)
            if not addr_result or not addr_result.get("success"):
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: Sage could not provide a wallet address "
                    f"for one-step split."
                )
            address = addr_result.get("address", "")
            if not address:
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: Sage returned an empty wallet address "
                    f"for one-step split."
                )
        except _TopupWalletDegraded:
            raise
        except Exception as e:
            self._abort_topup_for_wallet_degradation(
                f"{name} topup paused: wallet address lookup failed ({e})."
            )

        if is_cat:
            size_str = _format_amount_cat(trading_size_mojos, cfg.CAT_DECIMALS)
        else:
            size_str = _format_amount_xch(trading_size_mojos)

        # --- Snapshot before ---
        pre_owned_map = self._get_owned_coin_amount_map(wallet_id, f"{tag}-pre-osstep") or {}
        pre_owned_ids = set(pre_owned_map.keys())

        fee_mojos = self._tx_fee_mojos()
        fee_coin_id = None
        if is_cat and fee_mojos > 0 and self._fee_pool_enabled():
            fee_coin_id = self.fee_pool.reserve()
            if fee_coin_id:
                _fee_event = f"{tag}_fee_coin_reserved"
                log_event(_topup_event_log_level(_fee_event), _fee_event,
                          f"Reserved XCH fee coin {fee_coin_id[:12]}... "
                          f"for CAT top-up split")
            else:
                log_event("warning", f"{tag}_fee_coin_unavailable",
                          "Fee pool enabled but no unreserved XCH fee coin "
                          "is available for CAT top-up; refusing this split "
                          "so Sage cannot auto-select a pending fee input")
                return False

        _start_event = f"{tag}_osstep_start"
        log_event(_topup_event_log_level(_start_event), _start_event,
                  f"Sage one-step split: {num_to_create}×{size_str} from "
                  f"topup pool coin {source_coin_id[:12]}... via /create_transaction")

        # --- Submit the single create_transaction RPC ---
        weak_submit_onchain_confirmed = False
        try:
            result = sage_topup_split(
                source_coin_id=source_coin_id,
                num_coins=num_to_create,
                trading_size_mojos=trading_size_mojos,
                own_address=address,
                fee_mojos=fee_mojos,
                is_cat=is_cat,
                fee_coin_id=fee_coin_id,
            )
            if not result:
                if self._spacescan_self_send_confirmed(source_coin_id, address, tag):
                    log_event("info", f"{tag}_osstep_onchain_pending",
                              "Spacescan confirms the one-step split landed on-chain — "
                              "continuing while Sage catches up")
                    weak_submit_onchain_confirmed = True
                    result = {}
                else:
                    self._abort_topup_for_wallet_degradation(
                        f"{name} topup paused: /create_transaction returned no result."
                    )
            if isinstance(result, dict) and result.get("error"):
                err = result["error"]
                if self._spacescan_self_send_confirmed(source_coin_id, address, tag):
                    log_event("info", f"{tag}_osstep_onchain_pending",
                              "Spacescan confirms the one-step split landed on-chain — "
                              "continuing while Sage catches up")
                    weak_submit_onchain_confirmed = True
                elif self._looks_like_wallet_rpc_degradation(err):
                    self._abort_topup_for_wallet_degradation(
                        f"{name} topup paused: /create_transaction degraded ({err})."
                    )
                else:
                    log_event("warning", f"{tag}_osstep_fail",
                              f"/create_transaction error: {err}")
                    return False
        except _TopupWalletDegraded:
            raise
        except Exception as e:
            if self._spacescan_self_send_confirmed(source_coin_id, address, tag):
                log_event("info", f"{tag}_osstep_onchain_pending",
                          "Spacescan confirms the one-step split landed on-chain — "
                          "continuing while Sage catches up")
                weak_submit_onchain_confirmed = True
                result = {}
            elif self._looks_like_wallet_rpc_degradation(e):
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: /create_transaction RPC error ({e})."
                )
            else:
                log_event("warning", f"{tag}_osstep_error",
                          f"/create_transaction failed: {e}")
                return False

        tx_ids = self._extract_sage_transaction_ids(result or {})
        source_key = str(source_coin_id or "").strip().lower().replace("0x", "")

        def _source_still_selectable(selectable_ids: set) -> bool:
            selectable_keys = {
                str(cid or "").strip().lower().replace("0x", "")
                for cid in (selectable_ids or set())
            }
            return bool(source_key and source_key in selectable_keys)

        def _sage_pending_count() -> Optional[int]:
            try:
                from wallet_sage import get_pending_transactions
                pending = get_pending_transactions()
                if pending is not None:
                    return len(pending or [])
            except Exception:
                pass
            return None

        def _clear_split_debounce() -> None:
            if debounce_secs > 0:
                self._recent_topup_split_submissions.pop(split_key, None)

        if not tx_ids and not weak_submit_onchain_confirmed:
            pending_count = _sage_pending_count()
            selectable_now = (
                self._get_strict_selectable_coin_id_set(
                    wallet_id, f"{tag}-osstep-no-txid-sel"
                ) or set()
            )
            if pending_count == 0 and _source_still_selectable(selectable_now):
                log_event(
                    "info",
                    f"{tag}_osstep_not_submitted",
                    "/create_transaction returned no transaction id, Sage has no "
                    "pending transaction, and the source coin is still selectable; "
                    "treating the split as not submitted",
                    data={
                        "source_coin_id": str(source_coin_id or "")[:18],
                        "pending_count": pending_count,
                        "result_keys": (
                            sorted(result.keys())
                            if isinstance(result, dict)
                            else [type(result).__name__]
                        ),
                    },
                )
                return False
        if debounce_secs > 0:
            self._recent_topup_split_submissions[split_key] = time.time()
        log_event("info", f"{tag}_osstep_submitted",
                  "One-step split submitted"
                  + (f" (tx: {tx_ids[0][:16]}...)" if tx_ids else ""))

        def _amount_matches_target(amount: int, target: int) -> bool:
            if amount == target:
                return True
            tolerance = max(1, int(target * 0.01))
            return abs(amount - target) < tolerance

        # --- Wait for the N output coins to appear ---
        wait_start = time.time()
        wait_max = 120
        poll_interval = 4
        no_txid_grace_secs = 12
        tx_logged = False
        owned_logged = False

        while (time.time() - wait_start) < wait_max:
            if self._topup_should_stop():
                log_event("info", f"{tag}_stopped",
                          f"{name} top-up stopped while waiting for one-step split outputs")
                return False

            time.sleep(poll_interval)
            owned_map = self._get_owned_coin_amount_map(wallet_id, f"{tag}-osstep-owned") or {}
            selectable_ids = (
                self._get_strict_selectable_coin_id_set(wallet_id, f"{tag}-osstep-sel") or set()
            )
            tx_state = self._get_transaction_confirmation_state(tx_ids)
            elapsed = int(time.time() - wait_start)

            if tx_state["confirmed"] and not tx_logged:
                suffix = f" at height {tx_state['height']}" if tx_state["height"] else ""
                log_event("info", f"{tag}_osstep_tx_confirmed",
                          f"One-step split transaction confirmed{suffix}")
                tx_logged = True

            new_coins = [
                cid for cid, amt in owned_map.items()
                if cid not in pre_owned_ids
                and _amount_matches_target(amt, trading_size_mojos)
            ]
            owned_count = len(new_coins)
            sel_count = sum(1 for c in new_coins if c in selectable_ids)
            outputs_ready = owned_count >= num_to_create and sel_count >= num_to_create

            if (
                not tx_ids
                and not weak_submit_onchain_confirmed
                and elapsed >= no_txid_grace_secs
                and owned_count == 0
            ):
                pending_count = _sage_pending_count()
                source_selectable = _source_still_selectable(selectable_ids)
                if pending_count == 0 and source_selectable:
                    _clear_split_debounce()
                    log_event(
                        "info",
                        f"{tag}_osstep_not_submitted",
                        "/create_transaction returned no transaction id, no "
                        "pending transaction appeared after the submit grace "
                        "window, and the source coin is selectable again; "
                        "treating the split as not submitted",
                        data={
                            "source_coin_id": str(source_coin_id or "")[:18],
                            "pending_count": pending_count,
                            "elapsed": elapsed,
                            "result_keys": (
                                sorted(result.keys())
                                if isinstance(result, dict)
                                else [type(result).__name__]
                            ),
                        },
                    )
                    return False

            if owned_count >= num_to_create and not owned_logged:
                _owned_event = f"{tag}_osstep_outputs_owned"
                log_event(_topup_event_log_level(_owned_event), _owned_event,
                          f"One-step split outputs owned ({owned_count}/{num_to_create})")
                owned_logged = True

            if owned_count >= num_to_create and (outputs_ready or tx_state["confirmed"]):
                detail = (f"{sel_count}/{num_to_create} selectable" if outputs_ready
                          else f"{owned_count}/{num_to_create} owned, selectable lagging")
                log_event("info", f"{tag}_osstep_confirmed",
                          f"One-step split confirmed after {elapsed}s ({detail})")
                self._stamp_topup_output_designations(
                    name=name,
                    wallet_id=wallet_id,
                    output_coin_ids=new_coins[:num_to_create],
                    owned_amounts=owned_map,
                    is_cat=is_cat,
                )
                return True

            if elapsed % 20 == 0 and elapsed > 0:
                _wait_event = f"{tag}_osstep_wait"
                log_event(_topup_event_log_level(_wait_event), _wait_event,
                          f"Waiting for one-step split outputs... "
                          f"(tx={'confirmed' if tx_state['confirmed'] else 'pending'}, "
                          f"{owned_count}/{num_to_create} owned, "
                          f"{sel_count}/{num_to_create} selectable, {elapsed}s)")

        # --- Post-timeout check ---
        owned_map = self._get_owned_coin_amount_map(wallet_id, f"{tag}-osstep-timeout") or {}
        selectable_ids = (
            self._get_strict_selectable_coin_id_set(wallet_id, f"{tag}-osstep-timeout-sel") or set()
        )
        tx_state = self._get_transaction_confirmation_state(tx_ids)
        new_coins = [
            cid for cid, amt in owned_map.items()
            if cid not in pre_owned_ids
            and _amount_matches_target(amt, trading_size_mojos)
        ]
        owned_count = len(new_coins)
        sel_count = sum(1 for c in new_coins if c in selectable_ids)
        if tx_state["confirmed"] and owned_count >= num_to_create:
            log_event("info", f"{tag}_osstep_confirmed",
                      f"One-step split confirmed after timeout "
                      f"({owned_count}/{num_to_create} owned, selectable lagging)")
            self._stamp_topup_output_designations(
                name=name,
                wallet_id=wallet_id,
                output_coin_ids=new_coins[:num_to_create],
                owned_amounts=owned_map,
                is_cat=is_cat,
            )
            return True

        if not tx_ids and not weak_submit_onchain_confirmed:
            pending_count = _sage_pending_count()
            source_selectable = _source_still_selectable(selectable_ids)
            if source_selectable and pending_count in (0, None):
                _clear_split_debounce()
                log_event(
                    "info",
                    f"{tag}_osstep_not_submitted",
                    "One-step split timed out with no transaction id and the "
                    "source coin still selectable; clearing the split debounce "
                    "so the next topup pass can retry or fall back",
                    data={
                        "source_coin_id": str(source_coin_id or "")[:18],
                        "pending_count": pending_count,
                        "wait_max": wait_max,
                        "owned": owned_count,
                        "needed": num_to_create,
                        "selectable": sel_count,
                    },
                )
                return False

        # IMPORTANT: returning False here means the caller does NOT record
        # the intended spend against the topup-pool budget counter. If the
        # TX is actually in the mempool and confirms after this timeout,
        # the funding coin is consumed without the counter catching it —
        # subsequent cycles will think they have more budget than reality.
        # The check_topup_budget_drift self-heal (bot_health.py) reconciles
        # this drift against actual reserve size on each runtime-check
        # pass, so the damage is bounded. We log a warning so an operator
        # staring at the bot during a flake can spot the situation.
        outputs_owned = owned_count >= num_to_create and num_to_create > 0
        if outputs_owned:
            message = (
                f"One-step split outputs are owned but not selectable after {wait_max}s "
                f"(tx={'confirmed' if tx_state['confirmed'] else 'pending'}, "
                f"{owned_count}/{num_to_create} owned, "
                f"{sel_count}/{num_to_create} selectable). Runtime health will "
                "reconcile the top-up budget if the wallet settles later."
            )
        else:
            message = (
                f"One-step split not confirmed after {wait_max}s "
                f"(tx={'confirmed' if tx_state['confirmed'] else 'pending'}, "
                f"{owned_count}/{num_to_create} owned, "
                f"{sel_count}/{num_to_create} selectable). If the TX "
                f"eventually lands the topup-budget counter may drift; "
                f"bot_health.check_topup_budget_drift will reconcile on the "
                f"next runtime-check pass."
            )
        log_event(
            "info" if outputs_owned else "warning",
            f"{tag}_osstep_timeout",
            message,
            data={"tag": tag, "wait_max": wait_max,
                  "tx_confirmed": tx_state["confirmed"],
                  "owned": owned_count, "needed": num_to_create,
                  "selectable": sel_count},
        )
        return False

    def _two_step_split(self, name: str, wallet_id: int,
                         source_coin_id: str, pool_amount_mojos: int,
                         num_to_create: int, trading_size_mojos: int,
                         is_cat: bool) -> bool:
        """Two-step coin split (mirrors coin_prep_worker approach).

        Step 1: Send exact amount to self → creates a pool coin of precise size
        Step 2: Snapshot before/after to track the new coin ID, then split it

        For SAGE wallets this method delegates immediately to _sage_one_step_split
        which uses /create_transaction with selected_coin_ids.  That API honours
        the specific source coin, avoiding the known issue where /send_xch ignores
        the coin_ids hint and picks free tier_spare coins from other tiers instead
        of the designated topup pool coin.

        For CHIA wallets the original two-step (CLI split) path is used.
        """
        # ---- Sage fast-path: single /create_transaction call ----
        _wtype = get_wallet_type()
        if _wtype == "sage":
            return self._sage_one_step_split(
                name=name,
                wallet_id=wallet_id,
                source_coin_id=source_coin_id,
                num_to_create=num_to_create,
                trading_size_mojos=trading_size_mojos,
                is_cat=is_cat,
            )

        tag = f"topup_{name.lower()}"
        if self._topup_should_stop():
            log_event("info", f"{tag}_stopped", f"{name} top-up stopped before split")
            return False
        amount_str = (
            _format_amount_cat(pool_amount_mojos, cfg.CAT_DECIMALS)
            if is_cat else _format_amount_xch(pool_amount_mojos)
        )

        # ---- Step 1: Send-to-self to create pool coin ----
        try:
            addr_result = get_next_address(wallet_id=wallet_id, new_address=False)
            if not addr_result or not addr_result.get("success"):
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: Sage could not provide a wallet address "
                    f"for pool coin creation."
                )
            address = addr_result.get("address", "")
            if not address:
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: Sage returned an empty wallet address "
                    f"for pool coin creation."
                )
        except _TopupWalletDegraded:
            raise
        except Exception as e:
            self._abort_topup_for_wallet_degradation(
                f"{name} topup paused: wallet address lookup failed ({e})."
            )

        def _amount_matches_target(amount: int, target: int) -> bool:
            if amount == target:
                return True
            tolerance = max(1, int(target * 0.01))
            return abs(amount - target) < tolerance

        # Snapshot coins BEFORE the send. Use owned view as the primary truth so
        # a newly-created pool coin does not get confused with older hidden coins.
        before_snapshot = self._snapshot_coin_ids(wallet_id, f"{name}-before-pool")
        before_owned_map = self._get_owned_coin_amount_map(wallet_id, f"{name}-before-pool-owned") or {}

        log_event("info", f"{tag}_send_to_self",
                  f"Sending {amount_str} to self to create pool coin...")

        result = {}
        try:
            # Verify source coin is still spendable right before sending.
            # Another operation (gap closer, offer creation) might have consumed it.
            verify_result = _get_free_coins_rpc(wallet_id)
            if self._wallet_rpc_failed(verify_result):
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: spendable-coin verification failed "
                    f"before pool send."
                )
            verify_records = _extract_coin_records(verify_result)
            verify_ids = {_coin_id_from_record(r) for r in verify_records}
            if source_coin_id not in verify_ids:
                log_event("warning", f"{tag}_source_gone",
                          f"Source coin {source_coin_id[:16]}... no longer spendable — "
                          f"may have been consumed by another operation. Aborting split.")
                return False

            # Use source_coin_ids on Sage to ensure we spend from the reserve coin.
            # Chia's send_transaction doesn't support this param — it picks coins
            # automatically, but we've already re-fetched fresh IDs so it should
            # pick the largest available coin (our reserve).
            wallet_type = get_wallet_type()
            send_kwargs = {
                "wallet_id": wallet_id,
                "amount_mojos": pool_amount_mojos,
                "address": address,
                "fee_mojos": self._tx_fee_mojos(),
            }
            if wallet_type == "sage":
                send_kwargs["source_coin_ids"] = [source_coin_id]
            result = send_transaction(**send_kwargs)
            if not result:
                if self._spacescan_self_send_confirmed(source_coin_id, address, tag):
                    log_event("info", f"{tag}_send_onchain_pending",
                              "Spacescan confirms the pool self-send landed on-chain — "
                              "continuing while Sage catches up")
                    result = {}
                else:
                    self._abort_topup_for_wallet_degradation(
                        f"{name} topup paused: send-to-self returned no result from Sage."
                    )
            if isinstance(result, dict) and result.get("error"):
                send_error = result.get("error")
                if self._spacescan_self_send_confirmed(source_coin_id, address, tag):
                    log_event("info", f"{tag}_send_onchain_pending",
                              "Spacescan confirms the pool self-send landed on-chain — "
                              "continuing while Sage catches up")
                elif self._looks_like_wallet_rpc_degradation(send_error):
                    self._abort_topup_for_wallet_degradation(
                        f"{name} topup paused: send-to-self RPC degraded ({send_error})."
                    )
                else:
                    log_event("warning", f"{tag}_send_fail",
                              f"send_transaction error: {send_error}")
                    return False

            # Extract transaction info from response for tracking
            tx_ids = self._extract_sage_transaction_ids(result)
            if isinstance(result, dict):
                # Sage may return coin_spends with output coin info
                coin_spends = result.get("coin_spends", [])
                if coin_spends:
                    log_event("info", f"{tag}_send_tx_info",
                              f"Transaction has {len(coin_spends)} coin spends")

            send_info = "Pool coin creation submitted"
            if tx_ids:
                send_info += f" (tx: {tx_ids[0][:16]}...)"
            send_info += f" [source: {source_coin_id[:12]}..., amount: {pool_amount_mojos}]"
            log_event("info", f"{tag}_send_ok", send_info)
        except _TopupWalletDegraded:
            raise
        except Exception as e:
            if self._spacescan_self_send_confirmed(source_coin_id, address, tag):
                log_event("info", f"{tag}_send_onchain_pending",
                          "Spacescan confirms the pool self-send landed on-chain — "
                          "continuing while Sage catches up")
            elif self._looks_like_wallet_rpc_degradation(e):
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: send-to-self RPC error ({e})."
                )
            else:
                log_event("warning", f"{tag}_send_error", f"send_transaction failed: {e}")
                return False

        # ---- Wait for the pool coin to appear ----
        # Strategy: Poll spendable coins and look for a coin matching our
        # exact pool amount that wasn't in the before-snapshot.
        # Also track coin IDs we've already seen to avoid confusion from
        # concurrent operations (gap closer, etc.) changing the coin set.
        pool_coin_id = None
        pool_confirmed = False
        pool_owned_logged = False
        pool_tx_logged = False
        poll_start = time.time()
        max_wait = 180
        poll_interval_s = 5
        known_coin_ids = {
            str(cid or "").strip().lower()
            for cid in (before_owned_map.keys() or before_snapshot.keys())
            if cid
        }

        # Log the send response for debugging
        if isinstance(result, dict):
            resp_keys = list(result.keys())
            log_event("debug", f"{tag}_send_response",
                      f"send_xch response keys: {resp_keys}")

        while (time.time() - poll_start) < max_wait:
            if self._topup_should_stop():
                log_event("info", f"{tag}_stopped", f"{name} top-up stopped while waiting for pool coin")
                return False
            time.sleep(poll_interval_s)
            # Fresh scan of ALL current spendable coins
            current = _get_free_coins_rpc(wallet_id)
            if self._wallet_rpc_failed(current):
                if self._spacescan_self_send_confirmed(source_coin_id, address, tag):
                    self._abort_topup_for_wallet_degradation(
                        f"{name} topup paused: pool self-send is on-chain but Sage "
                        f"did not refresh its spendable coin view."
                    )
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: spendable-coin polling failed while "
                    f"waiting for the pool coin."
                )
            records = _extract_coin_records(current)
            owned_map = self._get_owned_coin_amount_map(wallet_id, f"{tag}-pool-owned") or {}
            selectable_ids = self._get_strict_selectable_coin_id_set(wallet_id, f"{tag}-pool-selectable") or set()
            tx_state = self._get_transaction_confirmation_state(tx_ids)

            if tx_state["confirmed"] and not pool_tx_logged:
                suffix = f" at height {tx_state['height']}" if tx_state["height"] else ""
                log_event("info", f"{tag}_pool_tx_confirmed",
                          f"Pool coin transaction confirmed{suffix}")
                pool_tx_logged = True

            candidate_ids = sorted(
                cid for cid, amt in owned_map.items()
                if cid not in known_coin_ids and _amount_matches_target(amt, pool_amount_mojos)
            )
            if candidate_ids:
                pool_coin_id = candidate_ids[0]
                if not pool_owned_logged:
                    log_event("info", f"{tag}_pool_owned",
                              f"Pool coin is present in owned wallet view [ID: {pool_coin_id[:12]}...]")
                    pool_owned_logged = True
                if pool_coin_id in selectable_ids:
                    pool_confirmed = True
            else:
                for r in records:
                    cid = _coin_id_from_record(r)
                    amt = _coin_amount(r)
                    if not cid or cid in known_coin_ids:
                        continue
                    if _amount_matches_target(amt, pool_amount_mojos):
                        pool_coin_id = cid
                        pool_confirmed = True
                        break
                    known_coin_ids.add(cid)

            if pool_confirmed and pool_coin_id:
                if is_cat:
                    coin_str = _format_amount_cat(pool_amount_mojos, cfg.CAT_DECIMALS)
                else:
                    coin_str = _format_amount_xch(pool_amount_mojos)

                log_event("info", f"{tag}_pool_found",
                          f"Pool coin confirmed: {coin_str} "
                          f"[ID: {pool_coin_id[:12]}...] "
                          f"({int(time.time() - poll_start)}s)")
                break

            elapsed = int(time.time() - poll_start)
            if elapsed % 30 == 0 and elapsed > 0:
                total_coins = len(records)
                new_count = len([1 for r in records
                                  if _coin_id_from_record(r) not in before_snapshot])
                pool_state = "no exact owned output yet"
                if pool_coin_id:
                    owned_ready = pool_coin_id in owned_map
                    selectable_ready = pool_coin_id in selectable_ids
                    pool_state = (
                        f"owned={'yes' if owned_ready else 'no'}, "
                        f"selectable={'yes' if selectable_ready else 'no'}, "
                        f"tx={'confirmed' if tx_state['confirmed'] else 'pending'}"
                    )
                log_event("info", f"{tag}_pool_wait",
                          f"Waiting for pool coin ({_format_amount_xch(pool_amount_mojos) if not is_cat else _format_amount_cat(pool_amount_mojos, cfg.CAT_DECIMALS)})... "
                          f"({elapsed}s, {total_coins} spendable coins, "
                          f"{new_count} new since send, {pool_state})")

            # At 120s mark, try get_pending_transactions to check if tx is still alive
            if 118 < elapsed < 125:
                try:
                    if get_wallet_type() == "sage":
                        from wallet_sage import get_pending_transactions
                        pending = get_pending_transactions() or []
                        pending_count = len(pending) if isinstance(pending, list) else 0
                        log_event("info", f"{tag}_pending_check",
                                  f"Pending transactions: {pending_count} "
                                  f"(if 0, tx may have been dropped)")
                except Exception as e:
                    log_event("debug", f"{tag}_pending_check_failed",
                              f"Pending tx check failed (non-critical): {e}")

        if not pool_confirmed or not pool_coin_id:
            if pool_coin_id:
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: pool coin exists in Sage's owned view but "
                    f"did not become selectable after {max_wait}s."
                )
            if self._spacescan_self_send_confirmed(source_coin_id, address, tag):
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: pool self-send is on-chain but Sage did "
                    f"not surface the new pool coin after {max_wait}s."
                )
            log_event("warning", f"{tag}_pool_timeout",
                      f"Pool coin not confirmed after {max_wait}s")
            return False

        # ---- Step 2: Split the tracked pool coin ----
        log_event("info", f"{tag}_splitting",
                  f"Splitting pool coin into {num_to_create} trading coins "
                  f"[pool ID: {pool_coin_id[:12]}...]")

        wallet_type = get_wallet_type()

        if wallet_type == "sage":
            # Sage native /split — output_count = num_to_create (even split)
            try:
                split_result = split_coins_rpc(
                    wallet_id=wallet_id,
                    target_coin_id=pool_coin_id,
                    num_coins=num_to_create,
                    amount_per_coin=trading_size_mojos,
                    fee_mojos=self._tx_fee_mojos(),
                    is_cat=is_cat,
                )
                if split_result is None:
                    if self._spacescan_coin_spent_confirmed(pool_coin_id, tag, "split"):
                        log_event("info", f"{tag}_split_onchain_pending",
                                  "Spacescan shows the pool coin spent on-chain despite "
                                  "a weak Sage split response — continuing confirmation poll")
                    else:
                        self._abort_topup_for_wallet_degradation(
                            f"{name} topup paused: Sage /split returned no result."
                        )
                if isinstance(split_result, dict) and split_result.get("error"):
                    split_error = split_result.get("error")
                    if self._spacescan_coin_spent_confirmed(pool_coin_id, tag, "split"):
                        log_event("info", f"{tag}_split_onchain_pending",
                                  "Spacescan shows the pool coin spent on-chain despite "
                                  "a weak Sage split response — continuing confirmation poll")
                    elif self._looks_like_wallet_rpc_degradation(split_error):
                        self._abort_topup_for_wallet_degradation(
                            f"{name} topup paused: Sage /split degraded ({split_error})."
                        )
                    else:
                        log_event("warning", f"{tag}_split_rpc_fail",
                                  f"Sage /split error: {split_error}")
                        return False
                split_tx_ids = self._extract_sage_transaction_ids(split_result)
                split_msg = "Sage /split submitted successfully"
                if split_tx_ids:
                    split_msg += f" (tx: {split_tx_ids[0][:16]}...)"
                log_event("info", f"{tag}_split_submitted", split_msg)
            except _TopupWalletDegraded:
                raise
            except Exception as e:
                if self._spacescan_coin_spent_confirmed(pool_coin_id, tag, "split"):
                    log_event("info", f"{tag}_split_onchain_pending",
                              "Spacescan shows the pool coin spent on-chain despite "
                              "a Sage split exception — continuing confirmation poll")
                elif self._looks_like_wallet_rpc_degradation(e):
                    self._abort_topup_for_wallet_degradation(
                        f"{name} topup paused: Sage /split RPC error ({e})."
                    )
                else:
                    log_event("warning", f"{tag}_split_rpc_error",
                              f"Sage /split error: {e}")
                    return False
        else:
            split_tx_ids = []
            # Chia CLI split — needs display units
            if is_cat:
                cli_coin_size = Decimal(str(int(self.get_target_cat_coin_size())))
            else:
                cli_coin_size = Decimal(trading_size_mojos) / Decimal("1000000000000")

            # Use the low-level CLI path from _split_via_cli but just the CLI part
            bare_coin_id = pool_coin_id.replace("0x", "")
            if not self._fingerprint or not self._fingerprint.strip():
                self._fingerprint = self._resolve_fingerprint()

            cmd = [
                "chia", "wallet", "coins", "split",
                "-f", self._fingerprint,
                "-i", str(wallet_id),
                "-n", str(num_to_create),
                "-a", str(cli_coin_size),
                "-t", bare_coin_id,
                "-m", "0"
            ]

            try:
                import subprocess as sp
                process = sp.Popen(
                    cmd, stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE, text=True
                )
                stdout, stderr = process.communicate(input="y\n", timeout=60)
                output = stdout + stderr

                if "submitted to" in output.lower() or "transaction" in output.lower():
                    log_event("info", f"{tag}_split_submitted",
                              "CLI split submitted successfully")
                else:
                    log_event("warning", f"{tag}_split_cli_fail",
                              f"CLI split failed: {output[:200]}")
                    return False
            except Exception as e:
                log_event("warning", f"{tag}_split_cli_error", f"CLI split error: {e}")
                return False

        # ---- Wait for split confirmation via tx + owned + selectable state ----
        split_start = time.time()
        split_max_wait = 120
        split_poll_interval_s = 4
        split_tx_logged = False
        split_owned_logged = False
        pre_split_owned_ids = set(
            (self._get_owned_coin_amount_map(wallet_id, f"{tag}-pre-split-owned") or {}).keys()
        )

        while (time.time() - split_start) < split_max_wait:
            if self._topup_should_stop():
                log_event("info", f"{tag}_stopped", f"{name} top-up stopped while waiting for split confirmation")
                return False
            time.sleep(split_poll_interval_s)
            result = _get_free_coins_rpc(wallet_id)
            if self._wallet_rpc_failed(result):
                if self._spacescan_coin_spent_confirmed(pool_coin_id, tag, "split"):
                    self._abort_topup_for_wallet_degradation(
                        f"{name} topup paused: split is on-chain but Sage did not "
                        f"refresh its spendable coin view."
                    )
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: spendable-coin polling failed while "
                    f"waiting for split confirmation."
                )
            owned_map = self._get_owned_coin_amount_map(wallet_id, f"{tag}-split-owned") or {}
            selectable_ids = self._get_strict_selectable_coin_id_set(wallet_id, f"{tag}-split-selectable") or set()
            tx_state = self._get_transaction_confirmation_state(split_tx_ids)
            elapsed = int(time.time() - split_start)

            pool_visible = pool_coin_id in owned_map
            pool_selectable = pool_coin_id in selectable_ids
            pool_consumed = (not pool_visible) or (pool_coin_id and not pool_selectable)
            new_output_ids = sorted(
                cid for cid, amount in owned_map.items()
                if cid not in pre_split_owned_ids and _amount_matches_target(amount, trading_size_mojos)
            )
            owned_output_count = len(new_output_ids)
            selectable_output_count = sum(1 for cid in new_output_ids if cid in selectable_ids)
            outputs_selectable = (
                owned_output_count >= num_to_create and
                selectable_output_count >= num_to_create
            )

            if tx_state["confirmed"] and not split_tx_logged:
                suffix = f" at height {tx_state['height']}" if tx_state["height"] else ""
                log_event("info", f"{tag}_split_tx_confirmed",
                          f"Split transaction confirmed{suffix}")
                split_tx_logged = True

            if pool_consumed and owned_output_count >= num_to_create and not split_owned_logged:
                if outputs_selectable:
                    log_event("info", f"{tag}_split_outputs_ready",
                              f"Split outputs are owned and selectable ({owned_output_count}/{num_to_create})")
                else:
                    log_event("info", f"{tag}_split_outputs_owned",
                              f"Split outputs are owned ({owned_output_count}/{num_to_create}) — "
                              f"waiting for selectable view to catch up")
                split_owned_logged = True

            if pool_consumed and owned_output_count >= num_to_create and (outputs_selectable or tx_state["confirmed"]):
                if outputs_selectable:
                    detail = f"{selectable_output_count}/{num_to_create} selectable"
                else:
                    detail = f"{owned_output_count}/{num_to_create} owned, selectable lagging"
                log_event("info", f"{tag}_split_confirmed",
                          f"Split confirmed after {elapsed}s ({detail})")
                return True

            if elapsed % 20 == 0 and elapsed > 0:
                tx_label = "confirmed" if tx_state["confirmed"] else "pending"
                log_event("info", f"{tag}_split_wait",
                          f"Waiting for split... (tx={tx_label}, "
                          f"{owned_output_count}/{num_to_create} owned, "
                          f"{selectable_output_count}/{num_to_create} selectable, {elapsed}s)")

        # Final post-timeout diagnostic
        final_result = _get_free_coins_rpc(wallet_id)
        if self._wallet_rpc_failed(final_result):
            if self._spacescan_coin_spent_confirmed(pool_coin_id, tag, "split"):
                self._abort_topup_for_wallet_degradation(
                    f"{name} topup paused: split is on-chain but Sage did not "
                    f"finish refreshing after timeout."
                )
            self._abort_topup_for_wallet_degradation(
                f"{name} topup paused: spendable-coin refresh failed after split timeout."
            )

        owned_map = self._get_owned_coin_amount_map(wallet_id, f"{tag}-split-timeout-owned") or {}
        selectable_ids = self._get_strict_selectable_coin_id_set(wallet_id, f"{tag}-split-timeout-selectable") or set()
        tx_state = self._get_transaction_confirmation_state(split_tx_ids)
        new_output_ids = sorted(
            cid for cid, amount in owned_map.items()
            if cid not in pre_split_owned_ids and _amount_matches_target(amount, trading_size_mojos)
        )
        owned_output_count = len(new_output_ids)
        selectable_output_count = sum(1 for cid in new_output_ids if cid in selectable_ids)
        if tx_state["confirmed"] and owned_output_count >= num_to_create:
            log_event("info", f"{tag}_split_confirmed",
                      f"Split confirmed after {split_max_wait}s "
                      f"({owned_output_count}/{num_to_create} owned, selectable lagging)")
            return True

        log_event("info", f"{tag}_split_timeout",
                  f"Split not confirmed after {split_max_wait}s "
                  f"(tx={'confirmed' if tx_state['confirmed'] else 'pending'}, "
                  f"{owned_output_count}/{num_to_create} owned, "
                  f"{selectable_output_count}/{num_to_create} selectable)")
        return False

    def _snapshot_coin_ids(self, wallet_id: int, label: str) -> dict:
        """Snapshot current spendable coins as {coin_id: amount_mojos}.

        Used for before/after diffing to track newly created coins.
        Same approach as coin_prep_worker.
        """
        result = _get_free_coins_rpc(wallet_id)
        records = _extract_coin_records(result)
        snapshot = {}
        for r in records:
            cid = _coin_id_from_record(r)
            amt = _coin_amount(r)
            if cid:
                snapshot[cid] = amt
        return snapshot

    def _diff_coin_snapshots(self, before: dict, after: dict) -> list:
        """Find NEW coins that appeared between snapshots.

        Returns list of {"coin_id": ..., "amount": ...} for new coins only.
        """
        new_ids = set(after.keys()) - set(before.keys())
        return [{"coin_id": cid, "amount": after[cid]} for cid in new_ids]

    def _consolidate_coins(self, name: str, wallet_id: int,
                           total_amount: int, is_cat: bool,
                           source_coin_ids: Optional[List[str]] = None) -> bool:
        """Consolidate specific coins into one large coin using Sage's /combine.

        F68 FIX: switched from send_xch/send_cat (which silently ignore coin_ids
        and let Sage pick additional coins at will — including draining the
        sniper pool) to /combine (which respects coin_ids as a strict whitelist
        — same endpoint coin_prep uses for its splits/combines).

        Args:
            source_coin_ids: REQUIRED — the exact coins to combine. /combine
                will spend only these. Pass None returns False (use a
                different code path if you genuinely want Sage to pick).
        """
        self._last_consolidate_not_submitted = False
        try:
            if not source_coin_ids:
                log_event("warning", f"consolidate_{name.lower()}_no_ids",
                          "Consolidation aborted: no source_coin_ids provided. "
                          "Caller must specify exact coins to combine.")
                return False

            # Defensive filter: NEVER combine sniper or fee coins. The sniper and
            # fee pools are dedicated pools with distinct sizes and should never
            # be cannibalised to fund trading-tier topups. If upstream logic ever
            # leaks a sniper/fee coin ID into source_coin_ids, we strip it here.
            filtered_ids = self._filter_out_protected_coin_ids(source_coin_ids)
            if len(filtered_ids) < len(source_coin_ids):
                log_event("warning", f"consolidate_{name.lower()}_filtered_protected",
                          f"Stripped {len(source_coin_ids) - len(filtered_ids)} "
                          f"sniper/fees coin(s) from consolidation input "
                          f"(defensive guard — should not normally fire)")
            if len(filtered_ids) < 2:
                log_event("info", f"consolidate_{name.lower()}_too_few_after_filter",
                          f"Consolidation skipped: only {len(filtered_ids)} "
                          f"coin(s) remain after protected-pool filter")
                return False

            if get_wallet_type() != "sage":
                log_event("info", f"consolidate_{name.lower()}_skip_non_sage",
                          "Consolidation skipped: /combine is Sage-only. "
                          "Chia wallet path not supported for targeted combine.")
                return False

            if not hasattr(self, "_recent_consolidate_submissions"):
                self._recent_consolidate_submissions = {}
            consolidate_key = (
                f"{'cat' if is_cat else 'xch'}:{wallet_id}:{total_amount}:"
                f"{','.join(sorted(str(cid).lower() for cid in filtered_ids))}"
            )
            debounce_secs = int(
                getattr(cfg, "TOPUP_CONSOLIDATE_DEBOUNCE_SECS", 600) or 600
            )
            if debounce_secs > 0:
                now = time.time()
                self._recent_consolidate_submissions = {
                    k: ts for k, ts in self._recent_consolidate_submissions.items()
                    if now - ts < debounce_secs
                }
                last_submitted = self._recent_consolidate_submissions.get(
                    consolidate_key
                )
                if last_submitted is not None and now - last_submitted < debounce_secs:
                    elapsed = int(now - last_submitted)
                    log_event(
                        "info",
                        f"consolidate_{name.lower()}_debounce",
                        f"Skipping duplicate {name} consolidation submitted "
                        f"{elapsed}s ago; waiting for wallet/chain state to settle",
                    )
                    return _TOPUP_PENDING

            fee = self._tx_fee_mojos()
            from wallet_sage import combine_coins
            result = combine_coins(coin_ids=filtered_ids, fee_mojos=fee)

            # /combine returns {summary, coin_spends} on success — same shape
            # as send_xch/send_cat.
            if result and (result.get("success") is True
                           or result.get("coin_spends") is not None):
                tx_ids = self._extract_sage_transaction_ids(result)
                if not tx_ids:
                    result_keys = (
                        list(result.keys()) if isinstance(result, dict) else []
                    )
                    submit_state = self._combine_no_txid_submission_state(
                        wallet_id,
                        f"consolidate_{name.lower()}_combine-selectable",
                        filtered_ids,
                    )
                    event_data = {
                        "pending_count": submit_state.get("pending_count"),
                        "result_keys": result_keys,
                        "input_count": len(filtered_ids),
                        "selectable_count": submit_state.get("selectable_count"),
                        "elapsed": submit_state.get("elapsed"),
                    }
                    if submit_state.get("state") == "not_submitted":
                        self._last_consolidate_not_submitted = True
                        log_event(
                            "info",
                            f"consolidate_{name.lower()}_combine_not_submitted",
                            "Sage /combine returned no transaction id, no pending "
                            "transaction, and all input coins are still selectable; "
                            "will retry next topup cycle",
                            data=event_data,
                        )
                        return False
                    log_event(
                        "info",
                        f"consolidate_{name.lower()}_combine_unverified",
                        "Sage /combine returned no transaction id; waiting for "
                        "wallet/chain state to settle before marking it submitted",
                        data=event_data,
                    )
                    return _TOPUP_PENDING
                if debounce_secs > 0:
                    self._recent_consolidate_submissions[consolidate_key] = time.time()
                n_spends = len(result.get("coin_spends") or [])
                log_event("info", f"consolidate_{name.lower()}_combine_ok",
                          f"Combined {len(filtered_ids)} coin(s) via /combine "
                          f"(Sage spent {n_spends} input coin(s))")
                return True
            else:
                error = (result or {}).get("error", "Unknown")
                log_event("warning", f"consolidate_{name.lower()}_fail",
                          f"/combine failed: {error}")
                return False

        except Exception as e:
            log_event("error", f"consolidate_{name.lower()}_error",
                      f"Consolidation error: {e}")
            return False

    def _filter_out_protected_coin_ids(self, coin_ids: List[str]) -> List[str]:
        """Strip sniper/fees coin IDs from a list — defensive guard for combines.

        Topup consolidation paths should never pass sniper/fees IDs (upstream
        tier filters exclude them), but defence-in-depth: if a leak occurs, we
        refuse to combine those coins. They remain untouched in their dedicated
        pools.

        IMPORTANT: DB coin IDs are stored normalised with a `0x` prefix via
        norm_coin_id (database.py:34). Earlier this function stripped the
        prefix before the IN-query, so the lookup matched zero rows and the
        "protected" set was silently empty — making the defensive guard inert
        whenever a sniper or fee coin actually leaked into a combine input.
        We now query with the prefixed form and compare prefixed-to-prefixed.
        """
        try:
            from database import get_connection, norm_coin_id
            # Build a (original → normalised_with_0x) map so we can reject
            # each original id without losing its caller-facing format.
            orig_to_norm = {}
            for cid in coin_ids or []:
                if not cid:
                    continue
                orig_to_norm[cid] = norm_coin_id(str(cid))
            if not orig_to_norm:
                return []
            normalised_list = list(orig_to_norm.values())
            placeholders = ",".join(["?"] * len(normalised_list))
            rows = get_connection().execute(
                f"SELECT coin_id, assigned_tier FROM coins WHERE coin_id IN ({placeholders})",
                normalised_list,
            ).fetchall()
            protected = {
                str(r["coin_id"]).lower()
                for r in rows
                if (r["assigned_tier"] or "").lower() in ("sniper", "fees")
            }
            if not protected:
                return list(coin_ids or [])
            out = []
            skipped = 0
            for original, normalised in orig_to_norm.items():
                if normalised in protected:
                    skipped += 1
                    continue
                out.append(original)
            if skipped:
                log_event(
                    "warning",
                    "protected_coin_filter_skipped",
                    f"Defensive combine guard removed {skipped} sniper/fee "
                    f"coin(s) from a consolidation input",
                    data={"skipped_count": skipped,
                          "remaining": len(out)},
                )
            return out
        except Exception:
            # On any DB error, fail open — return original list (don't block topup)
            return list(coin_ids or [])

    # ------------------------------------------------------------------
    # Misfit coin detection and absorption
    # ------------------------------------------------------------------

    @staticmethod
    def _is_misfit_coin(coin_amount_mojos: int,
                        tier_sizes_mojos: Dict[str, int],
                        max_size_ratio: float,
                        floor_tolerance: float = 0.98) -> bool:
        """Return True if this coin cannot fund any configured tier offer.

        This is a thin wrapper that routes through the single-source-of-truth
        classifier in :mod:`coin_classifier`. Historically there were FIVE
        different classifiers in the codebase, each with different thresholds,
        which led to bugs like the 2026-04-17 ladder-shape regression where a
        23.4k CAT coin was flagged as a misfit here (0.98/1.5 bounds) but
        accepted by reconcile (±20% bounds). Now all five route through the
        same authoritative function, making inconsistency impossible.

        Args:
            coin_amount_mojos: Coin size in mojos.
            tier_sizes_mojos:  Mapping of tier name → target offer size (mojos).
            max_size_ratio:    From COIN_MAX_SIZE_RATIO config (e.g. 1.5).
                               Pass float('inf') if the ratio guard is disabled.
            floor_tolerance:   Fraction of tier floor that still counts as usable.
                               Default 0.98 allows a coin to be up to 2% below
                               the exact tier size before it is flagged.
        """
        from coin_classifier import is_misfit_coin as _cc_is_misfit
        return _cc_is_misfit(
            coin_amount_mojos,
            tier_sizes_mojos,
            max_size_ratio=max_size_ratio,
            floor_tolerance=floor_tolerance,
        )

    def _absorb_misfits_to_reserve(self, name: str, wallet_id: int,
                                   inventory: Dict[str, list],
                                   tier_sizes_mojos: Dict[str, int],
                                   is_cat: bool) -> bool:
        """Fold stranded misfit tier_spare coins back into the reserve coin.

        When a fill returns change that lands between tier sizes (e.g. a
        4.39 XCH coin from a 5 XCH coin used to back a 0.63 XCH offer),
        the change coin is labelled as a tier_spare but can never be
        selected for any offer: too large given COIN_MAX_SIZE_RATIO for
        the smaller tier, too small to fund the larger tier. It stalls.

        This method consolidates those misfits together WITH the existing
        reserve coin into one larger reserve coin via a send-to-self
        transaction. The enlarged reserve is then available for the next
        topup cycle's targeted split.

        Only fires when a reserve coin already exists.  The no-reserve
        recovery path (Strategy 3 in _smart_topup_wallet) handles the
        case where there is no reserve.

        Sage only — Chia wallet cannot specify source_coin_ids so we
        skip to avoid accidentally consuming the wrong coins.

        Returns True if a consolidation transaction was submitted.
        """
        try:
            reserve_coins = inventory.get("reserve", [])
            if not reserve_coins:
                return False  # No reserve — Strategy 3 handles this

            if get_wallet_type() != "sage":
                # source_coin_ids is a Sage-only feature; skip for Chia.
                return False

            max_size_ratio = float(getattr(cfg, "COIN_MAX_SIZE_RATIO", "1.5") or 1.5)
            if max_size_ratio <= 0:
                max_size_ratio = float("inf")  # ratio guard disabled

            # Misfit detection must use BASE tier sizes (no prep headroom).
            # _get_tier_sizes_mojos applies a prep_mult (typically 1.10) so
            # that coins are cut slightly larger than the live offer size.
            # This gives the wallet room for fee deductions and price drift.
            # But a freshly-minted base-size coin (e.g. 1.4803 XCH for mid)
            # is below the prepped floor (1.4803×1.10×0.98 = 1.596) and
            # would be incorrectly flagged as a misfit — absorbed back into
            # the reserve on the very next cycle, causing an infinite loop.
            # Dividing by prep_mult restores the true offer sizes.
            prep_mult = self._get_coin_prep_headroom_multiplier()
            pm = Decimal(str(prep_mult)) if prep_mult and prep_mult > 1 else Decimal("1")
            if pm > Decimal("1"):
                # fees tier uses an absolute coin size (not prepped), keep it as-is
                base_tier_mojos: Dict[str, int] = {
                    k: (int(Decimal(str(v)) / pm)
                        if k not in (get_fee_tier_name(),)
                        else v)
                    for k, v in tier_sizes_mojos.items()
                }
            else:
                base_tier_mojos = tier_sizes_mojos

            # Scan trading tier buckets for coins that can't fund any tier offer.
            # Uses a 2% floor tolerance so coins that are a handful of mojos
            # below the exact tier floor (fee-rounding artefacts from splits)
            # are not incorrectly flagged as misfits.
            # NOTE: sniper coins are intentionally excluded. The sniper tier is
            # not included in tier_sizes_mojos (its size is price-derived at offer
            # creation time), so every sniper coin would be falsely flagged as a
            # misfit and absorbed on each topup cycle, blocking tier splits forever.
            # Stranded sniper coins are harmless — they sit idle until the next
            # coin prep or a future cleanup pass.
            misfit_records = []
            for tier_name in ("inner", "mid", "outer", "extreme"):
                bucket = inventory.get(tier_name, [])
                for rec in bucket:
                    amt = _coin_amount(rec)
                    if self._is_misfit_coin(amt, base_tier_mojos, max_size_ratio):
                        misfit_records.append(rec)

            # Also sweep the 'small' bucket — dust + unknown-designation coins
            # that fell between tier sizes when the classifier ran. These
            # typically originate as change outputs from filled offers
            # (seen during the 2026-04-21 sweep tests: 16 CAT orphans at
            # ~4.8k and ~1.9k each between extreme=2.6k and outer=5.7k).
            # Without this path they just sit idle forever — the primary
            # misfit loop only looks at tier buckets, and the classifier
            # routes these to "small" because their inferred tier is "none".
            #
            # Safety filters:
            # - Skip coins too small to cover the consolidation tx fee
            #   (XCH only; CAT fees are paid separately in XCH).
            # - Cap at 20 coins per absorption to avoid building a single
            #   monster TX that Sage's CLVM cost budget would reject.
            small_bucket = inventory.get("small", [])
            _absorb_cap = 20 - len(misfit_records)  # leave room for tier misfits
            fee_floor = self._tx_fee_mojos() * 2 if not is_cat else 0
            for rec in small_bucket:
                if _absorb_cap <= 0:
                    break
                amt = _coin_amount(rec)
                if amt <= fee_floor:
                    continue  # not worth the fee
                misfit_records.append(rec)
                _absorb_cap -= 1

            if not misfit_records:
                return False

            reserve_rec = reserve_coins[0]
            reserve_id = _coin_id_from_record(reserve_rec)
            reserve_amt = _coin_amount(reserve_rec)

            misfit_ids: List[str] = []
            total_misfit = 0
            for r in misfit_records:
                cid = _coin_id_from_record(r)
                if cid:
                    misfit_ids.append(cid)
                    total_misfit += _coin_amount(r)

            if not misfit_ids:
                return False

            # ---- Race-condition guard ----
            # Between the inventory scan (start of _topup_worker) and now,
            # the offer-creation thread may have locked some of the identified
            # coins. Re-fetch the current selectable set and filter to only
            # coins that are still free. If any have been grabbed, skip this
            # cycle (the topup will retry next time needs_topup() fires).
            fresh_result = _get_free_coins_rpc(wallet_id)
            fresh_sel = {
                _coin_id_from_record(r)
                for r in _extract_coin_records(fresh_result)
            }
            if reserve_id not in fresh_sel:
                log_event("info", f"topup_{name.lower()}_absorb_skip_race",
                          "Reserve coin no longer selectable — "
                          "skipping absorption this cycle (will retry)")
                return False
            # Filter misfits to only those still selectable
            misfit_ids = [cid for cid in misfit_ids if cid in fresh_sel]
            if not misfit_ids:
                log_event("info", f"topup_{name.lower()}_absorb_skip_race",
                          "All misfit coins were locked since inventory scan — "
                          "skipping absorption this cycle (will retry)")
                return False
            # Recalculate total with the confirmed-selectable set
            confirmed_misfit_records = [
                r for r in misfit_records
                if _coin_id_from_record(r) in set(misfit_ids)
            ]
            total_misfit = sum(_coin_amount(r) for r in confirmed_misfit_records)

            total_amount = reserve_amt + total_misfit
            fee = self._tx_fee_mojos()
            # XCH: fee deducted from the coin value (same wallet).
            # CAT: fee paid separately from XCH balance.
            send_amount = total_amount - fee if not is_cat else total_amount

            if send_amount <= 0:
                log_event("debug", f"topup_{name.lower()}_absorb_skip",
                          "Misfit absorption skipped — combined amount cannot "
                          "cover transaction fee")
                return False

            if is_cat:
                amt_str = _format_amount_cat(total_amount, cfg.CAT_DECIMALS)
                mis_str = _format_amount_cat(total_misfit, cfg.CAT_DECIMALS)
            else:
                amt_str = _format_amount_xch(total_amount)
                mis_str = _format_amount_xch(total_misfit)

            # F68 FIX: use Sage's /combine endpoint (which respects coin_ids as
            # a strict whitelist) instead of send_xch/send_cat (which silently
            # ignored coin_ids and let Sage pull in arbitrary wallet coins —
            # including the sniper pool). This mirrors the pattern coin_prep
            # uses for its splits/combines.
            combine_ids = [reserve_id] + misfit_ids
            # Defensive filter: strip any sniper/fees coin IDs that leaked in
            filtered_ids = self._filter_out_protected_coin_ids(combine_ids)
            if len(filtered_ids) < len(combine_ids):
                log_event("warning", f"topup_{name.lower()}_absorb_filtered_protected",
                          f"Stripped {len(combine_ids) - len(filtered_ids)} "
                          f"sniper/fees coin(s) from misfit absorption input "
                          f"(defensive guard — should not normally fire)")
            if len(filtered_ids) < 2:
                log_event("info", f"topup_{name.lower()}_absorb_too_few_after_filter",
                          f"Absorption aborted: only {len(filtered_ids)} "
                          f"coin(s) remain after protected-pool filter")
                return False

            absorb_key = None
            debounce_secs = int(getattr(cfg, "TOPUP_ABSORB_DEBOUNCE_SECS", 600) or 600)
            if debounce_secs > 0:
                now = time.time()
                self._recent_absorb_submissions = {
                    k: ts for k, ts in self._recent_absorb_submissions.items()
                    if now - ts < debounce_secs
                }
                absorb_key = (
                    f"{'cat' if is_cat else 'xch'}:{wallet_id}:{send_amount}:"
                    f"{','.join(sorted(str(cid).lower() for cid in filtered_ids))}"
                )
                last_submitted = self._recent_absorb_submissions.get(absorb_key)
                if last_submitted is not None and now - last_submitted < debounce_secs:
                    elapsed = int(now - last_submitted)
                    log_event("info", f"topup_{name.lower()}_absorb_debounce",
                              f"Skipping duplicate {name} misfit absorption submitted "
                              f"{elapsed}s ago; waiting for wallet/chain state to settle")
                    return _TOPUP_PENDING

            log_event("info", f"topup_{name.lower()}_absorb_misfits",
                      f"Absorbing {len(confirmed_misfit_records)} stranded {name} "
                      f"misfit coin(s) ({mis_str}) into reserve "
                      f"[{reserve_id[:12]}...] — new reserve will be {amt_str}")

            from wallet_sage import combine_coins
            result = combine_coins(coin_ids=filtered_ids, fee_mojos=fee)

            # Sage's send_xch/send_cat returns {summary, coin_spends} on success
            # (no "success" key in that response format). Accept either form.
            sage_submitted = bool(
                result and (
                    result.get("success") is True
                    or result.get("coin_spends") is not None
                )
            )
            if sage_submitted:
                tx_ids = self._extract_sage_transaction_ids(result)
                if not tx_ids:
                    result_keys = (
                        list(result.keys()) if isinstance(result, dict) else []
                    )
                    submit_state = self._combine_no_txid_submission_state(
                        wallet_id,
                        f"topup_{name.lower()}_absorb-selectable",
                        filtered_ids,
                    )
                    event_data = {
                        "pending_count": submit_state.get("pending_count"),
                        "result_keys": result_keys,
                        "input_count": len(filtered_ids),
                        "selectable_count": submit_state.get("selectable_count"),
                        "elapsed": submit_state.get("elapsed"),
                    }
                    if submit_state.get("state") == "not_submitted":
                        log_event(
                            "info",
                            f"topup_{name.lower()}_absorb_not_submitted",
                            "Sage /combine returned no transaction id, no pending "
                            "transaction, and all input coins are still selectable; "
                            "will retry next topup cycle",
                            data=event_data,
                        )
                        return False
                    log_event(
                        "info",
                        f"topup_{name.lower()}_absorb_unverified",
                        "Sage /combine returned no transaction id; waiting for "
                        "wallet/chain state to settle before marking it submitted",
                        data=event_data,
                    )
                    return _TOPUP_PENDING
                if absorb_key:
                    self._recent_absorb_submissions[absorb_key] = time.time()
                # Log how many coin_spends Sage included for transparency
                n_spends = len(result.get("coin_spends") or [])
                extra = (f" (Sage used {n_spends} input coin(s) in the spend)"
                         if n_spends else "")
                log_event("success", f"topup_{name.lower()}_absorb_ok",
                          f"Misfit absorption submitted — enlarged reserve "
                          f"({amt_str}) will be available after confirmation{extra}")
                # Credit the absorbed misfit total back to the topup pool
                # spend counter. The misfits were originally carved from the
                # reserve (contributing to spent), and folding them back in
                # means that XCH is once again available for future splits.
                # Without this, the counter drifts permanently upward and
                # blocks tier refills even when the reserve physically has
                # coins — the bug we chased for the 2026-04-21 session.
                self._record_topup_pool_refund(is_cat, int(total_misfit))
                # Stamp a timestamp the deposit-advisor check uses to avoid
                # false-positive prompts on the brand-new reserve coin that
                # absorption just created (internal bucket reshuffle, not a
                # genuine deposit from outside).
                try:
                    from database import set_setting as _set_setting
                    _abs_key = ("last_misfit_absorb_cat_at" if is_cat
                                else "last_misfit_absorb_xch_at")
                    _set_setting(_abs_key, str(int(time.time())))
                except Exception:
                    pass  # advisory is best-effort — don't block absorption
                return True

            # Log the actual Sage error (not just "Unknown") for diagnosis
            sage_error = (result or {}).get("error") or (result or {}).get("message")
            error_detail = str(sage_error) if sage_error else f"result={result!r}"
            log_event("info", f"topup_{name.lower()}_absorb_skip_sage",
                      f"Misfit absorption declined by Sage ({error_detail}) — "
                      "will retry next topup cycle")
            return False

        except Exception as exc:
            log_event("error", f"topup_{name.lower()}_absorb_error",
                      f"Misfit absorption unexpected error: {exc}")
            return False

    def _poll_for_confirmation(self, pre_xch: int, pre_cat: int,
                                max_polls: int = 36, poll_interval: int = 5):
        """Poll until coin counts change (confirms splits/consolidation).

        Max 3 minutes (36 polls × 5 seconds). Shorter than the old 10-minute
        timeout because if a split hasn't confirmed by then, the transaction
        likely failed or the wallet is stuck — better to retry next cycle
        than block the topup thread for 10 minutes.
        """
        for i in range(max_polls):
            if self._topup_should_stop():
                log_event("info", "topup_stopped", "Coin top-up stopped while waiting for inventory confirmation")
                return
            time.sleep(poll_interval)
            elapsed = (i + 1) * poll_interval

            try:
                xch_result = _get_free_coins_rpc(cfg.WALLET_ID_XCH)
                cat_result = _get_free_coins_rpc(cfg.CAT_WALLET_ID)

                new_xch = len(_extract_coin_records(xch_result))
                new_cat = len(_extract_coin_records(cat_result))

                if new_xch != pre_xch or new_cat != pre_cat:
                    self._xch_coins = new_xch
                    self._cat_coins = new_cat
                    log_event(_topup_event_log_level("topup_inventory_changed"),
                              "topup_inventory_changed",
                              f"Coin inventory changed in {elapsed}s. "
                              f"XCH: {pre_xch}→{new_xch}, CAT: {pre_cat}→{new_cat}")
                    return

                if elapsed % 15 == 0:
                    log_event(_topup_event_log_level("topup_waiting"), "topup_waiting",
                              f"Waiting for confirmation... XCH: {new_xch}, "
                              f"CAT: {new_cat} ({elapsed}s / {max_polls * poll_interval}s max)")

            except Exception as e:
                log_event("warning", "topup_wait_poll_failed",
                          f"Topup confirmation poll failed: {e}")

        log_event("warning", "topup_timeout",
                  f"No coin count change after {max_polls * poll_interval}s — "
                  f"will retry next cycle")

    # -------------------------------------------------------------------
    # Full coin prep (subprocess)
    # -------------------------------------------------------------------

    def start_coin_prep(self) -> bool:
        """Launch the full coin_prep_worker as a subprocess."""
        # Kill any existing worker before starting a new one.
        # Two workers on the same wallet causes coin conflicts.
        if self._prep_process and self._prep_process.poll() is None:
            old_pid = self._prep_process.pid
            log_event("warning", "coin_prep_kill",
                      f"Killing previous coin prep worker (PID: {old_pid}) before new run")
            try:
                self._prep_process.terminate()
                try:
                    self._prep_process.wait(timeout=3)
                except Exception:
                    self._prep_process.kill()
                    self._prep_process.wait(timeout=2)
            except Exception as e:
                log_event("warning", "coin_prep_kill_failed",
                          f"Could not terminate previous coin prep worker (PID {self._prep_process.pid}): {e}")
            self._prep_process = None
            with self._lock:
                self._prep_running = False

        # Atomic check-and-set: both the guard and the flag update must be
        # inside the same lock acquisition to prevent two callers from both
        # passing the check and spawning duplicate workers.
        with self._lock:
            if self._prep_running or self._topup_running:
                return False
            self._prep_running = True

        try:
            worker_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "coin_prep_worker.py"
            )

            if not os.path.exists(worker_path):
                log_event("error", "coin_prep_missing",
                          f"coin_prep_worker.py not found at {worker_path}")
                with self._lock:
                    self._prep_running = False
                return False
            env = _coin_prep_worker_environment()

            # Build CLI args to pass correct config to the worker.
            # This ensures the worker uses the ACTUAL bot settings
            # (from GUI) instead of stale .env values.
            cmd = _coin_prep_worker_command(worker_path)
            max_buy = getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 25)
            max_sell = getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 25)

            # Liquidity mode scopes the pool — in buy_only we zero the CAT
            # side so no sell coins get prepared (and vice versa). The fee
            # pool is kept in both single-sided modes because buying AND
            # selling both require fee coins.
            _liquidity_mode = (getattr(cfg, "LIQUIDITY_MODE", "two_sided") or "two_sided").lower()
            _is_buy_only = (_liquidity_mode == "buy_only")
            _is_sell_only = (_liquidity_mode == "sell_only")
            if _is_buy_only:
                max_sell = 0
            if _is_sell_only:
                max_buy = 0

            # Coin prep multiplier: prep up to N× coins for spare capacity
            multiplier = getattr(cfg, "COIN_PREP_MULTIPLIER", Decimal("1.0"))
            prep_headroom_pct = getattr(cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10"))

            if cfg.TIER_ENABLED:
                # Tier-aware coin prep: pass PER-SIDE tier sizes and counts so the
                # worker preps XCH coins at BUY sizes and CAT coins at SELL sizes.
                # Using the old shared --tier-sizes defaulted to SELL-side sizes for
                # both wallets, causing XCH coins to be wrong size when BUY≠SELL.
                buy_tier_sizes  = self._configured_tier_sizes_xch(side="buy")
                sell_tier_sizes = self._configured_tier_sizes_xch(side="sell")
                xch_tier_counts = get_weighted_tier_prep_counts(
                    max_buy, multiplier,
                    tier_sizes_xch=buy_tier_sizes, side="xch")
                cat_tier_counts = get_weighted_tier_prep_counts(
                    max_sell, multiplier,
                    tier_sizes_xch=sell_tier_sizes, side="cat")
                # Liquidity mode overrides — zero the disabled side's tier
                # counts so coin prep doesn't build a pool that won't be used.
                if _is_buy_only:
                    cat_tier_counts = {k: 0 for k in cat_tier_counts}
                if _is_sell_only:
                    # Keep size-bucket keys so downstream loops still iterate,
                    # but zero the trading counts. Fee pool is added below
                    # regardless and survives this clear.
                    xch_tier_counts = {k: 0 for k in xch_tier_counts}
                # Sniper arb requires both sides; skip the sniper pool entirely
                # when the mode pins us to a single side.
                if self._sniper_pool_enabled() and not (_is_buy_only or _is_sell_only):
                    sniper_count = int(getattr(cfg, "SNIPER_PREP_COUNT", 0) or 0)
                    xch_tier_counts["sniper"] = sniper_count
                    cat_tier_counts["sniper"] = sniper_count
                cat_total_coins = sum(cat_tier_counts.values())
                if self._fee_pool_enabled():
                    xch_tier_counts[get_fee_tier_name()] = get_fee_pool_count()
                total_coins = sum(xch_tier_counts.values())

                # When BUY_LADDER_REVERSED is active, xch_tier_counts has already
                # been flipped (position-inner→coin-extreme etc.) by
                # get_weighted_tier_prep_counts → flip_position_tiers_to_coin_size_tiers.
                # We must apply the same flip to the coin SIZES so that each coin
                # tier is sized at the OFFER it actually backs — not at the coin-tier
                # name's literal size.  Without this, coin prep tries to build 20
                # "extreme"-named coins at 5.33 XCH each (4.8448×1.1) even though
                # those coins back position-INNER offers that only commit 0.67 XCH.
                # The inverse mapping: coin tier → position tier whose size it uses.
                _COIN_TO_POS: Dict[str, str] = {
                    v: k for k, v in _BUY_REVERSED_POSITION_TO_COIN_SIZE.items()
                }
                if getattr(cfg, "BUY_LADDER_REVERSED", False):
                    # Re-key: each coin tier uses the offer-size of the position
                    # that maps TO it under the reversal.
                    buy_tier_sizes_for_coins = {
                        coin_t: buy_tier_sizes.get(pos_t, Decimal("0"))
                        for coin_t, pos_t in _COIN_TO_POS.items()
                    }
                    # Preserve any non-positional tiers already in buy_tier_sizes
                    for _t, _s in buy_tier_sizes.items():
                        if _t not in buy_tier_sizes_for_coins:
                            buy_tier_sizes_for_coins[_t] = _s
                else:
                    buy_tier_sizes_for_coins = buy_tier_sizes

                buy_tier_sizes_str  = ",".join(
                    f"{t}={s}" for t, s in buy_tier_sizes_for_coins.items()
                )
                sell_tier_sizes_str = ",".join(f"{t}={s}" for t, s in sell_tier_sizes.items())
                # Fee/sniper tiers reuse their buy-side size (XCH-only)
                if self._fee_pool_enabled():
                    fee_name = get_fee_tier_name()
                    fee_sz = Decimal(str(get_fee_coin_size_xch()))
                    buy_tier_sizes_str  += f",{fee_name}={fee_sz}"
                    sell_tier_sizes_str += f",{fee_name}={fee_sz}"
                # Sniper: when the pool is enabled the counts were added to
                # xch_tier_counts/cat_tier_counts above (line ~6330). The prep
                # worker skips any tier whose SIZE is missing from the CLI
                # tier-size strings, so we MUST emit the sniper size on both
                # sides here — otherwise the worker sees `sniper=N` counts but
                # no matching size and never preps the pool.
                if self._sniper_pool_enabled():
                    try:
                        sniper_sz = Decimal(str(getattr(cfg, "SNIPER_SIZE_XCH", Decimal("0"))))
                    except Exception:
                        sniper_sz = Decimal("0")
                    if sniper_sz > 0:
                        buy_tier_sizes_str  += f",sniper={sniper_sz}"
                        sell_tier_sizes_str += f",sniper={sniper_sz}"
                xch_counts_str = ",".join(f"{k}={v}" for k, v in xch_tier_counts.items())
                cat_counts_str = ",".join(f"{k}={v}" for k, v in cat_tier_counts.items())

                cmd.extend(["--xch-target", str(total_coins)])
                cmd.extend(["--buy-tier-sizes",  buy_tier_sizes_str])
                cmd.extend(["--sell-tier-sizes", sell_tier_sizes_str])
                cmd.extend(["--tier-counts-xch", xch_counts_str])
                cmd.extend(["--tier-counts-cat", cat_counts_str])
                cmd.extend(["--cat-target", str(cat_total_coins)])
                cmd.extend(["--prep-headroom-pct", str(prep_headroom_pct)])

                tier_detail = " + ".join(
                    f"{c} {t} × {buy_tier_sizes_for_coins.get(t, Decimal('0'))}"
                    for t, c in xch_tier_counts.items() if c > 0
                )
                log_event("info", "coin_prep_config",
                          f"Tier coin prep ({multiplier}×): {total_coins} XCH coins = {tier_detail} "
                          f"(+{prep_headroom_pct}% headroom, BUY sizes for XCH / SELL sizes for CAT)")
            else:
                # Uniform coin prep with multiplier
                target_xch_size = self.get_target_xch_coin_size()
                total_coins = int((max_buy + max_sell) * multiplier)
                # Ensure at least max_buy + max_sell (1× minimum)
                total_coins = max(total_coins, max_buy + max_sell)
                cmd.extend(["--xch-target", str(total_coins)])
                cmd.extend(["--xch-size", str(target_xch_size)])
                cmd.extend(["--cat-target", str(total_coins)])
                cmd.extend(["--prep-headroom-pct", str(prep_headroom_pct)])
                log_event("info", "coin_prep_config",
                          f"Coin prep config ({multiplier}×): {total_coins} XCH coins × "
                          f"{target_xch_size} each, {total_coins} CAT coins "
                          f"(+{prep_headroom_pct}% headroom)")

            # Pass the bot's current weighted mid to the worker so CAT sizing
            # reflects what the bot is actually quoting, not Dexie's last_price
            # (which can lag by minutes on quiet pairs). Without this the
            # worker logs "Using Dexie last_price for CAT sizing (no live mid
            # passed via --live-price)" twice per prep.
            try:
                _live_mid = None
                _pe = getattr(self, "price_engine", None)
                if _pe is not None and hasattr(_pe, "current_mid"):
                    try:
                        _live_mid = _pe.current_mid()
                    except Exception:
                        _live_mid = None
                if _live_mid is None:
                    # Fall back to whatever the api_server helper exposes.
                    try:
                        from api_server import _get_live_mid_price_str  # type: ignore
                        _live_mid = _get_live_mid_price_str()
                    except Exception:
                        _live_mid = None
                if _live_mid is not None and str(_live_mid).strip():
                    cmd.extend(["--live-price", str(_live_mid)])
            except Exception:
                pass

            self._prep_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge into stdout to prevent pipe deadlock
                stdin=subprocess.DEVNULL,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                env=env,
                **hidden_subprocess_kwargs(),
            )
            # Stamp launch time so check_coin_prep_status() can enforce a
            # maximum runtime — without a ceiling, a wedged subprocess used
            # to pin _prep_running True indefinitely, blocking all coin
            # maintenance paths until a manual restart.
            self._prep_process_started_at = time.time()

            # Drain the worker's stdout pipe continuously so the worker never
            # blocks on a full pipe buffer.  The worker delivers real-time log
            # events via HTTP (ApiMirrorStream), so we can safely discard what
            # comes through the pipe.  Without this drainer the 64 KB pipe
            # buffer fills up after ~300 log lines and the worker stalls
            # indefinitely.
            _proc_ref = self._prep_process

            def _drain_stdout():
                try:
                    while True:
                        chunk = _proc_ref.stdout.read(4096)
                        if not chunk:
                            break
                except Exception:
                    pass

            _drain_thread = threading.Thread(
                target=_drain_stdout,
                daemon=True,
                name="prep-stdout-drain",
            )
            _drain_thread.start()

            log_event("info", "coin_prep_started",
                      f"Coin prep worker started (PID: {self._prep_process.pid})")
            return True

        except Exception as e:
            with self._lock:
                self._prep_running = False
            log_event("error", "coin_prep_start_failed",
                      f"Failed to start coin prep worker: {e}")
            return False

    def check_coin_prep_status(self) -> Dict:
        """Check if coin prep subprocess is still running.

        Enforces a hard runtime ceiling so a wedged coin_prep_worker
        subprocess cannot pin ``_prep_running`` True forever and lock
        out all future topup/prep paths until a manual restart.
        When the age exceeds COIN_PREP_MAX_RUNTIME_SECS (default 10
        minutes) the subprocess is killed and the running flag is
        released. The operator gets a critical log with enough detail
        to investigate if the kill recurs.
        """
        if not self._prep_process:
            return {"running": False}

        # Runtime-ceiling escape hatch.
        max_runtime = float(
            getattr(cfg, "COIN_PREP_MAX_RUNTIME_SECS", 600) or 600
        )
        started_at = float(
            getattr(self, "_prep_process_started_at", 0) or 0
        )
        if (self._prep_process.poll() is None
                and started_at > 0
                and (time.time() - started_at) > max_runtime):
            age = time.time() - started_at
            log_event(
                "critical",
                "coin_prep_runtime_exceeded",
                f"coin_prep_worker subprocess (PID {self._prep_process.pid}) "
                f"has been running for {age:.0f}s, exceeding the "
                f"{max_runtime:.0f}s runtime ceiling. Killing the subprocess "
                f"and releasing _prep_running so coin maintenance can resume. "
                f"Investigate the worker if this recurs — it usually means "
                f"a wallet RPC hang inside the subprocess.",
                data={"pid": self._prep_process.pid,
                      "age_secs": age,
                      "ceiling_secs": max_runtime},
            )
            try:
                self._prep_process.kill()
                try:
                    self._prep_process.wait(timeout=5)
                except Exception:
                    pass
            except Exception as _kill_err:
                log_event("warning", "coin_prep_kill_failed",
                          f"Could not kill wedged coin prep worker: {_kill_err}")
            with self._lock:
                self._prep_running = False
            self._prep_process = None
            return {"running": False, "exit_code": -1,
                    "killed_for_runtime": True}

        poll = self._prep_process.poll()

        if poll is not None:
            with self._lock:
                self._prep_running = False

            try:
                stdout_data = self._prep_process.stdout.read() if self._prep_process.stdout else ""
                stderr_data = self._prep_process.stderr.read() if self._prep_process.stderr else ""
                if stdout_data:
                    log_event("info", "coin_prep_stdout",
                              f"Worker output (last 500 chars): ...{stdout_data[-500:]}")
                if stderr_data:
                    log_event("error", "coin_prep_stderr",
                              f"Worker errors: {stderr_data[-500:]}")
                if poll != 0:
                    log_event("error", "coin_prep_failed",
                              f"Worker exited with code {poll}. stderr: {stderr_data[-300:]}")
            except Exception as e:
                log_event("warning", "coin_prep_output_read_failed",
                          f"Could not read coin prep worker output: {e}")

            try:
                from user_paths import worker_cancelled_ids_file
                cancelled_file = worker_cancelled_ids_file()
            except Exception:
                cancelled_file = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "worker_cancelled_ids.json"
                )
            if os.path.exists(cancelled_file):
                try:
                    with open(cancelled_file, "r") as f:
                        cancelled = json.load(f)
                    self._worker_cancelled_ids.update(cancelled)
                    os.remove(cancelled_file)
                    log_event("info", "coin_prep_cancelled_ids",
                              f"Loaded {len(cancelled)} cancelled IDs from worker")
                except Exception as e:
                    log_event("warning", "coin_prep_cancelled_ids_load_failed",
                              f"Could not load worker cancelled IDs file: {e}")

            self.update_coin_counts()

            return {
                "running": False,
                "exit_code": poll,
                "cancelled_ids": list(self._worker_cancelled_ids),
            }

        return {"running": True, "pid": self._prep_process.pid}

    def get_worker_cancelled_ids(self) -> set:
        """Get and clear worker-cancelled IDs."""
        ids = self._worker_cancelled_ids.copy()
        self._worker_cancelled_ids.clear()
        return ids

    # -------------------------------------------------------------------
    # State queries
    # -------------------------------------------------------------------

    def _get_tier_sizes_mojos(self, is_cat: bool = False) -> Dict[str, int]:
        """Get tier sizes in mojos for tier-aware coin classification.

        F62 (2026-04-09): picks buy sizes for XCH coin prep and sell sizes
        for CAT coin prep, so each wallet is seeded with the coins its own
        offer side will actually spend. Pre-F62 both sides shared one set
        of sizes which, under reverse-buy, left the XCH wallet stocked for
        only ~half of its actual capacity.

        Returns {"inner": mojos, "mid": mojos, "outer": mojos, "extreme": mojos}
        For CAT, derives from XCH tier sizes / price with configurable prep headroom.
        """
        if not is_cat:
            result = dict(get_tier_sizes_mojos_from_cfg(is_cat=False))
            if self._fee_pool_enabled():
                result[get_fee_tier_name()] = get_fee_coin_size_mojos()
            return result

        prep_mult = self._get_coin_prep_headroom_multiplier()
        # CAT wallet feeds SELL offers → use sell tier sizes.
        tier_sizes_xch = self._configured_tier_sizes_xch(side="sell")
        if is_cat:
            price = self._get_current_price()
            cat_scale = Decimal(10) ** Decimal(cfg.CAT_DECIMALS)
            result = {}
            for tier, xch_size in tier_sizes_xch.items():
                if price and price > 0:
                    cat_amount = (xch_size / price * prep_mult).quantize(Decimal("1"))
                else:
                    cat_amount = cfg.CAT_COIN_SIZE
                result[tier] = int(cat_amount * cat_scale)
            return result

    def get_target_xch_coin_size(self, side: str = "buy") -> Decimal:
        """Get prepared XCH coin size for classification and splitting.

        This is the prepared coin size, not the live offer size. Prepared
        coins are larger than live offers by the configurable headroom.

        F62 (2026-04-09): XCH coins fund BUY offers, so default side="buy"
        and the tiered path reads BUY_MID_SIZE_XCH (per-side) instead of
        the shared legacy MID_SIZE_XCH. Callers that need the sell-side
        equivalent (CAT coin prep) pass side="sell".
        """
        prep_mult = self._get_coin_prep_headroom_multiplier()
        if cfg.TIER_ENABLED:
            from config import get_buy_tier_size_xch, get_sell_tier_size_xch
            if (side or "buy").strip().lower() == "sell":
                mid = get_sell_tier_size_xch("mid")
            else:
                mid = get_buy_tier_size_xch("mid")
            if mid and mid > 0:
                return Decimal(str(mid)) * prep_mult
            # Legacy fallback — preserve the pre-F62 behaviour for
            # configs that haven't run Smart Settings yet.
            return cfg.MID_SIZE_XCH * prep_mult
        trade_size = getattr(cfg, "DEFAULT_TRADE_XCH", None)
        if trade_size and trade_size > 0:
            return trade_size * prep_mult
        return cfg.XCH_COIN_SIZE * prep_mult

    def get_target_cat_coin_size(self) -> Decimal:
        """Get prepared CAT coin size for classification and splitting.

        Derives from XCH trade size and current mid price:
          CAT per offer = trade_size_xch / price
          With configurable prep headroom.

        Falls back to CAT_COIN_SIZE config if price unavailable.
        """
        try:
            # CAT coins fund SELL offers, so use the sell-side mid size.
            xch_trade_size = self.get_target_xch_coin_size(side="sell")
            # Try to get price from the price engine (cached last price)
            price = self._get_current_price()
            if price and price > 0:
                cat_per_offer = xch_trade_size / price
                cat_coin_size = cat_per_offer.quantize(Decimal("1"))
                return cat_coin_size
        except Exception as e:
            log_event("debug", "cat_coin_size_calc_failed",
                      f"CAT coin size calculation failed (falling back to config): {e}")
        # Fallback to config value
        return cfg.CAT_COIN_SIZE

    def _get_coin_prep_headroom_multiplier(self) -> Decimal:
        """Return the multiplier applied to prepared coin sizes."""
        try:
            headroom_pct = Decimal(str(getattr(cfg, "COIN_PREP_HEADROOM_PCT", Decimal("10"))))
        except Exception:
            headroom_pct = Decimal("10")
        if headroom_pct < 0:
            headroom_pct = Decimal("0")
        return Decimal("1") + (headroom_pct / Decimal("100"))

    def _get_current_price(self) -> Optional[Decimal]:
        """Get current mid price for CAT size derivation.

        Priority:
          1. price_engine cached `last_price` (fast, no API call).
          2. price_engine `get_price()` for a fresh weighted Tibet+Dexie mid
             when the cache is empty — happens at startup before any bot
             cycle has run. This is what the live bot trades against.
          3. Direct Dexie `last_price` ticker — last-resort fallback for
             environments without a price_engine (e.g. topup worker run
             standalone). NOTE: Dexie's last_price can lag the live mid
             significantly on thin markets. Using it to classify CAT coins
             was the root cause of outer CAT coins being re-labeled as
             inner at startup (13.65M-mojo coins fell inside the wrong-
             price "inner" range and got reassigned by the rebalancer).
        """
        if hasattr(self, '_price_engine') and self._price_engine:
            try:
                last = self._price_engine.get_last_price()
                if last and last > 0:
                    return last
            except Exception as e:
                log_event("debug", "price_engine_cache_fetch_failed",
                          f"Price engine cache fetch failed (will try fresh fetch): {e}")

            # Cache empty (e.g. startup before first cycle). Force a fresh
            # weighted-mid fetch so classification agrees with what the bot
            # trades against.
            try:
                fresh = self._price_engine.get_price()
                if isinstance(fresh, dict):
                    p = fresh.get("mid_price") or fresh.get("mid") or fresh.get("price")
                else:
                    p = fresh
                if p:
                    p_dec = Decimal(str(p))
                    if p_dec > 0:
                        return p_dec
            except Exception as e:
                log_event("debug", "price_engine_fresh_fetch_failed",
                          f"Price engine fresh fetch failed (will try Dexie): {e}")

        # Last resort: Dexie last_price ticker. Can lag the live mid —
        # avoid when possible.
        try:
            import requests
            cat_asset_id = cfg.CAT_ASSET_ID
            if cat_asset_id:
                _dexie_base = str(getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space") or "https://api.dexie.space").rstrip("/")
                try:
                    from api_call_tracker import record as _t
                    _t("dexie", "/v2/prices/tickers")
                except Exception:
                    pass
                resp = requests.get(
                    f"{_dexie_base}/v2/prices/tickers?ticker_id={cat_asset_id}_xch",
                    timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tickers = data.get("tickers", [])
                    if tickers and tickers[0].get("last_price"):
                        price = Decimal(str(tickers[0]["last_price"]))
                        if price > 0:
                            log_event("debug", "price_dexie_last_fallback",
                                      f"Using Dexie last_price {price} for CAT sizing "
                                      f"(may lag live mid)")
                            return price
        except Exception as e:
            log_event("debug", "dexie_price_fetch_failed",
                      f"Direct Dexie price fetch failed (coin sizing will use config default): {e}")
        return None

    def is_busy(self) -> bool:
        """Check if any coin operation is in progress.

        Reads both flags under the lock to avoid torn reads where one flag
        is stale while the other is current.
        """
        with self._lock:
            return self._prep_running or self._topup_running

    def reset_backoff(self):
        """Reset no-coins backoff (called after fills bring new coins)."""
        if self._no_coins_backoff:
            self._no_coins_backoff = False
            self._no_coins_backoff_count = 0
            self._last_topup_time = 0  # clear emergency cooldown
            self._last_drip_time = 0   # clear drip cooldown too
            log_event("debug", "topup_backoff_reset",
                      "Topup backoff reset — fills brought new coins, cooldown cleared")
        self._clear_topup_budget_backoff("fills brought new coins")

    def get_status(self) -> Dict:
        """Get current coin manager status for GUI/API."""
        return {
            "xch_coins": self._xch_coins,
            "cat_coins": self._cat_coins,
            "xch_locked_coins": self._xch_locked_coins,
            "cat_locked_coins": self._cat_locked_coins,
            "xch_total_coins": self._xch_total_coins,
            "cat_total_coins": self._cat_total_coins,
            "prep_running": self._prep_running,
            "topup_running": self._topup_running,
            "no_coins_backoff": self._no_coins_backoff,
            "topup_budget_backoff": self._topup_budget_backoff_active(),
            "topup_budget_backoff_until": self._topup_budget_backoff_until,
            "inventory": self.get_inventory_summary(),
        }
