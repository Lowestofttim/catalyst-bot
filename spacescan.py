"""
Spacescan Pro API — Golden Source of Truth for On-Chain Verification

The blockchain is the source of truth, not the wallet.
If it's not confirmed on-chain, it's not true.

Endpoints used:
  GET /coin/info/{coin_id}          — Check if coin spent (fill verification)
  GET /address/xch-balance/{addr}   — XCH balance check
  GET /address/token-balance/{addr} — CAT token balance check

Pro API: pro-api.spacescan.io with x-api-key header
Free API: api.spacescan.io (lower rate limits)
"""

import re
import time
from pathlib import Path
from decimal import Decimal
from typing import Optional, Dict, Set

import requests

from config import cfg
from database import log_event


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_last_call_time = 0.0  # Rate limiting: track last API call
_rate_limited_until = 0.0  # Non-blocking 429 backoff timestamp

# Tier-aware rate limiting and call budgeting
# Free plan limits are documented publicly by Spacescan.
# Paid plans vary, so the paid-key interval here is just the app's faster default.
_PRO_CALL_INTERVAL = 2.0   # seconds between calls when a paid key is configured
_FREE_CALL_INTERVAL = 12.0  # seconds between calls on the free tier

# Monthly call budget tracking (resets on module load / bot restart)
_calls_this_session = 0
_session_start_time = time.time()

# Free tier monthly budget — reserve most for fill verification
_FREE_MONTHLY_BUDGET = 1000
_FREE_DAILY_BUDGET = 30  # ~1000/month, leave headroom
_calls_today = 0
_today_date = ""  # Track date for daily reset
_known_wallet_addresses_cache: Set[str] = set()
_known_wallet_addresses_cache_at = 0.0
_ADDRESS_RE = re.compile(r"\b(?:xch|txch)1[0-9a-z]{20,}\b")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_base_url() -> str:
    """Return the appropriate Spacescan API base URL."""
    if cfg.SPACESCAN_API_KEY:
        return getattr(cfg, "SPACESCAN_PRO_URL", "https://pro-api.spacescan.io")
    return getattr(cfg, "SPACESCAN_FREE_URL", "https://api.spacescan.io")


def _get_headers() -> dict:
    """Return headers for Spacescan API (includes API key for Pro)."""
    headers = {"Accept": "application/json"}
    if cfg.SPACESCAN_API_KEY:
        headers["x-api-key"] = cfg.SPACESCAN_API_KEY
    return headers


def is_pro_tier() -> bool:
    """Check if we have a paid Spacescan API key configured."""
    return bool(cfg.SPACESCAN_API_KEY)


def _get_call_interval() -> float:
    """Return the appropriate rate limit interval for our tier."""
    return _PRO_CALL_INTERVAL if is_pro_tier() else _FREE_CALL_INTERVAL


def _check_daily_budget(call_type: str = "general") -> bool:
    """Check if we have budget remaining for free tier.
    Paid-key mode always returns True here because exact paid-plan quotas vary.
    Free tier tracks daily calls and reserves budget for fill verification.

    Args:
        call_type: "fill_verify" (critical, always allowed) or "balance" (skipped if tight)
    """
    global _calls_today, _today_date

    # Paid-key mode: plan-specific quotas are not modeled here
    if is_pro_tier():
        return True

    # Free tier: track daily budget
    import datetime
    today = datetime.date.today().isoformat()
    if today != _today_date:
        _today_date = today
        _calls_today = 0

    # Fill verification is always allowed (the whole point of Spacescan)
    if call_type == "fill_verify":
        return True

    # Balance checks: only if we have budget headroom
    # Reserve at least 10 calls/day for fill verification
    if _calls_today >= (_FREE_DAILY_BUDGET - 10):
        log_event("debug", "spacescan_budget_exceeded",
                  f"Free tier daily budget nearly exhausted ({_calls_today}/{_FREE_DAILY_BUDGET}). "
                  f"Skipping {call_type} to reserve for fill verification.")
        return False

    return True


def _rate_limit():
    """Enforce minimum interval between API calls (tier-aware)."""
    global _last_call_time, _calls_this_session, _calls_today
    interval = _get_call_interval()
    elapsed = time.time() - _last_call_time
    if elapsed < interval:
        time.sleep(interval - elapsed)
    _last_call_time = time.time()


def _get_known_wallet_addresses() -> Set[str]:
    """Return wallet addresses explicitly seen in local app history."""
    global _known_wallet_addresses_cache, _known_wallet_addresses_cache_at

    now = time.time()
    if _known_wallet_addresses_cache and (now - _known_wallet_addresses_cache_at) < 300:
        return set(_known_wallet_addresses_cache)

    addresses: Set[str] = set()
    current = str(getattr(cfg, "WALLET_ADDRESS", "") or "").strip()
    if current:
        addresses.add(current)

    markers = (
        "Wallet address:",
        "Receive address:",
        "Sage change address set to ",
    )
    base_dir = Path(__file__).resolve().parent
    log_paths = list(base_dir.glob("*.log"))
    archive_dir = base_dir / "_archive"
    if archive_dir.exists():
        log_paths.extend(archive_dir.rglob("*.log"))

    for path in log_paths:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if not any(marker in line for marker in markers):
                        continue
                    addresses.update(_ADDRESS_RE.findall(line))
        except Exception:
            continue

    _known_wallet_addresses_cache = set(addresses)
    _known_wallet_addresses_cache_at = now
    return set(addresses)


def _spacescan_get(endpoint: str) -> Optional[Dict]:
    """Make a GET request to Spacescan API with rate limiting and error handling.

    Args:
        endpoint: API path (e.g., "/coin/info/0xabc...")

    Returns:
        JSON response dict, or None on error.
    """
    global _calls_this_session, _calls_today, _rate_limited_until

    _rate_limit()

    # Non-blocking 429 backoff — skip calls during cooldown
    if time.time() < _rate_limited_until:
        return None

    url = f"{_get_base_url()}{endpoint}"
    timeout = getattr(cfg, "SPACESCAN_TIMEOUT", 10)

    try:
        response = requests.get(url, headers=_get_headers(), timeout=timeout)

        if response.status_code == 429:
            log_event("warning", "spacescan_rate_limited",
                      "Spacescan rate limit hit — backing off 60s (non-blocking)")
            _rate_limited_until = time.time() + 60
            return None

        if response.status_code == 404:
            log_event("debug", "spacescan_not_found",
                      f"Spacescan 404 for {endpoint[:60]}...")
            return None

        if response.status_code >= 500:
            log_event("warning", "spacescan_server_error",
                      f"Spacescan server error {response.status_code}")
            return None

        response.raise_for_status()
        data = response.json()

        if data.get("status") != "success":
            log_event("warning", "spacescan_api_error",
                      f"Spacescan returned non-success: {data.get('status')}")
            return None

        _calls_this_session += 1
        _calls_today += 1
        return data

    except requests.exceptions.Timeout:
        log_event("warning", "spacescan_timeout",
                  f"Spacescan request timed out after {timeout}s")
        return None
    except requests.exceptions.ConnectionError:
        log_event("warning", "spacescan_connection_error",
                  "Cannot connect to Spacescan API")
        return None
    except Exception as e:
        log_event("error", "spacescan_error",
                  f"Spacescan request failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API — Coin Verification (Fill Detection)
# ---------------------------------------------------------------------------

def is_coin_spent(coin_id: str) -> Optional[Dict]:
    """Check if a specific coin has been spent on-chain.

    Args:
        coin_id: The coin's unique identifier (hex, with or without 0x prefix)

    Returns:
        Dict with keys: {spent: bool, spent_block: str, receiver_address: str}
        or None on API error.
    """
    if not getattr(cfg, "SPACESCAN_ENABLED", True):
        return None

    # Ensure 0x prefix
    if not coin_id.startswith("0x"):
        coin_id = f"0x{coin_id}"

    data = _spacescan_get(f"/coin/info/{coin_id}")
    if not data:
        return None

    coin = data.get("coin", {})

    # Pro API returns 200/success even for non-existent coins,
    # but with amount_value=null and empty sender/receiver.
    # Detect this and treat as "coin not found".
    if coin.get("amount_value") is None and not coin.get("sender") and not coin.get("receiver"):
        log_event("debug", "spacescan_coin_not_found",
                  f"Coin {coin_id[:16]}... not found on-chain (empty response)")
        return None

    # Handle both Pro API format and docs format for spent_block
    spent_block = coin.get("spent_block")
    # Pro API may return spent_block as None, empty string, or 0 for unspent
    is_spent = (spent_block is not None and spent_block != "" and spent_block != 0
                and str(spent_block) != "0")

    receiver = coin.get("receiver", {})
    receiver_addr = receiver.get("address", "") if isinstance(receiver, dict) else ""
    sender = coin.get("sender", {})
    sender_addr = sender.get("address", "") if isinstance(sender, dict) else ""

    return {
        "spent": is_spent,
        "spent_block": str(spent_block) if spent_block else "",
        "receiver_address": receiver_addr,
        "amount": coin.get("amount", coin.get("amount_value", "")),
        "amount_mojo": coin.get("amount_mojo", ""),
        "sender_address": sender_addr,
    }


def is_known_wallet_address(address: str, explicit_addresses: Optional[set[str]] = None) -> bool:
    """Return True when an address is recognised as belonging to this wallet."""
    addr = str(address or "").strip()
    if not addr:
        return False

    known_wallet_addresses = _get_known_wallet_addresses()
    if explicit_addresses:
        try:
            known_wallet_addresses.update(
                str(item or "").strip() for item in explicit_addresses if str(item or "").strip()
            )
        except Exception:
            pass
    return addr in known_wallet_addresses


def verify_fill(coin_id: str, our_address: str,
                explicit_addresses: Optional[set[str]] = None) -> Optional[bool]:
    """Verify that a coin was spent to an external address (= real fill).

    This is the golden gate: only returns True if we have on-chain proof
    that the coin was spent AND the receiver is NOT us (which would be a cancel).

    Args:
        coin_id: The coin locked by the offer that disappeared
        our_address: Our wallet address (to detect self-spends / cancels)

    Returns:
        True = confirmed fill (coin spent to external address)
        False = confirmed NOT a fill (unspent or self-spend)
        None = verification unavailable (timeout / API error)

    IMPORTANT: Returns None on API errors. Callers must fail closed and avoid
    recording a fill without positive on-chain confirmation. This is the
    conservative approach — better to miss a fill than record a phantom one.
    """
    if not coin_id:
        log_event("debug", "spacescan_no_coin_id",
                  "Cannot verify fill — no coin_id available")
        return False

    result = is_coin_spent(coin_id)

    if result is None:
        log_event("warning", "spacescan_verify_failed",
                  f"Cannot verify coin {coin_id[:16]}... — API error.")
        return None  # Distinct from False (explicit rejection)

    if not result["spent"]:
        log_event("info", "spacescan_phantom_detected",
                  f"Coin {coin_id[:16]}... NOT spent on-chain. "
                  f"Phantom fill prevented!")
        return False

    # Coin IS spent — check if it went to us (cancel) or external (fill)
    receiver = result["receiver_address"]
    known_wallet_addresses = set(explicit_addresses or set())
    if our_address:
        known_wallet_addresses.add(our_address)

    if receiver and is_known_wallet_address(receiver, known_wallet_addresses):
        log_event("info", "spacescan_self_spend",
                  f"Coin {coin_id[:16]}... spent to known own address "
                  f"{receiver[:16]}... Not a fill.")
        return False

    if (getattr(cfg, "WALLET_TYPE", "") == "sage" and
            not getattr(cfg, "SAGE_SET_CHANGE_ADDRESS", False)):
        log_event("warning", "spacescan_fill_ambiguous",
                  f"Coin {coin_id[:16]}... spent to {receiver[:16]}..., but "
                  f"Sage change-address pinning is off. Treating as unverified.")
        return None

    # Coin spent to external address = CONFIRMED FILL
    log_event("success", "spacescan_fill_confirmed",
              f"CONFIRMED: Coin {coin_id[:16]}... spent to {receiver[:16]}... "
              f"in block {result['spent_block']}. Real fill!")
    return True


# ---------------------------------------------------------------------------
# Public API — Balance Verification
# ---------------------------------------------------------------------------

def get_xch_balance(address: str) -> Optional[Decimal]:
    """Fetch the on-chain XCH balance for an address.

    Args:
        address: XCH address (xch1...)

    Returns:
        Balance as Decimal (in XCH), or None on error.
        Returns None on free tier if daily budget is exhausted.
    """
    if not _check_daily_budget("balance"):
        return None

    data = _spacescan_get(f"/address/xch-balance/{address}")
    if not data:
        return None

    try:
        return Decimal(str(data["xch"]))
    except (KeyError, ValueError) as e:
        log_event("warning", "spacescan_balance_parse_error",
                  f"Could not parse XCH balance: {e}")
        return None


def get_token_balance(address: str, asset_id: str = None) -> Optional[Decimal]:
    """Fetch the on-chain token (CAT) balance for an address.

    Args:
        address: XCH address (xch1...)
        asset_id: If provided, return balance for this specific token.
                  If None, returns the first token balance found.

    Returns:
        Balance as Decimal, or None on error / token not found.
        Returns None on free tier if daily budget is exhausted.
    """
    if not _check_daily_budget("balance"):
        return None

    data = _spacescan_get(f"/address/token-balance/{address}")
    if not data:
        return None

    # Pro API returns token list under "data" key, free API uses "balance"
    balances = data.get("data", data.get("balance", []))
    if not balances:
        return Decimal("0")

    # If specific asset_id requested, find it
    if asset_id:
        for token in balances:
            if token.get("asset_id") == asset_id:
                try:
                    return Decimal(str(token["balance"]))
                except (KeyError, ValueError):
                    return None
        return Decimal("0")  # Token not found = zero balance

    # No specific asset — return first token balance
    try:
        return Decimal(str(balances[0]["balance"]))
    except (KeyError, ValueError, IndexError):
        return None


def get_api_stats() -> Dict:
    """Return current API usage stats for monitoring/GUI display."""
    import datetime
    uptime_hours = (time.time() - _session_start_time) / 3600
    return {
        "tier": "paid" if is_pro_tier() else "free",
        "calls_this_session": _calls_this_session,
        "calls_today": _calls_today,
        "session_uptime_hours": round(uptime_hours, 1),
        "daily_budget": "plan-dependent" if is_pro_tier() else _FREE_DAILY_BUDGET,
        "call_interval_secs": _get_call_interval(),
    }


def should_check_balance() -> bool:
    """Helper for bot_loop: should we run the periodic balance check this loop?

    Free tier: balance checks are disabled entirely (budget reserved for fills).
    Paid-key mode: always OK here; bot_loop controls frequency separately.
    """
    if not is_pro_tier():
        return False  # Free tier: skip balance checks, save budget for fills
    return True


def check_balance_discrepancy(our_address: str, wallet_xch: Decimal,
                               wallet_cat: Decimal = None,
                               cat_asset_id: str = None) -> Dict:
    """Compare wallet balances against on-chain truth.

    Args:
        our_address: Our XCH wallet address
        wallet_xch: What the wallet reports as XCH balance
        wallet_cat: What the wallet reports as CAT balance (optional)
        cat_asset_id: The CAT asset ID to check (optional)

    Returns:
        Dict with discrepancy info:
        {
            "xch_onchain": Decimal or None,
            "xch_wallet": Decimal,
            "xch_diff": Decimal or None,
            "xch_ok": bool,
            "cat_onchain": Decimal or None,
            "cat_wallet": Decimal or None,
            "cat_diff": Decimal or None,
            "cat_ok": bool,
        }
    """
    threshold = getattr(cfg, "SPACESCAN_BALANCE_THRESHOLD_XCH", Decimal("0.1"))
    result = {
        "xch_onchain": None, "xch_wallet": wallet_xch,
        "xch_diff": None, "xch_ok": True,
        "cat_onchain": None, "cat_wallet": wallet_cat,
        "cat_diff": None, "cat_ok": True,
    }

    # Check XCH
    xch_onchain = get_xch_balance(our_address)
    if xch_onchain is not None:
        result["xch_onchain"] = xch_onchain
        result["xch_diff"] = abs(xch_onchain - wallet_xch)
        result["xch_ok"] = result["xch_diff"] <= threshold

        if not result["xch_ok"]:
            log_event("warning", "balance_discrepancy_xch",
                      f"XCH balance mismatch! Wallet: {wallet_xch}, "
                      f"On-chain: {xch_onchain}, Diff: {result['xch_diff']}")

    # Check CAT (if requested)
    if wallet_cat is not None and cat_asset_id:
        cat_onchain = get_token_balance(our_address, cat_asset_id)
        if cat_onchain is not None:
            result["cat_onchain"] = cat_onchain
            result["cat_diff"] = abs(cat_onchain - wallet_cat)
            # Use a generous threshold for CAT (1% or 100 tokens)
            cat_threshold = max(wallet_cat * Decimal("0.01"), Decimal("100"))
            result["cat_ok"] = result["cat_diff"] <= cat_threshold

            if not result["cat_ok"]:
                log_event("warning", "balance_discrepancy_cat",
                          f"CAT balance mismatch! Wallet: {wallet_cat}, "
                          f"On-chain: {cat_onchain}, Diff: {result['cat_diff']}")

    return result
