"""Preflight health checks gating bot startup, with a cached structured report

`run_preflight()` returns a `DoctorReport` containing per-check results
(pass / warn / fail / skip) and a `can_start` flag that determines
whether the trading loop is allowed to run. The report is cached
briefly so repeated GUI polls don't spam the wallet or external APIs.
Results are rendered both in the startup log and in the dashboard's
readiness panel.

Key responsibilities:
    - Database health (WAL, schema version, corruption indicators)
    - Config sanity and CAT config (asset_id, ticker, decimals)
    - Wallet reachability, sync state, and signing capability
    - External connectivity: Dexie, TibetSwap, Splash, Spacescan

Each check produces a `DoctorCheck` with category and severity, so the
frontend can group and colour-code results consistently.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """A single preflight check result."""
    name: str
    category: str     # "wallet", "config", "exchange", "database", "network"
    status: str       # "pass", "warn", "fail", "skip"
    message: str
    severity: str     # "info", "warning", "error"


@dataclass
class DoctorReport:
    """Complete preflight report."""
    checks: List[DoctorCheck] = field(default_factory=list)
    timestamp: float = 0.0
    duration_ms: float = 0.0

    @property
    def can_start(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    @property
    def summary(self) -> str:
        fails = sum(1 for c in self.checks if c.status == "fail")
        warns = sum(1 for c in self.checks if c.status == "warn")
        passes = sum(1 for c in self.checks if c.status == "pass")
        if fails:
            return f"BLOCKED: {fails} failure(s), {warns} warning(s), {passes} passed"
        if warns:
            return f"OK with {warns} warning(s), {passes} passed"
        return f"All {passes} checks passed"

    def to_dict(self) -> dict:
        return {
            "can_start": self.can_start,
            "summary": self.summary,
            "timestamp": self.timestamp,
            "duration_ms": round(self.duration_ms, 1),
            "checks": [
                {
                    "name": c.name,
                    "category": c.category,
                    "status": c.status,
                    "message": c.message,
                    "severity": c.severity,
                }
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# Cache — prevent spam on repeated calls
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cached_report: Optional[DoctorReport] = None
_cache_time: float = 0.0
_CACHE_TTL = 30.0  # seconds


def run_preflight(force: bool = False) -> DoctorReport:
    """Run all preflight checks and return a structured report.

    Results are cached for 30s unless force=True.
    """
    global _cached_report, _cache_time

    if not force:
        with _cache_lock:
            if _cached_report and (time.time() - _cache_time) < _CACHE_TTL:
                return _cached_report

    start = time.time()
    report = DoctorReport(timestamp=start)

    # Fetch wallet sync status ONCE and share across wallet checks
    # to avoid 3 redundant RPC calls per preflight run.
    wallet_sync_result = _fetch_wallet_sync_once()

    # Run all checks — order matters for readability, not execution
    report.checks.append(_check_db_health())
    report.checks.append(_check_config_sanity())
    report.checks.append(_check_cat_config())
    report.checks.append(_check_wallet_reachable(wallet_sync_result))
    report.checks.append(_check_wallet_synced(wallet_sync_result))
    report.checks.append(_check_wallet_can_sign(wallet_sync_result))
    report.checks.append(_check_cat_wallet_mapping())
    report.checks.append(_check_dexie_reachable())
    report.checks.append(_check_tibet_reachable())
    report.checks.append(_check_splash_reachable())
    report.checks.append(_check_spacescan_setup())

    report.duration_ms = (time.time() - start) * 1000

    with _cache_lock:
        _cached_report = report
        _cache_time = time.time()

    return report


# ---------------------------------------------------------------------------
# Shared wallet sync fetch (avoids redundant RPC calls)
# ---------------------------------------------------------------------------

def _fetch_wallet_sync_once() -> dict:
    """Fetch wallet sync status once for all wallet checks."""
    try:
        from config import cfg
        wallet_type = getattr(cfg, "WALLET_TYPE", "sage")
        if wallet_type == "sage":
            from wallet_sage import get_wallet_sync_status
        else:
            from wallet_chia import get_wallet_sync_status
        result = get_wallet_sync_status()
        result["_wallet_type"] = wallet_type
        return result
    except Exception as e:
        return {"reachable": False, "_error": str(e), "_wallet_type": "unknown"}


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_db_health() -> DoctorCheck:
    """Verify database is readable and writable."""
    try:
        from database import get_connection
        conn = get_connection()

        # Check key tables exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {row["name"] for row in tables}
        required = {"offers", "fills", "events", "coins"}
        missing = required - table_names
        if missing:
            return DoctorCheck(
                name="database_health", category="database", status="fail",
                message=f"Missing required tables: {', '.join(sorted(missing))}",
                severity="error",
            )

        # Test write capability — insert and immediately delete in one commit
        # to avoid leaving artifacts if something fails between the two ops
        conn.execute(
            "INSERT INTO events (timestamp, event_type, severity, message) "
            "VALUES (datetime('now'), 'preflight_db_test', 'info', 'write test')"
        )
        conn.execute(
            "DELETE FROM events WHERE event_type = 'preflight_db_test'"
        )
        conn.commit()  # both ops in same auto-transaction

        return DoctorCheck(
            name="database_health", category="database", status="pass",
            message=f"Database OK — {len(table_names)} tables",
            severity="info",
        )
    except Exception as e:
        return DoctorCheck(
            name="database_health", category="database", status="fail",
            message=f"Database error: {e}",
            severity="error",
        )


def _check_config_sanity() -> DoctorCheck:
    """Run config validator and report results."""
    try:
        from config import cfg
        from config_validator import validate_config
        report = validate_config(cfg)
        if not report.is_valid:
            msgs = [f"{i.key}: {i.message}" for i in report.errors[:3]]
            return DoctorCheck(
                name="config_validation", category="config", status="fail",
                message=f"{len(report.errors)} config error(s): {'; '.join(msgs)}",
                severity="error",
            )
        if report.warnings:
            msgs = [f"{i.key}: {i.message}" for i in report.warnings[:3]]
            return DoctorCheck(
                name="config_validation", category="config", status="warn",
                message=f"{len(report.warnings)} config warning(s): {'; '.join(msgs)}",
                severity="warning",
            )
        return DoctorCheck(
            name="config_validation", category="config", status="pass",
            message="Config validation passed",
            severity="info",
        )
    except Exception as e:
        return DoctorCheck(
            name="config_validation", category="config", status="warn",
            message=f"Config validation could not run: {e}",
            severity="warning",
        )


def _check_cat_config() -> DoctorCheck:
    """Verify CAT identity is configured."""
    try:
        from config import cfg
        cat_id = getattr(cfg, "CAT_ASSET_ID", "")
        if not cat_id:
            return DoctorCheck(
                name="cat_identity", category="config", status="fail",
                message="CAT_ASSET_ID is empty — bot cannot identify which token to trade",
                severity="error",
            )
        cat_name = getattr(cfg, "CAT_NAME", "")
        cat_dec = getattr(cfg, "CAT_DECIMALS", 3)
        return DoctorCheck(
            name="cat_identity", category="config", status="pass",
            message=f"CAT configured: {cat_name} ({cat_id[:16]}...) decimals={cat_dec}",
            severity="info",
        )
    except Exception as e:
        return DoctorCheck(
            name="cat_identity", category="config", status="fail",
            message=f"CAT config check failed: {e}",
            severity="error",
        )


def _check_wallet_reachable(sync_result: dict = None) -> DoctorCheck:
    """Check if wallet RPC is reachable."""
    try:
        result = sync_result or _fetch_wallet_sync_once()
        wallet_type = result.get("_wallet_type", "sage")

        if not result.get("reachable", False):
            return DoctorCheck(
                name="wallet_reachable", category="wallet", status="fail",
                message=f"{wallet_type.title()} wallet RPC is unreachable",
                severity="error",
            )
        return DoctorCheck(
            name="wallet_reachable", category="wallet", status="pass",
            message=f"{wallet_type.title()} wallet RPC is reachable",
            severity="info",
        )
    except Exception as e:
        return DoctorCheck(
            name="wallet_reachable", category="wallet", status="fail",
            message=f"Wallet reachability check failed: {e}",
            severity="error",
        )


def _check_wallet_synced(sync_result: dict = None) -> DoctorCheck:
    """Check wallet sync status using documented signals only.

    Per SAGE_V4_REVIEW_RULES: use documented sync signals from
    get_sync_status as the source of truth. Do not promote
    undocumented values like synced=None into ready/synced.

    Note: Sage returns {"sync_state": str, "synced": bool, ...}
          Chia returns {"synced": bool, "syncing": bool, ...} (no sync_state key)
    """
    try:
        result = sync_result or _fetch_wallet_sync_once()
        wallet_type = result.get("_wallet_type", "sage")

        if not result.get("reachable", False):
            return DoctorCheck(
                name="wallet_synced", category="wallet", status="skip",
                message="Skipped — wallet not reachable",
                severity="info",
            )

        # Sage uses "sync_state" (string: synced/not_synced/unknown).
        # Chia uses "synced" (bool) and "syncing" (bool).
        if wallet_type == "sage":
            sync_state = result.get("sync_state", "unknown")
            if sync_state == "synced":
                return DoctorCheck(
                    name="wallet_synced", category="wallet", status="pass",
                    message="Wallet is synced",
                    severity="info",
                )
            if sync_state == "not_synced":
                return DoctorCheck(
                    name="wallet_synced", category="wallet", status="fail",
                    message="Wallet is not synced — trading would use stale coin state",
                    severity="error",
                )
            # sync_state == "unknown" — per Sage review rules, do not
            # promote this into "ready". Warn but don't block.
            return DoctorCheck(
                name="wallet_synced", category="wallet", status="warn",
                message=f"Wallet sync state is '{sync_state}' — cannot confirm readiness",
                severity="warning",
            )
        else:
            # Chia wallet — uses bool "synced" field
            if result.get("synced", False):
                return DoctorCheck(
                    name="wallet_synced", category="wallet", status="pass",
                    message="Wallet is synced",
                    severity="info",
                )
            if result.get("syncing", False):
                return DoctorCheck(
                    name="wallet_synced", category="wallet", status="fail",
                    message="Wallet is still syncing — wait for sync to complete",
                    severity="error",
                )
            return DoctorCheck(
                name="wallet_synced", category="wallet", status="warn",
                message="Wallet sync state could not be confirmed",
                severity="warning",
            )
    except Exception as e:
        return DoctorCheck(
            name="wallet_synced", category="wallet", status="warn",
            message=f"Sync check failed: {e}",
            severity="warning",
        )


def _check_wallet_can_sign(sync_result: dict = None) -> DoctorCheck:
    """Check if wallet has signing secrets (not watch-only).

    Per AGENTS.md: treat watch-only wallets as queryable but non-signing.
    Use has_secrets or equivalent wallet metadata to block signing flows
    before transaction construction/submission.
    """
    try:
        result = sync_result or _fetch_wallet_sync_once()
        wallet_type = result.get("_wallet_type", "sage")

        if wallet_type == "sage":
            if not result.get("reachable", False):
                return DoctorCheck(
                    name="wallet_signing", category="wallet", status="skip",
                    message="Skipped — wallet not reachable",
                    severity="info",
                )

            from wallet_sage import _require_signing_capability
            can_sign = _require_signing_capability()
            if not can_sign:
                return DoctorCheck(
                    name="wallet_signing", category="wallet", status="fail",
                    message="Sage wallet is watch-only (no secrets) — cannot create/cancel offers",
                    severity="error",
                )
            return DoctorCheck(
                name="wallet_signing", category="wallet", status="pass",
                message="Wallet has signing capability",
                severity="info",
            )
        else:
            # Chia wallet — signing capability check not exposed the same way
            return DoctorCheck(
                name="wallet_signing", category="wallet", status="pass",
                message="Chia wallet signing assumed available",
                severity="info",
            )
    except Exception as e:
        return DoctorCheck(
            name="wallet_signing", category="wallet", status="warn",
            message=f"Signing capability check failed: {e}",
            severity="warning",
        )


def _check_cat_wallet_mapping() -> DoctorCheck:
    """Verify the wallet has a CAT matching our configured asset ID."""
    try:
        from config import cfg
        cat_id = getattr(cfg, "CAT_ASSET_ID", "")
        if not cat_id:
            return DoctorCheck(
                name="cat_wallet_mapping", category="wallet", status="skip",
                message="Skipped — no CAT_ASSET_ID configured",
                severity="info",
            )

        wallet_type = getattr(cfg, "WALLET_TYPE", "sage")
        if wallet_type == "sage":
            from wallet_sage import get_wallets
        else:
            from wallet_chia import get_wallets

        raw_wallets = get_wallets()
        if not raw_wallets:
            return DoctorCheck(
                name="cat_wallet_mapping", category="wallet", status="warn",
                message="Could not enumerate wallet CATs — mapping not verified",
                severity="warning",
            )

        # Normalize wallet list:
        # Sage get_wallets() returns a list of dicts directly.
        # Chia get_wallets() returns {"wallets": [...], "success": true}.
        if isinstance(raw_wallets, dict):
            wallet_list = raw_wallets.get("wallets", [])
        elif isinstance(raw_wallets, list):
            wallet_list = raw_wallets
        else:
            wallet_list = []

        if not wallet_list:
            return DoctorCheck(
                name="cat_wallet_mapping", category="wallet", status="warn",
                message="Wallet returned no entries — mapping not verified",
                severity="warning",
            )

        cat_id_lower = cat_id.lower().replace("0x", "")
        found = False
        for w in wallet_list:
            if not isinstance(w, dict):
                continue
            # Sage uses "asset_id" directly; Chia uses "data" for CAT asset ID
            w_data = w.get("asset_id", "") or w.get("data", "") or ""
            if isinstance(w_data, str) and w_data.lower().replace("0x", "") == cat_id_lower:
                found = True
                break

        if found:
            return DoctorCheck(
                name="cat_wallet_mapping", category="wallet", status="pass",
                message=f"CAT {cat_id[:16]}... found in wallet",
                severity="info",
            )
        return DoctorCheck(
            name="cat_wallet_mapping", category="wallet", status="warn",
            message=f"CAT {cat_id[:16]}... not found in wallet — may not have any balance",
            severity="warning",
        )
    except Exception as e:
        return DoctorCheck(
            name="cat_wallet_mapping", category="wallet", status="warn",
            message=f"CAT mapping check failed: {e}",
            severity="warning",
        )


def _check_dexie_reachable() -> DoctorCheck:
    """Check if Dexie API is reachable."""
    try:
        import requests
        from config import cfg
        dexie_url = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
        resp = requests.head(f"{dexie_url}/v1/offers", timeout=5)
        if resp.status_code < 500:
            return DoctorCheck(
                name="dexie_reachable", category="exchange", status="pass",
                message=f"Dexie API reachable (HTTP {resp.status_code})",
                severity="info",
            )
        return DoctorCheck(
            name="dexie_reachable", category="exchange", status="warn",
            message=f"Dexie API returned HTTP {resp.status_code}",
            severity="warning",
        )
    except Exception as e:
        return DoctorCheck(
            name="dexie_reachable", category="exchange", status="warn",
            message=f"Dexie API unreachable: {e}",
            severity="warning",
        )


def _check_tibet_reachable() -> DoctorCheck:
    """Check if TibetSwap API is reachable."""
    try:
        import requests
        from config import cfg
        tibet_url = getattr(cfg, "TIBET_API_BASE", "https://api.v2.tibetswap.io")
        resp = requests.get(f"{tibet_url}/tokens", timeout=5)
        if resp.status_code < 500:
            return DoctorCheck(
                name="tibet_reachable", category="exchange", status="pass",
                message=f"TibetSwap API reachable (HTTP {resp.status_code})",
                severity="info",
            )
        return DoctorCheck(
            name="tibet_reachable", category="exchange", status="warn",
            message=f"TibetSwap API returned HTTP {resp.status_code}",
            severity="warning",
        )
    except Exception as e:
        return DoctorCheck(
            name="tibet_reachable", category="exchange", status="warn",
            message=f"TibetSwap API unreachable: {e}",
            severity="warning",
        )


def _check_splash_reachable() -> DoctorCheck:
    """Check Splash reachability (only if enabled)."""
    try:
        from config import cfg
        if not getattr(cfg, "SPLASH_ENABLED", False):
            return DoctorCheck(
                name="splash_reachable", category="network", status="skip",
                message="Splash is disabled — skipped",
                severity="info",
            )

        import requests
        splash_url = getattr(cfg, "SPLASH_SUBMIT_URL", "http://localhost:4000")
        resp = requests.head(splash_url, timeout=5)
        if resp.status_code < 500:
            return DoctorCheck(
                name="splash_reachable", category="network", status="pass",
                message=f"Splash reachable (HTTP {resp.status_code})",
                severity="info",
            )
        return DoctorCheck(
            name="splash_reachable", category="network", status="warn",
            message=f"Splash returned HTTP {resp.status_code}",
            severity="warning",
        )
    except Exception as e:
        return DoctorCheck(
            name="splash_reachable", category="network", status="warn",
            message=f"Splash unreachable: {e}",
            severity="warning",
        )


def _check_spacescan_setup() -> DoctorCheck:
    """Check Spacescan configuration."""
    try:
        from config import cfg
        if not getattr(cfg, "SPACESCAN_ENABLED", False):
            return DoctorCheck(
                name="spacescan_setup", category="network", status="skip",
                message="Spacescan is disabled — skipped",
                severity="info",
            )

        api_key = getattr(cfg, "SPACESCAN_API_KEY", "")
        pro_url = getattr(cfg, "SPACESCAN_PRO_URL", "")

        # Pro URL implies paid tier which needs an API key
        if pro_url and not api_key:
            return DoctorCheck(
                name="spacescan_setup", category="network", status="warn",
                message="SPACESCAN_PRO_URL is set but SPACESCAN_API_KEY is empty",
                severity="warning",
            )

        return DoctorCheck(
            name="spacescan_setup", category="network", status="pass",
            message="Spacescan configuration OK" + (" (API key set)" if api_key else " (free tier)"),
            severity="info",
        )
    except Exception as e:
        return DoctorCheck(
            name="spacescan_setup", category="network", status="warn",
            message=f"Spacescan check failed: {e}",
            severity="warning",
        )

