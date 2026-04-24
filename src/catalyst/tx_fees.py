"""Transaction fee estimation with three modes and short-TTL caching

Chooses between a manual flat XCH fee, a local full-node auto-estimate
(mTLS RPC), and a Coinset cloud fallback with retry/backoff on 429/5xx
responses. Also supplies fee-pool helpers and a settings snapshot for
the GUI. Used by offer_manager and coin_prep_worker whenever a spend
bundle needs fees attached.

Key responsibilities:
    - `xch_to_mojos` / `mojos_to_xch` conversions and rounding
    - Manual / node-auto / Coinset-auto fee selection
    - Separate TTL caches for node-suggested and Coinset-suggested fees
    - GUI-facing snapshot of current fee configuration

Cache TTLs are short (30–60s) so fee guidance stays responsive to
mempool conditions without hammering the node or Coinset.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal, ROUND_UP
from typing import Dict, Optional

from config import cfg


XCH_MOJOS = Decimal("1000000000000")
_AUTO_FEE_MODES = {"auto", "node", "suggested"}
FEE_TIER_NAME = "fees"
_SUGGESTED_FEE_CACHE: Dict[tuple[int, int], tuple[float, Dict]] = {}
_SUGGESTED_FEE_CACHE_TTL_SECS = 30

# Coinset fee estimate cache (separate TTL — less aggressive than coin queries)
_COINSET_FEE_CACHE: Dict[tuple[int, int], tuple[float, Dict]] = {}
_COINSET_FEE_CACHE_TTL_SECS = 60


def _decimal_or_zero(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def xch_to_mojos(value) -> int:
    amount = _decimal_or_zero(value)
    if amount <= 0:
        return 0
    return int((amount * XCH_MOJOS).to_integral_value(rounding=ROUND_UP))


def mojos_to_xch(mojos: int) -> Decimal:
    try:
        mojo_int = int(mojos or 0)
    except Exception:
        mojo_int = 0
    if mojo_int <= 0:
        return Decimal("0")
    return Decimal(mojo_int) / XCH_MOJOS


def get_transaction_fee_mode() -> str:
    mode = str(getattr(cfg, "TRANSACTION_FEE_MODE", "auto") or "auto").strip().lower()
    return mode if mode in _AUTO_FEE_MODES.union({"manual"}) else "auto"


def get_wallet_fee_environment() -> Dict:
    wallet_type = str(getattr(cfg, "WALLET_TYPE", "sage") or "sage").strip().lower()
    has_full_node_rpc = _get_full_node_cert_paths() is not None
    if wallet_type == "sage" and not has_full_node_rpc:
        return {
            "wallet_type": wallet_type,
            "has_full_node_rpc": False,
            "supports_auto_estimate": False,
            "reason": "sage_no_full_node_rpc",
            "message": "Sage has no local full-node fee estimator in this setup, so auto falls back to the manual fee.",
        }
    if has_full_node_rpc:
        return {
            "wallet_type": wallet_type,
            "has_full_node_rpc": True,
            "supports_auto_estimate": True,
            "reason": "full_node_rpc_available",
            "message": "A full-node fee estimate is available.",
        }
    return {
        "wallet_type": wallet_type,
        "has_full_node_rpc": False,
        "supports_auto_estimate": False,
        "reason": "full_node_rpc_unavailable",
        "message": "No full-node fee estimator is available, so auto falls back to the manual fee.",
    }


def get_manual_transaction_fee_mojos() -> int:
    return xch_to_mojos(getattr(cfg, "TRANSACTION_FEE_XCH", Decimal("0")))


def _get_full_node_cert_paths() -> Optional[tuple[str, str]]:
    wallet_cert = str(getattr(cfg, "CHIA_WALLET_CERT", "") or "").strip()
    wallet_key = str(getattr(cfg, "CHIA_WALLET_KEY", "") or "").strip()
    if not wallet_cert or not wallet_key:
        return None

    ssl_root = os.path.dirname(os.path.dirname(wallet_cert))
    full_node_cert = os.path.join(ssl_root, "full_node", "private_full_node.crt")
    full_node_key = os.path.join(ssl_root, "full_node", "private_full_node.key")
    if not (os.path.exists(full_node_cert) and os.path.exists(full_node_key)):
        return None
    return full_node_cert, full_node_key


def _get_chia_ca_cert() -> Optional[str]:
    """Return Chia private CA cert path for TLS server verification, or None.

    Chia signs all component certs with a per-installation private CA stored at
    ssl/ca/private_ca.crt.  Passing this as verify= lets requests validate the
    full-node's self-signed cert without disabling TLS verification entirely.
    Falls back to None (caller must use verify=False) if the CA cert isn't found.
    """
    wallet_cert = str(getattr(cfg, "CHIA_WALLET_CERT", "") or "").strip()
    if not wallet_cert:
        return None
    ssl_root = os.path.dirname(os.path.dirname(wallet_cert))
    ca_cert = os.path.join(ssl_root, "ca", "private_ca.crt")
    return ca_cert if os.path.exists(ca_cert) else None


def _full_node_rpc(endpoint: str, payload: dict, timeout: int = 5) -> Optional[Dict]:
    cert_pair = _get_full_node_cert_paths()
    if not cert_pair:
        return None

    try:
        import requests

        base_url = str(getattr(cfg, "CHIA_FULL_NODE_RPC_URL", "https://localhost:8555") or "").rstrip("/")
        if not base_url:
            return None
        # Use the Chia CA cert when available so TLS server identity is verified.
        # Fall back to verify=False only when the CA cert is missing (older Chia
        # installs that don't expose the private_ca.crt at the expected path).
        # Chia RPC uses self-signed certs on localhost — MITM risk is minimal but
        # proper verification is preferred.  # nosec B501
        _tls_verify = _get_chia_ca_cert() or False
        response = requests.post(
            f"{base_url}/{endpoint}",
            json=payload,
            cert=cert_pair,
            headers={"Content-Type": "application/json"},
            verify=_tls_verify,
            timeout=(3, timeout),
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def _coinset_fee_estimate(target_seconds: int, cost: int) -> Optional[Dict]:
    """Query Coinset cloud API for a fee estimate.

    Used as the primary auto-fee source for Sage users who have no local
    full node.  Coinset mirrors the full-node RPC, so the request/response
    format is identical to get_fee_estimate.

    Returns a normalised snapshot dict (same shape as full-node path) or
    None if Coinset is disabled, unreachable, or returns bad data.
    """
    if not getattr(cfg, "COINSET_ENABLED", True):
        return None

    cache_key = (target_seconds, cost)
    now = time.time()
    cached = _COINSET_FEE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _COINSET_FEE_CACHE_TTL_SECS:
        return dict(cached[1])

    try:
        import requests as _requests
        api_url = str(getattr(cfg, "COINSET_API_URL", "https://api.coinset.org") or "https://api.coinset.org").rstrip("/")
        timeout = int(getattr(cfg, "COINSET_TIMEOUT", 5) or 5)
        # F77 (2026-04-17): retry on 429/5xx with exponential backoff.
        # Coinset occasionally rate-limits during congestion; a silent
        # failure caused Smart Settings to fall back to a flat manual
        # fee, which is less accurate for the current network conditions.
        r = None
        data = None
        for _attempt in range(3):
            r = _requests.post(
                f"{api_url}/get_fee_estimate",
                json={"cost": cost, "target_times": [target_seconds]},
                headers={"content-type": "application/json", "User-Agent": "CATalyst/2.0"},
                timeout=(3, timeout),
            )
            if r.status_code == 200:
                break
            # 429/500/502/503/504 → retry; other 4xx → give up immediately
            if r.status_code not in (429, 500, 502, 503, 504):
                return None
            if _attempt < 2:
                time.sleep(1 + _attempt * 2)  # 1s, 3s
        if r is None or r.status_code != 200:
            return None
        data = r.json()
        if not data.get("success"):
            return None

        estimates = data.get("estimates") or []
        estimated = int(Decimal(str(estimates[0] if estimates else 0)).to_integral_value(rounding=ROUND_UP))
        snapshot = {
            "available": True,
            "source": "coinset",
            "reason": "coinset_api",
            "message": "Fee estimated via Coinset cloud API (mirrors full-node get_fee_estimate).",
            "target_seconds": target_seconds,
            "cost": cost,
            "fee_mojos": max(0, estimated),
            "fee_xch": format(mojos_to_xch(max(0, estimated)), 'f'),
            "full_node_synced": bool(data.get("full_node_synced", False)),
            "mempool_size": int(data.get("mempool_size", 0) or 0),
            "mempool_fees": int(data.get("mempool_fees", 0) or 0),
            "last_block_cost": int(data.get("last_block_cost", 0) or 0),
            "raw": data,
        }
        _COINSET_FEE_CACHE[cache_key] = (now, snapshot)
        return snapshot
    except Exception:
        return None


def get_suggested_transaction_fee(target_seconds: int = None, cost: int = None) -> Dict:
    target = int(target_seconds or getattr(cfg, "TRANSACTION_FEE_TARGET_SECS", 300) or 300)
    cost_val = int(cost or getattr(cfg, "TRANSACTION_FEE_ESTIMATE_COST", 20_000_000) or 20_000_000)
    target = max(0, target)
    cost_val = max(1, cost_val)
    cache_key = (target, cost_val)
    now = time.time()

    cached = _SUGGESTED_FEE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _SUGGESTED_FEE_CACHE_TTL_SECS:
        return dict(cached[1])

    env = get_wallet_fee_environment()
    payload = {"cost": cost_val, "target_times": [target]}
    result = _full_node_rpc("get_fee_estimate", payload, timeout=6) if env.get("supports_auto_estimate") else None
    if result and result.get("success"):
        estimates = result.get("estimates") or []
        estimated = int(Decimal(str(estimates[0] if estimates else 0)).to_integral_value(rounding=ROUND_UP))
        snapshot = {
            "available": True,
            "source": "full_node_rpc",
            "reason": env.get("reason"),
            "message": env.get("message"),
            "target_seconds": target,
            "cost": cost_val,
            "fee_mojos": max(0, estimated),
            "fee_xch": str(mojos_to_xch(max(0, estimated))),
            "full_node_synced": bool(result.get("full_node_synced", False)),
            "raw": result,
        }
        _SUGGESTED_FEE_CACHE[cache_key] = (now, snapshot)
        return dict(snapshot)

    # No local full node — try Coinset cloud API before giving up
    coinset_result = _coinset_fee_estimate(target, cost_val)
    if coinset_result:
        _SUGGESTED_FEE_CACHE[cache_key] = (now, coinset_result)
        return dict(coinset_result)

    snapshot = {
        "available": False,
        "source": "unavailable" if env.get("supports_auto_estimate") else "manual_fallback_only",
        "reason": env.get("reason"),
        "message": env.get("message"),
        "target_seconds": target,
        "cost": cost_val,
        "fee_mojos": 0,
        "fee_xch": "0",
        "full_node_synced": False,
        "raw": result or {},
    }
    _SUGGESTED_FEE_CACHE[cache_key] = (now, snapshot)
    return dict(snapshot)


def get_effective_transaction_fee_mojos() -> int:
    manual = get_manual_transaction_fee_mojos()
    mode = get_transaction_fee_mode()
    if mode in _AUTO_FEE_MODES:
        suggested = get_suggested_transaction_fee()
        if suggested.get("available"):
            return int(suggested.get("fee_mojos", 0) or 0)
    return manual


def get_fee_pool_count() -> int:
    try:
        return max(0, int(getattr(cfg, "FEE_PREP_COUNT", 0) or 0))
    except Exception:
        return 0


def get_fee_coin_size_xch() -> Decimal:
    return _decimal_or_zero(getattr(cfg, "FEE_COIN_SIZE_XCH", Decimal("0")))


def get_fee_coin_size_mojos() -> int:
    return xch_to_mojos(get_fee_coin_size_xch())


def fee_pool_configured() -> bool:
    return get_fee_pool_count() > 0 and get_fee_coin_size_mojos() > 0


def fee_pool_enabled() -> bool:
    return get_effective_transaction_fee_mojos() > 0 and fee_pool_configured()


def get_fee_tier_name() -> str:
    return FEE_TIER_NAME


def get_fee_pool_plan() -> Dict:
    return {
        "enabled": fee_pool_enabled(),
        "configured": fee_pool_configured(),
        "tier_name": FEE_TIER_NAME,
        "count": get_fee_pool_count(),
        "coin_size_mojos": get_fee_coin_size_mojos(),
        "coin_size_xch": str(get_fee_coin_size_xch()),
    }


def get_fee_settings_snapshot() -> Dict:
    env = get_wallet_fee_environment()
    suggested = get_suggested_transaction_fee()
    effective_mojos = get_effective_transaction_fee_mojos()
    return {
        "mode": get_transaction_fee_mode(),
        "wallet_type": env.get("wallet_type"),
        "environment": env,
        "manual_fee_mojos": get_manual_transaction_fee_mojos(),
        "manual_fee_xch": str(getattr(cfg, "TRANSACTION_FEE_XCH", Decimal("0"))),
        "effective_fee_mojos": effective_mojos,
        "effective_fee_xch": format(mojos_to_xch(effective_mojos), 'f'),
        "suggested": suggested,
        "fee_pool_configured": fee_pool_configured(),
        "fee_pool_enabled": fee_pool_enabled(),
        "fee_coin_size_mojos": get_fee_coin_size_mojos(),
        "fee_coin_size_xch": str(get_fee_coin_size_xch()),
        "fee_prep_count": get_fee_pool_count(),
        "tier_name": FEE_TIER_NAME,
    }

