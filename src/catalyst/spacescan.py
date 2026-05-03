"""On-chain verification client — the golden source of truth

Wraps the Spacescan Pro (`pro-api.spacescan.io`) and Free
(`api.spacescan.io`) APIs so the rest of the bot can confirm state from
the blockchain instead of trusting wallet RPC alone. The main entry
points are `is_coin_spent()`, `verify_fill()`, and the XCH / CAT balance
checks used by `fill_tracker` and reconciliation logic. A tier-aware
non-blocking rate limiter applies a 2 s interval on paid keys and 12 s
on the free tier, plus a per-day call budget (roughly 30 calls/day ~=
1000/month) to keep free-tier usage inside published limits.

Key responsibilities:
    - Query coin-spent state for fill verification
    - Query XCH and CAT balances for wallet reconciliation
    - Route Pro vs Free endpoints based on configured API key
    - Enforce rate and daily-budget limits without blocking the caller
"""

import re
import threading
import time
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Set

import requests

from config import cfg
from database import log_event


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_last_call_time = 0.0  # Rate limiting: track last API call
_rate_limited_until = 0.0  # Non-blocking 429 backoff timestamp

# Process-wide lock around _spacescan_get so parallel threads (e.g. the
# 6 fill-verify workers) serialize their API access. Without this, six
# threads each pass _rate_limit() at the same instant (since elapsed >
# interval is true for all of them simultaneously), all hit the endpoint,
# all get 429, all log spacescan_rate_limited + spacescan_verify_failed —
# turning one cooldown into 12 warning lines.
_api_lock = threading.Lock()

# Per-event-type warning dedup: only log the first occurrence inside any
# active cooldown window. Reset when cooldown clears or a successful call
# proves the API is healthy again.
_last_warned: Dict[str, float] = {}
_WARN_DEDUP_WINDOW = 60.0  # seconds


def _maybe_log_warn(event_type: str, message: str) -> None:
    """Log a warning at most once per _WARN_DEDUP_WINDOW per event_type.

    Suppresses cascade noise when N parallel callers hit the same failure
    mode (rate-limit, timeout, verify-failed) inside one cooldown.
    """
    now = time.time()
    last = _last_warned.get(event_type, 0.0)
    if now - last >= _WARN_DEDUP_WINDOW:
        _last_warned[event_type] = now
        log_event("warning", event_type, message)

# Tier-aware rate limiting and call budgeting
# Free plan limits are documented publicly by Spacescan.
# Paid plans vary, so the paid-key interval here is just the app's faster default.
_PRO_CALL_INTERVAL = 2.0   # seconds between calls when a paid key is configured
_FREE_CALL_INTERVAL = 12.0  # seconds between calls on the free tier

# Monthly call budget tracking (resets on module load / bot restart)
_calls_this_session = 0
_session_start_time = time.time()
_calls_by_endpoint: Dict[str, int] = {}

# Free tier daily budget — reserve most for fill verification
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


def _reset_daily_counter_if_needed() -> None:
    """Reset the runtime daily counter after midnight."""
    global _calls_today, _today_date
    import datetime
    today = datetime.date.today().isoformat()
    if today != _today_date:
        _today_date = today
        _calls_today = 0


def _endpoint_label(endpoint: Optional[str]) -> str:
    """Return a compact, non-identifying label for diagnostics."""
    path = str(endpoint or "").split("?", 1)[0].strip() or "unknown"
    replacements = (
        ("/coin/info/", "/coin/info/{coin_id}"),
        ("/address/xch-balance/", "/address/xch-balance/{address}"),
        ("/address/token-balance/", "/address/token-balance/{address}"),
        ("/token/info/", "/token/info/{asset_id}"),
        ("/token/holders/", "/token/holders/{asset_id}"),
        ("/cat/transactions/", "/cat/transactions/{asset_id}"),
    )
    for prefix, label in replacements:
        if path.startswith(prefix):
            return label
    return path


def _record_successful_call(count: int = 1, endpoint: Optional[str] = None) -> None:
    """Increment counters for successful Spacescan responses."""
    global _calls_this_session, _calls_today
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    if count <= 0:
        return
    _reset_daily_counter_if_needed()
    _calls_this_session += count
    _calls_today += count
    if endpoint:
        label = _endpoint_label(endpoint)
        _calls_by_endpoint[label] = int(_calls_by_endpoint.get(label, 0) or 0) + count


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
    global _calls_today

    _reset_daily_counter_if_needed()

    # Paid-key mode: plan-specific quotas are not modeled here
    if is_pro_tier():
        return True

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
    global _calls_this_session, _calls_today, _rate_limited_until, _last_warned

    # Cheap pre-lock cooldown gate — no point queuing on the lock if we're
    # going to no-op anyway. Threads inside the cooldown window return
    # immediately and don't compete for the API slot.
    if time.time() < _rate_limited_until:
        return None

    # Serialize across threads. Without this, six fill-verify workers can
    # pass _rate_limit() at the same instant and all hit the endpoint
    # simultaneously, turning a single cooldown into N rate-limit logs.
    with _api_lock:
        # Re-check the cooldown after acquiring the lock — another thread
        # may have just hit a 429 while we were waiting.
        if time.time() < _rate_limited_until:
            return None

        _rate_limit()

        url = f"{_get_base_url()}{endpoint}"
        timeout = getattr(cfg, "SPACESCAN_TIMEOUT", 10)

        try:
            response = requests.get(url, headers=_get_headers(), timeout=timeout)

            if response.status_code == 429:
                _maybe_log_warn("spacescan_rate_limited",
                                "Spacescan rate limit hit — backing off 60s (non-blocking)")
                _rate_limited_until = time.time() + 60
                return None

            if response.status_code == 404:
                log_event("debug", "spacescan_not_found",
                          f"Spacescan 404 for {endpoint[:60]}...")
                return None

            if response.status_code >= 500:
                _maybe_log_warn("spacescan_server_error",
                                f"Spacescan server error {response.status_code}")
                return None

            response.raise_for_status()
            data = response.json()

            if data.get("status") != "success":
                _maybe_log_warn("spacescan_api_error",
                                f"Spacescan returned non-success: {data.get('status')}")
                return None

            _record_successful_call(endpoint=endpoint)
            # Successful call — clear the dedup memory so a NEW failure
            # surfaces immediately instead of being silenced for 60s.
            _last_warned.clear()
            return data

        except requests.exceptions.Timeout:
            _maybe_log_warn("spacescan_timeout",
                            f"Spacescan request timed out after {timeout}s")
            return None
        except requests.exceptions.ConnectionError:
            _maybe_log_warn("spacescan_connection_error",
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
        # offer_info contains the canonical offer status directly from Spacescan.
        # More reliable than receiver_address for CAT coins where the settlement
        # puzzle always returns sender==receiver==maker (our own address), making
        # it impossible to distinguish a fill from a cancel via receiver alone.
        "offer_info": coin.get("offer_info") or [],
        # child_coins are the UTXOs created when this coin was spent.
        # For offer settlement, child coins reveal the taker's address even
        # when the top-level receiver field is misleading.
        "child_coins": data.get("coins") or [],
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


def _known_wallet_addresses_for_verification(result: Dict, our_address: str,
                                             explicit_addresses: Optional[set[str]] = None) -> Set[str]:
    """Build the address set trusted for one coin verification.

    Spacescan's related coin graph can reveal wallet change addresses through
    parent/sibling entries. Learn those locally only when the graph is anchored
    by an address we already trust. Child owners are deliberately excluded from
    learning because they are the taker destinations on real fills.
    """
    known_wallet_addresses: Set[str] = set()
    try:
        known_wallet_addresses.update(_get_known_wallet_addresses())
    except Exception:
        pass

    if explicit_addresses:
        try:
            known_wallet_addresses.update(
                str(item or "").strip() for item in explicit_addresses if str(item or "").strip()
            )
        except Exception:
            pass

    our_addr = str(our_address or "").strip()
    if our_addr:
        known_wallet_addresses.add(our_addr)

    related_owners: Set[str] = set()
    for coin in result.get("child_coins") or []:
        cointype = str(coin.get("cointype") or "").strip().lower()
        if cointype not in {"parent", "sibling", "siblings"}:
            continue
        owner = str(coin.get("owner_address") or "").strip()
        if owner:
            related_owners.add(owner)

    sender = str(result.get("sender_address") or "").strip()
    receiver = str(result.get("receiver_address") or "").strip()
    graph_is_ours = (
        any(owner in known_wallet_addresses for owner in related_owners)
        or (sender and sender in known_wallet_addresses)
        or (receiver and receiver in known_wallet_addresses)
    )
    if graph_is_ours:
        known_wallet_addresses.update(related_owners)

    return known_wallet_addresses


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
        # Deduped: when a rate-limit cascade hits 6 fill-verify threads at
        # once, we used to log one of these per thread. The first call
        # already logged the underlying cause (rate_limited / timeout); a
        # per-coin verify_failed for each one just adds noise.
        _maybe_log_warn("spacescan_verify_failed",
                        f"Cannot verify coin {coin_id[:16]}... — API error "
                        "(further verify failures suppressed for 60s).")
        return None  # Distinct from False (explicit rejection)

    if not result["spent"]:
        log_event("info", "spacescan_phantom_detected",
                  f"Coin {coin_id[:16]}... NOT spent on-chain. "
                  f"Phantom fill prevented!")
        return False

    # -----------------------------------------------------------------------
    # Priority 1: Use Spacescan's offer_info — the most reliable signal.
    #
    # For CAT offer coins, the top-level `receiver` field always shows the
    # MAKER's own address (the settlement puzzle is "owned" by the maker),
    # making it impossible to distinguish a fill from a cancel via receiver
    # alone. offer_info is populated directly from the spend bundle and
    # unambiguously tells us whether the offer was completed or cancelled.
    #
    # Observed status codes (cross-referenced with Dexie):
    #   4 = completed / filled   → return True
    #   3 = cancelled / expired  → return False
    #   1 = active / open        → coin may still be live; fall through
    # -----------------------------------------------------------------------
    offer_info = result.get("offer_info") or []
    if offer_info:
        # A coin can be associated with multiple offer files — the same coin is
        # reused when the bot cancels and reposts an offer (different Dexie hash,
        # same underlying UTXO). Spacescan lists ALL of them in offer_info.
        # Priority: status=4 (COMPLETED) ALWAYS wins over status=3 (CANCELLED).
        # If ANY version was filled, the coin was genuinely spent as a fill.
        _oi_completed = [oi for oi in offer_info if oi.get("offer_status") == 4]
        _oi_cancelled = [oi for oi in offer_info if oi.get("offer_status") == 3]

        if _oi_completed:
            oi_hash = str(_oi_completed[0].get("hash_base_58") or "")[:16]
            log_event("success", "spacescan_fill_via_offer_info",
                      f"Coin {coin_id[:16]}... offer_info reports status=4 "
                      f"(COMPLETED, offer {oi_hash}...) — confirmed fill.")
            return True
        elif _oi_cancelled:
            oi_hash = str(_oi_cancelled[0].get("hash_base_58") or "")[:16]
            log_event("info", "spacescan_cancel_via_offer_info",
                      f"Coin {coin_id[:16]}... offer_info reports status=3 "
                      f"(CANCELLED, offer {oi_hash}...) — not a fill.")
            return False
        # status=1 (active) or unknown: fall through to child-coin / receiver logic

    # -----------------------------------------------------------------------
    # Priority 2: Child coin analysis.
    #
    # When the offer coin is spent in a settlement, the child coins show
    # the actual destinations. If ANY child coin went to an external
    # (non-wallet) address, the offer was genuinely filled.
    #
    # This catches cases where offer_info is absent (e.g. non-offer spends)
    # and handles partial fills correctly (some CAT to taker + change to us).
    # -----------------------------------------------------------------------
    known_wallet_addresses = _known_wallet_addresses_for_verification(
        result, our_address, explicit_addresses
    )

    child_coins = result.get("child_coins") or []
    child_coin_entries = [c for c in child_coins if c.get("cointype") == "child"]
    if child_coin_entries:
        for child in child_coin_entries:
            owner = str(child.get("owner_address") or "").strip()
            if owner and owner not in known_wallet_addresses:
                log_event("success", "spacescan_fill_via_child_coin",
                          f"Coin {coin_id[:16]}... child coin went to external "
                          f"address {owner[:16]}... — confirmed fill.")
                return True
        # All children went to our own addresses = self-spend (cancel or internal tx)
        log_event("info", "spacescan_cancel_via_child_coins",
                  f"Coin {coin_id[:16]}... all {len(child_coin_entries)} child "
                  f"coin(s) owned by our wallet — not a fill.")
        return False

    # -----------------------------------------------------------------------
    # Priority 3: Fall back to top-level receiver field.
    #
    # Reliable for standard XCH coins but NOT for CAT offer coins.
    # Only reached when offer_info and child coins are both unavailable.
    # -----------------------------------------------------------------------
    receiver = result["receiver_address"]

    if receiver and receiver in known_wallet_addresses:
        log_event("info", "spacescan_self_spend",
                  f"Coin {coin_id[:16]}... spent to known own address "
                  f"{receiver[:16]}... Not a fill.")
        return False

    if (getattr(cfg, "WALLET_TYPE", "") == "sage" and
            not getattr(cfg, "SAGE_SET_CHANGE_ADDRESS", False)):
        log_event("warning", "spacescan_fill_ambiguous",
                  f"Coin {coin_id[:16]}... spent to {receiver[:16] if receiver else 'unknown'}..., "
                  f"but Sage change-address pinning is off. Treating as unverified.")
        return None

    # Coin spent to external address = CONFIRMED FILL
    log_event("success", "spacescan_fill_confirmed",
              f"CONFIRMED: Coin {coin_id[:16]}... spent to "
              f"{receiver[:16] if receiver else 'unknown'}... "
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
    except (KeyError, ValueError, InvalidOperation) as e:
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


def record_external_call(count: int = 1, endpoint: Optional[str] = None) -> None:
    """Increment session/daily call counters from external modules.

    market_data_collector makes Spacescan HTTP requests through its own
    helper (_spacescan_smart_get) rather than through _spacescan_get in
    this module.  This function lets it report those calls so the
    diagnostics panel shows accurate totals.
    """
    if isinstance(count, str) and endpoint is None:
        endpoint = count
        count = 1
    _record_successful_call(count=count, endpoint=endpoint)


def get_api_stats() -> Dict:
    """Return current API usage stats for monitoring/GUI display."""
    _reset_daily_counter_if_needed()
    uptime_hours = (time.time() - _session_start_time) / 3600
    return {
        "tier": "paid" if is_pro_tier() else "free",
        "calls_this_session": _calls_this_session,
        "calls_today": _calls_today,
        "calls_by_endpoint": dict(_calls_by_endpoint),
        "session_uptime_hours": round(uptime_hours, 1),
        "daily_budget": "plan-dependent" if is_pro_tier() else _FREE_DAILY_BUDGET,
        "call_interval_secs": _get_call_interval(),
    }


def should_check_balance() -> bool:
    """Helper for bot_loop: should we run the periodic balance check this loop?

    Free tier: balance checks are disabled entirely (budget reserved for fills).
    Sage mode: disabled because Sage reports whole-wallet balances while the
    Spacescan endpoint checks one address. Comparing those creates false drift
    warnings whenever prepared coins or live offers sit on other puzzle hashes.
    Paid-key mode for compatible wallets: bot_loop controls frequency separately.
    """
    if str(getattr(cfg, "WALLET_TYPE", "") or "").lower() == "sage":
        return False
    if not is_pro_tier():
        return False  # Free tier: skip balance checks, save budget for fills
    return True


def check_balance_discrepancy(our_address: str, wallet_xch: Decimal,
                               wallet_cat: Decimal = None,
                               cat_asset_id: str = None) -> Dict:
    """Compare wallet balances against on-chain truth.

    XCH comparison uses BASELINE CALIBRATION rather than raw diff:
      The Spacescan /address/xch-balance/ endpoint returns the sum of ALL
      coin value attributable to an address, including XCH locked inside
      CAT, NFT and DID puzzle wrappers. A market-maker wallet that holds
      any CAT/NFT will therefore always show a positive (on_chain − wallet)
      delta equal to the wrapped puzzle value — this is expected, not drift.

      On the first successful check we store `(on_chain − wallet)` as the
      baseline under the bot_settings key `spacescan_xch_baseline_delta`.
      On subsequent checks we alarm only if the current delta has moved
      away from the baseline by more than `SPACESCAN_BALANCE_THRESHOLD_XCH`.
      That catches real drift (coins unexpectedly appearing / disappearing)
      while ignoring the constant overhead.

      To force recalibration (after a sweep, wallet swap, etc.) delete the
      `spacescan_xch_baseline_delta` row from bot_settings.

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
            "xch_diff": Decimal or None,          # raw (on_chain − wallet)
            "xch_baseline_delta": Decimal or None,# expected constant delta
            "xch_drift": Decimal or None,         # |current_delta − baseline|
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
        "xch_diff": None, "xch_baseline_delta": None, "xch_drift": None,
        "xch_ok": True,
        "cat_onchain": None, "cat_wallet": wallet_cat,
        "cat_diff": None, "cat_ok": True,
    }

    # Check XCH
    xch_onchain = get_xch_balance(our_address)
    if xch_onchain is not None:
        result["xch_onchain"] = xch_onchain
        current_delta = xch_onchain - wallet_xch  # signed
        result["xch_diff"] = abs(current_delta)

        # Look up (or establish) the baseline delta for this address.
        # Key is address-specific so changing WALLET_ADDRESS forces
        # automatic recalibration.
        try:
            from database import get_setting, set_setting  # local import to avoid cycles
            baseline_key = f"spacescan_xch_baseline_delta:{our_address}"
            baseline_str = get_setting(baseline_key, None)

            if baseline_str is None:
                # First observation — establish the baseline and treat as OK.
                set_setting(baseline_key, str(current_delta))
                result["xch_baseline_delta"] = current_delta
                result["xch_drift"] = Decimal("0")
                result["xch_ok"] = True
                log_event("info", "spacescan_baseline_set",
                          f"Spacescan XCH baseline delta established: "
                          f"{current_delta} XCH (on-chain {xch_onchain} − "
                          f"wallet {wallet_xch}). This represents XCH held "
                          f"inside CAT/NFT/DID puzzles and is expected to "
                          f"stay roughly constant. Drift from this baseline "
                          f"beyond {threshold} XCH will raise a warning.")
            else:
                try:
                    baseline_delta = Decimal(baseline_str)
                except Exception:
                    baseline_delta = current_delta
                    set_setting(baseline_key, str(current_delta))

                drift = abs(current_delta - baseline_delta)
                result["xch_baseline_delta"] = baseline_delta
                result["xch_drift"] = drift
                result["xch_ok"] = drift <= threshold

                if not result["xch_ok"]:
                    log_event("warning", "balance_discrepancy_xch",
                              f"XCH balance drift from baseline! "
                              f"Wallet: {wallet_xch}, On-chain: {xch_onchain}, "
                              f"Current delta: {current_delta}, "
                              f"Baseline delta: {baseline_delta}, "
                              f"Drift: {drift} XCH (threshold {threshold}).")
        except Exception as e:
            # Baseline plumbing failed — fall back to raw-diff check so the
            # caller still gets a result, but note it.
            log_event("debug", "spacescan_baseline_fallback",
                      f"Baseline lookup failed ({e}); falling back to raw diff")
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
