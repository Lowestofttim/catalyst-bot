#!/usr/bin/env python3
"""Standalone subprocess that reshapes the coin set for optimal trading

This module is launched as a separate process by CoinManager.start_coin_prep
and is never imported as a library by the main bot. The CoinPrepWorker class
drives a PrepPhase state machine that consolidates dust, builds a trading
pool, and splits that pool into tier-sized coins for both XCH and CAT wallets.
Work runs in parallel across the two wallets so the main bot can keep quoting
offers while prep is underway.

Key responsibilities:
    - Analyze current coin state and plan the split/consolidation target
    - Consolidate dust and small coins into a consolidated pool coin
    - Split pool coins into tier-sized trading coins for XCH and CAT
    - Write progress to a status JSON file consumed by the GUI
    - Mirror log events to the bot's HTTP API for live feedback

Because this is a separate process, it does not share memory with the main
bot — all coordination happens via the status file, log mirroring, and the
wallet's own view of the coin set after transactions confirm.
"""

import os
import sys
import json
import time
import subprocess
import threading
import re
from queue import Empty, Full, Queue
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from win_subprocess import hidden_subprocess_kwargs

from wallet import (
    get_all_offers,
    cancel_offer as rpc_cancel_offer,
    cancel_offers_batch,
    get_wallet_sync_status,
    get_spendable_coins_rpc,
    get_transaction,
)
from coin_prep_utils import (
    should_extend_pending_consumed_split_grace,
    should_retry_unconsumed_split,
)
from tx_fees import (
    fee_pool_enabled,
    get_effective_transaction_fee_mojos,
    get_fee_tier_name,
)

# Load environment
from dotenv import load_dotenv
import argparse
load_dotenv()

_LOCAL_API_TOKEN = os.environ.get("BOT_LOCAL_WRITE_TOKEN", "").strip()
_API_LOG_POST_TIMEOUT_S = 0.15
_API_LOG_QUEUE_MAX = 400


def _local_api_headers() -> dict:
    headers = {}
    if _LOCAL_API_TOKEN:
        headers["X-Bot-Local-Token"] = _LOCAL_API_TOKEN
    return headers

# Database integration for coin designations (V3)
# The prep worker writes designations at birth so the DB stays in sync
try:
    from database import (
        init_database, upsert_coin, set_coin_designation, designate_reserve,
        get_reserve_coins, mark_coins_gone, get_setting, set_setting
    )
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False


def _mark_coin_already_advised(coin_id: str) -> None:
    """Suppress the deposit-advisory alert for a coin that coin prep itself
    just created/designated as reserve. Without this the advisor treats
    the post-consolidation pool coin as a freshly-arrived external deposit
    and asks the operator to allocate it, even though it was already
    accounted for in the coin-prep plan.
    """
    if not DB_AVAILABLE or not coin_id:
        return
    try:
        raw = get_setting("deposit_advisory_advised_coins", "") or ""
        existing = {s.strip() for s in raw.split(",") if s.strip()}
        if coin_id in existing:
            return
        existing.add(coin_id)
        set_setting("deposit_advisory_advised_coins", ",".join(sorted(existing)))
    except Exception:
        pass



class PrepPhase(Enum):
    """Current phase of coin preparation"""
    IDLE = "idle"
    ANALYZING = "analyzing"
    CONSOLIDATING = "consolidating"
    CREATING_POOL = "creating_pool"
    SPLITTING = "splitting"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    ERROR = "error"


_STRUCTURED_COIN_PREP_LINE_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s")


class ApiMirrorStream:
    """
    Mirror raw worker stdout/stderr into the main API log stream.

    This lets the superlog capture the full coin prep transcript, including
    startup banner lines and raw Sage prints, while suppressing the timestamped
    lines already forwarded by CoinPrepWorker.log().
    """

    def __init__(self, stream, event_type: str, severity: str):
        self.stream = stream
        self.event_type = event_type
        self.severity = severity
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, data):
        if not data:
            return 0

        self.stream.write(data)

        with self._lock:
            self._buffer += data
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._emit_line(line.rstrip("\r"))

        return len(data)

    def flush(self):
        self.stream.flush()
        with self._lock:
            if self._buffer:
                self._emit_line(self._buffer.rstrip("\r"))
                self._buffer = ""

    def isatty(self):
        return getattr(self.stream, "isatty", lambda: False)()

    def fileno(self):
        return self.stream.fileno()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def _emit_line(self, line: str):
        if not line.strip():
            return
        if _STRUCTURED_COIN_PREP_LINE_RE.match(line):
            return
        try:
            import requests as _req
            _req.post("http://localhost:5000/api/log", json={
                "severity": self.severity,
                "event_type": self.event_type,
                "message": line,
            }, headers=_local_api_headers(), timeout=_API_LOG_POST_TIMEOUT_S)
        except Exception:
            pass


@dataclass
class CoinPrepStatus:
    """Status of coin preparation - written to file for GUI to read"""
    phase: str
    progress: float  # 0.0 to 1.0
    message: str
    xch_coins_current: int
    xch_coins_target: int
    cat_coins_current: int
    cat_coins_target: int
    error: Optional[str] = None
    timestamp: float = 0.0
    run_id: Optional[str] = None

    def to_dict(self):
        data = asdict(self)
        data['percentage'] = int(self.progress * 100)
        return data

class CoinPrepWorker:
    """
    Intelligent coin preparation worker with PARALLEL OPTIMIZATION
    
    Runs complete flow:
    1. Analyze current state (5%)
    2. Consolidate if needed (10-15%)
    3. Create trading pools IN PARALLEL (25-55%) ⚡
    4. Split pool coins IN PARALLEL (65-85%) ⚡
    5. Verify and report completion (95-100%)
    
    PARALLEL OPTIMIZATION:
    - Submit XCH pool transaction (25%)
    - Wait 5 seconds
    - Submit CAT pool transaction (35%)
    - Confirm BOTH in parallel (45-55%)
    - Same for splitting phase (65-85%)
    
    Time savings: ~80 seconds (44% faster!)
    """
    
    def __init__(self):
        # Detect wallet type (chia or sage)
        self.wallet_type = os.getenv("WALLET_TYPE", "sage").lower().strip()
        self.is_sage = (self.wallet_type == "sage")
        # Only start the HTTP log-forwarder when running as the real worker
        # subprocess (main() sets _CLI_WORKER_SUBPROCESS=1 before instantiating).
        # When unit tests instantiate CoinPrepWorker, the api-log loop must NOT
        # POST to localhost:5000 — it would push test-generated messages into
        # a running bot's live log feed and look like a rogue prep run.
        self._is_subprocess = os.getenv("_CLI_WORKER_SUBPROCESS") == "1"
        self._api_log_queue = Queue(maxsize=_API_LOG_QUEUE_MAX)
        self._api_log_drop_count = 0
        if self._is_subprocess:
            self._api_log_worker = threading.Thread(
                target=self._api_log_loop,
                name="coin-prep-api-log",
                daemon=True,
            )
            self._api_log_worker.start()
        else:
            self._api_log_worker = None

        # Configuration from env
        env_fp = os.getenv("WALLET_FINGERPRINT")
        if env_fp:
            self.fingerprint = env_fp
            print(f"[INIT] Using fingerprint from env: {env_fp}")
        else:
            print("[INIT] No env fingerprint, attempting auto-detect...")
            self.fingerprint = self._get_fingerprint()

        self.xch_wallet_id = int(os.getenv("CHIA_WALLET_ID_XCH", "1"))
        # CAT wallet_id: default 2 (Sage dynamic ID). get_wallets() in the
        # subprocess will override this to match the configured CAT_ASSET_ID.
        self.cat_wallet_id = int(os.getenv("CAT_WALLET_ID", "2"))
        
        # Coin targets — derive from bot settings.
        # We create DOUBLE the offer count so there are always spare coins
        # available for requotes, sniping, quick replacements, etc.
        # e.g. 25 buy + 25 sell → 50 XCH coins + 50 CAT coins
        #
        # IMPORTANT: We use _CLI_XCH_TARGET (set by main() from --xch-target)
        # instead of XCH_TARGET_COINS from .env, because load_dotenv() can
        # load stale values from .env that cause double-counting.
        max_buy = int(os.getenv("MAX_ACTIVE_BUY_OFFERS", os.getenv("MAX_ACTIVE_BUY", "25")))
        max_sell = int(os.getenv("MAX_ACTIVE_SELL_OFFERS", os.getenv("MAX_ACTIVE_SELL", "25")))

        # Check if main() set explicit CLI overrides (uses private env keys
        # that can't clash with stale .env values)
        xch_target_override = os.getenv("_CLI_XCH_TARGET")
        cat_target_override = os.getenv("_CLI_CAT_TARGET")

        prep_headroom_raw = os.getenv(
            "_CLI_PREP_HEADROOM_PCT",
            os.getenv("COIN_PREP_HEADROOM_PCT", "10")
        )
        try:
            self.coin_prep_headroom_pct = max(Decimal("0"), Decimal(str(prep_headroom_raw)))
        except Exception:
            self.coin_prep_headroom_pct = Decimal("10")
        self.coin_prep_headroom_multiplier = (
            Decimal("1") + (self.coin_prep_headroom_pct / Decimal("100"))
        )

        # XCH coins = buy + sell count (double up for spares)
        if xch_target_override:
            self.xch_target_coins = int(xch_target_override)
            self.log(f"   XCH target: {self.xch_target_coins} coins (from CLI --xch-target)")
        else:
            self.xch_target_coins = max_buy + max_sell
            self.log(f"   XCH target: {self.xch_target_coins} coins ({max_buy} for offers + {max_sell} spare)")

        # Coin size = what the bot uses per offer (DEFAULT_TRADE_XCH).
        # The prepared coin is larger by the configurable headroom so Sage
        # does not need to top up from another prepared coin.
        default_trade_xch = os.getenv("DEFAULT_TRADE_XCH")
        if default_trade_xch:
            self.offer_xch_size = Decimal(default_trade_xch)
            self.xch_coin_size = self._apply_prep_headroom_xch(self.offer_xch_size)
            self.log(f"   XCH offer size: {self.offer_xch_size} (from DEFAULT_TRADE_XCH)")
            self.log(f"   XCH prep coin size: {self.xch_coin_size} (+{self.coin_prep_headroom_pct}% headroom)")
        else:
            # Fallback to legacy XCH_COIN_SIZE
            self.offer_xch_size = Decimal(os.getenv("XCH_COIN_SIZE", "0.25"))
            self.xch_coin_size = self._apply_prep_headroom_xch(self.offer_xch_size)
            self.log(f"   XCH offer size: {self.offer_xch_size} (from XCH_COIN_SIZE fallback)")
            self.log(f"   XCH prep coin size: {self.xch_coin_size} (+{self.coin_prep_headroom_pct}% headroom)")

        self.xch_reserve = Decimal(os.getenv("XCH_RESERVE", "0.03"))

        # CAT settings — same approach, derive from bot config
        # CAT_DECIMALS is the canonical name; MZ_DECIMALS is the legacy alias kept for
        # any old .env files that haven't been migrated yet.
        self.cat_decimals = int(os.getenv("CAT_DECIMALS") or os.getenv("MZ_DECIMALS", "3"))

        # CAT coins = buy + sell count (double up for spares)
        if cat_target_override:
            self.cat_target_coins = int(cat_target_override)
            self.log(f"   CAT target: {self.cat_target_coins} coins (from CLI --cat-target)")
        else:
            self.cat_target_coins = max_buy + max_sell
            self.log(f"   CAT target: {self.cat_target_coins} coins ({max_sell} for offers + {max_buy} spare)")

        # CAT coin size: derive from XCH trade size + current price if possible
        # Otherwise fall back to CAT_COIN_SIZE from .env
        cat_coin_size_override = os.getenv("CAT_COIN_SIZE")
        if default_trade_xch:
            # Try to calculate: CAT per offer = XCH per offer / price
            # This gives us the right-sized CAT coins for each sell offer
            self.cat_coin_size = self._derive_cat_coin_size(self.offer_xch_size)
        elif cat_coin_size_override:
            self.cat_coin_size = Decimal(cat_coin_size_override)
            self.log(f"   CAT coin size: {self.cat_coin_size} (from CAT_COIN_SIZE)")
        else:
            self.cat_coin_size = Decimal("4000")
            self.log(f"   CAT coin size: {self.cat_coin_size} (default fallback)")

        # CAT_RESERVE is the canonical name; MZ_RESERVE is the legacy alias.
        self.cat_reserve = Decimal(os.getenv("CAT_RESERVE") or os.getenv("MZ_RESERVE", "0"))

        # --- Tier configuration (set by coin_manager.start_coin_prep) ---
        tier_sizes_str = os.getenv("_CLI_TIER_SIZES")  # legacy shared sizes
        # F62 (2026-04-09): per-side sizes. When both are provided they
        # override --tier-sizes — XCH coin prep uses buy sizes, CAT coin
        # prep uses sell sizes. Enables asymmetric ladders.
        buy_tier_sizes_str  = os.getenv("_CLI_BUY_TIER_SIZES")
        sell_tier_sizes_str = os.getenv("_CLI_SELL_TIER_SIZES")
        tier_counts_str = os.getenv("_CLI_TIER_COUNTS")  # legacy: applies to BOTH sides
        tier_counts_xch_str = os.getenv("_CLI_TIER_COUNTS_XCH")  # per-side XCH (buy ladder)
        tier_counts_cat_str = os.getenv("_CLI_TIER_COUNTS_CAT")  # per-side CAT (sell ladder)

        def _parse_sizes(spec):
            out = {}
            if not spec:
                return out
            for pair in spec.split(","):
                if "=" not in pair:
                    continue
                k, v = pair.split("=", 1)
                try:
                    out[k.strip()] = Decimal(v.strip())
                except Exception:
                    continue
            return out

        # Per-side counts take precedence; legacy --tier-counts is the fallback
        # for both sides (symmetric prep).
        any_tier_counts = bool(tier_counts_str or tier_counts_xch_str or tier_counts_cat_str)
        any_tier_sizes = bool(tier_sizes_str or buy_tier_sizes_str or sell_tier_sizes_str)
        if any_tier_sizes and any_tier_counts:
            self.tier_enabled = True
            # Parse intended live tier sizes from the caller config. When
            # per-side strings are present they take priority; otherwise
            # fall back to the legacy shared --tier-sizes.
            _legacy_live_sizes = _parse_sizes(tier_sizes_str)
            self.offer_tier_xch_sizes_buy  = _parse_sizes(buy_tier_sizes_str)  or dict(_legacy_live_sizes)
            self.offer_tier_xch_sizes_sell = _parse_sizes(sell_tier_sizes_str) or dict(_legacy_live_sizes)
            # `offer_tier_xch_sizes` remains as a MAX-of-both-sides view
            # that legacy code paths (tier_order, status reports) consume.
            # Individual coin splitting uses the per-side dicts.
            all_tier_names = set(self.offer_tier_xch_sizes_buy) | set(self.offer_tier_xch_sizes_sell)
            self.offer_tier_xch_sizes = {
                tn: max(
                    self.offer_tier_xch_sizes_buy.get(tn, Decimal("0")),
                    self.offer_tier_xch_sizes_sell.get(tn, Decimal("0")),
                )
                for tn in all_tier_names
            }

            # Prepared tier coins get extra headroom above the live offer size.
            # Per-side prep sizes are built from the per-side live sizes so
            # XCH coins are prepped at buy sizes and CAT coins at sell sizes.
            self.tier_xch_sizes_buy = {
                tier_name: self._apply_prep_headroom_xch(size_xch)
                for tier_name, size_xch in self.offer_tier_xch_sizes_buy.items()
            }
            self.tier_xch_sizes_sell = {
                tier_name: self._apply_prep_headroom_xch(size_xch)
                for tier_name, size_xch in self.offer_tier_xch_sizes_sell.items()
            }
            # Legacy `tier_xch_sizes` for back-compat — defaults to the
            # BUY-side values since this was historically the XCH side.
            self.tier_xch_sizes = dict(self.tier_xch_sizes_buy)

            def _parse_counts(spec):
                out = {}
                if not spec:
                    return out
                for pair in spec.split(","):
                    if "=" not in pair:
                        continue
                    k, v = pair.split("=", 1)
                    try:
                        out[k.strip()] = int(v.strip())
                    except ValueError:
                        continue
                return out

            legacy_counts = _parse_counts(tier_counts_str)
            self.xch_tier_counts = _parse_counts(tier_counts_xch_str) if tier_counts_xch_str else dict(legacy_counts)
            self.cat_tier_counts = _parse_counts(tier_counts_cat_str) if tier_counts_cat_str else dict(legacy_counts)

            # Build a unified `tier_counts` for legacy code paths that still
            # consume a single dict. Use max() so per-amount partition logic
            # never under-allocates either side.
            all_tier_names = set(self.xch_tier_counts) | set(self.cat_tier_counts) | set(legacy_counts)
            self.tier_counts = {
                tn: max(
                    int(self.xch_tier_counts.get(tn, 0) or 0),
                    int(self.cat_tier_counts.get(tn, 0) or 0),
                )
                for tn in all_tier_names
            }

            # Derive CAT sizes per tier from XCH sizes / price × headroom
            self.tier_cat_sizes = self._derive_tier_cat_sizes()
            # Drop any CAT counts for tiers that have no CAT size (e.g. fees,
            # which is XCH-only). Avoids accidentally trying to multi-send CAT
            # for an XCH-only tier.
            self.cat_tier_counts = {
                tn: cnt
                for tn, cnt in self.cat_tier_counts.items()
                if self.tier_cat_sizes.get(tn, Decimal("0")) > 0
            }

            self.tier_order = sorted(
                self.offer_tier_xch_sizes.keys(),
                key=lambda t: self.offer_tier_xch_sizes[t],
                reverse=True,
            )

            # Update totals for pool creation
            self.xch_target_coins = sum(self.xch_tier_counts.values())
            self.cat_target_coins = sum(self.cat_tier_counts.values())

            self.log("\n   🏗️ TIER MODE:")
            for tn in self.tier_order:
                xcnt = int(self.xch_tier_counts.get(tn, 0) or 0)
                ccnt = int(self.cat_tier_counts.get(tn, 0) or 0)
                live_xsz = self.offer_tier_xch_sizes.get(tn, Decimal("0"))
                prep_xsz = self.tier_xch_sizes.get(tn, Decimal("0"))
                csz = self.tier_cat_sizes.get(tn, Decimal("0"))
                if csz > 0:
                    self.log(
                        f"     {tn}: XCH={xcnt} × {prep_xsz} / CAT={ccnt} × {csz:,.0f} "
                        f"(live size {live_xsz} XCH)"
                    )
                else:
                    self.log(
                        f"     {tn}: XCH={xcnt} × {prep_xsz} (live size {live_xsz} XCH, XCH-only pool)"
                    )
        else:
            self.tier_enabled = False
            self.offer_tier_xch_sizes = {}
            self.tier_xch_sizes = {}
            self.tier_counts = {}
            self.xch_tier_counts = {}
            self.cat_tier_counts = {}
            self.tier_cat_sizes = {}
            self.tier_order = []

        # Status file for GUI communication
        self.status_file = "coin_prep_status.json"
        
        # Thread-safe status updates
        self.status_lock = threading.Lock()
        
        # The wallet always ends prep with one extra leftover coin per side —
        # the change/reserve coin that doubles as the topup buffer for coin
        # management. Surface that in the GUI target so the "Coins Ready"
        # display matches the actual wallet state (e.g. 111/111 instead of
        # 111/110).
        self.xch_expected_total_coins = self.xch_target_coins + 1
        self.cat_expected_total_coins = self.cat_target_coins + 1

        # Initial status — report prepared-coin targets, not expected_total.
        # xch_expected_total_coins (+1 for reserve) is used internally for
        # split confirmation polling and health checks, but the GUI target
        # should reflect the trading-coin target the user configured.
        self.status = CoinPrepStatus(
            phase=PrepPhase.IDLE.value,
            progress=0.0,
            message="Initializing...",
            xch_coins_current=0,
            xch_coins_target=self.xch_target_coins,
            cat_coins_current=0,
            cat_coins_target=self.cat_target_coins,
            timestamp=time.time()
        )
        
        # Initialise database for designation writes
        self._db_ready = False
        if DB_AVAILABLE:
            try:
                init_database()
                self._db_ready = True
                self.log("   DB: connected — will write coin designations at birth")
            except Exception as e:
                self.log(f"   DB: init failed ({e}) — designations will be skipped")
        else:
            self.log("   DB: module not available — designations will be skipped")

        self.log("🪙 Coin Prep Worker initialized (PARALLEL MODE)")
        self.log(f"   XCH: {self.xch_target_coins} coins @ {self.xch_coin_size} each")
        self.log(f"   CAT: {self.cat_target_coins} coins @ {self.cat_coin_size} each")
        self.log("   ⚡ Parallel optimization enabled!")

    def _fee_pool_enabled(self) -> bool:
        return fee_pool_enabled()

    def _tx_fee_mojos(self) -> int:
        return get_effective_transaction_fee_mojos()

    def _split_tx_fee_mojos(self) -> int:
        """Split RPC currently cannot pin a dedicated fee coin safely in Sage."""
        fee_mojos = self._tx_fee_mojos()
        if self.is_sage and fee_mojos > 0:
            return 0
        return fee_mojos

    def _refresh_coin_targets(self):
        """Keep internal total-coin expectations and GUI targets in sync."""
        self.xch_expected_total_coins = self.xch_target_coins + 1
        self.cat_expected_total_coins = self.cat_target_coins + 1
        if hasattr(self, "status"):
            with self.status_lock:
                # GUI target = prepared-coin count only (xch_target_coins).
                # xch_expected_total_coins (+1 reserve) is for internal use.
                self.status.xch_coins_target = self.xch_target_coins
                self.status.cat_coins_target = self.cat_target_coins

    @staticmethod
    def _prepared_coin_count_from_total(total_count: int) -> int:
        """Convert a wallet-total count into prepared trading coins."""
        try:
            return max(0, int(total_count or 0) - 1)
        except Exception:
            return 0

    def _set_status_coin_counts(self, xch_total: Optional[int] = None, cat_total: Optional[int] = None):
        """Store prepared-coin counts for GUI progress updates."""
        with self.status_lock:
            if xch_total is not None:
                self.status.xch_coins_current = self._prepared_coin_count_from_total(xch_total)
            if cat_total is not None:
                self.status.cat_coins_current = self._prepared_coin_count_from_total(cat_total)

    @staticmethod
    def _sage_submit_succeeded(result) -> bool:
        """Treat Sage RPC submission as successful only for non-error responses."""
        if result is None:
            return False
        if isinstance(result, dict):
            if result.get("error"):
                return False
            if result.get("success") is False:
                return False
            status = str(result.get("status", "")).strip().lower()
            if status in {"error", "failed", "failure"}:
                return False
        return True

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
            single = result.get("transaction_id")
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
            if not tx_id:
                continue
            clean = str(tx_id).strip().lower()
            if not clean:
                continue
            if not clean.startswith("0x"):
                clean = "0x" + clean
            if clean not in seen:
                seen.add(clean)
                normalized.append(clean)
        return normalized

    def _get_transaction_confirmation_state(self, tx_ids: List[str]) -> Dict[str, object]:
        """Summarize confirmation state for a list of Sage transaction ids."""
        tx_ids = [tid for tid in (tx_ids or []) if tid]
        if not tx_ids:
            return {"known": False, "confirmed": False, "confirmed_count": 0, "total": 0, "height": 0}

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
            confirmed = tx_info.get("confirmed", False)
            height = int(tx_info.get("confirmed_at_height", 0) or 0)
            if confirmed or height > 0:
                confirmed_count += 1
                best_height = max(best_height, height)

        total = len(tx_ids)
        confirmed = confirmed_count > 0 if total == 1 else confirmed_count == total
        return {
            "known": any_known,
            "confirmed": confirmed,
            "confirmed_count": confirmed_count,
            "total": total,
            "height": best_height,
        }

    # ------------------------------------------------------------------
    # DB designation helpers — coins get their roles assigned at birth
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_0x(coin_id: str) -> str:
        """Ensure coin ID has 0x prefix — DB stores IDs with 0x."""
        if not coin_id:
            return coin_id
        return coin_id if coin_id.startswith("0x") else "0x" + coin_id

    def _designate_coins_from_snapshot(self, wallet_id: int, wallet_type: str,
                                        tier_name: str = None,
                                        designation: str = "tier_spare"):
        """
        Snapshot the wallet and designate ALL current coins.

        Used after splits when we know every coin in this wallet belongs
        to a specific role. For the topup pool coin (the large leftover
        after tier splits), it detects the largest non-tier coin and tags
        it as the topup source (internally 'reserve').
        """
        if not self._db_ready:
            return

        coins = self._get_coins_via_rpc(wallet_id, wallet_type)
        if not coins:
            self.log(f"   DB: no coins to designate for {wallet_type}")
            return

        count = 0
        for c in coins:
            coin_id = self._ensure_0x(c.get("coin_id", ""))
            amount = c.get("amount", 0)
            if not coin_id:
                continue
            try:
                upsert_coin(coin_id, wallet_type, amount)
                set_coin_designation(coin_id, designation,
                                    assigned_tier=tier_name or "none")
                count += 1
            except Exception as e:
                self.log(f"   DB: failed to designate {coin_id[:16]}...: {e}")

        self.log(f"   DB: designated {count} {wallet_type} coins as {designation}"
                 f"{f' / {tier_name}' if tier_name else ''}")

    def _designate_new_tier_coins(self, before_snapshot: dict, after_snapshot: dict,
                                   wallet_type: str, tier_name: str):
        """
        Diff two snapshots and designate the NEW coins as tier_spare.

        The coins that existed before but vanished are marked gone.
        The coins that are new get designated with their tier.
        The leftover (topup pool / change) coin is the one NOT matching
        any tier size — we tag it as the topup pool source.
        """
        if not self._db_ready:
            return

        # Mark disappeared coins as gone (they got consumed by splits)
        gone_ids = set(before_snapshot.keys()) - set(after_snapshot.keys())
        for coin_id in gone_ids:
            cid = self._ensure_0x(coin_id)
            try:
                mark_coins_gone([cid])
            except Exception:
                pass

        # Designate new coins
        new_ids = set(after_snapshot.keys()) - set(before_snapshot.keys())
        tier_count = 0
        reserve_coin = None

        # Work out expected tier size in mojos for matching
        if wallet_type == "xch":
            expected_mojos = int(self.tier_xch_sizes.get(tier_name, Decimal("0"))
                                 * Decimal("1000000000000"))
        else:
            expected_mojos = int(self.tier_cat_sizes.get(tier_name, Decimal("0"))
                                 * (10 ** self.cat_decimals))

        for coin_id in new_ids:
            cid = self._ensure_0x(coin_id)
            amount = after_snapshot[coin_id]
            try:
                upsert_coin(cid, wallet_type, amount)

                # If amount matches tier size (exact) → tier_spare
                # If amount doesn't match → it's the topup pool change coin
                if expected_mojos > 0 and amount == expected_mojos:
                    set_coin_designation(cid, "tier_spare",
                                        assigned_tier=tier_name)
                    tier_count += 1
                else:
                    # Likely the topup pool change coin — track the largest one
                    if reserve_coin is None or amount > reserve_coin[1]:
                        reserve_coin = (cid, amount)
            except Exception as e:
                self.log(f"   DB: designation error for {cid[:16]}...: {e}")

        if tier_count > 0:
            self.log(f"   DB: {tier_count} new {wallet_type} coins → tier_spare/{tier_name}")

        # Designate the largest non-tier coin as the topup pool source
        # (internally stored as 'reserve' in the DB; displayed as 'topup pool' in logs)
        if reserve_coin:
            try:
                designate_reserve(reserve_coin[0], wallet_type, reserve_coin[1])
                _mark_coin_already_advised(reserve_coin[0])
                self.log(f"   DB: topup pool coin {wallet_type} → {reserve_coin[0][:16]}... "
                         f"({reserve_coin[1]:,} mojos)")
            except Exception as e:
                self.log(f"   DB: topup pool designation failed: {e}")

    def _designate_reserve_after_consolidation(self, wallet_id: int,
                                                wallet_type: str):
        """
        After consolidation, there's exactly 1 coin per wallet.
        Tag it as the topup pool source (internally 'reserve') — it will
        be split into trading coins by the topup worker during live trading.
        """
        if not self._db_ready:
            return

        coins = self._get_coins_via_rpc(wallet_id, wallet_type)
        if not coins:
            return

        for c in coins:
            coin_id = self._ensure_0x(c.get("coin_id", ""))
            amount = c.get("amount", 0)
            if coin_id:
                try:
                    upsert_coin(coin_id, wallet_type, amount)
                    designate_reserve(coin_id, wallet_type, amount)
                    _mark_coin_already_advised(coin_id)
                    self.log(f"   DB: post-consolidation {wallet_type} topup pool coin → "
                             f"{coin_id[:16]}... ({amount:,} mojos)")
                except Exception as e:
                    self.log(f"   DB: topup pool designation failed: {e}")

    def _build_tier_amount_plan(self, wallet_type: str):
        """Build exact per-amount tier expectations for the current prep mode."""
        plan = {}
        if not self.tier_enabled:
            return plan

        for tier_name in self.tier_order:
            if wallet_type == "xch":
                expected_count = int(self.xch_tier_counts.get(tier_name, 0) or 0)
            else:
                expected_count = int(self.cat_tier_counts.get(tier_name, 0) or 0)
            if expected_count <= 0:
                continue
            if wallet_type == "xch":
                amount = int(self.tier_xch_sizes.get(tier_name, Decimal("0")) * Decimal("1000000000000"))
            else:
                amount = int(self.tier_cat_sizes.get(tier_name, Decimal("0")) * (10 ** self.cat_decimals))
            if amount <= 0:
                continue
            plan.setdefault(amount, []).append((tier_name, expected_count))

        return plan

    def _partition_coins_for_designation(self, coins, wallet_type: str):
        """Allocate coins to tiers by tier amount and expected counts.

        F60 (2026-04-09): fuzzy amount matching. Previously this was an
        EXACT amount-match lookup (plan keyed by `int(size × 1e12)`), but
        Sage's split + multi_send flow can deduct a single transaction fee
        from the pool coin mid-flight, leaving each output N-fee_mojos
        smaller than the plan expects. On fee coins (50 × 0.001 XCH) the
        discrepancy was ~261,582 mojos per coin — small in absolute terms
        but enough to make the exact-match fall through. Result: the whole
        fees tier ended up in `unmatched` and got merged back into reserve
        by `_merge_xch_fee_change_into_reserve`, leaving the bot with 50
        fewer coins than the run had actually successfully prepared.

        Fix: for each coin, find the CLOSEST plan amount within a relative
        tolerance (1% or ±10,000,000 mojos, whichever is smaller). This
        handles both large-tier coins (where 1% is generous) and tiny fee
        coins (where 10M mojos = 0.00001 XCH is enough slack for the fee
        to be absorbed).
        """
        plan = self._build_tier_amount_plan(wallet_type)
        # Build a flat list of (plan_amount, tier_name, expected_count) so
        # we can find the closest plan entry for each coin.
        plan_entries = []  # list[(amount:int, tier:str, expected:int)]
        for plan_amount, specs in plan.items():
            for tier_name, expected in specs:
                plan_entries.append((int(plan_amount), tier_name, int(expected)))

        # Tolerance: 1% of the plan amount, or a minimum of 10M mojos
        # (0.00001 XCH) to cover typical single-tx fees on tiny fee coins.
        def _within_tolerance(coin_amount: int, plan_amount: int) -> bool:
            if plan_amount <= 0:
                return False
            abs_tol = max(int(plan_amount * 0.01), 10_000_000)
            return abs(coin_amount - plan_amount) <= abs_tol

        # Sort coins by amount descending so biggest tiers get first pick
        # (prevents a mid-sized coin stealing an extreme slot, etc.)
        all_coins = sorted(
            [c for c in (coins or []) if c.get("amount", 0) > 0],
            key=lambda c: c.get("amount", 0),
            reverse=True,
        )

        # Track remaining slots per (plan_amount, tier_name) so each plan
        # entry can only absorb its expected count of coins.
        slots_remaining = {
            (amt, tier): exp for (amt, tier, exp) in plan_entries
        }

        assigned = {}
        unmatched = []

        for coin in all_coins:
            coin_amount = int(coin.get("amount", 0) or 0)
            # Find the plan entry (amount, tier) that is within tolerance
            # AND has remaining slots, picking the CLOSEST match.
            best_key = None
            best_diff = None
            for (plan_amount, tier_name, _exp) in plan_entries:
                if slots_remaining.get((plan_amount, tier_name), 0) <= 0:
                    continue
                if not _within_tolerance(coin_amount, plan_amount):
                    continue
                diff = abs(coin_amount - plan_amount)
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_key = (plan_amount, tier_name)
            if best_key is not None:
                assigned.setdefault(best_key[1], []).append(coin)
                slots_remaining[best_key] -= 1
            else:
                unmatched.append(coin)

        unmatched.sort(key=lambda c: c.get("amount", 0), reverse=True)
        return assigned, unmatched

    def _merge_xch_fee_change_into_reserve(self) -> bool:
        """Merge leftover XCH fee-funding change back into reserve before final DB sweep.

        Design: the coin-prep fee coin is whatever Sage's auto_combine left
        behind during consolidation — a small dedicated coin that funds TX
        fees during prep operations. After every split has confirmed, what
        remains is at most slightly diminished from its starting size
        (Sage's split RPC handles fees internally for many calls), and we
        merge it back into the reserve so the final wallet state is just
        ``[reserve, 119 tier coins]``.

        Polling note: the previous version waited for ``get_pending_transactions()``
        to return EMPTY before checking coin counts. That's broken in two
        ways — our own combine TX sits in pending until block inclusion
        (~52s typical), and any unrelated wallet activity also blocks the
        check forever. The new version tracks the combine's own TX ID and
        considers it confirmed the moment that specific ID leaves the
        pending list, regardless of other wallet activity.
        """
        if not (self.is_sage and self.tier_enabled and self._fee_pool_enabled() and self._tx_fee_mojos() > 0):
            return False

        try:
            from wallet_sage import combine_coins, get_pending_transactions
        except Exception as e:
            self.log(f"XCH fee cleanup unavailable: {e}")
            return False

        coins = self._get_coins_via_rpc(self.xch_wallet_id, "xch-fee-cleanup", selectable_only=True)
        if not coins:
            return False

        _assigned, unmatched = self._partition_coins_for_designation(coins, "xch")
        if len(unmatched) <= 1:
            return False

        reserve_coin = unmatched[0]
        extra_coins = unmatched[1:]
        extra_ids = [c.get("coin_id", "").replace("0x", "") for c in extra_coins if c.get("coin_id")]
        extra_total = sum(c.get("amount", 0) for c in extra_coins)
        if not extra_ids or extra_total <= 0:
            return False

        reserve_id = reserve_coin.get("coin_id", "").replace("0x", "")
        if not reserve_id:
            return False

        self.log(f"XCH fee cleanup: merging {len(extra_ids)} extra coin(s) ({extra_total:,} mojos) back into reserve")
        result = combine_coins([reserve_id] + extra_ids, fee_mojos=self._tx_fee_mojos())
        if not self._sage_submit_succeeded(result):
            self.log("XCH fee cleanup combine was not accepted by Sage")
            return False

        # Track OUR specific combine TX so unrelated wallet activity (e.g.
        # background mempool watchers) doesn't extend the wait. If Sage
        # didn't return a TX ID, fall back to a short coin-count poll.
        my_tx_ids = {tx.lstrip("0x").lower()
                     for tx in self._extract_sage_transaction_ids(result) if tx}
        expected_after = len(coins) - len(extra_ids)
        # Compute the expected post-merge reserve amount so we can poll
        # for the new coin specifically — not just for the total count
        # to drop. Without this, "count dropped" is satisfied the moment
        # the inputs disappear from mempool, but the new merged-output
        # coin can lag the wallet RPC by several seconds. The downstream
        # _designate_final_sweep then sees only the trading coins and
        # tags reserve=0, leaving the visible-but-unowned coin to be
        # picked up later by the bot's startup reconcile (and missing
        # the post-prep deposit-advisory baseline write).
        try:
            _expected_merged_amount = (
                int(reserve_coin.get("amount", 0))
                + int(extra_total)
                - int(self._tx_fee_mojos())
            )
        except Exception:
            _expected_merged_amount = 0
        # Identify the input coin_ids so we can distinguish the new
        # merged-output coin from any pre-existing wallet coins.
        _input_ids = set()
        for c in [reserve_coin] + extra_coins:
            _cid = str(c.get("coin_id", "")).strip().lower()
            if _cid.startswith("0x"):
                _cid = _cid[2:]
            if _cid:
                _input_ids.add(_cid)

        # 90s ceiling — typical Chia block time is ~52s, two blocks is
        # plenty for a 2-input combine. Beyond that something else is
        # wrong and the next prep cycle will absorb the stray instead.
        max_seconds = 90
        poll_interval = 5
        for poll in range(max_seconds // poll_interval):
            pending = get_pending_transactions() or []
            pending_ids = {
                str(tx.get("transaction_id", "")).lstrip("0x").lower()
                for tx in pending if isinstance(tx, dict)
            }
            our_tx_still_pending = bool(my_tx_ids and (my_tx_ids & pending_ids))

            if not our_tx_still_pending:
                # Either our TX confirmed or Sage didn't return an ID — verify
                # by coin count. If counts dropped to the expected level, done.
                visible = self._get_coins_via_rpc(self.xch_wallet_id, "xch-fee-cleanup-confirm", selectable_only=True) or []
                confirmed_count = self.get_confirmed_coin_count(self.xch_wallet_id)
                # Strict success: count matches AND a NEW (non-input)
                # coin with the expected merged amount is visible. This
                # eliminates the post-merge reserve-not-tagged race.
                _merged_visible = False
                if _expected_merged_amount > 0:
                    for vc in visible:
                        v_cid = str(vc.get("coin_id", "")).strip().lower()
                        if v_cid.startswith("0x"):
                            v_cid = v_cid[2:]
                        if v_cid in _input_ids:
                            continue
                        v_amt = int(vc.get("amount", 0) or 0)
                        # Allow 0.001 XCH / 1000 mojos tolerance for
                        # fee-rounding artefacts from Sage.
                        if abs(v_amt - _expected_merged_amount) <= 1_000_000_000:
                            _merged_visible = True
                            break
                if (len(visible) <= expected_after
                        and confirmed_count <= expected_after
                        and (_merged_visible or _expected_merged_amount <= 0)):
                    self.log(f"XCH fee cleanup confirmed after {poll * poll_interval}s")
                    return True
                # TX no longer pending but counts/coin haven't updated yet —
                # likely a sync lag, give it another tick or two. Wait
                # one extra cycle past the previous threshold so the
                # merged-coin visibility check has a real chance to
                # succeed before we fall back to "assuming success".
                if my_tx_ids and poll >= 3:
                    self.log("XCH fee cleanup TX cleared mempool but coin view lags — assuming success")
                    return True

            if poll > 0 and poll % 4 == 0:
                self.log(f"XCH fee cleanup still pending after {poll * poll_interval}s")
            time.sleep(poll_interval)

        self.log(f"XCH fee cleanup did not confirm within {max_seconds}s — stray will be absorbed by the next prep cycle")
        return False

    def _designate_final_sweep(self):
        """Final pass after all splits complete."""
        if not self._db_ready:
            self.log("   DB: final sweep SKIPPED - DB not ready")
            return

        self.log("\n   DB: Final designation sweep...")

        try:
            from database import get_connection
            gc = get_connection()
            gone_result = gc.execute("UPDATE coins SET status='gone' WHERE status='free'")
            gc.commit()
            self.log(f"   DB: reset {gone_result.rowcount} coins to 'gone' before re-scan")
        except Exception as ge:
            self.log(f"   DB: pre-sweep reset failed: {ge}")

        for wallet_id, wallet_type in [(self.xch_wallet_id, "xch"), (self.cat_wallet_id, "cat")]:
            if self.tier_enabled:
                expected_target = self.xch_target_coins if wallet_type == "xch" else self.cat_target_coins
                expected_count = expected_target + 1
            else:
                expected_count = 0

            coins = []
            if expected_count > 0:
                # We need all prepared coins selectable. The reserve change coin
                # (the multi_send leftover) is often the last to appear — don't
                # wait 120s for it. As soon as all prepared coins are visible,
                # proceed. A single immediate snapshot is tried first; only poll
                # if we're genuinely short on prepared coins.
                coins = self._get_coins_via_rpc(wallet_id, f"{wallet_type}-sweep", selectable_only=True)
                visible_count = len(coins) if coins else 0
                if visible_count >= expected_count:
                    self.log(f"   DB {wallet_type}: all {visible_count} coins visible immediately")
                elif visible_count >= expected_target:
                    self.log(f"   DB {wallet_type}: {visible_count} coins visible (prepared coins present, reserve lag OK) — proceeding")
                else:
                    # Genuinely short on prepared coins — poll up to 30s
                    for sweep_poll in range(1, 7):
                        time.sleep(5)
                        coins = self._get_coins_via_rpc(wallet_id, f"{wallet_type}-sweep", selectable_only=True)
                        visible_count = len(coins) if coins else 0
                        if visible_count >= expected_target:
                            self.log(f"   DB {wallet_type}: {visible_count} coins visible after {sweep_poll * 5}s — proceeding")
                            break
                        self.log(f"   DB {wallet_type}: {sweep_poll * 5}s - visible {visible_count}/{expected_count}...")
                    else:
                        self.log(f"   DB {wallet_type}: only {visible_count}/{expected_count} coins after 30s — proceeding with what we have")
            else:
                coins = self._get_coins_via_rpc(wallet_id, wallet_type, selectable_only=True)

            if not coins:
                self.log(f"   DB: no {wallet_type} coins from RPC!")
                continue

            first_raw = coins[0].get("coin_id", "")
            first_fixed = self._ensure_0x(first_raw)
            self.log(f"   DB {wallet_type}: raw ID sample: '{first_raw[:30]}...'")
            self.log(f"   DB {wallet_type}: fixed sample:  '{first_fixed[:30]}...'")
            self.log(f"   DB {wallet_type}: {len(coins)} coins to process")

            tier_counts_db = {}
            upsert_ok = 0
            upsert_fail = 0
            desig_ok = 0
            desig_fail = 0

            for coin in coins:
                coin_id = self._ensure_0x(coin.get("coin_id", ""))
                amount = coin.get("amount", 0)
                if not coin_id:
                    continue
                try:
                    result = upsert_coin(coin_id, wallet_type, amount)
                    if result:
                        upsert_ok += 1
                    else:
                        upsert_fail += 1
                        self.log(f"   DB: upsert returned False for {coin_id[:20]}...")
                except Exception as e:
                    upsert_fail += 1
                    self.log(f"   DB: upsert EXCEPTION: {e}")

            assigned_by_tier, unmatched = self._partition_coins_for_designation(coins, wallet_type)

            for tier_name, tier_coins in assigned_by_tier.items():
                for coin in tier_coins:
                    coin_id = self._ensure_0x(coin.get("coin_id", ""))
                    if not coin_id:
                        continue
                    try:
                        result = set_coin_designation(coin_id, "tier_spare", assigned_tier=tier_name)
                        if result:
                            desig_ok += 1
                        else:
                            desig_fail += 1
                            self.log(f"   DB: set_designation returned False for {coin_id[:20]}...")
                    except Exception as e:
                        desig_fail += 1
                        self.log(f"   DB: set_designation EXCEPTION: {e}")
                tier_counts_db[tier_name] = len(tier_coins)

            reserve_candidate = unmatched[0] if unmatched else None
            if reserve_candidate:
                reserve_coin_id = self._ensure_0x(reserve_candidate.get("coin_id", ""))
                reserve_amount = reserve_candidate.get("amount", 0)
                try:
                    result = designate_reserve(reserve_coin_id, wallet_type, reserve_amount)
                    if result:
                        desig_ok += 1
                        _mark_coin_already_advised(reserve_coin_id)
                    else:
                        desig_fail += 1
                        self.log("   DB: reserve designation returned False")
                except Exception as e:
                    desig_fail += 1
                    self.log(f"   DB: reserve designation EXCEPTION: {e}")

            extra_unmatched = unmatched[1:] if reserve_candidate else unmatched
            if extra_unmatched:
                self.log(f"   DB {wallet_type}: {len(extra_unmatched)} unmatched non-reserve coin(s) remain after final sweep")

            summary_parts = [f"{tn}={cnt}" for tn, cnt in sorted(tier_counts_db.items())]
            reserve_str = "reserve=0"
            if reserve_candidate:
                reserve_str = f"reserve=1 ({reserve_candidate.get('amount', 0):,})"
            self.log(f"   DB {wallet_type}: {', '.join(summary_parts)}, {reserve_str}")
            self.log(f"   DB {wallet_type}: upsert={upsert_ok} ok/{upsert_fail} fail, designation={desig_ok} ok/{desig_fail} fail")

            try:
                from database import get_connection
                vconn = get_connection()
                rows = vconn.execute(
                    "SELECT designation, COUNT(*) as cnt FROM coins WHERE wallet_type=? AND status='free' GROUP BY designation",
                    (wallet_type,)
                ).fetchall()
                verify_parts = [f"{dict(r)['designation']}={dict(r)['cnt']}" for r in rows]
                self.log(f"   DB VERIFY {wallet_type}: {', '.join(verify_parts)}")
            except Exception as ve:
                self.log(f"   DB VERIFY {wallet_type}: query failed: {ve}")

        try:
            try:
                from user_paths import data_dir as _dd
                debug_path = os.path.join(_dd(), "designation_debug.json")
            except Exception:
                debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "designation_debug.json")
            from database import get_connection
            dconn = get_connection()
            debug_data = {"timestamp": datetime.now().isoformat()}
            for wt in ["xch", "cat"]:
                rows = dconn.execute(
                    "SELECT designation, COUNT(*) as cnt FROM coins WHERE wallet_type=? AND status='free' GROUP BY designation",
                    (wt,)
                ).fetchall()
                debug_data[wt] = {dict(r)["designation"]: dict(r)["cnt"] for r in rows}
            with open(debug_path, "w") as f:
                json.dump(debug_data, f, indent=2)
            self.log("   DB: debug summary written to designation_debug.json")
        except Exception as de:
            self.log(f"   DB: debug file write failed: {de}")

    def _get_live_price(self) -> Optional[Decimal]:
        """Return the live XCH/CAT mid the bot is trading against.

        Priority:
        1. `_CLI_LIVE_PRICE` env var — set by api_server when launching prep
           via `--live-price`. This is the weighted Tibet+Dexie mid the bot
           itself uses, so prep sizes CAT coins consistent with the live
           ladder. Use this whenever available.
        2. Dexie `last_price` fallback — for historical callers that launch
           the worker directly (CLI, tests) without the `--live-price` arg.
           Note: Dexie's last_price can lag the live mid significantly on
           thin-liquidity markets, which undersizes CAT coins and breaks
           sniper sell creation. Prefer path 1.
        """
        cli_price = os.getenv("_CLI_LIVE_PRICE", "").strip()
        if cli_price:
            try:
                p = Decimal(cli_price)
                if p > 0:
                    return p
            except Exception as e:
                self.log(f"   ⚠️ Invalid _CLI_LIVE_PRICE ({cli_price!r}): {e}")

        try:
            import requests
            cat_asset_id = os.getenv("CAT_ASSET_ID", "")
            if cat_asset_id:
                _dexie_base = os.getenv("DEXIE_API_BASE", "https://api.dexie.space").rstrip("/")
                resp = requests.get(
                    f"{_dexie_base}/v2/prices/tickers?ticker_id={cat_asset_id}_xch",
                    timeout=10
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tickers = data.get("tickers", [])
                    if tickers and tickers[0].get("last_price"):
                        p = Decimal(str(tickers[0]["last_price"]))
                        if p > 0:
                            self.log(
                                f"   ⚠️ Using Dexie last_price {p} for CAT sizing "
                                f"(no live mid passed via --live-price; may lag "
                                f"the bot's weighted mid)"
                            )
                            return p
        except Exception as e:
            self.log(f"   ⚠️ Dexie price fetch failed: {e}")

        return None

    def _derive_cat_coin_size(self, trade_size_xch: Decimal) -> Decimal:
        """Calculate CAT coin size from XCH trade size using current price.

        For sell offers, the bot needs CAT coins big enough to cover
        trade_size_xch worth of CAT at current prices.
        e.g. if trade_size = 0.6 XCH and price = 0.000064 XCH/CAT:
             CAT needed = 0.6 / 0.000064 = 9375 CAT per coin

        We add the configured prep headroom and round up.
        Falls back to CAT_COIN_SIZE or 4000 if price fetch fails.
        """
        price = self._get_live_price()
        if price and price > 0:
            cat_per_offer = trade_size_xch / price
            cat_coin_size = (
                cat_per_offer * self.coin_prep_headroom_multiplier
            ).quantize(Decimal("1"))
            self.log(
                f"   CAT prep coin size: {cat_coin_size} "
                f"(derived: {trade_size_xch} XCH / {price} price × "
                f"{self.coin_prep_headroom_multiplier} headroom)"
            )
            return cat_coin_size

        # Fallback
        fallback = Decimal(os.getenv("CAT_COIN_SIZE", "4000"))
        self.log(f"   CAT coin size: {fallback} (fallback — could not derive from price)")
        return fallback

    def _derive_tier_cat_sizes(self) -> Dict[str, Decimal]:
        """Derive prepared CAT coin sizes from live XCH tier sizes and headroom.

        Each tier needs different-sized CAT coins:
        - Inner (large XCH) → large CAT coins
        - Extreme (small XCH) → small CAT coins

        Falls back to uniform CAT_COIN_SIZE if price fetch fails.
        """
        result = {}
        price = self._get_live_price()

        if price and price > 0:
            # F62 (2026-04-09): CAT coin sizes come from the SELL side live
            # tier sizes (sell offers lock CAT coins). Fall back to the
            # buy/legacy sizes only when the sell-side dict is empty.
            live_sizes = (
                getattr(self, 'offer_tier_xch_sizes_sell', None)
                or self.offer_tier_xch_sizes
                or self.tier_xch_sizes
            )
            for tier_name, xch_size in live_sizes.items():
                if tier_name == get_fee_tier_name():
                    result[tier_name] = Decimal("0")
                    continue
                cat_per_offer = xch_size / price
                cat_coin_size = (
                    cat_per_offer * self.coin_prep_headroom_multiplier
                ).quantize(Decimal("1"))
                result[tier_name] = cat_coin_size
            self.log(
                f"   Tier CAT sizes derived from SELL sizes at price {price} "
                f"with +{self.coin_prep_headroom_pct}% headroom"
            )
        else:
            # Fallback: use uniform CAT_COIN_SIZE for all tiers
            fallback = Decimal(os.getenv("CAT_COIN_SIZE", "4000"))
            for tier_name in self.tier_xch_sizes:
                result[tier_name] = Decimal("0") if tier_name == get_fee_tier_name() else fallback
            self.log(f"   ⚠️ Using fallback CAT size {fallback} for all tiers (no price available)")

        return result

    def _apply_prep_headroom_xch(self, live_size_xch: Decimal) -> Decimal:
        """Inflate a live offer size into the prepared coin size."""
        return (
            live_size_xch * self.coin_prep_headroom_multiplier
        ).quantize(Decimal("0.00000001"))

    def _get_fingerprint(self) -> str:
        """Get wallet fingerprint — tries RPC first (fast), then CLI (slow fallback)."""
        # Sage: use get_current_key() from wallet_sage
        if self.is_sage:
            try:
                from wallet_sage import get_current_key
                key = get_current_key()
                if key and key.get("fingerprint"):
                    fp = str(key["fingerprint"])
                    self.log(f"✅ Got fingerprint from Sage RPC: {fp}")
                    return fp
            except Exception as e:
                self.log(f"⚠️ Sage fingerprint detection failed: {e}")
            self.log("⚠️ Could not detect Sage fingerprint — using placeholder")
            return "0"

        # Chia: Method 1: RPC via wallet.py (fast, ~1 second)
        try:
            from wallet import get_wallets
            result = get_wallets()
            if result and result.get("success"):
                fp = result.get("fingerprint")
                if fp:
                    self.log(f"✅ Got fingerprint from RPC: {fp}")
                    return str(fp)
        except Exception as e:
            self.log(f"⚠️ RPC fingerprint failed: {e}, trying CLI...")

        # Chia: Method 2: CLI fallback (can be slow — 30s timeout)
        try:
            result = subprocess.run(
                ["chia", "wallet", "show"],
                capture_output=True,
                text=True,
                timeout=30,
                **hidden_subprocess_kwargs(),
            )

            for line in result.stdout.split('\n'):
                if 'Fingerprint:' in line:
                    fp = line.split('Fingerprint:')[1].strip().split()[0]
                    self.log(f"✅ Got fingerprint from CLI: {fp}")
                    return fp
        except Exception as e:
            self.log(f"⚠️ CLI 'wallet show' failed: {e}")

        # Method 3: chia keys show
        try:
            result = subprocess.run(
                ["chia", "keys", "show"],
                capture_output=True,
                text=True,
                timeout=30,
                **hidden_subprocess_kwargs(),
            )

            for line in result.stdout.split('\n'):
                if 'Fingerprint:' in line or 'fingerprint:' in line.lower():
                    parts = line.split(':')
                    if len(parts) > 1:
                        fp = parts[1].strip().split()[0]
                        if fp.isdigit():
                            self.log(f"✅ Got fingerprint from keys: {fp}")
                            return fp
        except Exception as e:
            self.log(f"⚠️ CLI 'keys show' failed: {e}")

        raise Exception("Could not determine wallet fingerprint from RPC or CLI")
    
    def log(self, message: str, severity: str = None):
        """Log message with timestamp — prints to console AND pushes to GUI."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}")
        sys.stdout.flush()

        # Auto-detect severity from message content if not specified
        if severity is None:
            if "❌" in message or "ERROR" in message:
                severity = "error"
            elif "⚠️" in message:
                severity = "warning"
            elif "✅" in message or "🎉" in message or "COMPLETE" in message:
                severity = "success"
            else:
                severity = "info"

        self._enqueue_api_log(severity, "coin_prep", message)

    def _enqueue_api_log(self, severity: str, event_type: str, message: str):
        """Queue API log delivery so coin prep never blocks on GUI logging."""
        # Skip entirely when not running as the real subprocess — no consumer
        # thread means items would pile up forever, and in tests there's no
        # api_server that should receive them anyway.
        if not self._is_subprocess:
            return
        payload = {
            "severity": severity,
            "event_type": event_type,
            "message": message,
        }
        try:
            self._api_log_queue.put_nowait(payload)
        except Full:
            self._api_log_drop_count += 1
            if self._api_log_drop_count in {1, 25, 100}:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"⚠️ Coin prep log queue full — dropped {self._api_log_drop_count} GUI log lines",
                    flush=True,
                )

    def _api_log_loop(self):
        """Best-effort background delivery of coin prep logs to the local API."""
        try:
            import requests as _req
            session = _req.Session()
        except Exception:
            session = None

        while True:
            try:
                payload = self._api_log_queue.get(timeout=0.5)
            except Empty:
                continue

            if payload is None:
                self._api_log_queue.task_done()
                break

            if session is not None:
                try:
                    session.post(
                        "http://localhost:5000/api/log",
                        json=payload,
                        headers=_local_api_headers(),
                        timeout=_API_LOG_POST_TIMEOUT_S,
                    )
                except Exception:
                    pass
            self._api_log_queue.task_done()

    def shutdown_api_log(self, timeout: float = 3.0):
        """Flush pending log deliveries before the subprocess exits.

        The log queue is consumed by a daemon thread — daemons die the
        moment sys.exit() fires, which was dropping the final ERROR /
        OVERSHOOT lines before they reached the superlog. The caller is
        left with only the bare '❌ Coin preparation failed!' banner and
        no reason string.

        Call this right before sys.exit() so the queue sentinel gets a
        chance to drain outstanding POSTs.
        """
        if not getattr(self, "_is_subprocess", False):
            return
        worker = getattr(self, "_api_log_worker", None)
        queue = getattr(self, "_api_log_queue", None)
        if queue is None:
            return
        try:
            queue.put_nowait(None)
        except Exception:
            return
        if worker is not None:
            try:
                worker.join(timeout=max(0.1, float(timeout)))
            except Exception:
                pass
    
    def update_status(self, phase: PrepPhase = None, progress: float = None, 
                     message: str = None, error: str = None):
        """Update status and write to file for GUI (thread-safe)"""
        with self.status_lock:
            if phase:
                self.status.phase = phase.value
            if progress is not None:
                self.status.progress = progress
            if message:
                self.status.message = message
            if error:
                self.status.error = error
            
            self.status.timestamp = time.time()
            
            # Write to file for GUI to read
            try:
                with open(self.status_file, 'w') as f:
                    json.dump(self.status.to_dict(), f, indent=2)
            except Exception as e:
                self.log(f"⚠️ Could not write status file: {e}")
    
    _last_sync_warning = 0  # Cooldown for sync warnings
    _sync_lost_since = None  # When sync was first lost
    
    def check_wallet_sync(self, context: str = ""):
        """Check wallet sync status during long waits. Logs warnings if sync lost."""
        try:
            status = get_wallet_sync_status()
            now = time.time()
            
            if status["reachable"] and status["synced"]:
                # Recovered
                if self._sync_lost_since:
                    lost_for = int(now - self._sync_lost_since)
                    self.log(f"✅ Wallet sync recovered (was down {lost_for}s)")
                    self._sync_lost_since = None
                return True
            
            # Sync lost or wallet unreachable
            if self._sync_lost_since is None:
                self._sync_lost_since = now
            
            lost_for = int(now - self._sync_lost_since)
            
            # Warn periodically (first at 15s, then every 30s)
            if (now - self._last_sync_warning) >= 30 or (lost_for >= 15 and self._last_sync_warning == 0):
                self._last_sync_warning = now
                if not status["reachable"]:
                    self.log(f"⚠️ Wallet RPC unreachable{' during ' + context if context else ''} ({lost_for}s) — waiting for it to come back...")
                elif status["syncing"]:
                    self.log(f"⚠️ Wallet syncing{' during ' + context if context else ''} ({lost_for}s) — waiting...")
                else:
                    self.log(f"⚠️ Wallet not synced{' during ' + context if context else ''} ({lost_for}s) — waiting...")
            
            return False
        except Exception:
            return True  # Don't block on check errors
    
    def run_command(self, cmd: List[str], timeout: int = 30) -> Tuple[bool, str]:
        """Run CLI command and return (success, output)"""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                **hidden_subprocess_kwargs(),
            )
            output = result.stdout + result.stderr
            success = result.returncode == 0
            return success, output
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)
    
    def get_coin_count(self, wallet_id: int) -> int:
        """Get number of spendable coins in wallet.

        Uses Sage's documented spendable-count endpoints when available, with
        generic RPC / CLI fallbacks for other wallet backends.
        Only counts spendable (unlocked) coins — which is what we care
        about for coin prep.
        """
        if self.is_sage:
            try:
                from wallet_sage import get_selectable_coins_only, get_spendable_coin_count

                count = get_spendable_coin_count(wallet_id)
                if count > 0:
                    return count

                result = get_selectable_coins_only(wallet_id)
                if result and result.get("success"):
                    records = result.get("confirmed_records") or result.get("records") or []
                    return sum(
                        1 for rec in records
                        if rec.get("coin", {}).get("amount", 0) > 0
                    )
            except Exception as e:
                self.log(f"   ⚠️ Sage spendable count fallback error: {e}")

        # --- Strategy 1: Generic wallet RPC fallback ---
        try:
            result = get_spendable_coins_rpc(wallet_id)
            if result and result.get("success"):
                records = result.get("confirmed_records") or result.get("records") or []
                count = sum(1 for rec in records
                           if rec.get("coin", {}).get("amount", 0) > 0)
                return count
        except Exception:
            pass

        # --- Strategy 2: CLI fallback ---
        cmd = [
            "chia", "wallet", "coins", "list",
            "-f", self.fingerprint,
            "-i", str(wallet_id),
            "--no-paginate"
        ]

        success, output = self.run_command(cmd)
        if not success:
            return 0

        # Count "Coin ID:" lines
        count = output.count("Coin ID:")
        return count
    
    def get_confirmed_coin_count(self, wallet_id: int) -> int:
        """Get the true spendable/confirmed coin count for confirmation gates."""
        return self.get_coin_count(wallet_id)

    def get_balance(self, wallet_id: int) -> Decimal:
        """Get wallet balance — uses RPC for Sage, CLI for Chia."""
        # Sage: use wallet RPC directly
        if self.is_sage:
            try:
                from wallet_sage import get_wallet_balance
                result = get_wallet_balance(wallet_id)
                if result and result.get("success"):
                    wb = result.get("wallet_balance", {})
                    mojos = wb.get("spendable_balance", wb.get("confirmed_wallet_balance", 0))
                    # Convert mojos to display units
                    is_cat = (wallet_id != self.xch_wallet_id)
                    if is_cat:
                        return Decimal(str(mojos)) / (10 ** self.cat_decimals)
                    else:
                        return Decimal(str(mojos)) / Decimal("1000000000000")
            except Exception as e:
                self.log(f"⚠️ Sage get_balance failed: {e}")
            return Decimal("0")

        # Chia: parse CLI output
        cmd = [
            "chia", "wallet", "show",
            "-f", self.fingerprint
        ]

        success, output = self.run_command(cmd)
        if not success:
            return Decimal("0")

        # Parse multi-wallet output to find our specific wallet
        # Format:
        # MZ:
        #    -Total Balance:         387116.287  (387116287 mojo)
        #    ...
        #    -Wallet ID:             5

        lines = output.split('\n')
        current_balance = None

        for line in lines:
            line_stripped = line.strip()

            # Check if we found our wallet ID
            if '-Wallet ID:' in line_stripped:
                # Extract wallet ID from line like "   -Wallet ID:             5"
                try:
                    found_id = int(line_stripped.split(':')[1].strip())
                    if found_id == wallet_id and current_balance is not None:
                        # We found our wallet and already captured its balance
                        return current_balance
                except (ValueError, IndexError, AttributeError):
                    pass
                # Reset for next wallet
                current_balance = None

            # Look for Total Balance in any section
            if '-Total Balance:' in line_stripped or '-Spendable:' in line_stripped:
                try:
                    # Extract number from line like "   -Total Balance:         387116.287  (387116287 mojo)"
                    parts = line_stripped.split(':')
                    if len(parts) > 1:
                        value_part = parts[1].strip()
                        # Get first token (the number before units or mojo)
                        amount_str = value_part.split()[0]
                        current_balance = Decimal(amount_str)
                except (ValueError, IndexError, AttributeError, InvalidOperation):
                    pass
        
        return Decimal("0")
    

    def get_open_offers(self):
        """Get list of open offer IDs and their status"""
        try:
            cmd = ["chia", "wallet", "get_offers", "-f", self.fingerprint]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                **hidden_subprocess_kwargs(),
            )
            
            if result.returncode != 0:
                return []
            
            output = result.stdout
            lines = output.split('\n')
            
            offers = []
            current_id = None
            
            for i, line in enumerate(lines):
                if "Record with id:" in line:
                    current_id = line.split("Record with id:")[1].strip()
                elif "Status:" in line and current_id:
                    status = line.split("Status:")[1].strip()
                    offers.append({
                        "id": current_id,
                        "status": status
                    })
                    current_id = None
            
            return offers
        except Exception as e:
            self.log(f"❌ Error getting offers: {e}")
            return []
    




    def get_all_open_offers_rpc(self):
        """Get all open offers using wallet RPC.

        Sage uses 'active' status, Chia uses 'PENDING_ACCEPT' / 'PENDING_CONFIRM'.
        We accept both to be wallet-agnostic.
        Also fetch ALL offers (end=500) — V1 bug was truncating at 50.
        """
        try:
            # Use wallet.py function that uses RPC
            offers_list = get_all_offers(include_completed=False, start=0, end=500)

            if not offers_list:
                return []

            # Filter for open/pending offers (Sage='active', Chia='PENDING*')
            open_offers = []
            for offer in offers_list:
                status = str(offer.get("status", "")).upper()
                if "PENDING" in status or status == "ACTIVE":
                    trade_id = offer.get("trade_id") or offer.get("offer_id")
                    if trade_id:
                        open_offers.append({
                            "id": trade_id,
                            "status": status
                        })

            self.log(f"  Wallet RPC returned {len(offers_list)} offers, {len(open_offers)} are open/active")
            return open_offers
        except Exception as e:
            self.log(f"❌ Error getting offers via RPC: {e}")
            return []
    
    def cancel_all_offers(self):
        """Cancel all open offers using Sage's bulk cancel RPC.

        Uses cancel_offers_batch() from wallet_sage.py which sends a single
        cancel_offers RPC call with all offer IDs at once. Sage processes this
        as one transaction — much faster than cancelling one-by-one.
        Falls back to sequential cancel if bulk RPC fails.

        Confirmation polling verifies all offers are actually gone before
        returning. Coin prep needs the full wallet coin set unlocked; if any
        offer remains open after retries, prep fails cleanly instead of
        reshaping a partial wallet view.
        """
        try:
            self.log(f"\n{'='*60}")
            self.log("CANCELLING OPEN OFFERS (sequential -- wallet-safe)")
            self.log(f"{'='*60}")
            
            # Get all open offers
            open_offers = self.get_all_open_offers_rpc()
            
            if not open_offers:
                self.log("No open offers found")
                return True
            
            initial_count = len(open_offers)
            self.log(f"Found {initial_count} open offers")
            
            trade_ids = [o["id"] for o in open_offers]
            
            if not trade_ids:
                self.log("  All offers are protected — skipping cancellation")
                return True
            
            cancel_count = len(trade_ids)
            
            # Write IDs we're about to cancel to file, so bot can avoid false fill detection.
            # Lives under the user data dir so it's writable alongside bot.db.
            try:
                try:
                    from user_paths import worker_cancelled_ids_file
                    worker_cancelled_file = worker_cancelled_ids_file()
                except Exception:
                    worker_cancelled_file = "worker_cancelled_ids.json"
                with open(worker_cancelled_file, "w", encoding="utf-8") as f:
                    json.dump({"cancelled_ids": trade_ids, "timestamp": time.time()}, f)
            except Exception:
                pass  # Non-critical
            
            # Use Sage's bulk cancel RPC — one call to cancel ALL offers at once.
            # cancel_offers_batch() in wallet_sage.py handles:
            #   1. Bulk RPC call (cancel_offers with array of IDs)
            #   2. Sequential fallback if bulk fails
            #   3. Confirmation polling to verify offers are gone
            # This replaces the old batch-of-5 approach (~7min for 80 offers)
            # with a single RPC call + confirmation (~30-45s total).
            self.log(f"\n⚡ Using bulk cancel RPC for {cancel_count} offers...")
            self.update_status(
                PrepPhase.CONSOLIDATING,
                0.02,
                f"Bulk cancelling {cancel_count} offers..."
            )

            results = cancel_offers_batch(trade_ids, secure=True)

            # Count successes and failures from the batch result
            cancelled = 0
            failed_ids = []
            for tid in trade_ids:
                res = results.get(tid, {})
                if res and res.get("success"):
                    cancelled += 1
                else:
                    failed_ids.append(tid)

            self.log(f"\nBulk cancel result: {cancelled} succeeded, {len(failed_ids)} failed")

            # RETRY failed cancels individually (transient errors)
            permanently_failed = set()
            if failed_ids:
                max_retries = 3
                for attempt in range(1, max_retries + 1):
                    if not failed_ids:
                        break
                    wait_secs = 10 * attempt
                    self.log(f"\nRetrying {len(failed_ids)} failed cancels (attempt {attempt}/{max_retries}, waiting {wait_secs}s)...")
                    time.sleep(wait_secs)

                    still_failing = []
                    for tid in failed_ids:
                        try:
                            res = rpc_cancel_offer(tid, secure=True, timeout=120)
                            if res and res.get("success"):
                                cancelled += 1
                                self.log(f"   Retry OK: {tid[:16]}...")
                            else:
                                error = (res or {}).get("error", "Unknown")
                                still_failing.append(tid)
                                self.log(f"   Retry failed: {tid[:16]}... - {error}")
                        except Exception as e:
                            still_failing.append(tid)
                            self.log(f"   Retry error: {tid[:16]}... - {e}")
                        time.sleep(2)  # Brief pause between individual retries
                    failed_ids = still_failing

                # Any still failing after all retries are permanently failed
                permanently_failed = set(failed_ids)
                if permanently_failed:
                    self.log(f"\n{len(permanently_failed)} offers could not be cancelled after {max_retries} retries -- skipping them")
            
            if cancelled == 0:
                self.log("No offers were cancelled")
                return False
            
            # Build list of IDs we expect to be cancelled (exclude permanently failed)
            expected_cancelled_ids = [tid for tid in trade_ids if tid not in permanently_failed]
            
            if not expected_cancelled_ids:
                self.log("No offers to verify")
                return True
            
            # VERIFICATION LOOP — poll RPC until expected offers are gone.
            #
            # F62 (2026-04-09): aggressive straggler re-cancel.
            #
            # The old flow polled for 300s and then gave up — if any offers
            # were still active on-chain, it just logged TIMEOUT and moved
            # on, leaving their backing coins locked. On a busy Chia mempool
            # this left 4-10 offers stuck per run.
            #
            # New flow: poll for a short window (60s), and if any stragglers
            # remain, re-submit cancel RPCs for them and poll again. Loop up
            # to 3 rounds, total ~3 minutes max. This both speeds up the
            # common case (cancels typically confirm in 1-2 blocks) and
            # dramatically reduces the straggler count in the pathological
            # case (re-submission lands most in the next block).
            pf_note = f", skipping {len(permanently_failed)} uncancellable" if permanently_failed else ""
            self.log(f"\nVERIFYING CANCELLATIONS... (checking {len(expected_cancelled_ids)} offers{pf_note})")

            per_round_wait = 60   # 60s per round (covers ~1-3 blocks)
            max_rounds = 3        # total max wait ~ 3 min (vs old 5 min)
            check_interval = 5
            round_num = 1
            still_open: list = list(expected_cancelled_ids)

            while round_num <= max_rounds and still_open:
                round_start = time.time()
                elapsed = 0
                while elapsed < per_round_wait:
                    current_offers = self.get_all_open_offers_rpc()
                    current_ids = [o["id"] for o in current_offers]
                    still_open = [tid for tid in expected_cancelled_ids if tid in current_ids]
                    if not still_open:
                        total_elapsed = int(time.time() - round_start)
                        self.log(f"   ALL OFFERS CANCELLED! (round {round_num}, {total_elapsed}s)")
                        if permanently_failed:
                            self.log(f"   Note: {len(permanently_failed)} uncancellable offers remain open")
                        return True
                    remaining = len(still_open)
                    cancelled_so_far = len(expected_cancelled_ids) - remaining
                    self.log(f"   Round {round_num}/{max_rounds}: {cancelled_so_far}/{len(expected_cancelled_ids)} cancelled, {remaining} remaining ({elapsed}s)")
                    time.sleep(check_interval)
                    elapsed += check_interval

                # Round timed out with stragglers remaining — re-submit cancels
                if still_open and round_num < max_rounds:
                    self.log(f"\nRound {round_num} timed out with {len(still_open)} stragglers — re-submitting cancels...")
                    try:
                        re_results = cancel_offers_batch(still_open, secure=True)
                        re_ok = sum(1 for tid in still_open if (re_results.get(tid, {}) or {}).get("success"))
                        self.log(f"   Re-submit result: {re_ok}/{len(still_open)} accepted")
                    except Exception as _re_err:
                        self.log(f"   Re-submit failed: {_re_err}")
                        # Fall back to individual re-cancels
                        try:
                            _ok = 0
                            for _tid in still_open:
                                try:
                                    _r = rpc_cancel_offer(_tid, secure=True, timeout=60)
                                    if _r and _r.get("success"):
                                        _ok += 1
                                except Exception:
                                    pass
                                time.sleep(0.3)
                            self.log(f"   Individual re-submit: {_ok}/{len(still_open)} accepted")
                        except Exception:
                            pass
                round_num += 1

            # All rounds exhausted — log final state and proceed
            if still_open:
                self.log(f"\nTIMEOUT: {len(still_open)} offers still open after {max_rounds} rounds")
                self.log("Aborting coin prep — all offers must cancel before reshaping coins")
                return False
            else:
                self.log("\nAll cancellable offers confirmed!")

            return True
            
        except Exception as e:
            self.log(f"Cancellation error: {e}")
            import traceback
            self.log(f"   {traceback.format_exc()}")
            return False

    def consolidate_wallet(self, wallet_id: int, name: str) -> bool:
        """Consolidate all coins in wallet to single coin"""
        self.log(f"🔄 Consolidating {name} wallet...")

        if self.is_sage:
            return self._consolidate_wallet_sage(wallet_id, name)

        # --- Chia CLI path ---
        # Get current address
        cmd = [
            "chia", "wallet", "get_address",
            "-f", self.fingerprint,
            "-i", str(wallet_id)
        ]

        success, output = self.run_command(cmd)
        if not success:
            self.log("❌ Could not get address")
            return False

        # Parse address
        address = None
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('xch') or line.startswith('txch'):
                address = line
                break

        if not address:
            self.log("❌ Could not find address")
            return False

        self.log(f"Target address: {address[:20]}...")

        # Get balance
        balance = self.get_balance(wallet_id)
        self.log(f"Current balance: {balance}")

        # Skip if balance is 0 (coins may still be locked in pending cancels)
        if balance is None or balance == 0:
            self.log(f"⚠️ {name} balance is 0 — coins may still be locked in pending transactions. Skipping consolidation.")
            return False

        # Send entire balance to self (consolidates coins)
        cmd = [
            "chia", "wallet", "send",
            "-f", self.fingerprint,
            "-i", str(wallet_id),
            "-a", str(balance),  # Keep full Decimal precision
            "-t", address,
            "-m", "0"  # No fee
        ]

        self.log("Submitting consolidation transaction...")

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **hidden_subprocess_kwargs(),
            )

            stdout, stderr = process.communicate(input="y\n", timeout=120)
            output = stdout + stderr

            if "submitted to" in output.lower() or "transaction sent" in output.lower():
                self.log("✅ Consolidation submitted")
                return True
            else:
                self.log(f"❌ Consolidation failed: {output[:200]}")
                return False

        except Exception as e:
            self.log(f"❌ Consolidation error: {e}")
            return False

    def _priority_combine_fee_mojos(self, coin_count: int) -> int:
        """Return the tx fee (in mojos) to attach to a consolidation combine.

        F61 (2026-04-09): large combines tend to land several Chia blocks
        late because the tx is big (lots of input coins) and competes for
        block space. Attach a slightly higher fee to push it toward the
        front of the mempool queue. Scaling:
          • < 20 coins  → base tx fee (no bump)
          • 20-49 coins → 2× base
          • 50-99 coins → 4× base
          • ≥ 100 coins → 6× base

        The multipliers cap at 6× so even a 500-coin combine only pays a
        very small absolute amount (base fee is the user's
        TRANSACTION_FEE_XCH which defaults to ~0.0000131 XCH).
        """
        base = max(0, int(self._tx_fee_mojos()))
        if coin_count >= 100:
            return base * 6
        if coin_count >= 50:
            return base * 4
        if coin_count >= 20:
            return base * 2
        return base

    def _consolidate_wallet_sage(self, wallet_id: int, name: str) -> bool:
        """Consolidate coins via Sage's native endpoints.

        Strategy (in order of preference):
        1. auto_combine_xch / auto_combine_cat — Sage auto-selects optimal coins
        2. /combine — manual combine with explicit coin IDs (fallback)
        3. send-to-self — last resort (original Chia workaround)
        """
        try:
            from wallet_sage import auto_combine_xch, auto_combine_cat

            is_cat = (wallet_id != self.xch_wallet_id)

            # Log current coin count for context
            coin_count = self.get_coin_count(wallet_id)
            self.log(f"Current {name} coins: {coin_count}")

            if coin_count <= 1:
                self.log(f"✅ {name} already consolidated ({coin_count} coin)")
                return True

            # F61: scale the fee up for large combines to get faster block
            # inclusion. Without this bump, a 71-coin XCH combine can sit
            # in the mempool for 5+ blocks (~2m 30s) while smaller txes
            # land in the first block.
            combine_fee = self._priority_combine_fee_mojos(coin_count)
            base_fee = self._tx_fee_mojos()
            if combine_fee > base_fee:
                fee_multiplier = combine_fee // max(1, base_fee)
                self.log(f"Submitting auto-combine via Sage RPC "
                         f"(max_coins={coin_count}, fee={combine_fee:,} mojos, "
                         f"{fee_multiplier}× base for priority block inclusion)...")
            else:
                self.log(f"Submitting auto-combine via Sage RPC (max_coins={coin_count})...")

            if is_cat:
                result = auto_combine_cat(fee_mojos=combine_fee, max_coins=coin_count)
            else:
                result = auto_combine_xch(fee_mojos=combine_fee, max_coins=coin_count)

            if self._sage_submit_succeeded(result):
                self.log(f"✅ {name} auto-combine submitted via Sage (was {coin_count} coins)")
                return True
            else:
                self.log("⚠️ Sage auto-combine returned None — trying /combine endpoint...")
                return self._consolidate_wallet_sage_combine(wallet_id, name)

        except Exception as e:
            self.log(f"⚠️ Sage auto-combine error: {e} — trying /combine endpoint...")
            return self._consolidate_wallet_sage_combine(wallet_id, name)

    def _consolidate_wallet_sage_combine(self, wallet_id: int, name: str) -> bool:
        """Consolidate via Sage's /combine endpoint with explicit coin IDs.

        Source: sage-api struct Combine { coin_ids, fee, auto_submit }
        The /combine endpoint is generic — works for both XCH and CAT coins.
        """
        try:
            from wallet_sage import combine_coins, get_spendable_coins_rpc

            # Get all spendable coins to extract their IDs
            coins_result = get_spendable_coins_rpc(wallet_id)
            if not coins_result or not coins_result.get("success"):
                self.log("❌ Could not get coins for /combine — falling back to send-to-self")
                return self._consolidate_wallet_sage_fallback(wallet_id, name)

            records = coins_result.get("confirmed_records", [])
            # Filter unspent
            unspent = [r for r in records if r.get("spent_block_index", 0) == 0]

            if len(unspent) <= 1:
                self.log(f"✅ {name} already consolidated ({len(unspent)} coin)")
                return True

            # Extract coin IDs — cap at 500 per combine (Sage max)
            coin_ids = []
            for r in unspent[:500]:
                cid = r.get("coin_id", "")
                if not cid and r.get("coin"):
                    # Fallback: some formats have coin_id at top level
                    cid = r["coin"].get("coin_id", "")
                if cid:
                    coin_ids.append(cid)

            if len(coin_ids) < 2:
                self.log("❌ Not enough coin IDs found for /combine")
                return self._consolidate_wallet_sage_fallback(wallet_id, name)

            # F61: scaled priority fee for large combines (same as the
            # auto-combine path above) so the /combine fallback also
            # benefits from faster block inclusion.
            combine_fee = self._priority_combine_fee_mojos(len(coin_ids))
            self.log(f"Combining {len(coin_ids)} {name} coins via /combine "
                     f"(fee={combine_fee:,} mojos)...")
            result = combine_coins(coin_ids=coin_ids, fee_mojos=combine_fee)

            if self._sage_submit_succeeded(result):
                self.log(f"✅ {name} /combine submitted (was {len(coin_ids)} coins)")
                return True
            else:
                self.log("❌ /combine returned None — falling back to send-to-self")
                return self._consolidate_wallet_sage_fallback(wallet_id, name)

        except Exception as e:
            self.log(f"⚠️ /combine error: {e} — falling back to send-to-self")
            return self._consolidate_wallet_sage_fallback(wallet_id, name)

    def _consolidate_wallet_sage_fallback(self, wallet_id: int, name: str) -> bool:
        """Fallback consolidation: send entire balance to self.
        Used if Sage's auto_combine fails (e.g. older Sage version).
        """
        try:
            from wallet_sage import get_next_address, send_transaction, get_wallet_balance

            # Get receive address
            addr_result = get_next_address(wallet_id, new_address=False)
            if not addr_result or not addr_result.get("address"):
                self.log("❌ Could not get Sage address for fallback")
                return False
            address = addr_result["address"]

            # Get balance in mojos
            bal_result = get_wallet_balance(wallet_id)
            if not bal_result or not bal_result.get("success"):
                self.log("❌ Could not get Sage balance for fallback")
                return False
            wb = bal_result.get("wallet_balance", {})
            mojos = wb.get("spendable_balance", wb.get("confirmed_wallet_balance", 0))

            if mojos <= 0:
                self.log(f"⚠️ {name} balance is 0 — coins may still be locked. Skipping.")
                return False

            self.log("Submitting send-to-self consolidation...")
            result = send_transaction(wallet_id, mojos, address, fee_mojos=self._tx_fee_mojos())

            if self._sage_submit_succeeded(result):
                self.log("✅ Fallback consolidation submitted via Sage RPC")
                return True
            else:
                self.log("❌ Sage fallback consolidation returned None")
                return False

        except Exception as e:
            self.log(f"❌ Sage fallback consolidation error: {e}")
            return False
    
    def create_trading_pool(self, wallet_id: int, name: str, amount: Decimal) -> bool:
        """
        Create trading pool by sending exact amount to self
        This creates 2 coins: pool (exact amount) + reserve (remainder)
        """
        self.log(f"💰 Creating {name} trading pool of {amount}...")

        if self.is_sage:
            return self._create_trading_pool_sage(wallet_id, name, amount)

        # --- Chia CLI path ---
        # Get address
        cmd = [
            "chia", "wallet", "get_address",
            "-f", self.fingerprint,
            "-i", str(wallet_id)
        ]

        success, output = self.run_command(cmd)
        if not success:
            self.log("❌ Could not get address")
            return False

        # Parse address
        address = None
        for line in output.split('\n'):
            line = line.strip()
            if line.startswith('xch') or line.startswith('txch'):
                address = line
                break

        if not address:
            self.log("❌ Could not find address")
            return False

        # Send exact amount to self
        cmd = [
            "chia", "wallet", "send",
            "-f", self.fingerprint,
            "-i", str(wallet_id),
            "-a", str(float(amount)),
            "-t", address,
            "-m", "0"
        ]

        self.log("Submitting pool creation transaction...")

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **hidden_subprocess_kwargs(),
            )

            stdout, stderr = process.communicate(input="y\n", timeout=120)
            output = stdout + stderr

            if "submitted to" in output.lower() or "transaction sent" in output.lower():
                self.log(f"✅ {name} pool creation submitted")
                return True
            else:
                self.log(f"❌ Pool creation failed: {output[:200]}")
                return False

        except Exception as e:
            self.log(f"❌ Pool creation error: {e}")
            return False

    def _create_trading_pool_sage(self, wallet_id: int, name: str, amount: Decimal) -> bool:
        """Create trading pool via Sage RPC — send exact amount to self."""
        try:
            from wallet_sage import get_next_address, send_transaction

            # Get receive address
            addr_result = get_next_address(wallet_id, new_address=False)
            if not addr_result or not addr_result.get("address"):
                self.log("❌ Could not get Sage address for pool creation")
                return False
            address = addr_result["address"]

            # Convert display amount to mojos
            is_cat = (wallet_id != self.xch_wallet_id)
            if is_cat:
                mojos = int(amount * (10 ** self.cat_decimals))
            else:
                mojos = int(amount * Decimal("1000000000000"))

            self.log(f"Submitting pool creation transaction ({amount} {name} = {mojos} mojos)...")
            result = send_transaction(wallet_id, mojos, address, fee_mojos=self._tx_fee_mojos())

            if self._sage_submit_succeeded(result):
                self.log(f"✅ {name} pool creation submitted via Sage RPC")
                return True
            else:
                self.log("❌ Sage pool creation returned None")
                return False

        except Exception as e:
            self.log(f"❌ Sage pool creation error: {e}")
            return False

    # ========================================================================
    # SAGE TIERED POOL CREATION + SPLITTING (combined flow)
    # ========================================================================

    def _snapshot_coin_ids(self, wallet_id: int, name: str) -> dict:
        """
        Snapshot all current coins for a wallet. Returns {coin_id: amount_mojos}.
        Used for before/after diffing to identify exactly which coins were created.
        """
        coins = self._get_coins_via_rpc(wallet_id, name)
        if not coins:
            return {}
        return {c.get("coin_id", ""): c.get("amount", 0) for c in coins if c.get("coin_id")}

    def _diff_coin_snapshots(self, before: dict, after: dict) -> list:
        """
        Find NEW coins that appeared between snapshots.
        Returns list of {"coin_id": ..., "amount": ...} for coins in after but not before.
        """
        new_ids = set(after.keys()) - set(before.keys())
        return [{"coin_id": cid, "amount": after[cid]} for cid in new_ids]

    def _identify_tier_coins(self, new_coins: list, tier_info: list, is_cat: bool = False) -> dict:
        """
        Match new coins to tiers by amount. Each tier expects a coin with an exact
        amount of count × size_per_coin in mojos.

        HANDLES DUPLICATE AMOUNTS: When two tiers have the same total pool size
        (e.g. inner 7×4=28 XCH and mid 14×2=28 XCH), we need to match multiple
        coins with the same amount to different tiers. Uses a list per amount
        so each coin gets assigned to the next unmatched tier that needs it.

        Returns {tier_name: coin_id} mapping.
        Logs warnings if a tier can't find an exact match.
        """
        # Build expected amount → list of tier names (handles duplicates!)
        # e.g. {28000000000000: ["inner", "mid"]} when both tiers total 28 XCH
        expected = {}  # {amount_mojos: [tier_name, ...]}
        for tier_name, count, xch_mojos, cat_mojos, xch_size, cat_size in tier_info:
            target_mojos = cat_mojos if is_cat else xch_mojos
            if target_mojos not in expected:
                expected[target_mojos] = []
            expected[target_mojos].append(tier_name)

        # Match new coins to tiers
        tier_coin_map = {}  # {tier_name: coin_id}
        used_coins = set()

        # Track which tier names have been matched per amount
        matched_per_amount = {}  # {amount_mojos: index into expected[amount]}

        for coin in new_coins:
            amount = coin["amount"]
            if amount not in expected:
                continue
            if coin["coin_id"] in used_coins:
                continue

            # Find the next unmatched tier for this amount
            idx = matched_per_amount.get(amount, 0)
            tier_list = expected[amount]
            if idx >= len(tier_list):
                continue  # All tiers for this amount already matched

            tier_name = tier_list[idx]
            tier_coin_map[tier_name] = coin["coin_id"]
            used_coins.add(coin["coin_id"])
            matched_per_amount[amount] = idx + 1

        # Log results
        asset = "CAT" if is_cat else "XCH"
        for tier_name, count, xch_mojos, cat_mojos, xch_size, cat_size in tier_info:
            target_mojos = cat_mojos if is_cat else xch_mojos
            if tier_name in tier_coin_map:
                cid = tier_coin_map[tier_name].replace("0x", "")
                self.log(f"   ✅ {asset} {tier_name}: coin {cid[:16]}... = {target_mojos:,} mojos")
            else:
                self.log(f"   ❌ {asset} {tier_name}: no coin found for {target_mojos:,} mojos!")

        return tier_coin_map

    def create_and_split_tier_pools_sage(self, xch_pool_amount: Decimal, cat_pool_amount: Decimal) -> bool:
        """
        Sage-specific tiered coin prep — SEQUENTIAL approach.

        After consolidation leaves 1 big coin per side, for each tier (largest first):
          1. Send tier_total to self → creates pool coin of exact size + change coin
          2. Wait for on-chain confirmation
          3. Split pool coin into N equal pieces (Sage divides equally = correct size)
          4. Wait for on-chain confirmation
          5. Change coin carries forward to next tier

        WHY SEQUENTIAL: Sage confirmations can lag behind submission. The old
        parallel approach (multi_send all pools at once) could try to split
        coins before they were fully spendable → UNKNOWN_UNSPENT errors.
        Sequential ensures every coin is confirmed before we use it.

        NO DB DESIGNATION: The bot identifies tiers by coin size at runtime.
        Inner=1.6 XCH, mid=0.8, outer=0.4, extreme=0.16 — sizes are unique per tier.
        """
        from wallet_sage import (get_next_address, send_transaction,
                                 send_transaction_multi, send_cat_multi,
                                 split_coins_rpc, get_pending_transactions)

        self.log(f"\n{'='*60}")
        self.log("⚡ SAGE MULTI-SEND TIERED SPLITTING")
        self.log("   (multi_send ALL pools → confirm → split each → confirm)")
        self.log("   Eliminates tier coin consumption via atomic pool creation")
        self.log(f"{'='*60}")

        # Sort tiers largest-first (biggest coins first, change feeds smaller tiers)
        tier_order = sorted(
            self.tier_xch_sizes.keys(),
            key=lambda t: self.tier_xch_sizes[t],
            reverse=True
        )

        # Get receive address for send-to-self operations
        addr_result = get_next_address(self.xch_wallet_id, new_address=False)
        if not addr_result or not addr_result.get("address"):
            self.log("❌ Could not get Sage address")
            return False
        address = addr_result["address"]
        self.log(f"   Receive address: {address[:20]}...")

        # Calculate per-tier amounts. XCH and CAT counts can differ (asymmetric
        # buy/sell ladders) so we build per-side tuples independently.
        tier_info = []  # legacy combined view (only used for the empty-check below)
        xch_tier_info = []
        cat_tier_info = []
        for tier_name in tier_order:
            xch_count = int(self.xch_tier_counts.get(tier_name, 0) or 0)
            cat_count = int(self.cat_tier_counts.get(tier_name, 0) or 0)
            xch_size = self.tier_xch_sizes.get(tier_name, Decimal("0"))
            cat_size = self.tier_cat_sizes.get(tier_name, Decimal("0"))
            if xch_count <= 0 and cat_count <= 0:
                continue
            xch_mojos_each = int(xch_size * Decimal("1000000000000"))
            cat_mojos_each = int(cat_size * (10 ** self.cat_decimals))
            xch_mojos_total = xch_mojos_each * xch_count
            cat_mojos_total = cat_mojos_each * cat_count
            tier_info.append((tier_name, max(xch_count, cat_count), xch_mojos_total, cat_mojos_total, xch_size, cat_size))
            self.log(f"   {tier_name}: XCH={xch_count} × {xch_size} ({xch_mojos_total:,} mojos) / "
                     f"CAT={cat_count} × {cat_size:,.0f} ({cat_mojos_total:,} mojos)")
            if xch_count > 0 and xch_mojos_total > 0:
                xch_tier_info.append((tier_name, xch_count, xch_mojos_total, 0, xch_size, Decimal("0")))
            if cat_count > 0 and cat_mojos_total > 0:
                cat_tier_info.append((tier_name, cat_count, 0, cat_mojos_total, Decimal("0"), cat_size))

        if not tier_info:
            self.log("❌ No tiers configured!")
            return False

        # Total steps = all side-specific tier operations × 2 (pool + split)
        total_steps = (len(xch_tier_info) + len(cat_tier_info)) * 2
        step_done = 0
        xch_pool_coin_map = {}
        cat_pool_coin_map = {}

        def _assign_pool_coins_to_tiers(all_coins, tier_details):
            """Assign unique confirmed pool coins to tiers, even for duplicate totals."""
            candidates_by_amount = {}
            for coin in all_coins or []:
                amount = coin.get("amount", 0)
                coin_id = coin.get("coin_id", "").replace("0x", "").lower()
                if amount <= 0 or not coin_id:
                    continue
                candidates_by_amount.setdefault(amount, []).append(coin)

            for matches in candidates_by_amount.values():
                matches.sort(key=lambda c: c.get("coin_id", ""))

            assigned = {}
            used_ids = set()
            for tier_name, _count, pool_mojos in tier_details:
                selected = None
                for candidate in candidates_by_amount.get(pool_mojos, []):
                    candidate_id = candidate.get("coin_id", "").replace("0x", "").lower()
                    if candidate_id and candidate_id not in used_ids:
                        selected = candidate
                        used_ids.add(candidate_id)
                        break
                if selected is None:
                    return None
                assigned[tier_name] = selected

            return assigned

        # Helper: find a coin by exact amount from wallet
        def _find_coin_by_amount(wallet_id, target_mojos, label=""):
            """Find a specific coin by its exact mojo amount.

            Tries two sources:
            1. _get_coins_via_rpc (standard spendable query path)
            2. get_selectable_coins_only (direct strict selectable query)
            """
            # Source 1: standard spendable query path
            coins = self._get_coins_via_rpc(wallet_id, label)
            if coins:
                for c in coins:
                    if c.get("amount", 0) == target_mojos:
                        return c

            # Source 2: direct strict selectable query
            try:
                from wallet_sage import get_selectable_coins_only
                sel_result = get_selectable_coins_only(wallet_id)
                if sel_result and isinstance(sel_result, dict):
                    records = (sel_result.get("confirmed_records")
                               or sel_result.get("records") or [])
                    for rec in records:
                        coin = rec.get("coin", {})
                        amount = coin.get("amount", 0)
                        if amount == target_mojos:
                            # Build compatible format
                            coin_id = (rec.get("name") or rec.get("coin_id") or "")
                            parent = coin.get("parent_coin_info", "")
                            puzzle = coin.get("puzzle_hash", "")
                            if not coin_id or len(coin_id.replace("0x", "")) < 64:
                                if parent and puzzle:
                                    coin_id = self._compute_coin_id(parent, puzzle, amount)
                            if coin_id:
                                return {
                                    "coin_id": coin_id, "id": coin_id,
                                    "amount": amount, "amount_mojos": amount,
                                    "parent": parent, "puzzle_hash": puzzle,
                                }
            except Exception:
                pass  # Fall through — the retry loop in _do_tier_split will handle it

            return None

        # Helper: find the biggest coin (the change/reserve coin)
        def _find_biggest_coin(wallet_id, label=""):
            """Find the largest spendable coin (the change/reserve)."""
            coins = self._get_coins_via_rpc(wallet_id, label)
            if not coins:
                return None
            coins.sort(key=lambda c: c.get("amount", 0), reverse=True)
            return coins[0]

        # Helper: wait for all pending transactions to clear (on-chain confirmation)
        def _wait_for_pending_clear(label, timeout_s=300):
            """Poll get_pending_transactions until empty (all confirmed on-chain).

            This is the PROPER way to wait for Sage transactions to confirm.
            Unlike coin-count polling alone, this directly checks Sage's
            internal pending transaction queue.
            Returns True if cleared, False on timeout.
            """
            poll_interval = 5
            max_polls = timeout_s // poll_interval
            for poll in range(max_polls):
                time.sleep(poll_interval)
                pending = get_pending_transactions()
                if len(pending) == 0:
                    self.log(f"      ✅ {label} confirmed on-chain ({(poll + 1) * poll_interval}s)")
                    return True
                if (poll + 1) % 6 == 0:
                    self.log(f"      ⏳ {(poll + 1) * poll_interval}s — {len(pending)} pending txns...")
            self.log(f"      ⚠️ {label} still has pending txns after {timeout_s}s")
            return False

        # Helper: sequential send-to-self + split for one tier on one side
        def _do_tier_split(wallet_id, tier_name, count, pool_mojos, side_label, is_cat,
                           is_last_tier=False):
            """Send pool_mojos to self, then split the pool coin into count pieces.

            Returns True if successful, False on failure.

            PRINCIPLE: Always confirm actual state, never use time guesses.
            Every transition is gated by a confirmation check, not a sleep.

            Confirmation gates:
              A. Send-to-self → CONFIRM: pending_transactions empty
              B. Pool coin     → CONFIRM: _find_coin_by_amount finds exact match
              C. Spendable     → CONFIRM: are_coins_spendable returns True
              D. Split submit  → CONFIRM: pending_transactions empty
              E. Split done    → CONFIRM: pool coin no longer in coin list (consumed)
              F. DB recording  → record new coins immediately at birth
              G. Change coin   → CONFIRM change coin spendable before next tier
            """
            nonlocal step_done

            if count <= 1:
                self.log(f"   ⏭️ {side_label} {tier_name}: only 1 coin — skip (reserve IS the coin)")
                step_done += 2  # Skip both send + split steps
                return True

            # ═══════════════════════════════════════════════════════════
            # STEP A: Send pool_mojos to self
            # ═══════════════════════════════════════════════════════════
            # NOTE: Sage's send_xch/send_cat do NOT support coin_ids.
            # We rely on Gate G (change coin confirmation) between tiers
            # to ensure Sage always sees the change coin and uses it.
            self.log(f"\n   📤 {side_label} {tier_name}: sending {pool_mojos:,} mojos to self...")

            send_ok = False
            for attempt in range(3):
                try:
                    result = send_transaction(
                        wallet_id=wallet_id,
                        amount_mojos=pool_mojos,
                        address=address,
                        fee_mojos=self._tx_fee_mojos(),
                    )
                    if result is None:
                        self.log(f"      ❌ Send returned None (attempt {attempt + 1}/3)")
                        # CONFIRM: wait for any pending to clear before retrying
                        _wait_for_pending_clear(f"{side_label} {tier_name} send-retry-{attempt + 1}", timeout_s=60)
                        continue
                    if isinstance(result, dict) and result.get("error"):
                        err = result.get("error", "")[:100]
                        self.log(f"      ⚠️ Send error: {err} (attempt {attempt + 1}/3)")
                        # CONFIRM: wait for any pending to clear before retrying
                        _wait_for_pending_clear(f"{side_label} {tier_name} send-error-retry-{attempt + 1}", timeout_s=60)
                        continue
                    self.log("      ✅ Send submitted")
                    send_ok = True
                    break
                except Exception as e:
                    self.log(f"      ❌ Send exception: {e} (attempt {attempt + 1}/3)")
                    _wait_for_pending_clear(f"{side_label} {tier_name} send-exc-retry-{attempt + 1}", timeout_s=60)

            if not send_ok:
                self.log(f"      ❌ {side_label} {tier_name} send failed after 3 attempts")
                return False

            # CONFIRM GATE A: pending transactions must be empty (= on-chain)
            self.log("      ⏳ Waiting for on-chain confirmation...")
            if not _wait_for_pending_clear(f"{side_label} {tier_name} send", timeout_s=300):
                self.log("      ❌ Send never confirmed on-chain after 300s!")
                return False
            step_done += 1

            progress = 0.25 + (step_done / total_steps) * 0.65
            self.update_status(PrepPhase.SPLITTING, progress,
                             f"✅ {side_label} {tier_name}: send confirmed")

            # ═══════════════════════════════════════════════════════════
            # STEP B: CONFIRM pool coin exists in wallet coin list
            # ═══════════════════════════════════════════════════════════
            # The send-to-self creates a coin of exactly pool_mojos.
            # Sage may take time to sync this into its queryable coin list.
            # We poll until we find it — NEVER fall back to "largest coin".
            pool_coin = None
            self.log(f"      🔍 Confirming pool coin ({pool_mojos:,} mojos) exists...")
            for find_attempt in range(60):  # Up to 300s (5 min)
                pool_coin = _find_coin_by_amount(wallet_id, pool_mojos, f"{side_label}-{tier_name}-pool")
                if pool_coin:
                    self.log(f"      ✅ CONFIRMED: pool coin found after {find_attempt * 5}s")
                    break
                if find_attempt > 0 and find_attempt % 6 == 0:
                    self.log(f"      ⏳ {find_attempt * 5}s — pool coin not yet visible in wallet...")
                time.sleep(5)
            else:
                # Pool coin never appeared — log diagnostics, fail safely
                avail = self._get_coins_via_rpc(wallet_id, f"{side_label}-{tier_name}-debug")
                avail_amounts = [c.get("amount", 0) for c in (avail or [])][:8]
                self.log(f"      ❌ Pool coin ({pool_mojos:,} mojos) never appeared after 300s!")
                self.log(f"         Available coins: {avail_amounts}")
                self.log(f"         ABORTING {side_label} {tier_name} — refusing to split wrong coin")
                return False

            coin_id = pool_coin.get("coin_id", "").replace("0x", "")

            # ═══════════════════════════════════════════════════════════
            # STEP C: CONFIRM pool coin is spendable
            # ═══════════════════════════════════════════════════════════
            self.log(f"      🔍 Confirming coin {coin_id[:16]}... is spendable...")
            for spend_attempt in range(60):  # Up to 300s
                if self._are_coin_ids_selectable(
                    wallet_id, [coin_id], f"{side_label}-{tier_name}-strict-selectable"
                ):
                    self.log(f"      ✅ CONFIRMED: coin is spendable after {spend_attempt * 5}s")
                    break
                if spend_attempt > 0 and spend_attempt % 6 == 0:
                    self.log(f"      ⏳ {spend_attempt * 5}s — coin not yet spendable...")
                time.sleep(5)
            else:
                self.log(f"      ❌ Coin {coin_id[:16]}... never became spendable after 300s!")
                return False

            # ═══════════════════════════════════════════════════════════
            # STEP D: Split the confirmed, spendable pool coin
            # ═══════════════════════════════════════════════════════════
            self.log(f"   ✂️ {side_label} {tier_name}: splitting {coin_id[:16]}... ({pool_coin.get('amount', 0):,} mojos) into {count} pieces")

            split_ok = False
            for attempt in range(3):
                try:
                    result = split_coins_rpc(
                        wallet_id=wallet_id,
                        target_coin_id=coin_id,
                        num_coins=count,
                        amount_per_coin=0,  # Sage splits equally
                        fee_mojos=self._split_tx_fee_mojos(),
                        is_cat=is_cat,
                    )
                    if result is None:
                        self.log(f"      ❌ Split returned None (attempt {attempt + 1}/3)")
                        _wait_for_pending_clear(f"{side_label} {tier_name} split-retry-{attempt + 1}", timeout_s=60)
                        continue
                    if isinstance(result, dict) and result.get("error") == "UNKNOWN_UNSPENT":
                        self.log(f"      ⚠️ UNKNOWN_UNSPENT (attempt {attempt + 1}/3)")
                        # CONFIRM: re-check spendable before retrying
                        self.log("         Re-confirming coin is spendable...")
                        for recheck in range(12):
                            time.sleep(5)
                            if self._are_coin_ids_selectable(
                                wallet_id, [coin_id], f"{side_label}-{tier_name}-retry-selectable"
                            ):
                                self.log("         ✅ Coin confirmed spendable — retrying split")
                                break
                        continue
                    if isinstance(result, dict) and result.get("error"):
                        err = result.get("error", "")[:100]
                        self.log(f"      ⚠️ Split error: {err} (attempt {attempt + 1}/3)")
                        _wait_for_pending_clear(f"{side_label} {tier_name} split-err-{attempt + 1}", timeout_s=60)
                        continue
                    self.log(f"      ✅ Split submitted ({count} pieces)")
                    split_ok = True
                    break
                except Exception as e:
                    self.log(f"      ❌ Split exception: {e} (attempt {attempt + 1}/3)")
                    _wait_for_pending_clear(f"{side_label} {tier_name} split-exc-{attempt + 1}", timeout_s=60)

            if not split_ok:
                self.log(f"      ❌ {side_label} {tier_name} split failed after 3 attempts")
                return False

            # CONFIRM GATE D: pending transactions must be empty (= split on-chain)
            self.log("      ⏳ Waiting for split on-chain confirmation...")
            if not _wait_for_pending_clear(f"{side_label} {tier_name} split", timeout_s=300):
                self.log("      ❌ Split never confirmed on-chain after 300s!")
                return False

            # ═══════════════════════════════════════════════════════════
            # STEP E: CONFIRM split actually happened
            # ═══════════════════════════════════════════════════════════
            # The pool coin should be GONE (consumed by the split).
            # Poll until it disappears — this proves the split was real.
            self.log("      🔍 Confirming pool coin was consumed by split...")
            for verify_attempt in range(24):  # Up to 120s
                check_coin = _find_coin_by_amount(wallet_id, pool_mojos, f"{side_label}-{tier_name}-verify")
                if check_coin is None:
                    self.log(f"      ✅ CONFIRMED: pool coin consumed (split successful) after {verify_attempt * 5}s")
                    break
                if verify_attempt > 0 and verify_attempt % 6 == 0:
                    self.log(f"      ⏳ {verify_attempt * 5}s — pool coin still visible, waiting for wallet sync...")
                time.sleep(5)
            else:
                # Pool coin still visible after 120s — split may have failed silently
                self.log("      ⚠️ Pool coin still visible after 120s — split may not have taken effect")
                self.log("         Proceeding cautiously but this tier may have failed")

            # ═══════════════════════════════════════════════════════════
            # STEP F: Record new tier coins to DB immediately at birth
            # ═══════════════════════════════════════════════════════════
            # Don't wait for a final sweep — record each split's coins NOW.
            # This ensures DB is always 100% accurate and we can diagnose
            # any missing coins by knowing exactly when they were created.
            wallet_type = "cat" if is_cat else "xch"
            coin_size_mojos = pool_mojos // count  # Each piece = total / count
            self.log(f"      💾 Recording {count} × {coin_size_mojos:,} mojo {side_label} coins to DB...")
            db_recorded = 0
            if self._db_ready:
                try:
                    from database import upsert_coin, set_coin_designation
                    # Find the newly-created coins by their expected size
                    new_coins = self._get_coins_via_rpc(wallet_id, f"{side_label}-{tier_name}-db-record")
                    if new_coins:
                        for c in new_coins:
                            if c.get("amount", 0) == coin_size_mojos:
                                cid = self._ensure_0x(c.get("coin_id", ""))
                                if cid:
                                    try:
                                        upsert_coin(cid, wallet_type, coin_size_mojos)
                                        set_coin_designation(cid, "tier_spare",
                                                             assigned_tier=tier_name)
                                        db_recorded += 1
                                    except Exception as dbe:
                                        self.log(f"      ⚠️ DB record failed for {cid[:16]}...: {dbe}")
                    self.log(f"      💾 DB: {db_recorded}/{count} {side_label} {tier_name} coins recorded")
                except Exception as dbe:
                    self.log(f"      ⚠️ DB recording failed: {dbe}")
            else:
                self.log("      ⚠️ DB not ready — skipping coin recording")

            # ═══════════════════════════════════════════════════════════
            # CONFIRM GATE G: Change coin exists and is spendable
            # ═══════════════════════════════════════════════════════════
            # The send-to-self created 2 outputs: pool (now split) + change.
            # The change coin carries forward to the next tier's send.
            # If we start the next send before this change coin is visible
            # and spendable, Sage will grab tier coins instead! This gate
            # prevents that by confirming the change coin before proceeding.
            # ═══════════════════════════════════════════════════════════
            if not is_last_tier:
                wallet_type = "cat" if is_cat else "xch"
                tier_sizes = set()
                for tn in self.tier_order:
                    if wallet_type == "xch":
                        sz = int(self.tier_xch_sizes.get(tn, Decimal("0")) * Decimal("1000000000000"))
                    else:
                        sz = int(self.tier_cat_sizes.get(tn, Decimal("0")) * (10 ** self.cat_decimals))
                    if sz > 0:
                        tier_sizes.add(sz)

                self.log("      🔍 GATE G: Confirming change coin is spendable for next tier...")
                change_confirmed = False
                for change_poll in range(60):  # Up to 300s
                    all_coins = self._get_coins_via_rpc(wallet_id, f"{side_label}-{tier_name}-change")
                    if all_coins:
                        all_coins.sort(key=lambda c: c.get("amount", 0), reverse=True)
                        for c in all_coins:
                            amt = c.get("amount", 0)
                            if amt not in tier_sizes and amt > 0:
                                change_id = c.get("coin_id", "").replace("0x", "")
                                # Confirm it's spendable
                                if self._are_coin_ids_selectable(
                                    wallet_id, [change_id], f"{side_label}-{tier_name}-change-selectable"
                                ):
                                    self.log(f"      ✅ GATE G: Change coin {change_id[:16]}... "
                                             f"({amt:,} mojos) confirmed spendable after {change_poll * 5}s")
                                    change_confirmed = True
                                    break
                    if change_confirmed:
                        break
                    if change_poll > 0 and change_poll % 6 == 0:
                        self.log(f"      ⏳ {change_poll * 5}s — change coin not yet spendable...")
                    time.sleep(5)

                if not change_confirmed:
                    self.log("      ⚠️ Change coin not confirmed after 300s — "
                             "next tier may use wrong coins!")

            step_done += 1

            # Update progress
            progress = 0.25 + (step_done / total_steps) * 0.65
            self.update_status(PrepPhase.SPLITTING, progress,
                             f"✅ {side_label} {tier_name} done ({count} coins)")
            return True

        # ================================================================
        # MAIN FLOW: Multi-send + split approach
        # ================================================================
        # Instead of sequential send-to-self per tier (which lets Sage
        # consume tier coins via its exact-match coin selection), we now:
        #   1. multi_send ALL pool amounts in ONE atomic transaction
        #   2. Wait for single on-chain confirmation
        #   3. Find each pool coin by amount
        #   4. Split each pool coin using coin_ids (Sage supports this)
        #
        # This eliminates the tier coin consumption bug because Sage
        # does ONE coin selection for the total amount, which MUST use
        # the big reserve coin (no combination of smaller coins works).
        # ================================================================

        # ================================================================
        # SUBMIT-ONLY: Send multi_send but DON'T poll yet
        # ================================================================
        def _submit_multi_send(wallet_id, tier_info_for_side, side_label, is_cat):
            """Submit multi_send transaction (no polling). Returns submit details or None."""
            nonlocal step_done

            payments = []
            tier_details = []
            for (tier_name, count, xch_mojos, cat_mojos, xch_size, cat_size) in tier_info_for_side:
                pool_mojos = cat_mojos if is_cat else xch_mojos
                if count <= 1:
                    self.log(f"   ⏭️ {side_label} {tier_name}: only 1 coin — skip (reserve IS the coin)")
                    step_done += 2
                    continue
                payments.append({"address": address, "amount": pool_mojos})
                tier_details.append((tier_name, count, pool_mojos))

            if not payments:
                self.log(f"   ℹ️ {side_label}: no tiers need splitting")
                return []

            total_mojos = sum(p["amount"] for p in payments)
            self.log(f"\n   📤 {side_label} MULTI-SEND: {len(payments)} payments, "
                     f"total {total_mojos:,} mojos")
            for i, (tn, cnt, pm) in enumerate(tier_details):
                self.log(f"      Payment {i+1}: {tn} = {pm:,} mojos ({cnt} coins)")

            send_ok = False
            for attempt in range(3):
                try:
                    if is_cat:
                        result = send_cat_multi(payments, fee_mojos=self._tx_fee_mojos())
                    else:
                        result = send_transaction_multi(payments, fee_mojos=self._tx_fee_mojos())
                    if result is None:
                        self.log(f"      ❌ multi_send returned None (attempt {attempt + 1}/3)")
                        _wait_for_pending_clear(f"{side_label} multi_send retry-{attempt + 1}", timeout_s=60)
                        continue
                    if isinstance(result, dict) and result.get("error"):
                        err = result.get("error", "")[:200]
                        self.log(f"      ⚠️ multi_send error: {err} (attempt {attempt + 1}/3)")
                        _wait_for_pending_clear(f"{side_label} multi_send err-{attempt + 1}", timeout_s=60)
                        continue
                    tx_ids = self._extract_sage_transaction_ids(result)
                    if tx_ids:
                        self.log(f"      ✅ {side_label} multi_send submitted ({tx_ids[0][:20]}...)")
                    else:
                        self.log(f"      ✅ {side_label} multi_send submitted")
                    send_ok = True
                    break
                except Exception as e:
                    self.log(f"      ❌ multi_send exception: {e} (attempt {attempt + 1}/3)")
                    _wait_for_pending_clear(f"{side_label} multi_send exc-{attempt + 1}", timeout_s=60)

            if not send_ok:
                self.log(f"      ❌ {side_label} multi_send failed after 3 attempts")
                return None

            return {
                "tier_details": tier_details,
                "tx_ids": tx_ids if 'tx_ids' in locals() else [],
            }

        # ================================================================
        # PARALLEL POLL: Wait for ALL pool coins (XCH + CAT) simultaneously
        # ================================================================
        def _poll_all_pool_coins(xch_submit, cat_submit, timeout_s=300):
            """Poll until all XCH and CAT pool coins are spendable.
            Returns True if all confirmed, False on timeout."""
            nonlocal step_done

            xch_tiers = (xch_submit or {}).get("tier_details") or []
            cat_tiers = (cat_submit or {}).get("tier_details") or []
            xch_tx_ids = (xch_submit or {}).get("tx_ids") or []
            cat_tx_ids = (cat_submit or {}).get("tx_ids") or []
            xch_confirmed = not xch_tiers  # True if nothing to wait for
            cat_confirmed = not cat_tiers
            poll_started_at = time.time()
            xch_wait_started_at = poll_started_at
            cat_wait_started_at = poll_started_at
            xch_tx_logged = False
            cat_tx_logged = False
            xch_owned_logged = False
            cat_owned_logged = False

            self.log(f"\n   🔍 Polling for ALL pool coins simultaneously (up to {timeout_s}s)...")

            for poll in range(timeout_s // 5):
                if not xch_confirmed:
                    tx_state = self._get_transaction_confirmation_state(xch_tx_ids)
                    if tx_state["confirmed"] and not xch_tx_logged:
                        self.log(
                            "      ✅ XCH pool transaction confirmed"
                            + (f" at height {tx_state['height']}" if tx_state["height"] else "")
                        )
                        xch_tx_logged = True

                    owned_coins = self._get_owned_coins_via_rpc(self.xch_wallet_id, "xch-pool-owned")
                    selectable_ids = self._get_strict_selectable_coin_id_set(
                        self.xch_wallet_id, "xch-pool-confirm-selectable"
                    )
                    if owned_coins:
                        assigned_map = _assign_pool_coins_to_tiers(owned_coins, xch_tiers)
                        if assigned_map:
                            if not xch_owned_logged:
                                self.log("      ✅ XCH pool outputs are present in owned wallet view")
                                xch_owned_logged = True
                            pool_ids = [
                                assigned_map[tn].get("coin_id", "").replace("0x", "")
                                for (tn, _, pm) in xch_tiers
                            ]
                            all_ok = all(pid.lower() in selectable_ids for pid in pool_ids)
                            if all_ok:
                                xch_pool_coin_map.clear()
                                xch_pool_coin_map.update(assigned_map)
                                elapsed_xch = int(time.time() - xch_wait_started_at)
                                self.log(f"      XCH pools selectable after {elapsed_xch}s")
                                xch_confirmed = True
                                step_done += len(xch_tiers)
                                # Progress: 35% -> 45% when XCH pools confirmed
                                self.update_status(PrepPhase.SPLITTING, 0.45,
                                                 "Step 2/4: XCH pool coins confirmed")

                if not cat_confirmed:
                    tx_state = self._get_transaction_confirmation_state(cat_tx_ids)
                    if tx_state["confirmed"] and not cat_tx_logged:
                        self.log(
                            "      ✅ CAT pool transaction confirmed"
                            + (f" at height {tx_state['height']}" if tx_state["height"] else "")
                        )
                        cat_tx_logged = True

                    owned_coins = self._get_owned_coins_via_rpc(self.cat_wallet_id, "cat-pool-owned")
                    selectable_ids = self._get_strict_selectable_coin_id_set(
                        self.cat_wallet_id, "cat-pool-confirm-selectable"
                    )
                    if owned_coins:
                        assigned_map = _assign_pool_coins_to_tiers(owned_coins, cat_tiers)
                        if assigned_map:
                            if not cat_owned_logged:
                                self.log("      ✅ CAT pool outputs are present in owned wallet view")
                                cat_owned_logged = True
                            pool_ids = [
                                assigned_map[tn].get("coin_id", "").replace("0x", "")
                                for (tn, _, pm) in cat_tiers
                            ]
                            all_ok = all(pid.lower() in selectable_ids for pid in pool_ids)
                            if all_ok:
                                cat_pool_coin_map.clear()
                                cat_pool_coin_map.update(assigned_map)
                                elapsed_cat = int(time.time() - cat_wait_started_at)
                                self.log(f"      CAT pools selectable after {elapsed_cat}s")
                                cat_confirmed = True
                                step_done += len(cat_tiers)
                                # Progress: 45% -> 55% when CAT pools confirmed
                                self.update_status(PrepPhase.SPLITTING, 0.55,
                                                 "Step 2/4: CAT pool coins confirmed")

                if xch_confirmed and cat_confirmed:
                    self.update_status(PrepPhase.SPLITTING, 0.55,
                                     "✅ Step 2/4: All pool coins confirmed")
                    return True

                if poll > 0 and poll % 4 == 0:
                    sx = "✅" if xch_confirmed else "⏳"
                    sc = "✅" if cat_confirmed else "⏳"
                    elapsed = int(time.time() - poll_started_at)
                    self.log(f"      ⏳ {elapsed}s — XCH: {sx}, CAT: {sc}")
                time.sleep(5)

            # Check if Sage lost peer connectivity — the most common cause
            # of transactions being accepted locally but never confirmed.
            _peer_hint = ""
            try:
                from wallet_sage import get_peer_connections
                _peers = get_peer_connections()
                _pc = len(_peers) if isinstance(_peers, list) else -1
                if _pc == 0:
                    _peer_hint = (
                        " Sage wallet has 0 peers — transactions cannot reach "
                        "the network. Restart Sage to reconnect."
                    )
            except Exception:
                pass
            self.log(f"      ❌ Pool coins not all spendable after {timeout_s}s!{_peer_hint}")
            if _peer_hint:
                self.update_status(
                    PrepPhase.ERROR, 0.0,
                    "Sage has lost network connectivity (0 peers). "
                    "Restart Sage wallet to reconnect, then retry coin prep.",
                    error="no_peers",
                )
            return False

        # ================================================================
        # SUBMIT-ONLY: Submit a split (no polling). Returns coin_id or None.
        # ================================================================
        def _submit_split(wallet_id, tier_name, count, pool_mojos, side_label, is_cat, preselected_pool_coin=None):
            """Find the pool coin, confirm spendable, submit split. Returns submit details or None."""
            pool_coin = None
            if preselected_pool_coin:
                expected_coin_id = preselected_pool_coin.get("coin_id", "").replace("0x", "").lower()
                self.log(f"      Using confirmed {side_label} {tier_name} pool coin ({expected_coin_id[:16]}...)")
                pool_coin = self._wait_for_preselected_pool_coin(
                    wallet_id=wallet_id,
                    pool_coin=preselected_pool_coin,
                    side_label=side_label,
                    tier_name=tier_name,
                    timeout_s=300,
                    poll_interval_s=5,
                )
                if not pool_coin:
                    self.log("      Confirmed pool coin never became selectable after 300s!")
                    return None
            else:
                self.log(f"      Finding {side_label} {tier_name} pool coin ({pool_mojos:,} mojos)...")
                for find_attempt in range(60):
                    pool_coin = _find_coin_by_amount(wallet_id, pool_mojos, f"{side_label}-{tier_name}-pool")
                    if pool_coin:
                        break
                    if find_attempt > 0 and find_attempt % 6 == 0:
                        self.log(f"      {find_attempt * 5}s - pool coin not yet visible...")
                    time.sleep(5)
                if not pool_coin:
                    self.log(f"      Pool coin ({pool_mojos:,} mojos) never appeared after 300s!")
                    return None

            coin_id = pool_coin.get("coin_id", "").replace("0x", "")

            for spend_attempt in range(60):
                if self._are_coin_ids_selectable(
                    wallet_id, [coin_id], f"{side_label}-{tier_name}-submit-selectable"
                ):
                    break
                if spend_attempt > 0 and spend_attempt % 6 == 0:
                    self.log(f"      {spend_attempt * 5}s - not yet spendable...")
                time.sleep(5)
            else:
                self.log("      Coin never became spendable after 300s!")
                return None

            self.log(f"   Splitting {side_label} {tier_name}: {coin_id[:16]}... into {count} pieces")
            for attempt in range(3):
                try:
                    result = split_coins_rpc(
                        wallet_id=wallet_id,
                        target_coin_id=coin_id,
                        num_coins=count,
                        amount_per_coin=0,
                        fee_mojos=self._split_tx_fee_mojos(),
                        is_cat=is_cat,
                    )
                    if result is None:
                        self.log(f"      Split returned None (attempt {attempt + 1}/3)")
                        _wait_for_pending_clear(f"{side_label} {tier_name} split-retry", timeout_s=60)
                        continue
                    if isinstance(result, dict) and result.get("error") == "UNKNOWN_UNSPENT":
                        self.log("      UNKNOWN_UNSPENT - re-confirming spendable...")
                        for recheck in range(12):
                            time.sleep(5)
                            if self._are_coin_ids_selectable(
                                wallet_id, [coin_id], f"{side_label}-{tier_name}-submit-retry-selectable"
                            ):
                                break
                        continue
                    if isinstance(result, dict) and result.get("error"):
                        err = result.get("error", "")[:100]
                        self.log(f"      Split error: {err} (attempt {attempt + 1}/3)")
                        _wait_for_pending_clear(f"{side_label} {tier_name} split-err", timeout_s=60)
                        continue
                    tx_ids = self._extract_sage_transaction_ids(result)
                    if tx_ids:
                        self.log(
                            f"      {side_label} {tier_name} split submitted ({count} pieces, {tx_ids[0][:20]}...)"
                        )
                    else:
                        self.log(f"      {side_label} {tier_name} split submitted ({count} pieces)")
                    return {
                        "pool_coin_id": coin_id,
                        "tx_ids": tx_ids,
                    }
                except Exception as e:
                    self.log(f"      Split exception: {e} (attempt {attempt + 1}/3)")
                    _wait_for_pending_clear(f"{side_label} {tier_name} split-exc", timeout_s=60)

            self.log(f"      {side_label} {tier_name} split failed after 3 attempts")
            return None

        # ================================================================
        # PARALLEL POLL: Wait for ALL splits to confirm simultaneously
        # ================================================================
        def _poll_all_splits(pending_splits, timeout_s=120):
            """Poll until all splits confirm: pool gone + split coins spendable.

            OPTIMISED: Fetches coins once per wallet type per poll cycle,
            then checks ALL pending splits against that single snapshot.
            This means multiple splits can be detected in the same cycle
            instead of one-at-a-time sequential RPC calls.

            Args:
                pending_splits: list of (wallet_id, tier_name, count, pool_mojos,
                                         pool_coin_id, side_label, is_cat, tx_ids)
            Returns True if all confirmed.
            """
            nonlocal step_done

            confirmed = set()  # indices of confirmed splits
            retry_counts = {}  # pending index -> retry attempts used
            anomaly_reported = set()  # pending index -> anomaly already logged
            tx_confirmed_logged = set()
            owned_ready_logged = set()
            grace_extensions = {}  # pending index -> number of pending-tx grace extensions used
            owned_high_water = {}  # idx -> max owned_output_count ever seen
            chain_confirmed = set()  # idx -> coinset confirmed split landed on chain
            chain_check_failures = {}  # idx -> consecutive coinset query failures
            split_deadlines = {idx: timeout_s for idx in range(len(pending_splits))}
            # Sage on a busy chain regularly takes 50-80s to confirm a split
            # broadcast. The previous 45s threshold fired a "still intact"
            # warning + retry on every tier even when the original split was
            # going to land cleanly a few seconds later. 90s gives Sage a
            # full block-cycle of headroom before we treat the split as
            # stuck. The on-chain spent_height pre-check below still blocks
            # the actual retry RPC if the source coin is committed, so the
            # bumped threshold doesn't risk double-spending — it only
            # silences the false-alarm warning.
            retry_after_s = 90
            grace_extension_s = 60
            poll_started_at = time.time()
            self.log(f"\n   🔍 Polling for ALL {len(pending_splits)} splits to confirm...")

            # Identify unique wallet IDs we need to poll
            wallet_ids = set(wid for wid, _, _, _, _, _, _, _ in pending_splits)

            def _inspect_split_state(wid, cnt, pm, pcid, idx, owned_coin_map, selectable_coin_ids):
                coin_size = pm // cnt
                pool_coin_id = (pcid or "").replace("0x", "").lower()

                pool_still_visible = bool(pool_coin_id) and pool_coin_id in owned_coin_map

                split_candidate_ids = sorted(
                    cid for cid, amount in owned_coin_map.items()
                    if amount == coin_size
                )

                reserved_offset = 0
                for prev_idx, (prev_wid, _, prev_cnt, prev_pm, _, _, _, _) in enumerate(pending_splits):
                    if prev_idx >= idx:
                        break
                    if prev_wid == wid and (prev_pm // prev_cnt) == coin_size:
                        reserved_offset += prev_cnt
                output_ids = split_candidate_ids[reserved_offset:reserved_offset + cnt]
                owned_output_count = len(output_ids)

                # Fallback: Sage deducts the TX fee from output coin amounts, which makes
                # small coins (fee tier, small sniper) come back slightly under coin_size.
                # When the source coin is already consumed but exact-amount search finds
                # nothing, retry with a ±15% tolerance (minimum 1 M-mojo / 0.000001 XCH).
                fuzzy_match = False
                if owned_output_count == 0 and not pool_still_visible:
                    _tol = max(coin_size // 7, 1_000_000)  # ~14% or 0.000001 XCH
                    fuzzy_ids = sorted(
                        cid for cid, amount in owned_coin_map.items()
                        if 0 < amount <= coin_size + _tol and coin_size - _tol <= amount
                    )
                    fuzzy_ids = fuzzy_ids[reserved_offset:reserved_offset + cnt]
                    if len(fuzzy_ids) >= cnt:
                        output_ids = fuzzy_ids
                        owned_output_count = len(output_ids)
                        fuzzy_match = True

                selectable_output_count = sum(
                    1 for cid in output_ids
                    if cid in selectable_coin_ids
                )
                outputs_selectable = (
                    owned_output_count >= cnt and
                    selectable_output_count >= cnt
                )
                pool_still_selectable = (
                    bool(pool_coin_id) and
                    pool_coin_id in selectable_coin_ids
                )
                pool_consumed = (
                    (not pool_still_visible) or
                    (pool_coin_id and not pool_still_selectable)
                )
                return {
                    "pool_coin_id": pool_coin_id,
                    "pool_still_visible": pool_still_visible,
                    "pool_still_selectable": pool_still_selectable,
                    "pool_consumed": pool_consumed,
                    "owned_output_count": owned_output_count,
                    "selectable_output_count": selectable_output_count,
                    "outputs_selectable": outputs_selectable,
                    "fuzzy_match": fuzzy_match,
                }

            poll = 0
            next_progress_log_s = 20
            while True:
                elapsed_before_cycle_s = int(time.time() - poll_started_at)
                active_deadline_s = max(
                    split_deadlines.get(idx, timeout_s)
                    for idx in range(len(pending_splits))
                    if idx not in confirmed
                )
                if poll > 0 and elapsed_before_cycle_s >= active_deadline_s:
                    break
                # --- BULK FETCH: one RPC call per wallet type per cycle ---
                owned_coin_cache = {}
                selectable_coin_cache = {}
                for wid in wallet_ids:
                    # Skip wallets where all their splits are already confirmed
                    has_pending = any(
                        idx not in confirmed and w == wid
                        for idx, (w, _, _, _, _, _, _, _) in enumerate(pending_splits)
                    )
                    if has_pending:
                        owned_coin_cache[wid] = self._get_owned_coin_amount_map(wid, f"split-poll-cycle-{poll}")
                        selectable_coin_cache[wid] = self._get_strict_selectable_coin_id_set(
                            wid, f"split-poll-cycle-{poll}-selectable"
                        )

                # --- Check ALL pending splits against cached snapshots ---
                newly_confirmed = []
                for idx, (wid, tn, cnt, pm, pcid, sl, ic, tx_ids) in enumerate(pending_splits):
                    if idx in confirmed:
                        continue

                    owned_coin_map = owned_coin_cache.get(wid, {}) or {}
                    selectable_coin_ids = selectable_coin_cache.get(wid, set()) or set()
                    elapsed_s = int(time.time() - poll_started_at)
                    split_state = _inspect_split_state(
                        wid, cnt, pm, pcid, idx, owned_coin_map, selectable_coin_ids
                    )
                    tx_state = self._get_transaction_confirmation_state(tx_ids)
                    # High-water mark: once we've observed `cnt` exact-amount
                    # outputs, that's durable evidence the split landed.
                    # Sage's owned view occasionally drops a coin (e.g. when
                    # a pre-existing same-amount coin gets consumed by an
                    # unrelated TX), and without this mark we'd reset the
                    # confirmation gate and wait until the 300s timeout.
                    prev_hw = owned_high_water.get(idx, 0)
                    cur_owned = split_state["owned_output_count"]
                    if cur_owned > prev_hw:
                        owned_high_water[idx] = cur_owned
                    later_same_batch_confirmed = any(
                        prev_idx in confirmed and
                        prev_wid == wid and
                        prev_is_cat == ic and
                        prev_idx > idx
                        for prev_idx, (prev_wid, _, _, _, _, _, prev_is_cat, _) in enumerate(pending_splits)
                    )

                    if tx_state["confirmed"] and idx not in tx_confirmed_logged:
                        self.log(
                            f"      ✅ {sl} {tn} split transaction confirmed"
                            + (f" at height {tx_state['height']}" if tx_state["height"] else "")
                        )
                        tx_confirmed_logged.add(idx)

                    if (
                        split_state["pool_consumed"]
                        and split_state["owned_output_count"] >= cnt
                        and idx not in owned_ready_logged
                    ):
                        if split_state["outputs_selectable"]:
                            self.log(f"      ✅ {sl} {tn} outputs are owned and selectable")
                        else:
                            self.log(
                                f"      ✅ {sl} {tn} outputs are owned ({split_state['owned_output_count']}/{cnt})"
                                f" — waiting for selectable view to catch up"
                            )
                        owned_ready_logged.add(idx)

                    # Chain-truth shortcut: when Sage's local view stops
                    # making progress (pool gone, but owned count under cnt
                    # or selectable lagging, and tx tracker silent), ask
                    # Coinset whether the split actually landed on chain.
                    # This caps the wait at ~2 blocks instead of the 300s
                    # outer timeout. We only call out if the local view
                    # can't make a decision on its own.
                    needs_chain_check = (
                        split_state["pool_consumed"]
                        and idx not in chain_confirmed
                        and not tx_state["confirmed"]
                        and elapsed_s >= 30
                        and not (
                            split_state["outputs_selectable"]
                            and split_state["owned_output_count"] >= cnt
                        )
                    )
                    if needs_chain_check:
                        chain_result = self._coinset_split_landed_on_chain(
                            split_state["pool_coin_id"], cnt
                        )
                        if chain_result is True:
                            chain_confirmed.add(idx)
                            self.log(
                                f"      ✅ {sl} {tn} split confirmed on chain via Coinset "
                                f"(local view: {split_state['owned_output_count']}/{cnt} owned, "
                                f"{split_state['selectable_output_count']}/{cnt} selectable)"
                            )
                        elif chain_result is False:
                            # Pool not yet spent on chain — keep waiting.
                            # Don't escalate; the broadcast may just be slow.
                            pass
                        else:
                            # Coinset unreachable / not yet indexed
                            chain_check_failures[idx] = chain_check_failures.get(idx, 0) + 1

                    # Confirmation: any of —
                    #   (a) Sage shows everything: pool gone, all outputs
                    #       owned and selectable (or tx tracker confirmed)
                    #   (b) High-water mark hit cnt at any point AND pool
                    #       consumed AND chain-truth says the split landed
                    #   (c) Pool consumed AND chain-truth confirmed AND we
                    #       have at least cnt-1 outputs owned locally
                    sage_view_complete = (
                        split_state["pool_consumed"]
                        and split_state["owned_output_count"] >= cnt
                        and (split_state["outputs_selectable"] or tx_state["confirmed"])
                    )
                    chain_view_complete = (
                        idx in chain_confirmed
                        and split_state["pool_consumed"]
                        and (
                            owned_high_water.get(idx, 0) >= cnt
                            or split_state["owned_output_count"] >= max(1, cnt - 1)
                        )
                    )
                    if sage_view_complete or chain_view_complete:
                        newly_confirmed.append((idx, sl, tn, cnt, elapsed_s, split_state["outputs_selectable"]))
                        continue

                    if later_same_batch_confirmed and idx not in anomaly_reported:
                        if split_state["pool_still_visible"] and split_state["pool_still_selectable"]:
                            pool_state = "source coin still selectable"
                        elif split_state["pool_still_visible"]:
                            pool_state = "source coin visible but not selectable"
                        else:
                            pool_state = "source coin already consumed"
                        self.log(
                            f"      ⚠️ {sl} {tn} confirmation order anomaly — later {sl} tiers already confirmed "
                            f"while this tier is still pending ({pool_state}; "
                            f"{split_state['owned_output_count']}/{cnt} exact outputs owned; "
                            f"{split_state['selectable_output_count']}/{cnt} selectable)"
                        )
                        anomaly_reported.add(idx)

                    force_retry_now = (
                        later_same_batch_confirmed and
                        retry_counts.get(idx, 0) < 1 and
                        split_state["pool_still_visible"] and
                        split_state["pool_still_selectable"] and
                        not split_state["outputs_selectable"]
                    )

                    if force_retry_now or should_retry_unconsumed_split(
                        elapsed_s=elapsed_s,
                        pool_coin_visible=split_state["pool_still_visible"],
                        pool_coin_selectable=split_state["pool_still_selectable"],
                        outputs_selectable=split_state["outputs_selectable"],
                        retries_used=retry_counts.get(idx, 0),
                        retry_after_s=retry_after_s,
                        max_retries=1,
                    ):
                        # ────────────────────────────────────────────────────────
                        # AUTHORITATIVE on-chain check (Option B fix 2026-04-07):
                        # Sage's `selectable` view can lag the mempool by tens of
                        # seconds. Before submitting a retry, query the source
                        # coin DIRECTLY via get_coins_by_ids and check whether it
                        # is genuinely unspent. If `spent_height` is set, or if
                        # there's a pending `transaction_id`, the source coin is
                        # already committed to a TX — issuing a retry would race
                        # the original split and risk creating mismatched outputs
                        # at puzzle hashes the wallet doesn't recognise (the
                        # 2026-04-07 CAT sniper 23/25 incident).
                        # ────────────────────────────────────────────────────────
                        retry_pool_id = split_state.get("pool_coin_id") or ""
                        retry_blocked_reason = None
                        try:
                            from wallet_sage import get_coins_by_ids as _gcbi
                            chain_view = _gcbi([retry_pool_id]) if retry_pool_id else None
                        except Exception as _e_chain:
                            chain_view = None
                            self.log(
                                f"      ⚠️ {sl} {tn} on-chain pre-retry check failed "
                                f"({_e_chain}) — falling back to view-based decision"
                            )
                        if isinstance(chain_view, dict) and chain_view:
                            # Sage normalises ids with 0x prefix in the map keys
                            _key_a = retry_pool_id if retry_pool_id.startswith("0x") else "0x" + retry_pool_id
                            _key_b = retry_pool_id.replace("0x", "")
                            rec = chain_view.get(_key_a) or chain_view.get(_key_b)
                            if rec is None and chain_view:
                                # try the only entry as a last resort
                                try:
                                    rec = next(iter(chain_view.values()))
                                except Exception:
                                    rec = None
                            if rec is not None:
                                spent_h = rec.get("spent_height")
                                tx_id_chain = rec.get("transaction_id")
                                if spent_h not in (None, 0):
                                    retry_blocked_reason = (
                                        f"source coin already SPENT on-chain at height {spent_h} — "
                                        f"original split confirmed; outputs will arrive shortly"
                                    )
                                elif tx_id_chain:
                                    retry_blocked_reason = (
                                        f"source coin is bound to pending TX {str(tx_id_chain)[:16]}… — "
                                        f"original split is in mempool; retry would race it"
                                    )

                        if retry_blocked_reason:
                            self.log(
                                f"      🛑 {sl} {tn} retry SUPPRESSED ({retry_blocked_reason}) — "
                                f"extending grace instead"
                            )
                            # Burn the retry slot AND extend the deadline so we
                            # don't immediately re-evaluate next iteration.
                            retry_counts[idx] = 1  # mark as "used" so we don't try again
                            split_deadlines[idx] = split_deadlines.get(idx, timeout_s) + grace_extension_s
                            grace_extensions[idx] = grace_extensions.get(idx, 0) + 1
                            continue

                        if force_retry_now:
                            self.log(
                                f"      ⚠️ {sl} {tn} source coin is still intact while later {sl} tiers are confirming — retrying split once now"
                            )
                        else:
                            self.log(
                                f"      ⚠️ {sl} {tn} pool coin still intact after {elapsed_s}s — retrying split once"
                            )
                        self.update_status(
                            PrepPhase.SPLITTING,
                            0.70 + (len(confirmed) / len(pending_splits)) * 0.20,
                            f"🔁 Step 4/4: retrying {sl} {tn} split ({len(confirmed)}/{len(pending_splits)})",
                        )
                        retry_counts[idx] = retry_counts.get(idx, 0) + 1
                        try:
                            result = split_coins_rpc(
                                wallet_id=wid,
                                target_coin_id=split_state["pool_coin_id"],
                                num_coins=cnt,
                                amount_per_coin=0,
                                fee_mojos=self._split_tx_fee_mojos(),
                                is_cat=ic,
                            )
                            if result is None:
                                self.log(f"      ❌ {sl} {tn} retry split returned None")
                            elif isinstance(result, dict) and result.get("error"):
                                err = str(result.get("error", ""))[:100]
                                self.log(f"      ⚠️ {sl} {tn} retry split error: {err}")
                            else:
                                self.log(f"      ✅ {sl} {tn} retry split submitted")
                        except Exception as e:
                            self.log(f"      ❌ {sl} {tn} retry split exception: {e}")

                        continue

                    if should_extend_pending_consumed_split_grace(
                        elapsed_s=elapsed_s,
                        current_deadline_s=split_deadlines.get(idx, timeout_s),
                        pool_coin_visible=split_state["pool_still_visible"],
                        pool_coin_selectable=split_state["pool_still_selectable"],
                        tx_known=tx_state["known"],
                        tx_confirmed=tx_state["confirmed"],
                        owned_output_count=split_state["owned_output_count"],
                        selectable_output_count=split_state["selectable_output_count"],
                        expected_count=cnt,
                        extensions_used=grace_extensions.get(idx, 0),
                    ):
                        split_deadlines[idx] = split_deadlines.get(idx, timeout_s) + grace_extension_s
                        grace_extensions[idx] = grace_extensions.get(idx, 0) + 1
                        self.log(
                            f"      â±ï¸ {sl} {tn} split is nearly complete and still pending â€” extending confirmation grace by "
                            f"{grace_extension_s}s ({split_state['owned_output_count']}/{cnt} exact outputs owned; "
                            f"{split_state['selectable_output_count']}/{cnt} selectable)"
                        )

                # Log all newly confirmed splits from this cycle
                for idx, sl, tn, cnt, elapsed_confirm_s, selectable_ready in newly_confirmed:
                    if selectable_ready:
                        self.log(f"      ✅ {sl} {tn} split confirmed after {elapsed_confirm_s}s")
                    else:
                        self.log(
                            f"      ✅ {sl} {tn} split transaction confirmed and outputs owned after {elapsed_confirm_s}s"
                        )
                    confirmed.add(idx)
                    step_done += 1
                    # Progress: 70% → 90% across split confirmations
                    progress = 0.70 + (len(confirmed) / len(pending_splits)) * 0.20
                    self.update_status(PrepPhase.SPLITTING, progress,
                                     f"🔍 Step 4/4: {sl} {tn} confirmed ({len(confirmed)}/{len(pending_splits)})")
                    # NOTE: No DB writes here — _designate_final_sweep()
                    # does a clean wallet snapshot after ALL splits complete

                if len(confirmed) == len(pending_splits):
                    self.log(f"\n   🎉 All {len(confirmed)} splits confirmed!")
                    return True

                elapsed_now_s = int(time.time() - poll_started_at)
                if elapsed_now_s >= next_progress_log_s:
                    remaining = [f"{sl} {tn}" for i, (_, tn, _, _, _, sl, _, _) in enumerate(pending_splits)
                                 if i not in confirmed]
                    if remaining:
                        self.update_status(
                            PrepPhase.SPLITTING,
                            0.70 + (len(confirmed) / len(pending_splits)) * 0.20,
                            f"🔍 Step 4/4: waiting for {', '.join(remaining)} ({len(confirmed)}/{len(pending_splits)})",
                        )
                    self.log(f"      ⏳ {elapsed_now_s}s — {len(confirmed)}/{len(pending_splits)} confirmed, "
                            f"waiting: {', '.join(remaining)}")
                    next_progress_log_s += 20

                remaining_sleep_s = active_deadline_s - (time.time() - poll_started_at)
                if remaining_sleep_s <= 0:
                    break
                time.sleep(min(5, remaining_sleep_s))
                poll += 1

            # Mark any still pending; check if only optional tiers remain
            _optional_tiers = {"fees", "sniper"}
            _hard_failures = []
            _soft_failures = []
            for idx, (wid, tn, cnt, pm, pcid, sl, _, tx_ids) in enumerate(pending_splits):
                if idx not in confirmed:
                    owned_coin_map = self._get_owned_coin_amount_map(wid, f"{sl}-{tn}-timeout-diagnostic") or {}
                    selectable_coin_ids = self._get_strict_selectable_coin_id_set(
                        wid, f"{sl}-{tn}-timeout-diagnostic-selectable"
                    ) or set()
                    split_state = _inspect_split_state(
                        wid, cnt, pm, pcid, idx, owned_coin_map, selectable_coin_ids
                    )
                    tx_state = self._get_transaction_confirmation_state(tx_ids)
                    if split_state["pool_still_visible"] and split_state["pool_still_selectable"]:
                        pool_state = "source coin still selectable"
                    elif split_state["pool_still_visible"]:
                        pool_state = "source coin visible but not selectable"
                    else:
                        pool_state = "source coin already consumed"
                    outputs_state = (
                        f"{split_state['owned_output_count']}/{cnt} outputs owned; "
                        f"{split_state['selectable_output_count']}/{cnt} selectable; "
                        f"tx={'confirmed' if tx_state['confirmed'] else 'pending'}; "
                        f"retries={retry_counts.get(idx, 0)}; "
                        f"grace={grace_extensions.get(idx, 0)}"
                    )
                    # Optional tier (fees/sniper) whose on-chain TX completed: treat
                    # as a soft failure — coins exist but wallet view lagged behind.
                    # Non-optional tiers or unconsumed source coins are hard failures.
                    if tn in _optional_tiers and split_state["pool_consumed"]:
                        _soft_failures.append((sl, tn))
                        self.log(
                            f"      ⚠️ {sl} {tn} split unconfirmed after {timeout_s}s but TX went through "
                            f"({pool_state}; {outputs_state}) — continuing (optional tier)"
                        )
                    else:
                        _hard_failures.append((sl, tn))
                        self.log(
                            f"      ❌ {sl} {tn} split not confirmed after {timeout_s}s "
                            f"({pool_state}; {outputs_state})"
                        )
            if _hard_failures:
                return False
            # Only soft failures (optional tiers with consumed source coins) — treat as success
            if _soft_failures:
                self.log(
                    f"   ⚠️ {len(_soft_failures)} optional tier(s) unconfirmed but on-chain: "
                    + ", ".join(f"{sl} {tn}" for sl, tn in _soft_failures)
                    + " — proceeding"
                )
            return True

        # ================================================================
        # MAIN: TRULY PARALLEL — matches test_coin_prep_v2.py approach
        # ================================================================
        # Step 1: Submit BOTH multi_sends (no waiting)
        # Step 2: Poll for ALL pool coins (XCH + CAT) in ONE loop
        # Step 3: Submit ALL splits at once (no waiting between them)
        # Step 4: Poll for ALL splits in ONE loop
        # ================================================================

        # ================================================================
        # PROGRESS LAYOUT for 4-step parallel prep:
        #   0%  - 25% : Consolidation (handled before this point)
        #  25%  - 35% : STEP 1 — Submit multi_sends
        #  35%  - 55% : STEP 2 — Poll for pool coins
        #  55%  - 70% : STEP 3 — Submit all splits
        #  70%  - 90% : STEP 4 — Poll for split confirmations
        #  90%  - 100%: Final verification + DB sweep
        # ================================================================

        # ----------------------------------------------------------------
        # SEQUENTIAL multi_send: XCH first, wait for confirm, THEN CAT.
        # WHY: After consolidation there is only 1 XCH coin. The CAT
        # multi_send needs an XCH coin in the spend bundle (even with
        # fee=0). If we submit both at once the CAT tx tries to use the
        # same XCH coin that the XCH tx is already spending → mempool
        # rejects with MEMPOOL_CONFLICT. Waiting for XCH to confirm
        # creates change coins the CAT tx can reference safely.
        # ----------------------------------------------------------------

        # Pre-flight: verify Sage has peer connections before submitting
        # anything.  Without peers the transaction will be accepted locally
        # but never broadcast, wasting 300 seconds on a doomed wait.
        try:
            from wallet_sage import get_peer_connections
            _preflight_peers = get_peer_connections()
            _preflight_pc = len(_preflight_peers) if isinstance(_preflight_peers, list) else -1
            if _preflight_pc == 0:
                self.log("\n   ❌ PRE-FLIGHT CHECK FAILED: Sage has 0 peers")
                self.log("      Transactions cannot be broadcast without peer connections.")
                self.log("      Restart Sage wallet to reconnect to the Chia network.")
                self.update_status(
                    PrepPhase.ERROR, 0.0,
                    "Sage has no peer connections — restart Sage wallet to reconnect.",
                    error="no_peers",
                )
                return False
            self.log(f"\n   ✅ Pre-flight: Sage has {_preflight_pc} peers")
        except Exception as _pf_err:
            self.log(f"\n   ⚠️ Pre-flight peer check failed: {_pf_err} — continuing anyway")

        self.log(f"\n{'='*40}")
        self.log("📦 STEP 1a: Submit XCH multi_send")
        self.log(f"{'='*40}")
        self.update_status(PrepPhase.SPLITTING, 0.25, "📦 Step 1/4: Submitting XCH multi_send...")

        xch_submit = _submit_multi_send(
            wallet_id=self.xch_wallet_id,
            tier_info_for_side=xch_tier_info,
            side_label="XCH",
            is_cat=False,
        )
        if xch_submit is None:
            self.log("   ❌ XCH multi_send failed!")
            return False
        xch_tier_details = xch_submit.get("tier_details", [])
        self.update_status(PrepPhase.SPLITTING, 0.30, "🔍 Step 1/4: Waiting for XCH pool coins...")

        # Poll for XCH pool coins BEFORE submitting CAT
        self.log("\n   🔍 Waiting for XCH pool coins to confirm before CAT submission...")
        if not _poll_all_pool_coins(xch_submit, {"tier_details": [], "tx_ids": []}, timeout_s=300):
            self.log("   ❌ XCH pool coins not confirmed!")
            return False
        self.log("   ✅ XCH pool coins confirmed — safe to submit CAT multi_send")
        self.update_status(PrepPhase.SPLITTING, 0.40, "📦 Step 1/4: Submitting CAT multi_send...")

        self.log(f"\n{'='*40}")
        self.log("📦 STEP 1b: Submit CAT multi_send")
        self.log(f"{'='*40}")

        cat_submit = _submit_multi_send(
            wallet_id=self.cat_wallet_id,
            tier_info_for_side=cat_tier_info,
            side_label="CAT",
            is_cat=True,
        )
        if cat_submit is None:
            self.log("   ❌ CAT multi_send failed!")
            return False
        cat_tier_details = cat_submit.get("tier_details", [])
        self.update_status(PrepPhase.SPLITTING, 0.45, "🔍 Step 2/4: Waiting for CAT pool coins...")

        # Step 2: Poll for CAT pool coins (XCH already confirmed above)
        self.log(f"\n{'='*40}")
        self.log("🔍 STEP 2: Poll for CAT pool coins")
        self.log(f"{'='*40}")

        if not _poll_all_pool_coins({"tier_details": [], "tx_ids": []}, cat_submit, timeout_s=300):
            self.log("   ❌ CAT pool coins not confirmed!")
            return False
        self.update_status(PrepPhase.SPLITTING, 0.55, "✅ Step 2/4: All pool coins confirmed")

        # Step 3: Submit ALL splits at once
        self.log(f"\n{'='*40}")
        self.log("✂️ STEP 3: Submit ALL splits (XCH + CAT)")
        self.log(f"{'='*40}")
        self.update_status(PrepPhase.SPLITTING, 0.56, "✂️ Step 3/4: Submitting splits...")

        pending_splits = []  # (wallet_id, tier_name, count, pool_mojos, pool_coin_id, side_label, is_cat, tx_ids)

        # Submit fees and sniper first so their pool coins are spent before the larger
        # tier splits are submitted.  All outputs come from the same parent multi_send;
        # submitting 4 sibling spends before attempting fees causes Sage to lock the
        # fees output until those siblings confirm (~90s), stalling the whole prep.
        def _tier_sort_key(item):
            order = {"fees": 0, "sniper": 1}
            return order.get(item[0], 2)
        xch_tier_details_ordered = sorted(xch_tier_details, key=_tier_sort_key)

        # Non-optional tiers: failure aborts prep. Optional (fees/sniper): warn and skip.
        _optional_xch_tiers = {"fees", "sniper"}

        total_split_submits = len(xch_tier_details_ordered) + len(cat_tier_details)
        split_submit_idx = 0
        for tier_name, count, pool_mojos in xch_tier_details_ordered:
            split_submit = None
            for _split_attempt in range(3):
                split_submit = _submit_split(self.xch_wallet_id, tier_name, count, pool_mojos, "XCH", False, preselected_pool_coin=xch_pool_coin_map.get(tier_name))
                if split_submit is not None:
                    break
                if _split_attempt < 2:
                    self.log(f"   ⚠️ XCH {tier_name} split submit attempt {_split_attempt + 1}/3 failed — retrying in 10s...")
                    time.sleep(10)
            if split_submit is None:
                if tier_name in _optional_xch_tiers:
                    self.log(f"   ⚠️ XCH {tier_name} split failed after 3 attempts — skipping (optional tier, bot can still operate)")
                    split_submit_idx += 1
                    continue
                self.log(f"   ❌ XCH {tier_name} split submit failed!")
                return False
            pending_splits.append((self.xch_wallet_id, tier_name, count, pool_mojos, split_submit["pool_coin_id"], "XCH", False, split_submit.get("tx_ids", [])))
            split_submit_idx += 1
            # Progress: 55% → 70% across split submissions
            submit_progress = 0.55 + (split_submit_idx / max(total_split_submits, 1)) * 0.15
            self.update_status(PrepPhase.SPLITTING, submit_progress,
                             f"✂️ Step 3/4: XCH {tier_name} split submitted ({split_submit_idx}/{total_split_submits})")
            # Wait for Sage to acknowledge this spend (pool coin leaves selectable)
            # before submitting the next sibling — prevents sibling-locking rejections
            _ack_coin_id = split_submit["pool_coin_id"].replace("0x", "")
            for _ack_poll in range(15):
                if not self._are_coin_ids_selectable(self.xch_wallet_id, [_ack_coin_id], f"XCH-{tier_name}-post-submit-ack"):
                    self.log(f"   ✓ XCH {tier_name} pool coin acknowledged by Sage after {_ack_poll + 1}s")
                    break
                time.sleep(1)
            else:
                self.log(f"   ⚠️ XCH {tier_name} pool coin still selectable after 15s — proceeding anyway")

        for tier_name, count, pool_mojos in cat_tier_details:
            split_submit = None
            for _split_attempt in range(3):
                split_submit = _submit_split(self.cat_wallet_id, tier_name, count, pool_mojos, "CAT", True, preselected_pool_coin=cat_pool_coin_map.get(tier_name))
                if split_submit is not None:
                    break
                if _split_attempt < 2:
                    self.log(f"   ⚠️ CAT {tier_name} split submit attempt {_split_attempt + 1}/3 failed — retrying in 10s...")
                    time.sleep(10)
            if split_submit is None:
                self.log(f"   ❌ CAT {tier_name} split submit failed!")
                return False
            pending_splits.append((self.cat_wallet_id, tier_name, count, pool_mojos, split_submit["pool_coin_id"], "CAT", True, split_submit.get("tx_ids", [])))
            split_submit_idx += 1
            submit_progress = 0.55 + (split_submit_idx / max(total_split_submits, 1)) * 0.15
            self.update_status(PrepPhase.SPLITTING, submit_progress,
                             f"✂️ Step 3/4: CAT {tier_name} split submitted ({split_submit_idx}/{total_split_submits})")
            # Wait for Sage to acknowledge this spend (pool coin leaves selectable)
            # before submitting the next sibling — prevents sibling-locking rejections
            _ack_coin_id = split_submit["pool_coin_id"].replace("0x", "")
            for _ack_poll in range(15):
                if not self._are_coin_ids_selectable(self.cat_wallet_id, [_ack_coin_id], f"CAT-{tier_name}-post-submit-ack"):
                    self.log(f"   ✓ CAT {tier_name} pool coin acknowledged by Sage after {_ack_poll + 1}s")
                    break
                time.sleep(1)
            else:
                self.log(f"   ⚠️ CAT {tier_name} pool coin still selectable after 15s — proceeding anyway")

        # Step 4: Poll for ALL split confirmations simultaneously
        if pending_splits:
            self.log(f"\n{'='*40}")
            self.log(f"🔍 STEP 4: Poll for ALL {len(pending_splits)} splits to confirm")
            self.log(f"{'='*40}")
            self.update_status(PrepPhase.SPLITTING, 0.70, f"🔍 Step 4/4: Waiting for {len(pending_splits)} splits to confirm...")
            if not _poll_all_splits(pending_splits, timeout_s=300):
                self.log("   [split-fail] Split confirmation exhausted the 300s base window plus any pending-transaction grace")
                self.log("   ❌ Split confirmation failed within 300s — aborting coin prep")
                return False

        self.log("\n   ✅ All XCH + CAT splits complete!")

        # ================================================================
        # FINAL VERIFICATION — confirmation-based, not single-shot
        # ================================================================
        # We know exactly what coins should exist. Poll until the wallet
        # reports the expected count, or time out after 120s.
        # This handles Sage's coin sync lag after the last split.
        # ================================================================
        self.update_status(PrepPhase.SPLITTING, 0.92, "🔍 Verifying final coin counts...")
        self.log("\n--- Final Verification (confirmation-based) ---")

        total_xch_target = self.xch_target_coins
        total_cat_target = self.cat_target_coins
        xch_expected = self.xch_expected_total_coins
        cat_expected = self.cat_expected_total_coins

        self.log(
            "   Expected totals (including reserve): "
            f"XCH={xch_expected} ({total_xch_target} prepared + 1 reserve), "
            f"CAT={cat_expected} ({total_cat_target} prepared + 1 reserve)"
        )
        # Single snapshot — splits are already confirmed on-chain (Step 4 passed).
        # The selectable view can lag for the CAT reserve change coin; that's fine.
        # The DB designation sweep has its own polling, and Sage handles offer coin
        # selection internally. No need to wait here.
        xch_final = self.get_confirmed_coin_count(self.xch_wallet_id)
        cat_final = self.get_confirmed_coin_count(self.cat_wallet_id)
        self._set_status_coin_counts(xch_total=xch_final, cat_total=cat_final)
        xch_short = max(0, xch_expected - xch_final)
        cat_short = max(0, cat_expected - cat_final)
        if xch_short == 0 and cat_short == 0:
            self.log(f"   ✅ All coins confirmed — XCH: {xch_final}, CAT: {cat_final}")
        else:
            self.log(f"   ℹ️ Snapshot: XCH {xch_final}/{xch_expected}, CAT {cat_final}/{cat_expected} "
                     f"— splits confirmed on-chain, selectable view may lag (normal). Proceeding.")

        self.log(f"   XCH: {xch_final} coins (target: {total_xch_target} + reserve)")
        self.log(f"   CAT: {cat_final} coins (target: {total_cat_target} + reserve)")

        self.log("✅ Sage multi-send tiered coin prep complete!")

        return True

    # Old parallel Sage tier splitting code was removed.
    # The sequential approach above replaces it entirely.
    # Placeholder to mark the boundary:
    def split_coin_cli(self, wallet_id: int, name: str, num_coins: int, coin_size: Decimal) -> bool:
        """
        Split a single coin into many equal pieces using CLI.

        For large splits (>50 coins), splits in batches to avoid wallet
        RPC timeouts. Each batch gets its own transaction. Waits for
        confirmation between batches so the next batch has a valid coin.

        Also retries on failure — the wallet may have actually submitted
        the transaction despite a connection drop (common with 100+ coins).
        """
        MAX_PER_BATCH = 50  # Wallet RPC times out on 100-coin splits

        if num_coins <= MAX_PER_BATCH:
            # Small split — do it in one go
            return self._execute_single_split(wallet_id, name, num_coins, coin_size)

        # Large split — do it in batches
        remaining = num_coins
        batch_num = 0
        total_batches = (num_coins + MAX_PER_BATCH - 1) // MAX_PER_BATCH

        self.log(f"✂️ Large split: {num_coins} {name} coins in {total_batches} batches of up to {MAX_PER_BATCH}")

        while remaining > 0:
            batch_num += 1
            batch_size = min(remaining, MAX_PER_BATCH)

            self.log(f"\n📦 Batch {batch_num}/{total_batches}: splitting {batch_size} {name} coins...")

            success = self._execute_single_split(wallet_id, name, batch_size, coin_size)

            if not success:
                # Check if it actually worked despite the error (common with timeouts).
                # Poll the wallet repeatedly instead of a fixed wait.
                expected_so_far = num_coins - remaining + batch_size
                self.log("⏳ Split reported failure — polling wallet to check if coins appeared...")
                coins_appeared = self._poll_for_coin_count(
                    wallet_id, name, expected_so_far, max_polls=12, poll_secs=5
                )

                if coins_appeared:
                    self.log("✅ Coins appeared despite error! Continuing...")
                    success = True
                else:
                    # Real failure — wait for wallet to be healthy, then retry
                    self.log(f"⏳ Retrying batch {batch_num} — waiting for wallet to be ready...")
                    self._poll_wallet_healthy(max_polls=5, poll_secs=3)
                    success = self._execute_single_split(wallet_id, name, batch_size, coin_size)

                    if not success:
                        # Final check — poll again in case retry worked but connection dropped
                        coins_appeared = self._poll_for_coin_count(
                            wallet_id, name, expected_so_far, max_polls=12, poll_secs=5
                        )
                        if coins_appeared:
                            self.log("✅ Retry succeeded (coins appeared)!")
                            success = True
                        else:
                            current_count = self.get_coin_count(wallet_id)
                            self.log(f"❌ Batch {batch_num} failed after retry. {name}: {current_count} coins")
                            return False

            remaining -= batch_size

            if remaining > 0:
                # Wait for this batch to confirm before starting next
                # (need the change coin from this split for the next one)
                self.log(f"⏳ Waiting for batch {batch_num} to confirm before next batch...")
                pre_count = self.get_coin_count(wallet_id)
                for wait_round in range(60):  # Up to 5 minutes
                    time.sleep(5)
                    new_count = self.get_coin_count(wallet_id)
                    if new_count > pre_count:
                        self.log(f"✅ Batch {batch_num} confirmed! {name}: {new_count} coins. Starting next batch...")
                        break
                    if (wait_round + 1) % 6 == 0:
                        self.log(f"   ⏳ Still waiting... {name}: {new_count} coins ({(wait_round + 1) * 5}s)")
                else:
                    self.log(f"⚠️ Batch {batch_num} not confirmed after 5 min — proceeding anyway")

        self.log(f"✅ All {total_batches} {name} split batches submitted!")
        return True

    def _poll_for_coin_count(self, wallet_id: int, name: str, target_count: int,
                              max_polls: int = 12, poll_secs: int = 5) -> bool:
        """Poll wallet until coin count reaches target. Returns True if reached."""
        for i in range(max_polls):
            time.sleep(poll_secs)
            current = self.get_coin_count(wallet_id)
            if current >= target_count:
                self.log(f"   ✅ {name} coin count reached {current} (target: {target_count}) after {(i + 1) * poll_secs}s")
                return True
            if (i + 1) % 3 == 0:
                self.log(f"   ⏳ {name}: {current}/{target_count} coins ({(i + 1) * poll_secs}s)")
        current = self.get_coin_count(wallet_id)
        self.log(f"   ⏳ {name}: {current}/{target_count} coins after {max_polls * poll_secs}s — target not reached")
        return False

    def _poll_wallet_healthy(self, max_polls: int = 5, poll_secs: int = 3) -> bool:
        """Poll until wallet reports healthy/synced. Returns True if healthy."""
        for i in range(max_polls):
            try:
                healthy = self.check_wallet_sync("pre-retry")
                if healthy is True:
                    return True
            except Exception:
                pass
            time.sleep(poll_secs)
        self.log(f"⚠️ Wallet health not confirmed after {max_polls * poll_secs}s — proceeding anyway")
        return False

    @staticmethod
    def _compute_coin_id(parent_coin_info: str, puzzle_hash: str, amount: int) -> str:
        """Compute a coin's ID from its components.

        Chia coin ID = SHA256(parent_coin_info + puzzle_hash + int_to_bytes(amount))
        where int_to_bytes uses MINIMAL signed big-endian encoding.
        """
        import hashlib
        parent_bytes = bytes.fromhex(parent_coin_info.replace("0x", ""))
        puzzle_bytes = bytes.fromhex(puzzle_hash.replace("0x", ""))
        # Chia's int_to_bytes: minimal signed big-endian (NOT fixed 8 bytes!)
        if amount == 0:
            amount_bytes = b""
        else:
            byte_count = (amount.bit_length() + 8) >> 3
            amount_bytes = amount.to_bytes(byte_count, byteorder="big", signed=True)
        coin_id_bytes = hashlib.sha256(
            parent_bytes + puzzle_bytes + amount_bytes
        ).digest()
        return "0x" + coin_id_bytes.hex()

    def _get_coins_via_rpc(self, wallet_id: int, name: str, selectable_only: bool = False) -> list:
        """Get wallet coins via RPC with retry.

        When selectable_only=True, Sage queries the strict selectable view
        directly instead of the generic wallet fast path.
        """
        fast_poll = (
            "-pool-poll" in name
            or "-pool-confirm" in name
            or "-submit-selectable" in name
            or "-confirm-selectable" in name
        )
        max_attempts = 1 if selectable_only else (2 if fast_poll else 3)
        view_label = "selectable" if (selectable_only or self.is_sage) else "spendable"
        empty_wait_s = 2 if fast_poll else 10
        error_wait_s = 1 if fast_poll else 5
        for attempt in range(max_attempts):
            try:
                if self.is_sage and selectable_only:
                    from wallet_sage import get_selectable_coins_only
                    result = get_selectable_coins_only(wallet_id)
                else:
                    result = get_spendable_coins_rpc(wallet_id)
                if not result or not result.get("success"):
                    if not selectable_only:
                        self.log(f"⚠️ RPC coin list failed (attempt {attempt + 1}/{max_attempts})")
                        time.sleep(error_wait_s)
                    continue

                records = result.get("confirmed_records") or result.get("records") or []
                coins = []
                for rec in records:
                    coin = rec.get("coin", {})
                    parent = coin.get("parent_coin_info", "")
                    puzzle = coin.get("puzzle_hash", "")
                    amount = coin.get("amount", 0)

                    # Try direct ID field first (some Chia versions include it)
                    coin_id = rec.get("name") or rec.get("coin_id") or ""

                    # If not present (Chia 2.6.0+), compute from components
                    if not coin_id or len(coin_id.replace("0x", "")) < 64:
                        if parent and puzzle:
                            coin_id = self._compute_coin_id(parent, puzzle, amount)

                    if amount > 0 and coin_id:
                        coins.append({
                            "coin_id": coin_id,
                            "id": coin_id,
                            "amount": amount,
                            "amount_mojos": amount,
                            "parent": parent,
                            "puzzle_hash": puzzle,
                        })

                if coins:
                    return coins
                else:
                    if not selectable_only:
                        self.log(
                            f"⏳ No {view_label} coins returned for {name} "
                            f"(attempt {attempt + 1}/{max_attempts}) — waiting {empty_wait_s}s..."
                        )
                        time.sleep(empty_wait_s)
            except Exception as e:
                if not selectable_only:
                    self.log(f"⚠️ RPC error listing coins (attempt {attempt + 1}/{max_attempts}): {e}")
                    time.sleep(error_wait_s)

        return []

    def _get_coinset_client(self):
        """Lazily build a CoinsetClient for chain-truth lookups.

        The worker runs in a subprocess that doesn't share the bot's
        client instance, so we create our own. Returns None if Coinset
        is disabled, the import fails, or construction fails — the
        caller should fall back to wallet RPC heuristics in that case.
        """
        client = getattr(self, "_coinset_client_instance", None)
        if client is not None:
            return client
        if getattr(self, "_coinset_client_failed", False):
            return None
        try:
            from coinset_client import CoinsetClient
            self._coinset_client_instance = CoinsetClient()
            return self._coinset_client_instance
        except Exception as e:
            self._coinset_client_failed = True
            self.log(f"   ⚠️ Coinset client unavailable: {e}")
            return None

    def _coinset_split_landed_on_chain(
        self,
        pool_coin_id: str,
        expected_count: int,
    ) -> Optional[bool]:
        """Ask Coinset whether a split TX has landed on chain.

        Returns:
          True  — pool coin is spent on chain AND >= expected_count children
                  exist as on-chain coin records (or 1 less, allowing for
                  the rare fee-deduction edge where one output is short).
          False — pool coin is still unspent on chain (the split TX has
                  NOT landed; do NOT mark this split confirmed).
          None  — Coinset is unreachable / rate-limited / coin not yet
                  indexed; caller keeps using local heuristics.
        """
        if not pool_coin_id or expected_count <= 0:
            return None
        client = self._get_coinset_client()
        if client is None:
            return None

        normalised = str(pool_coin_id).lower()
        if not normalised.startswith("0x"):
            normalised = "0x" + normalised

        try:
            record = client.get_coin_by_name(normalised)
        except Exception:
            record = None
        if not record:
            return None
        try:
            spent_idx = int(record.get("spent_block_index", 0) or 0)
        except (TypeError, ValueError):
            spent_idx = 0
        if spent_idx <= 0:
            return False

        try:
            children = client.get_coin_records_by_parent_ids([normalised])
        except Exception:
            children = None
        if children is None:
            # Pool spent but children index lagging — caller will retry
            return None

        # Allow 1 missing child (Sage occasionally surfaces N-1 outputs
        # when a tiny CAT fee-adjusted coin lands at a slightly different
        # amount). The on-chain truth that the pool is spent + most
        # children are present is enough to stop waiting.
        return len(children) >= max(1, expected_count - 1)

    def _get_owned_coin_amount_map(self, wallet_id: int, name: str) -> Dict[str, int]:
        """Return the wallet's owned coin set as {coin_id: amount_mojos}.

        This is broader than the strict selectable view: it tells us which coins
        currently exist in the wallet, even if Sage has not yet surfaced them as
        selectable. Split confirmation needs both truths:
        - owned view: did the outputs actually appear?
        - selectable view: are they ready to use?
        """
        if not self.is_sage:
            coins = self._get_coins_via_rpc(wallet_id, f"{name}-owned-fallback") or []
            owned_map = {}
            for coin in coins:
                cid = (coin.get("coin_id") or coin.get("id") or "").replace("0x", "").lower()
                if cid:
                    owned_map[cid] = int(coin.get("amount") or coin.get("amount_mojos") or 0)
            return owned_map

        try:
            from wallet_sage import get_owned_coins
            owned_result = get_owned_coins(wallet_id) or {}
            owned_map = {}
            for cid, amount in owned_result.items():
                clean = str(cid).replace("0x", "").lower()
                if clean:
                    owned_map[clean] = int(amount or 0)
            return owned_map
        except Exception as e:
            self.log(f"⚠️ Owned coin view unavailable for {name}: {e}")
            return {}

    def _get_owned_coins_via_rpc(self, wallet_id: int, name: str) -> list:
        """Return owned wallet coins as lightweight coin dicts."""
        owned_map = self._get_owned_coin_amount_map(wallet_id, name)
        coins = []
        for coin_id, amount in owned_map.items():
            coins.append({
                "coin_id": "0x" + coin_id if not str(coin_id).startswith("0x") else coin_id,
                "id": "0x" + coin_id if not str(coin_id).startswith("0x") else coin_id,
                "amount": int(amount or 0),
                "amount_mojos": int(amount or 0),
            })
        return coins

    def _get_strict_selectable_coin_id_set(self, wallet_id: int, name: str) -> set:
        """Return the exact Sage selectable coin ids for a wallet.

        This uses the strict selectable view, not get_are_coins_spendable().
        For Sage, "spendable" can still include offer-locked coins, but prep
        gates that are deciding "can I safely use this coin right now?" need
        the stricter answer: the coin must be selectable/free.
        """
        coins = self._get_coins_via_rpc(wallet_id, name, selectable_only=True)
        ids = set()
        for coin in coins:
            cid = (coin.get("coin_id") or coin.get("id") or "").replace("0x", "").lower()
            if cid:
                ids.add(cid)
        return ids

    def _are_coin_ids_selectable(self, wallet_id: int, coin_ids: List[str], name: str) -> bool:
        """Return True only when every coin id is in Sage's strict selectable view."""
        normalized = {
            str(cid).replace("0x", "").lower()
            for cid in (coin_ids or [])
            if cid
        }
        if not normalized:
            return True
        selectable_ids = self._get_strict_selectable_coin_id_set(wallet_id, name)
        if not selectable_ids:
            return False
        return normalized.issubset(selectable_ids)

    def _wait_for_preselected_pool_coin(self, wallet_id: int, pool_coin: dict,
                                        side_label: str, tier_name: str,
                                        timeout_s: int = 120,
                                        poll_interval_s: int = 5) -> Optional[dict]:
        """Resolve a previously-confirmed pool coin from Sage's strict selectable view.

        First tries to find the exact preselected coin ID (30s window). If it
        doesn't appear — e.g. because the pool coin map was built from a stale
        wallet snapshot after a previous aborted run — falls back to finding any
        selectable coin that matches the target amount.  The amount-fallback is
        safe here because the pool coin amounts were already confirmed unique when
        the map was built; the only risk is a stale ID pointing at a spent coin.
        """
        expected_coin_id = (pool_coin or {}).get("coin_id", "").replace("0x", "").lower()
        if not expected_coin_id:
            return None

        target_amount = int((pool_coin or {}).get("amount")
                            or (pool_coin or {}).get("amount_mojos")
                            or 0)

        # Phase 1: try to find the exact preselected coin ID (up to 30s)
        id_timeout_s = min(30, timeout_s)
        id_polls = max(1, int(id_timeout_s / max(1, poll_interval_s)))

        for find_attempt in range(id_polls):
            strict_coins = self._get_coins_via_rpc(
                wallet_id,
                f"{side_label}-{tier_name}-pool-confirmed",
                selectable_only=True,
            ) or []

            if strict_coins:
                exact_match = next((
                    c for c in strict_coins
                    if c.get("coin_id", "").replace("0x", "").lower() == expected_coin_id
                ), None)
                if exact_match:
                    return exact_match

            if self._are_coin_ids_selectable(
                wallet_id,
                [expected_coin_id],
                f"{side_label}-{tier_name}-pool-confirmed-selectable",
            ):
                resolved = dict(pool_coin or {})
                resolved["coin_id"] = expected_coin_id
                resolved["id"] = expected_coin_id
                if target_amount > 0:
                    resolved.setdefault("amount", target_amount)
                    resolved.setdefault("amount_mojos", target_amount)
                return resolved

            time.sleep(poll_interval_s)

        # Phase 2: exact ID not found — fall back to amount match from selectable view
        # This handles cases where the pool coin map was built from stale wallet data
        # (e.g. after a previous run was aborted mid-flight).
        if target_amount > 0:
            self.log(f"      {side_label} {tier_name} preselected coin ID not found after {id_timeout_s}s — "
                     f"falling back to amount match ({target_amount:,} mojos)",
                     severity="info")
            fallback_polls = max(1, int((timeout_s - id_timeout_s) / max(1, poll_interval_s)))
            log_every = max(1, int(30 / max(1, poll_interval_s)))
            for fb_attempt in range(fallback_polls):
                strict_coins = self._get_coins_via_rpc(
                    wallet_id,
                    f"{side_label}-{tier_name}-pool-fallback",
                    selectable_only=True,
                ) or []
                # Allow ±1% tolerance for tx-fee deductions from multi_send outputs
                tol = max(1, int(target_amount * 0.01))
                amount_match = next((
                    c for c in strict_coins
                    if abs(int(c.get("amount_mojos", c.get("amount", 0))) - target_amount) <= tol
                ), None)
                if amount_match:
                    matched_id = amount_match.get("coin_id", "").replace("0x", "")
                    self.log(f"      ✅ {side_label} {tier_name} amount-fallback found coin {matched_id[:16]}...")
                    return amount_match
                if fb_attempt > 0 and fb_attempt % log_every == 0:
                    self.log(f"      {id_timeout_s + fb_attempt * poll_interval_s}s - fallback: no selectable coin at {target_amount:,} mojos...")
                time.sleep(poll_interval_s)

        return None

    def _log_coin_snapshot(self, wallet_id: int, name: str, label: str):
        """Log a snapshot of all current coins for this wallet — shows IDs and amounts."""
        try:
            coins = self._get_coins_via_rpc(wallet_id, name)
            if not coins:
                self.log(f"   [{label}] {name}: no coins found")
                return

            total_mojos = sum(c["amount_mojos"] for c in coins)
            self.log(f"   [{label}] {name}: {len(coins)} coins, total: {total_mojos:,} mojos")

            # Sort by amount descending for readability
            coins_sorted = sorted(coins, key=lambda c: c["amount_mojos"], reverse=True)

            # Keep snapshots compact; large verbose dumps slow the worker down.
            show_count = min(4, len(coins_sorted))
            for i, c in enumerate(coins_sorted[:show_count]):
                coin_id_short = c["id"][:16] + "..." if len(c["id"]) > 16 else c["id"]
                if wallet_id == self.xch_wallet_id:
                    amount_display = f"{c['amount_mojos'] / 1e12:.6f} XCH"
                else:
                    amount_display = f"{c['amount_mojos']:,} mojos"
                self.log(f"     #{i+1}: {coin_id_short} = {amount_display}")

            if len(coins_sorted) > show_count:
                self.log(f"     ... and {len(coins_sorted) - show_count} more")
        except Exception as e:
            self.log(f"   [{label}] Could not snapshot {name} coins: {e}")

    def _wait_for_transaction_confirmation(self, tx_id: str, name: str,
                                            wallet_id: int, expected_count: int,
                                            max_wait: int = 600) -> bool:
        """Wait for a transaction to confirm, using transaction ID + coin count verification.

        Uses a two-pronged approach:
        1. Primary: Poll get_transaction() for confirmed=True (blockchain confirmation)
        2. Fallback: Count coins reaching expected_count (if get_transaction unavailable)

        Args:
            tx_id: Transaction ID to track (hex string)
            name: "XCH" or "CAT" for logging
            wallet_id: Wallet to count coins in
            expected_count: Target coin count after confirmation
            max_wait: Maximum seconds to wait

        Returns True if confirmed, False if timed out.
        """
        start = time.time()
        poll_interval = 5
        tx_confirmed = False
        coins_confirmed = False
        last_log_time = 0

        self.log(f"⏳ Waiting for {name} transaction to confirm...")
        if tx_id:
            self.log(f"   Transaction ID: {tx_id[:20]}...")

        while (time.time() - start) < max_wait:
            elapsed = int(time.time() - start)

            # --- Strategy 1: Check transaction status via RPC ---
            if tx_id and not tx_confirmed:
                try:
                    tx_info = get_transaction(tx_id)
                    if tx_info and isinstance(tx_info, dict):
                        confirmed = tx_info.get("confirmed", False)
                        height = tx_info.get("confirmed_at_height", 0)

                        if confirmed and height > 0:
                            tx_confirmed = True
                            self.log(f"   ✅ {name} transaction confirmed at block height {height}!")

                            # Log new coins from the transaction
                            additions = tx_info.get("additions", [])
                            if additions:
                                self.log(f"   📋 {len(additions)} new coins created:")
                                for i, coin in enumerate(additions[:10]):
                                    coin_id = coin.get("parent_coin_info", "")[:16]
                                    amt = coin.get("amount", 0)
                                    if wallet_id == self.xch_wallet_id:
                                        self.log(f"     New coin #{i+1}: {coin_id}... = {amt / 1e12:.6f} XCH")
                                    else:
                                        self.log(f"     New coin #{i+1}: {coin_id}... = {amt:,} mojos")
                                if len(additions) > 10:
                                    self.log(f"     ... and {len(additions) - 10} more")
                except Exception:
                    pass  # get_transaction may not be available; fall through to coin count

            # --- Strategy 2: Verify coin count reached target ---
            current_count = self.get_coin_count(wallet_id)
            if current_count >= expected_count:
                coins_confirmed = True

            # Update GUI status and flush to file
            if wallet_id == self.xch_wallet_id:
                self._set_status_coin_counts(xch_total=current_count)
            else:
                self._set_status_coin_counts(cat_total=current_count)
            self.update_status(message=f"Waiting: {name} {current_count}/{expected_count} coins")

            # Both confirmed = done
            if tx_confirmed or coins_confirmed:
                if not tx_confirmed:
                    self.log(f"   ✅ {name} coins reached target ({current_count}/{expected_count}) — confirmed!")
                return True

            # Log progress periodically (every 10 seconds)
            if (time.time() - last_log_time) >= 10:
                last_log_time = time.time()
                status_parts = []
                if tx_id:
                    status_parts.append(f"tx: {'confirmed' if tx_confirmed else 'pending'}")
                status_parts.append(f"coins: {current_count}/{expected_count}")
                self.log(f"   ⏳ {name}: {', '.join(status_parts)} ({elapsed}s)")

            # Check wallet sync during long waits
            if elapsed > 0 and elapsed % 30 == 0:
                self.check_wallet_sync(f"{name} split confirmation")

            time.sleep(poll_interval)

        # Timed out
        final_count = self.get_coin_count(wallet_id)
        self.log(f"⚠️ {name} transaction not confirmed after {max_wait}s (coins: {final_count}/{expected_count})")
        return False

    def _execute_single_split(self, wallet_id: int, name: str, num_coins: int, coin_size: Decimal) -> bool:
        """Split a coin into multiple smaller coins using CLI.

        Uses CLI `chia wallet coins split` which reliably broadcasts.
        RPC split_coins was tested but fails 100% of the time during
        coin prep (coin conflict after consolidation/pool creation).
        See test_real_split.py results (2026-02-27).

        CLI -a flag takes DISPLAY UNITS (XCH or CAT tokens), NOT mojos.

        Args:
            wallet_id: Wallet ID to split in
            name: Label for logging (e.g. "XCH", "CAT")
            num_coins: Number of coins to split into
            coin_size: Size of each new coin (in XCH or token units, NOT mojos)
        """
        is_cat = (wallet_id != self.xch_wallet_id)

        self.log(f"✂️ Splitting {name} into {num_coins} coins of {coin_size} each...")

        # --- Snapshot coins BEFORE split ---
        self._log_coin_snapshot(wallet_id, name, "BEFORE SPLIT")

        # --- Find the best coin to split ---
        # IMPORTANT: Don't always pick the largest! After pool creation,
        # the wallet has a POOL coin (exact size for trading) and a RESERVE coin
        # (the remainder). We want to split the POOL coin, not the reserve.
        #
        # Strategy: pick the coin whose amount is CLOSEST to (but >= ) the
        # total mojos needed for this split. This naturally targets the pool
        # coin instead of the reserve. Falls back to largest if nothing fits.
        coins = self._get_coins_via_rpc(wallet_id, name)
        if not coins:
            self.log(f"❌ No spendable coins found for {name} wallet {wallet_id}")
            return False

        # Calculate total mojos needed for this split
        if is_cat:
            _mojos_needed = int(Decimal(str(coin_size)) * (10 ** self.cat_decimals)) * num_coins
        else:
            _mojos_needed = int(Decimal(str(coin_size)) * Decimal("1000000000000")) * num_coins

        # Find best-fit coin: smallest coin that's >= total needed
        # This avoids grabbing the reserve when a pool coin exists
        coins_big_enough = [c for c in coins if c.get("amount", 0) >= _mojos_needed]
        if coins_big_enough:
            # Pick the smallest coin that fits — this is the pool coin, not the reserve
            coins_big_enough.sort(key=lambda c: c.get("amount", 0))
            target_coin = coins_big_enough[0]
        else:
            # No single coin big enough — fall back to largest
            coins.sort(key=lambda c: c.get("amount", 0), reverse=True)
            target_coin = coins[0]

        coin_id = target_coin.get("coin_id", "")
        coin_amount = target_coin.get("amount", 0)

        if not coin_id or len(coin_id.replace("0x", "")) != 64:
            self.log(f"❌ Invalid coin ID for {name}: {coin_id}")
            return False

        # --- Check if the coin is big enough for the requested split ---
        # Convert coin_size (display units) to mojos for comparison
        if is_cat:
            mojos_per_coin = int(Decimal(str(coin_size)) * (10 ** self.cat_decimals))
        else:
            mojos_per_coin = int(Decimal(str(coin_size)) * Decimal("1000000000000"))
        total_mojos_needed = mojos_per_coin * num_coins

        if coin_amount < total_mojos_needed:
            # Coin too small — reduce num_coins to what actually fits
            max_coins = coin_amount // mojos_per_coin
            if max_coins < 1:
                self.log(f"❌ Largest {name} coin ({coin_amount} mojos) too small for even 1 coin of {coin_size}")
                return False
            self.log(f"⚠️ Largest {name} coin ({coin_amount} mojos) can't fit {num_coins} × {coin_size} ({total_mojos_needed} mojos needed)")
            self.log(f"   Reducing split from {num_coins} → {max_coins} coins")
            num_coins = int(max_coins)

        self.log(f"   Target coin: {coin_id[:16]}... ({coin_amount} mojos)")
        self.log(f"   Amount per coin: {coin_size} {'CAT' if is_cat else 'XCH'} (display units)")

        # --- Get starting coin count ---
        start_count = self.get_coin_count(wallet_id)
        self.log(f"   Starting coin count: {start_count}")

        if self.is_sage:
            # --- Sage RPC split ---
            try:
                from wallet_sage import split_coins_rpc as sage_split
                bare_coin_id = coin_id.replace("0x", "")
                self.log(f"   🔄 Sage RPC split: {num_coins} coins of {coin_size} from {bare_coin_id[:16]}...")
                result = sage_split(
                    wallet_id=wallet_id,
                    target_coin_id=bare_coin_id,
                    num_coins=num_coins,
                    amount_per_coin=mojos_per_coin,
                    fee_mojos=self._split_tx_fee_mojos(),
                    is_cat=is_cat,
                )
                if self._sage_submit_succeeded(result):
                    self.log("   ✅ Sage split submitted")
                else:
                    self.log("   ❌ Sage split returned None")
                    return False
            except Exception as e:
                self.log(f"   ❌ Sage split error: {e}")
                return False
        else:
            # --- Chia CLI split (reliable — broadcasts to network every time) ---
            bare_coin_id = coin_id.replace("0x", "")

            cmd = [
                "chia", "wallet", "coins", "split",
                "-f", self.fingerprint,
                "-i", str(wallet_id),
                "-n", str(num_coins),
                "-m", "0",
                "-a", str(coin_size),
                "-t", bare_coin_id,
            ]

            self.log(f"   🔄 CLI split: -n {num_coins} -a {coin_size} -t {bare_coin_id[:16]}...")

            try:
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    **hidden_subprocess_kwargs(),
                )
                stdout, stderr = process.communicate(input="y\n", timeout=60)
                output = stdout + stderr

                if "submitted to" in output.lower() or "transaction" in output.lower():
                    self.log("   ✅ CLI split submitted and broadcast")
                else:
                    self.log(f"   ❌ CLI split failed: {output[:300]}")
                    return False

            except subprocess.TimeoutExpired:
                self.log("   ❌ CLI split timed out after 60s")
                try:
                    process.kill()
                except Exception:
                    pass
                return False
            except Exception as e:
                self.log(f"   ❌ CLI split error: {e}")
                return False

        # --- Wait for confirmation via coin count polling ---
        # Split creates num_coins new coins + 1 change coin, consuming 1 input coin.
        # Net change: coin count goes up by num_coins.
        expected_count = start_count + num_coins
        confirmed = False
        poll_start = time.time()
        max_wait = 180  # 3 minutes (test showed ~61s)

        while (time.time() - poll_start) < max_wait:
            time.sleep(5)
            current_count = self.get_coin_count(wallet_id)
            elapsed = int(time.time() - poll_start)

            if current_count >= expected_count:
                self.log(f"   ✅ Split confirmed! ({current_count} coins, {elapsed}s)")
                confirmed = True
                break

            if elapsed % 30 == 0 and elapsed > 0:
                self.log(f"   ⏳ Waiting for split confirmation... ({current_count}/{expected_count} coins, {elapsed}s)")

        if not confirmed:
            # Check if at least some coins were created
            final_count = self.get_coin_count(wallet_id)
            new_coins = final_count - start_count
            if new_coins > 0:
                self.log(f"   ⚠️ Partial split: {new_coins}/{num_coins} coins created after {max_wait}s")
                confirmed = True
            else:
                self.log(f"   ❌ Split not confirmed after {max_wait}s (still {final_count} coins)")

        # --- Snapshot coins AFTER split ---
        self._log_coin_snapshot(wallet_id, name, "AFTER SPLIT")

        return confirmed
    
    def verify_coins(self) -> Tuple[int, int]:
        """Verify final coin counts and flush to status file for GUI"""
        xch_count = self.get_confirmed_coin_count(self.xch_wallet_id)
        cat_count = self.get_confirmed_coin_count(self.cat_wallet_id)

        self._set_status_coin_counts(xch_total=xch_count, cat_total=cat_count)
        # Flush to file so GUI sees the final counts
        self.update_status(
            message=(
                f"Verified: XCH={self._prepared_coin_count_from_total(xch_count)}/{self.xch_target_coins} (+reserve), "
                f"CAT={self._prepared_coin_count_from_total(cat_count)}/{self.cat_target_coins} (+reserve)"
            )
        )

        return xch_count, cat_count
    
    def create_pools_parallel(self, xch_pool_amount: Decimal, cat_pool_amount: Decimal) -> bool:
        """
        🚀 PARALLEL OPTIMIZATION: Create both pools with staggered submission
        
        Timeline:
        - 0s: Submit XCH pool transaction
        - 5s: Submit CAT pool transaction (stagger to avoid conflicts)
        - 5s-50s: Both transactions confirm in parallel
        
        Time savings: ~45 seconds!
        """
        self.log(f"\n{'='*60}")
        self.log("⚡ PARALLEL POOL CREATION")
        self.log(f"{'='*60}")
        
        results = {'xch': False, 'cat': False}
        
        # Phase 1: Submit XCH pool (25%)
        self.update_status(PrepPhase.CREATING_POOL, 0.25, f"💰 Creating XCH pool: {self.xch_target_coins * self.xch_coin_size:.4f} XCH...")
        results['xch'] = self.create_trading_pool(self.xch_wallet_id, "XCH", xch_pool_amount)
        
        if not results['xch']:
            self.log("❌ XCH pool submission failed")
            return False
        
        # Stagger: Wait 5 seconds before CAT submission
        self.log("⏳ Staggering 5 seconds before CAT submission...")
        time.sleep(5)
        
        # Phase 2: Submit CAT pool (35%)
        self.update_status(PrepPhase.CREATING_POOL, 0.35, f"💰 Creating CAT pool: {self.cat_target_coins * self.cat_coin_size:,.0f} tokens...")
        results['cat'] = self.create_trading_pool(self.cat_wallet_id, "CAT", cat_pool_amount)
        
        if not results['cat']:
            self.log("❌ CAT pool submission failed")
            return False
        
        # Phase 3: Wait for both to confirm in parallel (45-55%)
        self.log("\n⚡ Both transactions submitted! Waiting for confirmations...")
        self.update_status(PrepPhase.CREATING_POOL, 0.45, "⏳ Waiting for blockchain confirmation... (checking every 3s)")
        
        # Poll wallet for confirmed pool coins (no fixed wait — verify via wallet)
        self.log("⏳ Polling wallet for confirmed pool coins...")
        coin_check_interval = 3
        elapsed_coin_wait = 0
        
        xch_confirmed = False
        cat_confirmed = False
        
        max_pool_wait = 300  # 5 minutes — pool transactions should confirm well within this
        while elapsed_coin_wait < max_pool_wait:
            time.sleep(coin_check_interval)
            elapsed_coin_wait += coin_check_interval

            xch_coins = self.get_coin_count(self.xch_wallet_id)
            cat_coins = self.get_coin_count(self.cat_wallet_id)
            
            # Check if each wallet has the expected new coins
            if xch_coins >= 2 and not xch_confirmed:
                xch_confirmed = True
                self.log(f"   ✅ XCH pool confirmed! ({xch_coins} coins)")
                self.update_status(
                    PrepPhase.CREATING_POOL,
                    0.50,
                    f"✅ XCH pool confirmed! ({xch_coins} coins detected)"
                )
            
            if cat_coins >= 2 and not cat_confirmed:
                cat_confirmed = True
                self.log(f"   ✅ CAT pool confirmed! ({cat_coins} coins)")
                self.update_status(
                    PrepPhase.CREATING_POOL,
                    0.53,
                    f"✅ CAT pool confirmed! ({cat_coins} coins detected)"
                )
            
            # Both confirmed?
            if xch_confirmed and cat_confirmed:
                self.log("✅ Both pools confirmed on blockchain!")
                self.update_status(
                    PrepPhase.CREATING_POOL,
                    0.55,
                    "🎉 Both pools confirmed on blockchain!"
                )
                break
            
            # Show progress
            status = []
            if not xch_confirmed:
                status.append("XCH: waiting")
            if not cat_confirmed:
                status.append("CAT: waiting")
            
            if status:
                self.log(f"   ⏳ {', '.join(status)} ({elapsed_coin_wait}s)")
            
            progress = min(0.54, 0.45 + (elapsed_coin_wait / 300) * 0.09)
            self.update_status(
                PrepPhase.CREATING_POOL,
                progress,
                f"Confirming: {'XCH ✅' if xch_confirmed else 'XCH ⏳'} {'CAT ✅' if cat_confirmed else 'CAT ⏳'}"
            )
            
            # Check wallet sync during long waits
            if elapsed_coin_wait % 15 == 0:
                self.check_wallet_sync("pool creation")
        
        # Check if we timed out before both confirmed
        if not (xch_confirmed and cat_confirmed):
            not_confirmed = []
            if not xch_confirmed:
                not_confirmed.append("XCH")
            if not cat_confirmed:
                not_confirmed.append("CAT")
            self.log(f"   ❌ Pool confirmation timed out after {max_pool_wait}s — unconfirmed: {', '.join(not_confirmed)}")
            return False
        # Final verification (both pools confirmed by polling loop)
        final_xch_coins = self.get_coin_count(self.xch_wallet_id)
        final_cat_coins = self.get_coin_count(self.cat_wallet_id)
        
        self.update_status(PrepPhase.CREATING_POOL, 0.55, "Both pools confirmed!")
        self.log(f"✅ Both pools created and confirmed: XCH: {final_xch_coins} coins, CAT: {final_cat_coins} coins")

        return True

    def split_coins_tiered(self) -> bool:
        """
        Split coins into tier-specific sizes when TIER_ENABLED.

        Process:
        1. Sort tiers largest-first (inner → mid → outer → extreme)
        2. For each tier: split XCH reserve into N coins of that tier's size
        3. Wait for confirmation between tiers (change coin needed for next split)
        4. Repeat for CAT tiers
        5. Final confirmation poll

        Each split_coin_cli() call creates uniform coins — we call it once per tier.
        """
        self.log(f"\n{'='*60}")
        self.log("🏗️ TIERED SPLITTING")
        self.log(f"{'='*60}")

        # Sort tiers largest-first (split big coins first, remainder for smaller tiers)
        tier_order = sorted(
            self.tier_xch_sizes.keys(),
            key=lambda t: self.tier_xch_sizes[t],
            reverse=True
        )

        total_tiers = len(tier_order)
        total_xch_coins = sum(self.xch_tier_counts.values())
        total_cat_coins = sum(self.cat_tier_counts.values())

        # ---- Phase 1: XCH tier splits (65% → 75%) ----
        # CRITICAL: Each split consumes the "pool" coin and creates N new coins
        # plus a CHANGE coin. The next tier MUST wait for confirmation so the
        # change coin exists — otherwise it tries to split an already-spent coin
        # and the transaction silently fails on-chain.
        self.log("\n--- XCH Tier Splits ---")
        xch_coins_created = 0
        xch_tier_results = {}  # Track success/failure per tier for retry logic

        for idx, tier_name in enumerate(tier_order):
            count = int(self.xch_tier_counts.get(tier_name, 0) or 0)
            xch_size = self.tier_xch_sizes.get(tier_name, Decimal("0"))

            if count <= 0 or xch_size <= 0:
                self.log(f"   ⏭️ Skipping {tier_name} tier (count={count}, size={xch_size})")
                xch_tier_results[tier_name] = "skipped"
                continue

            progress = 0.65 + (idx / total_tiers) * 0.10  # 65% → 75%
            self.update_status(
                PrepPhase.SPLITTING, progress,
                f"✂️ XCH {tier_name}: {count} coins × {xch_size} each..."
            )
            self.log(f"\n   🔹 XCH {tier_name}: {count} coins × {xch_size} XCH")

            success = self.split_coin_cli(
                self.xch_wallet_id, f"XCH-{tier_name}",
                count, xch_size
            )

            if not success:
                self.log(f"   ❌ XCH {tier_name} split failed — stopping XCH tiers")
                xch_tier_results[tier_name] = "failed"
                # Stop immediately: Sage may have submitted the tx without confirming.
                # Attempting the next tier risks a double-spend on the same change coin.
                self.log("   ⚠️ Stopping XCH tier splits after failure (next tiers need the change coin)")
                break

            xch_tier_results[tier_name] = "submitted"
            xch_coins_created += count

            # MANDATORY: Wait for confirmation before next tier.
            # The change coin from THIS split is the input for the NEXT split.
            # Without confirmation, the next tier would try to spend the same
            # (already-spent) coin and silently fail on-chain.
            xch_tier_tx = ""  # tx-ID-based confirmation not yet implemented; uses coin-count polling

            # Use longer timeout (10 min) — we CANNOT proceed without this
            confirmed = self._wait_for_transaction_confirmation(
                xch_tier_tx, f"XCH-{tier_name}", self.xch_wallet_id,
                expected_count=xch_coins_created, max_wait=600
            )
            if confirmed:
                self.log(f"   ✅ XCH {tier_name} confirmed ({xch_coins_created} coins). Next tier...")
                xch_tier_results[tier_name] = "confirmed"
            else:
                # Still not confirmed after 10 min — this is serious.
                # The next tier will fail too if we proceed.
                # Log it and mark as unconfirmed — the retry logic will handle it.
                self.log(f"   ⚠️ XCH {tier_name} not confirmed after 10 min!")
                self.log("   ⚠️ Stopping XCH tier splits — next tiers need the change coin")
                xch_tier_results[tier_name] = "unconfirmed"
                break  # Don't attempt more XCH tiers — they'll all fail

        # Log XCH tier summary
        self.log(f"\n   📊 XCH tier results: {xch_tier_results}")
        self._log_coin_snapshot(self.xch_wallet_id, "XCH", "AFTER XCH TIERS")

        # ---- Phase 2: CAT tier splits (75% → 85%) ----
        # Same CRITICAL rule: must wait for each tier to confirm before next.
        self.log("\n--- CAT Tier Splits ---")
        cat_coins_created = 0
        cat_tier_results = {}

        for idx, tier_name in enumerate(tier_order):
            count = int(self.cat_tier_counts.get(tier_name, 0) or 0)
            cat_size = self.tier_cat_sizes.get(tier_name, Decimal("0"))

            if count <= 0 or cat_size <= 0:
                self.log(f"   ⏭️ Skipping CAT {tier_name} tier (count={count}, size={cat_size})")
                cat_tier_results[tier_name] = "skipped"
                continue

            progress = 0.75 + (idx / total_tiers) * 0.10  # 75% → 85%
            self.update_status(
                PrepPhase.SPLITTING, progress,
                f"✂️ CAT {tier_name}: {count} coins × {cat_size:,.0f} each..."
            )
            self.log(f"\n   🔹 CAT {tier_name}: {count} coins × {cat_size:,.0f} tokens")

            success = self.split_coin_cli(
                self.cat_wallet_id, f"CAT-{tier_name}",
                count, cat_size
            )

            if not success:
                self.log(f"   ⚠️ CAT {tier_name} split reported failure — polling...")
                coins_appeared = self._poll_for_coin_count(
                    self.cat_wallet_id, f"CAT-{tier_name}",
                    cat_coins_created + count,
                    max_polls=12, poll_secs=5
                )
                if coins_appeared:
                    self.log(f"   ✅ CAT {tier_name} coins appeared despite error!")
                    cat_tier_results[tier_name] = "submitted"
                else:
                    self.log(f"   ❌ CAT {tier_name} split failed — will retry later")
                    cat_tier_results[tier_name] = "failed"
                    self.log("   ⏳ Waiting 15s for wallet to settle after failure...")
                    time.sleep(15)
                    continue
            else:
                cat_tier_results[tier_name] = "submitted"

            cat_coins_created += count

            # MANDATORY: Wait for confirmation before next tier (change coin needed)
            cat_tier_tx = ""  # tx-ID-based confirmation not yet implemented; uses coin-count polling

            confirmed = self._wait_for_transaction_confirmation(
                cat_tier_tx, f"CAT-{tier_name}", self.cat_wallet_id,
                expected_count=cat_coins_created, max_wait=600
            )
            if confirmed:
                self.log(f"   ✅ CAT {tier_name} confirmed ({cat_coins_created} coins). Next tier...")
                cat_tier_results[tier_name] = "confirmed"
            else:
                self.log(f"   ⚠️ CAT {tier_name} not confirmed after 10 min!")
                self.log("   ⚠️ Stopping CAT tier splits — next tiers need the change coin")
                cat_tier_results[tier_name] = "unconfirmed"
                break

        # Log CAT tier summary
        self.log(f"\n   📊 CAT tier results: {cat_tier_results}")
        self._log_coin_snapshot(self.cat_wallet_id, "CAT", "AFTER CAT TIERS")

        # ---- Phase 3: Check totals + retry if short (85% → 94%) ----
        self.log("\n⚡ Tier splitting complete! Checking totals...")
        self.update_status(PrepPhase.SPLITTING, 0.85, "Checking coin totals...")

        # ---- Phase 3b: Retry any short tiers (up to 2 rounds) ----
        MAX_RETRY_ROUNDS = 2
        for retry_round in range(1, MAX_RETRY_ROUNDS + 1):
            xch_count = self.get_coin_count(self.xch_wallet_id)
            cat_count = self.get_coin_count(self.cat_wallet_id)

            xch_short = total_xch_coins - xch_count
            cat_short = total_cat_coins - cat_count

            if xch_short <= 0 and cat_short <= 0:
                self.log(f"✅ All tier splits confirmed! XCH: {xch_count}, CAT: {cat_count}")
                break

            self.log(f"\n{'='*60}")
            self.log(f"🔄 RETRY ROUND {retry_round}/{MAX_RETRY_ROUNDS} — coins short: "
                     f"XCH: {xch_short}, CAT: {cat_short}")
            self.log(f"{'='*60}")
            self.update_status(PrepPhase.SPLITTING, 0.88,
                             f"🔄 Retry {retry_round}: XCH short {xch_short}, CAT short {cat_short}")

            # First: wait for wallet to be fully synced and healthy.
            # Stale transactions may still be confirming in the background.
            self.log("⏳ Checking wallet sync before retry...")
            self.check_wallet_sync("pre-retry")
            time.sleep(10)  # Extra settle time for any in-flight transactions

            # Re-check counts — the settle time may have resolved things
            xch_count = self.get_coin_count(self.xch_wallet_id)
            cat_count = self.get_coin_count(self.cat_wallet_id)
            xch_short = total_xch_coins - xch_count
            cat_short = total_cat_coins - cat_count

            if xch_short <= 0 and cat_short <= 0:
                self.log(f"✅ After settling, all coins accounted for! "
                         f"XCH: {xch_count}, CAT: {cat_count}")
                break

            self.log(f"   After settle: XCH {xch_count}/{total_xch_coins}, "
                     f"CAT {cat_count}/{total_cat_coins}")

            # Retry XCH shortfall — use the smallest tier size that's still
            # short, so we don't create oversized coins
            if xch_short > 0:
                # Pick the smallest tier size to fill the gap (safest choice —
                # smaller coins are more flexible and won't overshoot)
                smallest_tier = min(self.tier_xch_sizes.keys(),
                                   key=lambda t: self.tier_xch_sizes[t])
                retry_size = self.tier_xch_sizes[smallest_tier]
                self.log(f"\n   🔹 XCH retry: {xch_short} coins × {retry_size} each "
                         f"(using {smallest_tier} tier size)")
                self._log_coin_snapshot(self.xch_wallet_id, "XCH", "BEFORE XCH RETRY")

                success = self.split_coin_cli(
                    self.xch_wallet_id, f"XCH-retry{retry_round}",
                    xch_short, retry_size
                )
                if success:
                    # Wait for retry to confirm
                    retry_tx = ""  # tx-ID-based confirmation not yet implemented; uses coin-count polling
                    self._wait_for_transaction_confirmation(
                        retry_tx, f"XCH-retry{retry_round}",
                        self.xch_wallet_id,
                        expected_count=total_xch_coins, max_wait=300
                    )
                else:
                    self.log("   ❌ XCH retry split failed — will try again" if
                             retry_round < MAX_RETRY_ROUNDS else
                             "   ❌ XCH retry split failed — giving up")

            # Retry CAT shortfall
            if cat_short > 0:
                smallest_cat_tier = min(self.tier_cat_sizes.keys(),
                                       key=lambda t: self.tier_cat_sizes[t])
                retry_cat_size = self.tier_cat_sizes[smallest_cat_tier]
                self.log(f"\n   🔹 CAT retry: {cat_short} coins × {retry_cat_size:,.0f} each "
                         f"(using {smallest_cat_tier} tier size)")
                self._log_coin_snapshot(self.cat_wallet_id, "CAT", "BEFORE CAT RETRY")

                # Stagger after XCH retry if we just did one
                if xch_short > 0:
                    self.log("   ⏳ Staggering 5s after XCH retry...")
                    time.sleep(5)

                success = self.split_coin_cli(
                    self.cat_wallet_id, f"CAT-retry{retry_round}",
                    cat_short, retry_cat_size
                )
                if success:
                    retry_tx = ""  # tx-ID-based confirmation not yet implemented; uses coin-count polling
                    self._wait_for_transaction_confirmation(
                        retry_tx, f"CAT-retry{retry_round}",
                        self.cat_wallet_id,
                        expected_count=total_cat_coins, max_wait=300
                    )
                else:
                    self.log("   ❌ CAT retry split failed — will try again" if
                             retry_round < MAX_RETRY_ROUNDS else
                             "   ❌ CAT retry split failed — giving up")
        else:
            # Only runs if we exhausted all retry rounds without breaking
            xch_count = self.get_coin_count(self.xch_wallet_id)
            cat_count = self.get_coin_count(self.cat_wallet_id)
            self.log(f"⚠️ Tier splits still incomplete after {MAX_RETRY_ROUNDS} retries: "
                     f"XCH: {xch_count}/{total_xch_coins}, CAT: {cat_count}/{total_cat_coins}")
            self.log("   The bot can still run with fewer coins — it will just have fewer offers")

        self._log_coin_snapshot(self.xch_wallet_id, "XCH", "FINAL TIER CHECK")
        self._log_coin_snapshot(self.cat_wallet_id, "CAT", "FINAL TIER CHECK")

        return True

    def split_coins_parallel(self) -> bool:
        """
        🚀 PARALLEL OPTIMIZATION: Split both wallets with staggered submission
        
        Timeline:
        - 0s: Submit XCH split
        - 5s: Submit CAT split (stagger)
        - 5s-50s: Both splits process in parallel
        
        Time savings: ~40 seconds!
        """
        self.log(f"\n{'='*60}")
        self.log("⚡ PARALLEL SPLITTING")
        self.log(f"{'='*60}")
        
        # Phase 1: Submit XCH split (65%)
        self.update_status(PrepPhase.SPLITTING, 0.65, f"✂️ Splitting {self.xch_target_coins * self.xch_coin_size:.4f} XCH → {self.xch_target_coins} coins of {self.xch_coin_size:.4f} each...")
        xch_success = self.split_coin_cli(
            self.xch_wallet_id, "XCH", 
            self.xch_target_coins, self.xch_coin_size
        )
        
        if not xch_success:
            self.log("❌ XCH split failed")
            return False

        # Wait for XCH split to confirm before starting CAT split.
        # Uses transaction ID tracking if available, falls back to coin count.
        xch_tx_id = ""  # tx-ID-based confirmation not yet implemented; uses coin-count polling

        xch_confirmed = self._wait_for_transaction_confirmation(
            xch_tx_id, "XCH", self.xch_wallet_id,
            expected_count=self.xch_target_coins, max_wait=600
        )
        if xch_confirmed:
            self.log("✅ XCH split confirmed! Starting CAT split...")
            self._log_coin_snapshot(self.xch_wallet_id, "XCH", "AFTER XCH SPLIT")
        else:
            self.log("⚠️ XCH split not confirmed after 10 min — proceeding with CAT split anyway")

        # Phase 2: Submit CAT split (75%)
        self.update_status(PrepPhase.SPLITTING, 0.75, f"✂️ Splitting {self.cat_target_coins * self.cat_coin_size:,.0f} tokens → {self.cat_target_coins} coins of {self.cat_coin_size:,.0f} each...")
        cat_success = self.split_coin_cli(
            self.cat_wallet_id, "CAT",
            self.cat_target_coins, self.cat_coin_size
        )

        if not cat_success:
            # Don't give up — the wallet may have submitted despite the timeout.
            # Poll the wallet to check if CAT coins appeared.
            self.log("⚠️ CAT split reported failure — polling wallet for coins...")
            coins_appeared = self._poll_for_coin_count(
                self.cat_wallet_id, "CAT", self.cat_target_coins,
                max_polls=12, poll_secs=5
            )
            if coins_appeared:
                cat_check = self.get_coin_count(self.cat_wallet_id)
                self.log(f"✅ CAT coins appeared despite error! ({cat_check} coins)")
                cat_success = True
            else:
                cat_check = self.get_coin_count(self.cat_wallet_id)
                self.log(f"   CAT coins: {cat_check}/{self.cat_target_coins} — not enough yet")
                # The confirmation loop below will keep watching

        # Phase 3: Wait for CAT split to confirm (85%)
        self.log("\n⚡ Both splits submitted! Waiting for blockchain confirmation...")
        self.update_status(PrepPhase.SPLITTING, 0.85, "Both splits submitted! Waiting for confirmation...")

        cat_tx_id = ""  # tx-ID-based confirmation not yet implemented; uses coin-count polling

        cat_confirmed = self._wait_for_transaction_confirmation(
            cat_tx_id, "CAT", self.cat_wallet_id,
            expected_count=self.cat_target_coins, max_wait=900
        )
        if cat_confirmed:
            self._log_coin_snapshot(self.cat_wallet_id, "CAT", "AFTER CAT SPLIT")
        
        # Final check
        final_xch = self.get_coin_count(self.xch_wallet_id)
        final_cat = self.get_coin_count(self.cat_wallet_id)
        
        if final_xch >= self.xch_target_coins and final_cat >= self.cat_target_coins:
            self.log("✅ Both splits completed in parallel!")
            return True
        else:
            self.log(f"⚠️ Splits may still be processing: XCH={final_xch}/{self.xch_target_coins}, CAT={final_cat}/{self.cat_target_coins}")
            # Don't fail - they might just need more time
            return True
    
    def run_full_preparation(self) -> bool:
        """
        Execute complete coin preparation flow with PARALLEL OPTIMIZATION
        
        Timeline:
        - 0-5%: Analyze
        - 10-15%: Consolidate (if needed)
        - 25-55%: Create pools IN PARALLEL ⚡
        - 65-85%: Split coins IN PARALLEL ⚡
        - 95-100%: Verify
        
        Total time: ~1.7 minutes (was ~3 minutes)
        """
        try:
            self.update_status(PrepPhase.ANALYZING, 0.05, "Analyzing current state...")
            
            # Get initial state
            xch_coins = self.get_coin_count(self.xch_wallet_id)
            cat_coins = self.get_coin_count(self.cat_wallet_id)
            xch_balance = self.get_balance(self.xch_wallet_id)
            cat_balance = self.get_balance(self.cat_wallet_id)
            
            self.log(f"\n{'='*60}")
            self.log("📊 INITIAL STATE")
            self.log(f"{'='*60}")
            self.log(f"XCH: {xch_coins} coins, {xch_balance} balance")
            self.log(f"CAT: {cat_coins} coins, {cat_balance} balance")

            # Detailed coin snapshot at start — shows every coin ID and amount
            self._log_coin_snapshot(self.xch_wallet_id, "XCH", "INITIAL")
            self._log_coin_snapshot(self.cat_wallet_id, "CAT", "INITIAL")

            # Clean stale DB rows: mark ALL existing coins as 'gone' before we start.
            # Consolidation destroys every coin. The final sweep will re-insert
            # only the coins that actually exist after prep, keeping the DB clean.
            if self._db_ready:
                try:
                    from database import get_connection
                    conn = get_connection()
                    result = conn.execute(
                        "UPDATE coins SET status='gone' WHERE status='free'"
                    )
                    conn.commit()
                    stale_count = result.rowcount
                    if stale_count > 0:
                        self.log(f"   DB: marked {stale_count} stale coins as 'gone' (fresh start)")
                except Exception as e:
                    self.log(f"   DB: stale cleanup failed: {e}")
            
            self._set_status_coin_counts(xch_total=xch_coins, cat_total=cat_coins)
            self.update_status(PrepPhase.ANALYZING, 0.05,
                             f"Current: XCH={xch_coins}, CAT={cat_coins}")

            # Calculate what we need
            if self.tier_enabled:
                # Sum across all tiers: per-side count × size per tier
                xch_pool_amount = sum(
                    Decimal(str(self.xch_tier_counts.get(t, 0) or 0)) * self.tier_xch_sizes.get(t, Decimal("0"))
                    for t in self.tier_xch_sizes
                )
                cat_pool_amount = sum(
                    Decimal(str(self.cat_tier_counts.get(t, 0) or 0)) * self.tier_cat_sizes.get(t, Decimal("0"))
                    for t in self.tier_cat_sizes
                )
                self.log("\nTarget (TIERED):")
                for tn in self.tier_order:
                    xcnt = int(self.xch_tier_counts.get(tn, 0) or 0)
                    ccnt = int(self.cat_tier_counts.get(tn, 0) or 0)
                    xs = self.tier_xch_sizes.get(tn, Decimal("0"))
                    cs = self.tier_cat_sizes.get(tn, Decimal("0"))
                    self.log(f"  {tn}: {xcnt} × {xs} XCH + {ccnt} × {cs:,.0f} CAT")
                self.log(f"  Total XCH pool: {xch_pool_amount}")
                self.log(f"  Total CAT pool: {cat_pool_amount:,.0f}")
            else:
                xch_pool_amount = self.xch_target_coins * self.xch_coin_size
                cat_pool_amount = self.cat_target_coins * self.cat_coin_size
                self.log("\nTarget:")
                self.log(f"XCH: {self.xch_target_coins} × {self.xch_coin_size} = {xch_pool_amount} pool")
                self.log(f"CAT: {self.cat_target_coins} × {self.cat_coin_size} = {cat_pool_amount} pool")

            # F62 (2026-04-09): PRE-FLIGHT OVERSHOOT CHECK.
            # Verify the planned pool target actually fits the current wallet
            # (confirmed + pending). If pool_target > available, the splits
            # would run out of XCH partway through and leave a crippled
            # ladder — which is exactly what happened when the user re-ran
            # Smart Settings during a pending combine. Better to fail cleanly
            # and ask the user to re-run Smart Settings than silently prep
            # ~40% of the planned coins.
            try:
                from wallet_sage import get_wallet_balance as _get_wb
                _xch_wb = _get_wb(self.xch_wallet_id) or {}
                _cat_wb = _get_wb(self.cat_wallet_id) or {}
                _xch_bal = (_xch_wb.get("wallet_balance") or {}) if isinstance(_xch_wb, dict) else {}
                _cat_bal = (_cat_wb.get("wallet_balance") or {}) if isinstance(_cat_wb, dict) else {}
                # Prefer unconfirmed (projected post-pending), fall back to confirmed.
                _xch_total_mojos = int(
                    _xch_bal.get("unconfirmed_wallet_balance")
                    or _xch_bal.get("confirmed_wallet_balance") or 0
                )
                _cat_total_mojos = int(
                    _cat_bal.get("unconfirmed_wallet_balance")
                    or _cat_bal.get("confirmed_wallet_balance") or 0
                )
                _xch_reserve_xch = Decimal(str(os.getenv("XCH_RESERVE", "0") or "0"))
                _xch_reserve_mojos = int((_xch_reserve_xch * Decimal("1000000000000")).quantize(Decimal("1")))
                _cat_scale = Decimal(10) ** Decimal(self.cat_decimals)
                _cat_reserve_mojos = int((Decimal(str(self.cat_reserve)) * _cat_scale).quantize(Decimal("1")))
                _xch_avail_mojos = max(0, _xch_total_mojos - _xch_reserve_mojos)
                _cat_avail_mojos = max(0, _cat_total_mojos - _cat_reserve_mojos)
                _xch_pool_mojos = int((Decimal(str(xch_pool_amount)) * Decimal("1000000000000")).quantize(Decimal("1")))
                _cat_pool_mojos = int((Decimal(str(cat_pool_amount)) * _cat_scale).quantize(Decimal("1")))

                _xch_overshoot = _xch_pool_mojos > _xch_avail_mojos
                _cat_overshoot = _cat_pool_mojos > _cat_avail_mojos
                if _xch_overshoot or _cat_overshoot:
                    self.log("")
                    self.log("=" * 60)
                    self.log("OVERSHOOT: coin prep pool exceeds available wallet")
                    self.log("=" * 60)
                    if _xch_overshoot:
                        self.log(
                            f"  XCH: pool wants {_xch_pool_mojos/1e12:.4f} XCH, "
                            f"wallet has {_xch_avail_mojos/1e12:.4f} XCH avail "
                            f"(total {_xch_total_mojos/1e12:.4f} - reserve {_xch_reserve_mojos/1e12:.4f})"
                        )
                    if _cat_overshoot:
                        _sf = float(_cat_scale)
                        self.log(
                            f"  CAT: pool wants {_cat_pool_mojos/_sf:,.0f} tokens, "
                            f"wallet has {_cat_avail_mojos/_sf:,.0f} avail "
                            f"(total {_cat_total_mojos/_sf:,.0f} - reserve {_cat_reserve_mojos/_sf:,.0f})"
                        )
                    self.log("")
                    self.log("This usually means Smart Settings ran against a")
                    self.log("temporarily-drained wallet (e.g. during a pending combine).")
                    self.log("Please restart the bot, wait for wallet to settle (all txs")
                    self.log("confirmed), then re-run Smart Settings and coin prep.")
                    self.log("=" * 60)
                    # Mark the run as failed and return. The status file
                    # shows the error so the GUI can render the message.
                    try:
                        self.update_status(
                            PrepPhase.ERROR,
                            0.05,
                            "Coin prep pool exceeds available wallet — re-run Smart Settings",
                            error="pool_exceeds_avail"
                        )
                    except Exception:
                        pass
                    return
            except Exception as _oe:
                # Don't fail the whole run on a precheck error; just log
                # and continue with existing behaviour.
                self.log(f"   (pool overshoot precheck skipped: {_oe})")

            # Phase 1: ⚡ PARALLEL CONSOLIDATION (10-15%)
# STEP 0: Cancel all open offers with verification
            self.update_status(PrepPhase.CONSOLIDATING, 0.05, "🗑️ Cancelling open offers...")
            cancel_success = self.cancel_all_offers()

            if not cancel_success:
                self.log("Offer cancellation failed - aborting coin prep before reshaping coins")
                try:
                    self.update_status(
                        PrepPhase.ERROR,
                        0.05,
                        "Offer cancellation failed - coin prep aborted",
                        error="offer_cancel_failed",
                    )
                except Exception:
                    pass
                return

            # Re-classify existing tier_spare coins against the CURRENT tier
            # sizes from cfg. Without this, coins designated by a previous
            # prep run keep their original assigned_tier even if Smart
            # Settings has changed the tier sizes since then — the bot
            # then fires tier_size_drift on the first cycle because the
            # measured median (stale-tier coins) doesn't match the live
            # target. The reclassify pass cleans up that residue so
            # downstream split decisions count only coins that genuinely
            # fit the current tier shape.
            try:
                from coin_manager import reclassify_tier_spare_coins
                _moved = reclassify_tier_spare_coins() or {}
                _changed = int(_moved.get("reclassified", 0)) + int(_moved.get("to_dust", 0))
                if _changed > 0:
                    self.log(
                        f"📋 Reclassified {_changed} existing coin(s) against current tier sizes "
                        f"({_moved.get('reclassified', 0)} re-tiered, "
                        f"{_moved.get('to_dust', 0)} demoted to dust)"
                    )
                else:
                    self.log("📋 Existing tier coins already match current tier sizes")
            except Exception as _reclass_err:
                self.log(f"⚠️ Pre-prep reclassify skipped: {_reclass_err}")

            # Re-count coins AFTER cancellation — cancelled offers release locked coins
            # The initial coin count (before cancellation) is stale and unreliable
            self.log("\n📊 Re-counting coins after cancellation...")
            time.sleep(5)  # Brief pause for wallet to update
            xch_coins = self.get_coin_count(self.xch_wallet_id)
            cat_coins = self.get_coin_count(self.cat_wallet_id)
            self.log(f"   XCH: {xch_coins} coins (post-cancel)")
            self.log(f"   CAT: {cat_coins} coins (post-cancel)")

            # Push updated counts to status so GUI reflects post-cancellation reality
            self._set_status_coin_counts(xch_total=xch_coins, cat_total=cat_coins)
            self.update_status(message=f"Post-cancel: XCH={xch_coins}, CAT={cat_coins}")

            self.log(f"\n{'='*60}")
            self.log("⚡ PARALLEL CONSOLIDATION")
            self.log(f"{'='*60}")

            # ALWAYS consolidate after cancellation (cancels release locked coins)
            xch_needs_consolidation = xch_coins > 1 or xch_coins == 0
            cat_needs_consolidation = cat_coins > 1

            # Track whether consolidation was actually submitted
            xch_consolidation_submitted = False
            cat_consolidation_submitted = False

            fee_enabled = self._tx_fee_mojos() > 0
            if fee_enabled and cat_needs_consolidation:
                self.log("Fee-enabled consolidation: CAT first so XCH fee liquidity stays available.")

            def _run_consolidation_step(name: str, wallet_id: int, coin_count: int,
                                        needs_consolidation: bool, progress: float):
                nonlocal xch_consolidation_submitted, cat_consolidation_submitted

                if needs_consolidation:
                    target_text = "1-2 coins" if name == "XCH" and fee_enabled else "1 coin"
                    self.update_status(
                        PrepPhase.CONSOLIDATING,
                        progress,
                        f"Consolidating {coin_count} {name} coins -> {target_text}..."
                    )
                    if not self.consolidate_wallet(wallet_id, name):
                        raise Exception(f"{name} consolidation failed")
                    if name == "XCH":
                        xch_consolidation_submitted = True
                    else:
                        cat_consolidation_submitted = True
                else:
                    self.log(f"{name} already consolidated ({coin_count} coin)")
                    self.update_status(
                        PrepPhase.CONSOLIDATING,
                        progress,
                        f"{name} already consolidated"
                    )

            if fee_enabled and cat_needs_consolidation:
                _run_consolidation_step("CAT", self.cat_wallet_id, cat_coins, cat_needs_consolidation, 0.10)
                if xch_needs_consolidation:
                    self.log("Staggering 5 seconds before XCH consolidation...")
                    time.sleep(5)
                _run_consolidation_step("XCH", self.xch_wallet_id, xch_coins, xch_needs_consolidation, 0.12)
            else:
                _run_consolidation_step("XCH", self.xch_wallet_id, xch_coins, xch_needs_consolidation, 0.10)
                if cat_needs_consolidation:
                    self.log("Staggering 5 seconds before CAT consolidation...")
                    time.sleep(5)
                _run_consolidation_step("CAT", self.cat_wallet_id, cat_coins, cat_needs_consolidation, 0.12)

            # Log parallel status
            if xch_needs_consolidation or cat_needs_consolidation:
                self.log("\n⚡ Both consolidations submitted! Waiting for confirmations...")
            
            # Verify BOTH wallets are consolidated before proceeding
            self.log("\nVerifying consolidation complete...")
            self.update_status(PrepPhase.CONSOLIDATING, 0.20, "Verifying both wallets consolidated...")

            max_verify_wait = 300  # 5 minute timeout - don't wait forever
            verify_interval = 5
            elapsed_verify = 0
            allow_extra_xch_fee_coin = self._tx_fee_mojos() > 0
            xch_target_label = "1-2" if allow_extra_xch_fee_coin else "1"

            prev_xch_check = None
            prev_cat_check = None
            stuck_count = 0

            while True:
                if max_verify_wait and elapsed_verify >= max_verify_wait:
                    self.log(f"Consolidation verification timeout after {elapsed_verify}s")
                    self.log(f"   XCH: {self.get_coin_count(self.xch_wallet_id)} coins, CAT: {self.get_coin_count(self.cat_wallet_id)} coins")
                    self.log("   Continuing anyway - transactions may still be pending")
                    break

                xch_check = self.get_coin_count(self.xch_wallet_id)
                cat_check = self.get_coin_count(self.cat_wallet_id)

                self._set_status_coin_counts(xch_total=xch_check, cat_total=cat_check)
                self.update_status(message=f"Consolidating: XCH={xch_check}, CAT={cat_check}")

                xch_ready = (1 <= xch_check <= 2) if allow_extra_xch_fee_coin else (xch_check == 1)
                if xch_ready and cat_check == 1:
                    self.log(f"Consolidation verified! XCH: {xch_check} coin(s), CAT: 1 coin")
                    break

                if prev_xch_check is not None and prev_cat_check is not None:
                    if xch_check == prev_xch_check and cat_check == prev_cat_check:
                        stuck_count += 1
                    else:
                        stuck_count = 0

                prev_xch_check = xch_check
                prev_cat_check = cat_check

                if stuck_count >= 12:
                    if xch_check > (2 if allow_extra_xch_fee_coin else 1) and not xch_consolidation_submitted:
                        self.log(f"\nXCH has {xch_check} coins but no consolidation was submitted!")
                        self.log("   Submitting XCH consolidation now...")
                        if self.consolidate_wallet(self.xch_wallet_id, "XCH"):
                            xch_consolidation_submitted = True
                            stuck_count = 0
                        else:
                            self.log("   XCH consolidation failed - will retry")

                    if cat_check > 1 and not cat_consolidation_submitted:
                        self.log(f"\nCAT has {cat_check} coins but no consolidation was submitted!")
                        self.log("   Submitting CAT consolidation now...")
                        if xch_consolidation_submitted:
                            time.sleep(5)
                        if self.consolidate_wallet(self.cat_wallet_id, "CAT"):
                            cat_consolidation_submitted = True
                            stuck_count = 0
                        else:
                            self.log("   CAT consolidation failed - will retry")

                    if stuck_count >= 12:
                        if xch_consolidation_submitted and cat_consolidation_submitted:
                            self.log("   Both consolidations submitted - still waiting for blockchain confirmation...")
                        stuck_count = 0

                if elapsed_verify > 0:
                    if elapsed_verify < 60:
                        time_str = f"{elapsed_verify}s"
                    else:
                        mins = elapsed_verify // 60
                        secs = elapsed_verify % 60
                        time_str = f"{mins}m {secs}s"
                    self.log(f"Waiting for consolidation... XCH: {xch_check}/{xch_target_label}, CAT: {cat_check}/1 ({time_str})")
                    self.update_status(
                        PrepPhase.CONSOLIDATING,
                        0.20,
                        f"Verifying: XCH {xch_check}/{xch_target_label}, CAT {cat_check}/1 ({time_str})"
                    )

                time.sleep(verify_interval)
                elapsed_verify += verify_interval

                if elapsed_verify % 15 == 0:
                    self.check_wallet_sync("consolidation")

            # Final check
            final_xch = self.get_coin_count(self.xch_wallet_id)
            final_cat = self.get_coin_count(self.cat_wallet_id)
            
            # Update status with confirmed counts after consolidation
            self._set_status_coin_counts(xch_total=final_xch, cat_total=final_cat)
            
            # Write to file so GUI sees updated counts
            self.update_status(
                PrepPhase.CONSOLIDATING,
                0.20,
                f"Consolidation complete! XCH: {final_xch} coin(s), CAT: {final_cat} coin(s)"
            )
            
            xch_final_ready = (1 <= final_xch <= 2) if self._tx_fee_mojos() > 0 else (final_xch == 1)
            if not xch_final_ready or final_cat != 1:
                self.log(f"⚠️ Consolidation verification timeout: XCH={final_xch}, CAT={final_cat}")
                self.log("   Continuing anyway - transactions may still be pending")

            # Designate consolidated coins as reserve in DB
            # (they'll get consumed by pool creation, but this establishes the DB record)
            self._designate_reserve_after_consolidation(self.xch_wallet_id, "xch")
            self._designate_reserve_after_consolidation(self.cat_wallet_id, "cat")

            # ---------------------------------------------------------------
            # CRITICAL: Wait for consolidated coins to become SPENDABLE.
            #
            # This is a defensive final readiness check before the multi-send
            # splitting phase. We want the spendable balance to agree with the
            # coin count so the next split does not hit
            # "Coin selection error: no spendable coins".
            # ---------------------------------------------------------------
            self.log("\n🔍 Waiting for consolidated coins to become spendable...")
            self.update_status(PrepPhase.CONSOLIDATING, 0.22,
                             "🔍 Waiting for spendable balance...")

            spendable_wait_max = 120  # 2 minutes should be plenty
            spendable_wait_elapsed = 0
            spendable_interval = 5

            # Threshold: wait until spendable balance is at least 85% of what the
            # pool needs.  Using > 0 is not enough — the reserve coin (0.21 XCH)
            # satisfies > 0 immediately even while the consolidated coin is still
            # pending on-chain, causing a false-pass and a "balance too low" crash.
            _xch_spendable_threshold = xch_pool_amount * Decimal("0.85")
            _cat_spendable_threshold = cat_pool_amount * Decimal("0.85")

            while spendable_wait_elapsed < spendable_wait_max:
                xch_bal = self.get_balance(self.xch_wallet_id)
                cat_bal = self.get_balance(self.cat_wallet_id)

                xch_ready = xch_bal >= _xch_spendable_threshold
                cat_ready = cat_bal >= _cat_spendable_threshold

                # Log every poll so the operator can see progress.
                self.log(
                    f"   Waiting for consolidated XCH to become spendable: "
                    f"{xch_bal} / {xch_pool_amount} XCH "
                    f"({'ready' if xch_ready else 'pending'}), "
                    f"CAT: {cat_bal:,.0f} / {cat_pool_amount:,.0f} "
                    f"({'ready' if cat_ready else 'pending'}) "
                    f"({spendable_wait_elapsed}s)"
                )

                if xch_ready and cat_ready:
                    self.log(f"   ✅ Spendable balances confirmed after {spendable_wait_elapsed}s "
                             f"— XCH: {xch_bal}, CAT: {cat_bal:,.0f}")
                    break

                if spendable_wait_elapsed > 0 and spendable_wait_elapsed % 15 == 0:
                    self.update_status(PrepPhase.CONSOLIDATING, 0.22,
                                     f"⏳ Waiting for spendable balance ({spendable_wait_elapsed}s)...")

                time.sleep(spendable_interval)
                spendable_wait_elapsed += spendable_interval
            else:
                # Exhausted wait — the consolidated coin is still pending (blockchain
                # congestion).  Log a clear warning and continue — the balance check
                # below will use whatever is actually spendable, and the multi_send
                # retry logic will handle it if coins are still not available.
                xch_bal = self.get_balance(self.xch_wallet_id)
                cat_bal = self.get_balance(self.cat_wallet_id)
                self.log(
                    f"   ⚠️ Consolidated coin not yet spendable after {spendable_wait_max}s "
                    f"— blockchain may be slow. "
                    f"Available: XCH {xch_bal} / {xch_pool_amount}, "
                    f"CAT {cat_bal:,.0f} / {cat_pool_amount:,.0f}. "
                    f"Continuing with available balance."
                )

            # --- Balance check: cap pool amounts at actual spendable balance ---
            # After consolidation we have all coins unlocked, so re-check balance
            post_xch_balance = self.get_balance(self.xch_wallet_id)
            post_cat_balance = self.get_balance(self.cat_wallet_id)
            self.log("\n📊 Post-consolidation balances:")
            self.log(f"   XCH: {post_xch_balance}  |  Pool target: {xch_pool_amount}")
            self.log(f"   CAT: {post_cat_balance:,.0f}  |  Pool target: {cat_pool_amount:,.0f}")

            if xch_pool_amount > post_xch_balance and post_xch_balance > 0:
                self.log(f"⚠️ XCH pool ({xch_pool_amount}) exceeds balance ({post_xch_balance}) — capping to balance")
                # Leave at least XCH_RESERVE as the floor (minimum 0.001 for tx fee)
                _xch_floor = max(self.xch_reserve, Decimal("0.001"))
                xch_pool_amount = post_xch_balance - _xch_floor
                if xch_pool_amount <= 0:
                    raise Exception(f"XCH balance too low ({post_xch_balance}) for pool creation")
                self.log(f"   Adjusted XCH pool: {xch_pool_amount}")

                # CRITICAL: Also reduce XCH-side tier COUNTS proportionally so the
                # actual per-tier payments fit within the capped XCH balance. Only
                # the XCH side is scaled — CAT counts (sell ladder) are unaffected
                # by an XCH shortfall.
                if self.tier_enabled and self.tier_xch_sizes and self.xch_tier_counts:
                    total_xch_mojos_orig = Decimal("0")
                    for tn in self.xch_tier_counts:
                        xs = self.tier_xch_sizes.get(tn, Decimal("0"))
                        cnt = int(self.xch_tier_counts.get(tn, 0) or 0)
                        total_xch_mojos_orig += xs * Decimal("1000000000000") * cnt

                    if total_xch_mojos_orig > 0:
                        available_mojos = int(xch_pool_amount * Decimal("1000000000000"))
                        scale = Decimal(str(available_mojos)) / total_xch_mojos_orig
                        self.log(f"   Scaling XCH tier counts by {float(scale):.2f} to fit XCH balance")

                        new_xch_counts = {}
                        for tn in self.xch_tier_counts:
                            orig_count = int(self.xch_tier_counts.get(tn, 0) or 0)
                            new_count = max(1, int(orig_count * float(scale))) if orig_count > 0 else 0
                            new_xch_counts[tn] = new_count
                        self.xch_tier_counts = new_xch_counts

                        # Refresh unified tier_counts (max of both sides)
                        all_tn = set(self.xch_tier_counts) | set(self.cat_tier_counts)
                        self.tier_counts = {
                            tn: max(int(self.xch_tier_counts.get(tn, 0) or 0),
                                    int(self.cat_tier_counts.get(tn, 0) or 0))
                            for tn in all_tn
                        }
                        self.xch_target_coins = sum(self.xch_tier_counts.values())
                        self._refresh_coin_targets()
                        self.log(f"   Adjusted XCH tier counts: {self.xch_tier_counts} (XCH total: {self.xch_target_coins})")

                        # Recalculate actual XCH pool amount with new counts
                        new_xch_total = Decimal("0")
                        for tn, cnt in self.xch_tier_counts.items():
                            xs = self.tier_xch_sizes.get(tn, Decimal("0"))
                            new_xch_total += xs * cnt
                        self.log(f"   New XCH total: {new_xch_total} (balance: {post_xch_balance})")
                        xch_pool_amount = new_xch_total

            if cat_pool_amount > post_cat_balance and post_cat_balance > 0:
                self.log(f"⚠️ CAT pool ({cat_pool_amount:,.0f}) exceeds balance ({post_cat_balance:,.0f}) — capping to balance")
                # Leave at least CAT_RESERVE as the floor (minimum 1 unit for tx)
                _cat_floor = max(self.cat_reserve, Decimal("1"))
                cat_pool_amount = post_cat_balance - _cat_floor
                if cat_pool_amount <= 0:
                    raise Exception(f"CAT balance too low ({post_cat_balance}) for pool creation")
                self.log(f"   Adjusted CAT pool: {cat_pool_amount:,.0f}")

                # CRITICAL: Also reduce CAT-side tier COUNTS proportionally so the
                # actual per-tier payments fit within the capped CAT balance.
                # Only the CAT side is scaled.
                if self.tier_enabled and self.tier_cat_sizes and self.cat_tier_counts:
                    total_cat_mojos_orig = Decimal("0")
                    for tn in self.cat_tier_counts:
                        cs = self.tier_cat_sizes.get(tn, Decimal("0"))
                        cnt = int(self.cat_tier_counts.get(tn, 0) or 0)
                        total_cat_mojos_orig += cs * (10 ** self.cat_decimals) * cnt

                    if total_cat_mojos_orig > 0:
                        available_mojos = int(cat_pool_amount * (10 ** self.cat_decimals))
                        scale = Decimal(str(available_mojos)) / total_cat_mojos_orig
                        self.log(f"   Scaling CAT tier counts by {float(scale):.2f} to fit CAT balance")

                        new_cat_counts = {}
                        for tn in self.cat_tier_counts:
                            orig_count = int(self.cat_tier_counts.get(tn, 0) or 0)
                            new_count = max(1, int(orig_count * float(scale))) if orig_count > 0 else 0
                            new_cat_counts[tn] = new_count
                        self.cat_tier_counts = new_cat_counts

                        all_tn = set(self.xch_tier_counts) | set(self.cat_tier_counts)
                        self.tier_counts = {
                            tn: max(int(self.xch_tier_counts.get(tn, 0) or 0),
                                    int(self.cat_tier_counts.get(tn, 0) or 0))
                            for tn in all_tn
                        }
                        self.cat_target_coins = sum(self.cat_tier_counts.values())
                        self._refresh_coin_targets()
                        self.log(f"   Adjusted CAT tier counts: {self.cat_tier_counts} (CAT total: {self.cat_target_coins})")

                        new_cat_total = Decimal("0")
                        for tn, cnt in self.cat_tier_counts.items():
                            cs = self.tier_cat_sizes.get(tn, Decimal("0"))
                            new_cat_total += cs * cnt
                        self.log(f"   New CAT total: {new_cat_total:,.0f} (balance: {post_cat_balance:,.0f})")
                        cat_pool_amount = new_cat_total

            # Phase 2+3: Create pools and split coins
            if self.is_sage and self.tier_enabled:
                # ── SPENDABILITY GATE ────────────────────────────────────
                # After consolidation, the coin may appear in get_coins(owned)
                # before it's truly selectable/spendable. If we fire multi_send
                # too early, Sage returns "no spendable coins."  Wait until:
                #   1) No pending transactions remain
                #   2) XCH consolidated coin shows up in selectable-only query
                # This gate adds ~0-30s but prevents the "no spendable coins"
                # failure that occurs when multi_send races consolidation.
                # ─────────────────────────────────────────────────────────
                self.log("\n⏳ Spendability gate: waiting for consolidated coins to become selectable...")
                from wallet_sage import get_pending_transactions, get_selectable_coins_only
                gate_timeout = 120  # 2 minutes max
                gate_poll = 5
                gate_ok = False
                for g in range(gate_timeout // gate_poll):
                    # Check 1: no pending transactions
                    pending = get_pending_transactions()
                    if len(pending) > 0:
                        if g > 0 and g % 6 == 0:
                            self.log(f"   ⏳ {g * gate_poll}s — {len(pending)} pending txns...")
                        time.sleep(gate_poll)
                        continue

                    # Check 2: XCH coin is selectable (not just owned)
                    xch_sel = get_selectable_coins_only(self.xch_wallet_id)
                    xch_sel_count = 0
                    if xch_sel and isinstance(xch_sel, dict):
                        xch_records = xch_sel.get("confirmed_records") or xch_sel.get("records") or []
                        xch_sel_count = sum(1 for r in xch_records
                                           if r.get("coin", {}).get("amount", 0) > 0)
                    if xch_sel_count >= 1:
                        self.log(f"   ✅ Consolidated coins selectable after {g * gate_poll}s "
                                 f"(XCH selectable: {xch_sel_count})")
                        gate_ok = True
                        break

                    if g > 0 and g % 4 == 0:
                        self.log(f"   ⏳ {g * gate_poll}s — XCH selectable: {xch_sel_count} (waiting for ≥1)...")
                    time.sleep(gate_poll)

                if not gate_ok:
                    self.log(f"   ⚠️ Spendability gate timed out after {gate_timeout}s — attempting multi_send anyway")

                # Sage + tiered: combined flow — create per-tier pools, then split each equally
                # (Sage's /split creates EQUAL pieces, so we need per-tier pool coins)
                if not self.create_and_split_tier_pools_sage(xch_pool_amount, cat_pool_amount):
                    raise Exception("Sage tier pool creation + splitting failed")
            else:
                # Original flow: single pool → tier split (Chia CLI) or uniform split
                if not self.create_pools_parallel(xch_pool_amount, cat_pool_amount):
                    raise Exception("Pool creation failed")

                # Phase 3: Split trading pools (65-85%)
                if self.tier_enabled:
                    # Tier-aware splitting: different sizes per tier
                    if not self.split_coins_tiered():
                        raise Exception("Tiered splitting failed")
                else:
                    # Uniform splitting: all coins same size
                    if not self.split_coins_parallel():
                        raise Exception("Splitting failed")
            
            # Phase 4: Verify (95%)
            # Sage tiered path already verified counts in create_and_split_tier_pools_sage()
            # so skip the redundant stability poll + snapshot for that path
            sage_tiered = self.is_sage and self.tier_enabled

            if not sage_tiered:
                # Non-Sage or non-tiered: poll wallet until coin counts stabilise
                self.update_status(PrepPhase.VERIFYING, 0.95, "🔍 Verifying final coin counts...")
                prev_xch, prev_cat = 0, 0
                for verify_round in range(12):  # Up to 60s
                    xch_now = self.get_coin_count(self.xch_wallet_id)
                    cat_now = self.get_coin_count(self.cat_wallet_id)
                    if xch_now == prev_xch and cat_now == prev_cat and verify_round > 0:
                        self.log(f"✅ Coin counts stable: XCH={xch_now}, CAT={cat_now}")
                        break
                    prev_xch, prev_cat = xch_now, cat_now
                    time.sleep(5)

            xch_final, cat_final = self.verify_coins()
            if sage_tiered and self._merge_xch_fee_change_into_reserve():
                xch_final, cat_final = self.verify_coins()


            # Final DB designation sweep — ensures every coin has a role
            self._designate_final_sweep()

            # End-of-prep drift verification. After every split, the
            # designations should match the current tier sizes. If anything
            # still drifts, log it as a hard error so the regression shows
            # up next test run. Missing this assertion is what let the
            # SBX→MZ residue cause a silent same-cycle drift warning.
            try:
                from coin_manager import check_tier_size_drift_standalone
                _post_drift = check_tier_size_drift_standalone() or []
                if _post_drift:
                    _summary = ", ".join(
                        f"{f['side']}/{f['tier']}={f['ratio']}× (n={f['coin_count']})"
                        for f in _post_drift
                    )
                    self.log(f"❌ POST-PREP DRIFT: {_summary} — coin prep finished but "
                             f"some tier sizes still don't match. Bot will refuse "
                             f"to start with this state.")
                    try:
                        from database import log_event as _le
                        _le("error", "tier_size_post_prep_drift",
                            f"Coin prep finished with residual drift: {_summary}")
                    except Exception:
                        pass
                else:
                    self.log("✅ Post-prep drift check: every tier matches its target size")
            except Exception as _drift_err:
                self.log(f"⚠️ Post-prep drift check skipped: {_drift_err}")

            self.log(f"\n{'='*60}")
            self.log("🎉 COIN PREPARATION COMPLETE!")
            self.log(f"{'='*60}")
            self.log(
                f"XCH: {xch_final} total coins "
                f"({self.xch_target_coins} prepared + reserve; expected total {self.xch_expected_total_coins})"
            )
            self.log(
                f"CAT: {cat_final} total coins "
                f"({self.cat_target_coins} prepared + reserve; expected total {self.cat_expected_total_coins})"
            )

            if not sage_tiered:
                # Only log full snapshot for non-Sage path (Sage already logged it)
                self._log_coin_snapshot(self.xch_wallet_id, "XCH", "FINAL")
                self._log_coin_snapshot(self.cat_wallet_id, "CAT", "FINAL")

            # Suppress the deposit-advisory alert for every coin the prep
            # run designated as reserve. Without this the advisor treats
            # the post-prep pool coins as fresh external deposits and asks
            # the operator to allocate them, even though they were already
            # accounted for in the prep plan.
            #
            # We dedup TWO ways so suppression survives common post-prep
            # mutations:
            #
            #   1. by coin_id — fast O(1) lookup for the unchanged case
            #   2. by post-prep reserve TOTAL amount — survives coin_id
            #      changes from later fee-change merges, topup splits,
            #      and full DB resets (which wipe the coin_id list but
            #      not the saved totals if the user backs them up). The
            #      advisory uses the recorded total to lift its threshold
            #      so the prep-leftover coin is never alerted on, even
            #      under XCH_RESERVE=0 where the configured-reserve
            #      threshold offers no protection.
            try:
                from database import set_setting as _set_setting
                for _wtype in ("xch", "cat"):
                    _coins = get_reserve_coins(_wtype) or []
                    _total_mojos = 0
                    for _rc in _coins:
                        _mark_coin_already_advised(_rc.get("coin_id") or "")
                        try:
                            _total_mojos += int(_rc.get("amount_mojos") or 0)
                        except Exception:
                            pass
                    # Record the post-prep reserve total in mojos so the
                    # advisor can recognise prep's intentional leftover
                    # by amount even after the coin_id has changed or
                    # the dedup list has been wiped by a DB reset.
                    try:
                        _set_setting(
                            f"last_prep_reserve_total_mojos_{_wtype}",
                            str(int(_total_mojos)),
                        )
                    except Exception:
                        pass
            except Exception as _e:
                self.log(f"   DB: deposit-advisory backfill skipped: {_e}")

            # Complete!
            self.update_status(
                PrepPhase.COMPLETE,
                1.0,
                f"Complete! XCH: {self._prepared_coin_count_from_total(xch_final)}/{self.xch_target_coins} (+reserve), "
                f"CAT: {self._prepared_coin_count_from_total(cat_final)}/{self.cat_target_coins} (+reserve)"
            )

            # Save successful prep settings so the GUI can detect "already prepped"
            try:
                # Derive max offers from actual target (not stale .env).
                # Tiered: target = tier_count_total = per_side × 2 × multiplier.
                # With multiplier=1, per_side = target / 2.
                _per_side = self.cat_target_coins // 2 if self.cat_target_coins > 0 else 0
                last_prep = {
                    "tier_enabled": self.tier_enabled,
                    "trade_size": float(getattr(self, "offer_xch_size", self.xch_coin_size)),
                    "prepared_trade_size_xch": float(self.xch_coin_size),
                    "prep_headroom_pct": float(self.coin_prep_headroom_pct),
                    "max_buy": _per_side,
                    "max_sell": _per_side,
                    "cat_asset_id": os.getenv("CAT_ASSET_ID", ""),
                    "xch_coins_total": xch_final,
                    "cat_coins_total": cat_final,
                    "xch_target": self.xch_target_coins,
                    "cat_target": self.cat_target_coins,
                    "timestamp": time.time(),
                }
                if self.tier_enabled:
                    last_prep["tier_sizes_xch"] = {k: float(v) for k, v in self.tier_xch_sizes.items()}
                    last_prep["offer_tier_sizes_xch"] = {
                        k: float(v) for k, v in self.offer_tier_xch_sizes.items()
                    }
                    last_prep["tier_sizes_cat"] = {k: float(v) for k, v in self.tier_cat_sizes.items()}
                    last_prep["tier_counts"] = dict(self.tier_counts)
                    last_prep["tier_counts_xch"] = dict(self.xch_tier_counts)
                    last_prep["tier_counts_cat"] = dict(self.cat_tier_counts)
                else:
                    last_prep["xch_coin_size"] = float(self.xch_coin_size)
                    last_prep["cat_coin_size"] = float(self.cat_coin_size)

                last_prep["designations_written"] = self._db_ready

                try:
                    from user_paths import data_dir as _dd
                    prep_json_path = os.path.join(_dd(), "coin_prep_last.json")
                except Exception:
                    prep_json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coin_prep_last.json")
                with open(prep_json_path, "w", encoding="utf-8") as f:
                    json.dump(last_prep, f, indent=2)
                self.log("💾 Saved prep settings to coin_prep_last.json")
            except Exception as e:
                self.log(f"⚠️ Could not save prep settings: {e}")

            return True
            
        except Exception as e:
            self.log(f"\n❌ ERROR: {e}")
            self.update_status(PrepPhase.ERROR, 0.0, f"Error: {e}", error=str(e))
            return False




def parse_arguments():
    """
    Parse command-line arguments — ONLY used for explicit overrides.

    The worker's __init__ derives all settings from GUI config
    (DEFAULT_TRADE_XCH, MAX_ACTIVE_BUY_OFFERS, etc.) automatically.
    CLI args are only needed when the caller (coin_manager.py) wants
    to force specific values.
    """
    parser = argparse.ArgumentParser(
        description='Intelligent Coin Preparation Worker - Dynamic Settings'
    )

    # All defaults are None so we can detect "not explicitly provided"
    parser.add_argument('--xch-target', type=int, default=None,
                        help='Override: target number of XCH coins')
    parser.add_argument('--xch-size', type=float, default=None,
                        help='Override: size per XCH coin')
    parser.add_argument('--cat-target', type=int, default=None,
                        help='Override: target number of CAT coins')
    parser.add_argument('--cat-size', type=float, default=None,
                        help='Override: size per CAT coin')
    parser.add_argument('--cat-wallet', type=int, default=None,
                        help='Override: CAT wallet ID')
    parser.add_argument('--fingerprint', type=str, default=None,
                        help='Override: wallet fingerprint')
    parser.add_argument('--tier-sizes', type=str, default=None,
                        help='Tier XCH sizes (legacy, shared): inner=1.0,mid=0.5,outer=0.25,extreme=0.1')
    # F62 (2026-04-09): per-side tier sizes. When both are provided they
    # override --tier-sizes — XCH coins are prepped at buy sizes and CAT
    # coins are prepped at sell sizes, enabling asymmetric ladders.
    parser.add_argument('--buy-tier-sizes', type=str, default=None,
                        help='Per-side XCH (buy) tier sizes: inner=0.4,mid=0.9,outer=1.6,extreme=2.8[,sniper=0.01][,fees=0.001]')
    parser.add_argument('--sell-tier-sizes', type=str, default=None,
                        help='Per-side CAT (sell, in XCH-equiv) tier sizes: inner=1.2,mid=0.7,outer=0.36,extreme=0.16[,sniper=0.01]')
    parser.add_argument('--tier-counts', type=str, default=None,
                        help='Tier coin counts (legacy, applied to BOTH sides): inner=4,mid=16,outer=16,extreme=14')
    parser.add_argument('--tier-counts-xch', type=str, default=None,
                        help='Per-side XCH tier counts (buy ladder): inner=4,mid=16,outer=16,extreme=14[,sniper=10][,fees=20]')
    parser.add_argument('--tier-counts-cat', type=str, default=None,
                        help='Per-side CAT tier counts (sell ladder): inner=4,mid=16,outer=16,extreme=14')
    parser.add_argument('--prep-headroom-pct', type=float, default=None,
                        help='Extra % headroom to add to prepared coins above live offer size')
    parser.add_argument('--live-price', type=str, default=None,
                        help='Live XCH/CAT mid price (weighted Tibet+Dexie). Overrides the '
                             'subprocess\' own Dexie last_price fetch so prep sizes CAT coins '
                             'against the same mid the bot uses. Format: decimal string.')
    parser.add_argument('--run-id', type=str, default=None,
                        help='Unique run ID for status file tracking (prevents stale status reads)')

    return parser.parse_args()

def main():
    """
    Main entry point.

    The worker's __init__ now intelligently derives settings from
    GUI config (DEFAULT_TRADE_XCH, MAX_ACTIVE_BUY_OFFERS etc.).
    CLI args only override when explicitly passed by the caller.
    """
    # Mark this process as the real worker subprocess so CoinPrepWorker
    # enables its HTTP log-forwarder. Without this flag, tests that
    # instantiate CoinPrepWorker directly would also start the forwarder
    # and spam a running bot's live log feed.
    os.environ["_CLI_WORKER_SUBPROCESS"] = "1"

    sys.stdout = ApiMirrorStream(sys.__stdout__, "coin_prep_raw", "info")
    sys.stderr = ApiMirrorStream(sys.__stderr__, "coin_prep_raw", "warning")

    print("="*60)
    print("🪙 Intelligent Coin Preparation Worker")
    print("⚡ PARALLEL OPTIMIZATION + SMART CONFIG")
    print("="*60)
    print()

    # Parse CLI arguments — only explicit overrides (defaults are all None)
    args = parse_arguments()

    # Only set env vars when a value was EXPLICITLY passed on command line.
    # Without this guard, stale .env defaults would override the smart
    # derivation in __init__ (the exact bug we're fixing).
    overrides = []
    if args.xch_target is not None:
        # Use _CLI_ prefix so it can't clash with stale .env values
        # loaded by load_dotenv() (XCH_TARGET_COINS in .env caused 100 instead of 50)
        os.environ["_CLI_XCH_TARGET"] = str(args.xch_target)
        overrides.append(f"XCH_TARGET={args.xch_target}")
    if args.xch_size is not None:
        os.environ["XCH_COIN_SIZE"] = str(args.xch_size)
        overrides.append(f"XCH_SIZE={args.xch_size}")
    if args.cat_target is not None:
        os.environ["_CLI_CAT_TARGET"] = str(args.cat_target)
        overrides.append(f"CAT_TARGET={args.cat_target}")
    if args.cat_size is not None:
        os.environ["CAT_COIN_SIZE"] = str(args.cat_size)
        overrides.append(f"CAT_SIZE={args.cat_size}")
    if args.cat_wallet is not None:
        os.environ["CAT_WALLET_ID"] = str(args.cat_wallet)
        overrides.append(f"CAT_WALLET={args.cat_wallet}")
    if args.fingerprint is not None:
        os.environ["WALLET_FINGERPRINT"] = args.fingerprint
        overrides.append(f"FP={args.fingerprint}")
    if args.tier_sizes is not None:
        os.environ["_CLI_TIER_SIZES"] = args.tier_sizes
        overrides.append(f"TIER_SIZES={args.tier_sizes}")
    # F62 (2026-04-09): per-side tier sizes. XCH coin prep uses buy sizes,
    # CAT coin prep uses sell sizes. When both are provided they override
    # the legacy --tier-sizes for their respective wallet.
    if args.buy_tier_sizes is not None:
        os.environ["_CLI_BUY_TIER_SIZES"] = args.buy_tier_sizes
        overrides.append(f"BUY_TIER_SIZES={args.buy_tier_sizes}")
    if args.sell_tier_sizes is not None:
        os.environ["_CLI_SELL_TIER_SIZES"] = args.sell_tier_sizes
        overrides.append(f"SELL_TIER_SIZES={args.sell_tier_sizes}")
    if args.tier_counts is not None:
        os.environ["_CLI_TIER_COUNTS"] = args.tier_counts
        overrides.append(f"TIER_COUNTS={args.tier_counts}")
    if args.tier_counts_xch is not None:
        os.environ["_CLI_TIER_COUNTS_XCH"] = args.tier_counts_xch
        overrides.append(f"TIER_COUNTS_XCH={args.tier_counts_xch}")
    if args.tier_counts_cat is not None:
        os.environ["_CLI_TIER_COUNTS_CAT"] = args.tier_counts_cat
        overrides.append(f"TIER_COUNTS_CAT={args.tier_counts_cat}")
    if args.prep_headroom_pct is not None:
        os.environ["_CLI_PREP_HEADROOM_PCT"] = str(args.prep_headroom_pct)
        overrides.append(f"PREP_HEADROOM={args.prep_headroom_pct}%")
    if args.live_price is not None:
        os.environ["_CLI_LIVE_PRICE"] = str(args.live_price)
        overrides.append(f"LIVE_PRICE={args.live_price}")

    if overrides:
        print(f"📊 CLI Overrides: {', '.join(overrides)}")
    else:
        print("📊 No CLI overrides — deriving from GUI config")
    print()
    
    # Initialize worker — __init__ derives settings from GUI config
    worker = CoinPrepWorker()

    # Pass run_id to worker so status file includes it (prevents stale reads)
    if args.run_id:
        worker.status.run_id = args.run_id
        print(f"🔗 Run ID: {args.run_id}")

    print("🎯 Final Configuration:")
    if worker.tier_enabled:
        print("   TIER MODE ENABLED")
        total_xch_coins = sum(worker.xch_tier_counts.values())
        total_cat_coins = sum(worker.cat_tier_counts.values())
        for tier_name in worker.tier_order:
            xcnt = int(worker.xch_tier_counts.get(tier_name, 0) or 0)
            ccnt = int(worker.cat_tier_counts.get(tier_name, 0) or 0)
            live_size = worker.offer_tier_xch_sizes.get(tier_name, Decimal("0"))
            prep_size = worker.tier_xch_sizes.get(tier_name, Decimal("0"))
            cat_size = worker.tier_cat_sizes.get(tier_name, Decimal("0"))
            print(
                f"   {tier_name}: XCH={xcnt} × {prep_size} / CAT={ccnt} × {cat_size:,.0f} "
                f"(live size {live_size} XCH)"
            )
        print(f"   Total: {total_xch_coins} XCH coins + {total_cat_coins} CAT coins")
    else:
        print(
            f"   XCH: {worker.xch_target_coins} coins @ {worker.offer_xch_size} live "
            f"→ {worker.xch_coin_size} prep"
        )
        print(f"   CAT: {worker.cat_target_coins} coins @ {worker.cat_coin_size} prep each")
    print()

    success = worker.run_full_preparation()

    if success:
        print()
        print("✅ Coin preparation successful!")
        worker.shutdown_api_log()
        sys.exit(0)
    else:
        print()
        # Surface the concrete failure reason via raw stdout. self.log
        # posts go through a daemon background queue that dies at sys.exit,
        # so the OVERSHOOT / ERROR / balance-too-low message often got
        # dropped before the superlog could record it. The status file
        # persists the reason, so mirror it here as a bare print that the
        # ApiMirrorStream captures synchronously.
        try:
            err = (worker.status.error or worker.status.message or "").strip()
        except Exception:
            err = ""
        if err:
            print(f"❌ Coin preparation failed: {err}")
        else:
            print("❌ Coin preparation failed!")
        worker.shutdown_api_log()
        sys.exit(1)


if __name__ == "__main__":
    main()

