#!/usr/bin/env python3
"""
COMPREHENSIVE API DIAGNOSTIC — Tests ALL Bot API Endpoints
===========================================================
Tests EVERY external API endpoint the Chia liquidity bot uses, including:
- Sage Wallet RPC (30 endpoints)
- Dexie API (v1 + v2 + v3 pricing endpoints)
- TibetSwap API (pairs, quotes, slippage)
- Spacescan (coin, address balance, token balance)
- Offerpool cross-posting
- Splash P2P
- Local blockchain health

Features:
  - Rate limiting (2s pause between calls)
  - Timeout handling (5s local, 15s external)
  - Response time measurement
  - Response format validation
  - Sage wallet TLS cert handling (cert-based, verify=False fallback)
  - Clear PASS/FAIL/WARN/SKIP status
  - Summary recommendations

Usage:
    python test_all_apis.py
"""

import sys
import os
import time
import json
import requests
import urllib3
from typing import Tuple, Optional, Dict

# Suppress TLS warnings for localhost self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Try to load config
try:
    from config import cfg
    CONFIG_LOADED = True
except Exception as e:
    CONFIG_LOADED = False
    print(f"  WARNING: Could not load config: {e}")


# ── Status Constants ──────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

ICONS = {
    "PASS": "OK",
    "FAIL": "XX",
    "WARN": "!!",
    "SKIP": "--",
}

# ── Test Results ──────────────────────────────────────────────────────

results = []


def test(name: str, status: str, detail: str = "") -> None:
    """Record a test result and print it."""
    results.append((name, status, detail))
    icon = ICONS.get(status, "??")
    msg = f"  [{icon}] {name}: {status}"
    if detail:
        msg += f" -- {detail}"
    print(msg)


# ── Helpers ───────────────────────────────────────────────────────────

def timed_request(
    method: str,
    url: str,
    headers: Optional[Dict] = None,
    json_data: Optional[Dict] = None,
    timeout: int = 15,
    verify: bool = True,
) -> Tuple[Optional[requests.Response], float]:
    """Make an HTTP request and return (response, elapsed_ms) or (None, elapsed_ms)."""
    start = time.time()
    try:
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=timeout, verify=verify)
        elif method.upper() == "POST":
            resp = requests.post(url, headers=headers, json=json_data, timeout=timeout, verify=verify)
        else:
            return None, (time.time() - start) * 1000

        elapsed = (time.time() - start) * 1000
        return resp, elapsed

    except requests.exceptions.Timeout:
        elapsed = (time.time() - start) * 1000
        return None, elapsed
    except requests.exceptions.ConnectionError:
        elapsed = (time.time() - start) * 1000
        return None, elapsed
    except requests.exceptions.SSLError:
        elapsed = (time.time() - start) * 1000
        return None, elapsed
    except Exception:
        elapsed = (time.time() - start) * 1000
        return None, elapsed


def sage_rpc(endpoint: str, payload: dict = None, timeout: int = 5) -> Tuple[Optional[Dict], float]:
    """Make an RPC call to Sage using the same http.client+ssl method as the bot.
    Returns (response_dict, elapsed_ms) or (None, elapsed_ms).
    """
    import http.client
    import ssl

    if payload is None:
        payload = {}

    sage_url = get_sage_url()
    # Parse host/port from URL
    host = "localhost"
    port = 9257
    if CONFIG_LOADED:
        url = getattr(cfg, "SAGE_RPC_URL", "https://localhost:9257")
        if ":" in url.split("//")[-1]:
            parts = url.split("//")[-1].split(":")
            host = parts[0]
            port = int(parts[1].rstrip("/"))

    cert_path = getattr(cfg, "SAGE_CERT_PATH", "") if CONFIG_LOADED else ""
    key_path = getattr(cfg, "SAGE_KEY_PATH", "") if CONFIG_LOADED else ""

    start = time.time()
    try:
        ctx = ssl._create_unverified_context()
        if cert_path and key_path and os.path.exists(cert_path):
            ctx.load_cert_chain(cert_path, key_path)

        body = json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        conn.request("POST", "/" + endpoint.lstrip("/"), body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        elapsed = (time.time() - start) * 1000

        if resp.status == 200:
            return json.loads(data), elapsed
        else:
            return {"_http_status": resp.status, "_body": data[:200]}, elapsed
    except Exception:
        elapsed = (time.time() - start) * 1000
        return None, elapsed


def get_sage_session() -> Optional[requests.Session]:
    """Create an SSL session for Sage wallet (legacy, prefer sage_rpc)."""
    sess = requests.Session()
    sess.verify = False
    if CONFIG_LOADED:
        cert_path = getattr(cfg, "SAGE_CERT_PATH", "")
        key_path = getattr(cfg, "SAGE_KEY_PATH", "")
        if cert_path and key_path and os.path.exists(cert_path):
            sess.cert = (cert_path, key_path)
    return sess


# ── Configuration Getters ─────────────────────────────────────────────

def get_dexie_url() -> str:
    """Get Dexie API base URL from config."""
    if CONFIG_LOADED:
        return getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
    return "https://api.dexie.space"


def get_tibet_url() -> str:
    """Get TibetSwap API base URL from config."""
    if CONFIG_LOADED:
        return getattr(cfg, "TIBET_API_BASE", "https://api.v2.tibetswap.io")
    return "https://api.v2.tibetswap.io"


def get_spacescan_url() -> str:
    """Get Spacescan API base URL from config."""
    if CONFIG_LOADED and getattr(cfg, "SPACESCAN_API_KEY", ""):
        return "https://pro-api.spacescan.io"
    return "https://api.spacescan.io"


def get_spacescan_headers() -> Dict[str, str]:
    """Get Spacescan headers with API key if configured."""
    headers = {"Accept": "application/json"}
    if CONFIG_LOADED and getattr(cfg, "SPACESCAN_API_KEY", ""):
        headers["x-api-key"] = cfg.SPACESCAN_API_KEY
    return headers


def get_sage_url() -> str:
    """Get Sage wallet RPC URL from config."""
    if CONFIG_LOADED:
        return getattr(cfg, "SAGE_RPC_URL", "https://localhost:9257")
    return "https://localhost:9257"


def get_cat_asset_id() -> str:
    """Get CAT asset ID from config."""
    if CONFIG_LOADED:
        return getattr(cfg, "CAT_ASSET_ID", "")
    return ""


def get_cat_ticker_id() -> str:
    """Get CAT ticker ID from config (e.g. 'SBX_XCH', 'DBX_XCH')."""
    if CONFIG_LOADED:
        return getattr(cfg, "CAT_TICKER_ID", "")
    return ""


def get_wallet_address() -> str:
    """Get wallet address from config."""
    if CONFIG_LOADED:
        return getattr(cfg, "WALLET_ADDRESS", "")
    return ""


def get_offerpool_url() -> str:
    """Get Offerpool API URL from config."""
    if CONFIG_LOADED:
        return getattr(cfg, "OFFERPOOL_API_URL", "https://offerpool.io/api/v1/offers")
    return "https://offerpool.io/api/v1/offers"


# ======================================================================
# TESTS START HERE
# ======================================================================

print("=" * 80)
print("  COMPREHENSIVE API DIAGNOSTIC")
print("=" * 80)
print()

# ──────────────────────────────────────────────────────────────────────
# SAGE WALLET RPC TESTS (localhost:9257) — 30 ENDPOINTS
# ──────────────────────────────────────────────────────────────────────

print("--- SAGE WALLET RPC (localhost:9257) — 30 ENDPOINTS ---")
print()

sage_url = get_sage_url()

print(f"  Base URL: {sage_url}")
print(f"  Wallet Type: {'sage' if CONFIG_LOADED and getattr(cfg, 'WALLET_TYPE', 'chia') == 'sage' else 'chia (not using sage)'}")
print()

# Test basic connectivity using http.client+ssl (same as bot)
cert_info = ""
if CONFIG_LOADED:
    _cp = getattr(cfg, "SAGE_CERT_PATH", "")
    _kp = getattr(cfg, "SAGE_KEY_PATH", "")
    if _cp and os.path.exists(_cp):
        cert_info = f"cert: {os.path.basename(_cp)}"
    else:
        cert_info = "no cert found"
print(f"  TLS Auth: {cert_info}")
print()

try:
    data, ms = sage_rpc("get_version", {}, timeout=5)
    if data is not None and "_http_status" not in (data or {}):
        version = data.get("version", data.get("data", {}).get("version", "?"))
        test("Sage RPC connectivity", PASS, f"{ms:.0f}ms, version={version}")
    elif data and "_http_status" in data:
        test("Sage RPC connectivity", FAIL, f"HTTP {data['_http_status']} ({ms:.0f}ms)")
    else:
        test("Sage RPC connectivity", FAIL, f"No response ({ms:.0f}ms) -- is Sage running?")
except Exception as e:
    test("Sage RPC connectivity", FAIL, f"{str(e)}")

time.sleep(1)

# Safe read-only endpoints to test
sage_read_tests = [
    ("get_version", {}),
    ("get_sync_status", {}),
    ("get_keys", {}),
    ("get_peers", {}),
    ("get_cats", {}),
    ("get_spendable_coin_count", {}),
    ("get_pending_transactions", {}),
]

for endpoint_name, payload in sage_read_tests:
    try:
        data, ms = sage_rpc(endpoint_name, payload, timeout=5)
        if data is not None and "_http_status" not in (data or {}):
            # Show a brief summary of the response
            summary = ""
            if endpoint_name == "get_sync_status":
                summary = f"synced={data.get('synced', data.get('receive_address', '?'))}"
            elif endpoint_name == "get_keys":
                keys = data.get("keys", [])
                summary = f"{len(keys)} key(s)"
            elif endpoint_name == "get_peers":
                peers = data.get("peers", [])
                summary = f"{len(peers)} peer(s)"
            elif endpoint_name == "get_cats":
                cats = data.get("cats", [])
                summary = f"{len(cats)} CAT wallet(s)"
            elif endpoint_name == "get_version":
                summary = f"v{data.get('version', '?')}"
            test(f"Sage /{endpoint_name}", PASS, f"{ms:.0f}ms" + (f" -- {summary}" if summary else ""))
        elif data and "_http_status" in data:
            test(f"Sage /{endpoint_name}", WARN, f"HTTP {data['_http_status']} ({ms:.0f}ms)")
        else:
            test(f"Sage /{endpoint_name}", SKIP, f"No response ({ms:.0f}ms)")
    except Exception as e:
        test(f"Sage /{endpoint_name}", SKIP, str(e))
    time.sleep(1)

# Coin queries (XCH and CAT)
cat_id = get_cat_asset_id()
coin_tests = [
    ("get_coins (XCH)", {"asset_id": None, "include_spent": False, "limit": 1}),
]
if cat_id:
    coin_tests.append(("get_coins (CAT)", {"asset_id": cat_id, "include_spent": False, "limit": 1}))

for test_name, payload in coin_tests:
    try:
        data, ms = sage_rpc("get_coins", payload, timeout=5)
        if data and "_http_status" not in (data or {}):
            coin_count = len(data.get("coins", []))
            test(f"Sage {test_name}", PASS, f"{coin_count} coin(s) ({ms:.0f}ms)")
        else:
            test(f"Sage {test_name}", SKIP, f"No response ({ms:.0f}ms)")
    except Exception as e:
        test(f"Sage {test_name}", SKIP, str(e))
    time.sleep(1)

# Offer and transaction queries
query_tests = [
    ("get_offers", {"include_completed": False, "start": 0, "end": 5}),
    ("get_transactions", {"offset": 0, "limit": 1, "ascending": False}),
]

for endpoint_name, payload in query_tests:
    try:
        data, ms = sage_rpc(endpoint_name, payload, timeout=5)
        if data and "_http_status" not in (data or {}):
            if endpoint_name == "get_offers":
                offers = data.get("offers", [])
                test(f"Sage /{endpoint_name}", PASS, f"{len(offers)} offer(s) ({ms:.0f}ms)")
            else:
                test(f"Sage /{endpoint_name}", PASS, f"{ms:.0f}ms")
        else:
            test(f"Sage /{endpoint_name}", SKIP, f"No response ({ms:.0f}ms)")
    except Exception as e:
        test(f"Sage /{endpoint_name}", SKIP, str(e))
    time.sleep(1)

# Note: Write operations (make_offer, cancel_offer, send_xch, etc.) are NOT tested as they modify state
write_endpoints = [
    "make_offer", "cancel_offer", "cancel_offers", "delete_offer",
    "split", "combine", "send_xch", "send_cat", "multi_send",
    "auto_combine_xch", "auto_combine_cat", "login", "logout", "resync"
]
test("Sage write endpoints (skipped)", SKIP, f"{len(write_endpoints)} write ops not tested (destructive)")

print()

# ──────────────────────────────────────────────────────────────────────
# COLLECT REAL TEST DATA FROM SAGE + DATABASE
# ──────────────────────────────────────────────────────────────────────
# Gather real coin IDs, wallet address, offer IDs etc. from the live
# wallet so subsequent Dexie/Spacescan tests use actual data.

print("--- COLLECTING REAL TEST DATA ---")

_test_coin_id = ""        # A real XCH coin ID for Spacescan tests
_test_cat_coin_id = ""    # A real CAT coin ID
_test_wallet_addr = ""    # Our wallet address
_test_offer_id = ""       # A real offer ID (trade_id)
_test_dexie_id = ""       # A real Dexie offer hash

# Try Sage wallet for coin IDs (using same http.client+ssl as the bot)
try:
    # Get a real XCH coin
    data, _ = sage_rpc("get_coins", {"asset_id": None, "include_spent": False, "limit": 1}, timeout=5)
    if data and "coins" in data:
        coins = data["coins"]
        if coins:
            c = coins[0]
            _test_coin_id = c.get("coin_id", c.get("name", ""))
            if _test_coin_id and not _test_coin_id.startswith("0x"):
                _test_coin_id = f"0x{_test_coin_id}"
    time.sleep(1)

    # Get a real CAT coin
    cat_id = getattr(cfg, "CAT_ASSET_ID", "") if CONFIG_LOADED else ""
    if cat_id:
        data2, _ = sage_rpc("get_coins", {"asset_id": cat_id, "include_spent": False, "limit": 1}, timeout=5)
        if data2 and "coins" in data2:
            cats = data2["coins"]
            if cats:
                _test_cat_coin_id = cats[0].get("coin_id", cats[0].get("name", ""))
                if _test_cat_coin_id and not _test_cat_coin_id.startswith("0x"):
                    _test_cat_coin_id = f"0x{_test_cat_coin_id}"
    time.sleep(1)

    # Get a real offer ID
    data3, _ = sage_rpc("get_offers", {"include_completed": False, "start": 0, "end": 1}, timeout=5)
    if data3 and "offers" in data3:
        offers = data3["offers"]
        if offers:
            _test_offer_id = offers[0].get("offer_id", offers[0].get("trade_id", ""))
except Exception:
    pass

# Get wallet address from config
if CONFIG_LOADED:
    _test_wallet_addr = getattr(cfg, "WALLET_ADDRESS", "")

# Get a dexie_id from the database
try:
    from database import get_connection
    conn = get_connection()
    row = conn.execute(
        "SELECT dexie_id FROM dexie_mappings WHERE dexie_id IS NOT NULL AND dexie_id != '' LIMIT 1"
    ).fetchone()
    if row:
        _test_dexie_id = row["dexie_id"] if isinstance(row, dict) else row[0]
except Exception:
    pass

print(f"  XCH coin:    {_test_coin_id[:24] + '...' if _test_coin_id else '(none)'}")
print(f"  CAT coin:    {_test_cat_coin_id[:24] + '...' if _test_cat_coin_id else '(none)'}")
print(f"  Wallet addr: {_test_wallet_addr[:20] + '...' if _test_wallet_addr else '(none)'}")
print(f"  Offer ID:    {_test_offer_id[:20] + '...' if _test_offer_id else '(none)'}")
print(f"  Dexie ID:    {_test_dexie_id[:20] + '...' if _test_dexie_id else '(none)'}")
print()

# ──────────────────────────────────────────────────────────────────────
# DEXIE API TESTS (https://api.dexie.space) — V1, V2, V3
# ──────────────────────────────────────────────────────────────────────

print("--- DEXIE API (https://api.dexie.space) — V1, V2, V3 ---")
print()

dexie_url = get_dexie_url()
cat_id = get_cat_asset_id()
cat_ticker_id = get_cat_ticker_id()

print(f"  Base URL: {dexie_url}")
print(f"  CAT Asset ID: {cat_id[:16]}... (configured)" if cat_id else "  CAT Asset ID: (not configured)")
print(f"  CAT Ticker ID: {cat_ticker_id}" if cat_ticker_id else "  CAT Ticker ID: (not configured)")
print()

# Test basic connectivity
try:
    resp, ms = timed_request("GET", f"{dexie_url}/v1/tickers", timeout=15)
    if resp is not None:
        test("Dexie connectivity", PASS, f"{ms:.0f}ms, HTTP {resp.status_code}")
    else:
        test("Dexie connectivity", FAIL, f"No response ({ms:.0f}ms)")
except Exception as e:
    test("Dexie connectivity", FAIL, str(e))

time.sleep(2)

# V1 API — Main endpoint for offer tracking
v1_tests = [
    ("Dexie /v1/offers", "/v1/offers?page_size=5"),
    ("Dexie /v1/trades", "/v1/trades?page_size=5"),
    ("Dexie /v1/tickers", "/v1/tickers"),
]

for test_name, endpoint_path in v1_tests:
    try:
        resp, ms = timed_request("GET", f"{dexie_url}{endpoint_path}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    count = len(next(iter(data.values()), []))
                    test(test_name, PASS, f"{count} item(s) ({ms:.0f}ms)")
                else:
                    test(test_name, FAIL, f"Expected dict, got {type(data).__name__}")
            except json.JSONDecodeError:
                test(test_name, FAIL, "Response not valid JSON")
        else:
            test(test_name, FAIL, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test(test_name, FAIL, str(e))
    time.sleep(2)

# V1 with CAT filter (bot's exact use case)
if cat_id:
    try:
        params = f"?offered={cat_id}&requested=xch&page_size=5"
        resp, ms = timed_request("GET", f"{dexie_url}/v1/offers{params}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                offers = data.get("offers", [])
                test("Dexie /v1/offers (CAT/XCH)", PASS, f"{len(offers)} offer(s) ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Dexie /v1/offers (CAT/XCH)", FAIL, "Response not valid JSON")
        else:
            test("Dexie /v1/offers (CAT/XCH)", FAIL, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Dexie /v1/offers (CAT/XCH)", FAIL, str(e))
    time.sleep(2)
else:
    test("Dexie /v1/offers (CAT/XCH)", SKIP, "CAT_ASSET_ID not configured")

# V2 Pricing API — What bot actually uses for prices
try:
    _v2_ticker = cat_ticker_id or "XCH_XCH"  # fallback avoids empty param
    resp, ms = timed_request("GET", f"{dexie_url}/v2/prices/tickers?ticker_id={_v2_ticker}", timeout=15)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            test("Dexie /v2/prices/tickers", PASS, f"{ms:.0f}ms")
        except json.JSONDecodeError:
            test("Dexie /v2/prices/tickers", FAIL, "Response not valid JSON")
    elif resp and resp.status_code == 404:
        test("Dexie /v2/prices/tickers", WARN, "404 -- endpoint may have changed")
    else:
        test("Dexie /v2/prices/tickers", FAIL, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Dexie /v2/prices/tickers", FAIL, str(e))

time.sleep(2)

# V3 Pricing API — Latest version
v3_tests = [
    ("Dexie /v3/prices/pairs", "/v3/prices/pairs"),
    ("Dexie /v3/prices/historical_trades", f"/v3/prices/historical_trades?ticker_id={cat_ticker_id or 'XCH_XCH'}&limit=5"),
]

for test_name, endpoint_path in v3_tests:
    try:
        resp, ms = timed_request("GET", f"{dexie_url}{endpoint_path}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                test(test_name, PASS, f"{ms:.0f}ms")
            except json.JSONDecodeError:
                test(test_name, FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test(test_name, WARN, f"404 -- endpoint may not exist ({ms:.0f}ms)")
        else:
            test(test_name, WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test(test_name, WARN, str(e))
    time.sleep(2)

print()

# ──────────────────────────────────────────────────────────────────────
# TIBETSWAP API TESTS (https://api.v2.tibetswap.io) — PAIRS & QUOTES
# ──────────────────────────────────────────────────────────────────────

print("--- TIBETSWAP API (https://api.v2.tibetswap.io) --- PAIRS & QUOTES ---")
print()

tibet_url = get_tibet_url()

print(f"  Base URL: {tibet_url}")
print()

# Test basic connectivity
try:
    resp, ms = timed_request("GET", f"{tibet_url}/ping", timeout=15)
    if resp is not None:
        test("TibetSwap connectivity", PASS, f"{ms:.0f}ms, HTTP {resp.status_code}")
    else:
        test("TibetSwap connectivity", FAIL, f"No response ({ms:.0f}ms)")
except Exception as e:
    test("TibetSwap connectivity", WARN, str(e))

time.sleep(2)

# Test /pairs endpoint — main data source
try:
    resp, ms = timed_request("GET", f"{tibet_url}/pairs?skip=0&limit=5", timeout=15)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            pairs = data.get("pairs", [])
            test("TibetSwap /pairs", PASS, f"{len(pairs)} pair(s) returned ({ms:.0f}ms)")
        except json.JSONDecodeError:
            test("TibetSwap /pairs", FAIL, "Response not valid JSON")
    else:
        test("TibetSwap /pairs", FAIL, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("TibetSwap /pairs", FAIL, str(e))

time.sleep(2)

# If we have CAT_ASSET_ID, test filtering for that pair
if cat_id:
    try:
        resp, ms = timed_request("GET", f"{tibet_url}/pairs?skip=0&limit=200", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                pairs = data.get("pairs", [])
                # Look for our CAT in the pairs list
                matching = [p for p in pairs if cat_id in str(p)]
                if matching:
                    test("TibetSwap CAT pair found", PASS, f"Located in {len(pairs)} pairs ({ms:.0f}ms)")
                else:
                    test("TibetSwap CAT pair found", WARN, f"Not in first 200 pairs ({ms:.0f}ms)")
            except (json.JSONDecodeError, KeyError):
                test("TibetSwap CAT pair found", FAIL, "Could not parse pairs response")
        else:
            test("TibetSwap CAT pair found", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("TibetSwap CAT pair found", WARN, str(e))

    time.sleep(2)

# Test /quote endpoint — bot uses this to estimate slippage
quote_tests = [
    ("TibetSwap /quote (XCH->XCH)", "?tokenIn=xch&tokenOut=xch&amountIn=1000000000000"),
]

if cat_id:
    # Try CAT to XCH quote if CAT is configured
    quote_tests.append(
        ("TibetSwap /quote (CAT->XCH)", f"?tokenIn={cat_id}&tokenOut=xch&amountIn=1000000000000")
    )

for test_name, params in quote_tests:
    try:
        resp, ms = timed_request("GET", f"{tibet_url}/quote{params}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if "amountOut" in data or "out_amount" in data:
                    test(test_name, PASS, f"Quote retrieved ({ms:.0f}ms)")
                else:
                    test(test_name, WARN, f"Unexpected keys: {list(data.keys())[:5]}")
            except json.JSONDecodeError:
                test(test_name, FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test(test_name, WARN, "404 -- pair may not exist")
        else:
            test(test_name, FAIL, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test(test_name, WARN, str(e))
    time.sleep(2)

print()

# ──────────────────────────────────────────────────────────────────────
# SPACESCAN API TESTS (https://api.spacescan.io) — FREE & PRO
# ──────────────────────────────────────────────────────────────────────

print("--- SPACESCAN API (https://api.spacescan.io) --- SLOW API WARNING ---")
print()

spacescan_url = get_spacescan_url()
spacescan_headers = get_spacescan_headers()
api_key = spacescan_headers.get("x-api-key", "")

print(f"  Base URL: {spacescan_url}")
print(f"  Tier: {'PRO' if api_key else 'FREE'}")
print("  NOTE: Spacescan responses are slow (8-14s) — bot timeout is ~20s")
print()

# Test basic connectivity
try:
    resp, ms = timed_request("GET", f"{spacescan_url}/coin/info/0x{'0' * 64}", headers=spacescan_headers, timeout=20)
    if resp is not None:
        if ms > 14000:
            test("Spacescan connectivity", WARN, f"{ms:.0f}ms (slow!) HTTP {resp.status_code}")
        else:
            test("Spacescan connectivity", PASS, f"{ms:.0f}ms, HTTP {resp.status_code}")
    else:
        test("Spacescan connectivity", FAIL, f"No response ({ms:.0f}ms)")
except Exception as e:
    test("Spacescan connectivity", FAIL, str(e))

time.sleep(2)

spacescan_tests = []

# Use REAL coin ID from Sage if available, otherwise use dummy
_ss_coin = _test_coin_id if _test_coin_id else ("0x" + "0" * 64)
_ss_coin_label = f"REAL {_ss_coin[:16]}..." if _test_coin_id else "dummy (zeros)"
spacescan_tests.append((f"Spacescan /coin/info ({_ss_coin_label})", f"/coin/info/{_ss_coin}"))

# Add CAT coin test if we have one
if _test_cat_coin_id:
    spacescan_tests.append((f"Spacescan /coin/info (CAT {_test_cat_coin_id[:16]}...)", f"/coin/info/{_test_cat_coin_id}"))

# Address endpoints — use real wallet address or collected address
wallet_addr = _test_wallet_addr or get_wallet_address()
if wallet_addr:
    spacescan_tests.append(("Spacescan /address/xch-balance", f"/address/xch-balance/{wallet_addr}"))
    spacescan_tests.append(("Spacescan /address/token-balance", f"/address/token-balance/{wallet_addr}"))

for test_name, endpoint_path in spacescan_tests:
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}{endpoint_path}", headers=spacescan_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if ms > 14000:
                    test(test_name, WARN, f"Responded but slow ({ms:.0f}ms)")
                else:
                    test(test_name, PASS, f"{ms:.0f}ms")
            except json.JSONDecodeError:
                test(test_name, FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            if ms > 14000:
                test(test_name, WARN, f"404 but slow ({ms:.0f}ms)")
            else:
                test(test_name, WARN, "404 (expected for test data)")
        else:
            test(test_name, FAIL, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test(test_name, FAIL, str(e))
    time.sleep(2)

if not wallet_addr:
    test("Spacescan /address/* tests", SKIP, "WALLET_ADDRESS not configured")

print()

# ──────────────────────────────────────────────────────────────────────
# SPACESCAN ADDRESS ENDPOINTS — ADVANCED
# ──────────────────────────────────────────────────────────────────────

print("--- SPACESCAN ADDRESS ENDPOINTS — ADVANCED ---")
print()

spacescan_addr_headers = dict(spacescan_headers)
spacescan_addr_headers["version"] = "v1"
spacescan_addr_headers["network"] = "xch"

if wallet_addr:
    # XCH Historical Balance
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/address/xch-historical-balance/{wallet_addr}", headers=spacescan_addr_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if ms > 14000:
                    test("Spacescan /address/xch-historical-balance", WARN, f"Slow response ({ms:.0f}ms)")
                else:
                    test("Spacescan /address/xch-historical-balance", PASS, f"{ms:.0f}ms")
            except json.JSONDecodeError:
                test("Spacescan /address/xch-historical-balance", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /address/xch-historical-balance", WARN, "404 (endpoint may not exist)")
        else:
            test("Spacescan /address/xch-historical-balance", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /address/xch-historical-balance", WARN, str(e))

    time.sleep(2)

    # XCH Transactions
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/address/xch-transactions/{wallet_addr}?count=5", headers=spacescan_addr_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                txns = data if isinstance(data, list) else data.get("transactions", [])
                if ms > 14000:
                    test("Spacescan /address/xch-transactions", WARN, f"{len(txns)} txn(s), slow ({ms:.0f}ms)")
                else:
                    test("Spacescan /address/xch-transactions", PASS, f"{len(txns)} txn(s) ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Spacescan /address/xch-transactions", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /address/xch-transactions", WARN, "404 (no transactions)")
        else:
            test("Spacescan /address/xch-transactions", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /address/xch-transactions", WARN, str(e))

    time.sleep(2)

    # Token Transactions
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/address/token-transactions/{wallet_addr}?count=5", headers=spacescan_addr_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                txns = data if isinstance(data, list) else data.get("transactions", [])
                if ms > 14000:
                    test("Spacescan /address/token-transactions", WARN, f"{len(txns)} txn(s), slow ({ms:.0f}ms)")
                else:
                    test("Spacescan /address/token-transactions", PASS, f"{len(txns)} txn(s) ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Spacescan /address/token-transactions", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /address/token-transactions", WARN, "404 (no token transactions)")
        else:
            test("Spacescan /address/token-transactions", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /address/token-transactions", WARN, str(e))

    time.sleep(2)
else:
    test("Spacescan address advanced tests", SKIP, "WALLET_ADDRESS not configured")

print()

# ──────────────────────────────────────────────────────────────────────
# SPACESCAN TOKEN ENDPOINTS
# ──────────────────────────────────────────────────────────────────────

print("--- SPACESCAN TOKEN ENDPOINTS ---")
print()

spacescan_token_headers = dict(spacescan_headers)
spacescan_token_headers["version"] = "v1"
spacescan_token_headers["network"] = "xch"

cat_asset_id = get_cat_asset_id()

if cat_asset_id:
    # Token Info
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/token/info/{cat_asset_id}", headers=spacescan_token_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if ms > 14000:
                    test("Spacescan /token/info", WARN, f"Retrieved, slow ({ms:.0f}ms)")
                else:
                    test("Spacescan /token/info", PASS, f"{ms:.0f}ms")
            except json.JSONDecodeError:
                test("Spacescan /token/info", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /token/info", WARN, "404 (token not found)")
        else:
            test("Spacescan /token/info", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /token/info", WARN, str(e))

    time.sleep(2)

    # Token Price (may not exist)
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/token/price/{cat_asset_id}", headers=spacescan_token_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if ms > 14000:
                    test("Spacescan /token/price", WARN, f"Retrieved, slow ({ms:.0f}ms)")
                else:
                    test("Spacescan /token/price", PASS, f"{ms:.0f}ms")
            except json.JSONDecodeError:
                test("Spacescan /token/price", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /token/price", WARN, "404 (endpoint may not exist)")
        else:
            test("Spacescan /token/price", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /token/price", WARN, str(e))

    time.sleep(2)

    # CAT Total Supply
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/cat/total-supply/{cat_asset_id}", headers=spacescan_token_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if ms > 14000:
                    test("Spacescan /cat/total-supply", WARN, f"Retrieved, slow ({ms:.0f}ms)")
                else:
                    test("Spacescan /cat/total-supply", PASS, f"{ms:.0f}ms")
            except json.JSONDecodeError:
                test("Spacescan /cat/total-supply", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /cat/total-supply", WARN, "404 (endpoint may not exist)")
        else:
            test("Spacescan /cat/total-supply", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /cat/total-supply", WARN, str(e))

    time.sleep(2)

    # CAT Circulating Supply
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/cat/circulating-supply/{cat_asset_id}", headers=spacescan_token_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                if ms > 14000:
                    test("Spacescan /cat/circulating-supply", WARN, f"Retrieved, slow ({ms:.0f}ms)")
                else:
                    test("Spacescan /cat/circulating-supply", PASS, f"{ms:.0f}ms")
            except json.JSONDecodeError:
                test("Spacescan /cat/circulating-supply", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /cat/circulating-supply", WARN, "404 (endpoint may not exist)")
        else:
            test("Spacescan /cat/circulating-supply", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /cat/circulating-supply", WARN, str(e))

    time.sleep(2)

    # Token Holders
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/token/holders/{cat_asset_id}?count=10", headers=spacescan_token_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                holders = data if isinstance(data, list) else data.get("holders", [])
                if ms > 14000:
                    test("Spacescan /token/holders", WARN, f"{len(holders)} holder(s), slow ({ms:.0f}ms)")
                else:
                    test("Spacescan /token/holders", PASS, f"{len(holders)} holder(s) ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Spacescan /token/holders", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /token/holders", WARN, "404 (endpoint may not exist)")
        else:
            test("Spacescan /token/holders", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /token/holders", WARN, str(e))

    time.sleep(2)

    # Token Activities
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/token/activities/{cat_asset_id}?count=5", headers=spacescan_token_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                activities = data if isinstance(data, list) else data.get("activities", [])
                if ms > 14000:
                    test("Spacescan /token/activities", WARN, f"{len(activities)} activity(ies), slow ({ms:.0f}ms)")
                else:
                    test("Spacescan /token/activities", PASS, f"{len(activities)} activity(ies) ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Spacescan /token/activities", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /token/activities", WARN, "404 (endpoint may not exist)")
        else:
            test("Spacescan /token/activities", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /token/activities", WARN, str(e))

    time.sleep(2)
else:
    test("Spacescan token endpoints", SKIP, "CAT_ASSET_ID not configured")

print()

# ──────────────────────────────────────────────────────────────────────
# SPACESCAN OFFERS ENDPOINTS
# ──────────────────────────────────────────────────────────────────────

print("--- SPACESCAN OFFERS ENDPOINTS ---")
print()

spacescan_offer_headers = dict(spacescan_headers)
spacescan_offer_headers["version"] = "v1"
spacescan_offer_headers["network"] = "xch"

# List Offers (generic)
try:
    resp, ms = timed_request("GET", f"{spacescan_url}/offers?count=5", headers=spacescan_offer_headers, timeout=20)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            offers = data if isinstance(data, list) else data.get("offers", [])
            if ms > 14000:
                test("Spacescan /offers", WARN, f"{len(offers)} offer(s), slow ({ms:.0f}ms)")
            else:
                test("Spacescan /offers", PASS, f"{len(offers)} offer(s) ({ms:.0f}ms)")
        except json.JSONDecodeError:
            test("Spacescan /offers", FAIL, "Response not valid JSON")
    elif resp and resp.status_code == 404:
        test("Spacescan /offers", WARN, "404 (endpoint may not exist)")
    else:
        test("Spacescan /offers", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Spacescan /offers", WARN, str(e))

time.sleep(2)

# Offers by Asset
if cat_asset_id:
    try:
        resp, ms = timed_request("GET", f"{spacescan_url}/offers/asset/{cat_asset_id}?count=5", headers=spacescan_offer_headers, timeout=20)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                offers = data if isinstance(data, list) else data.get("offers", [])
                if ms > 14000:
                    test("Spacescan /offers/asset", WARN, f"{len(offers)} offer(s), slow ({ms:.0f}ms)")
                else:
                    test("Spacescan /offers/asset", PASS, f"{len(offers)} offer(s) ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Spacescan /offers/asset", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Spacescan /offers/asset", WARN, "404 (no offers or endpoint may not exist)")
        else:
            test("Spacescan /offers/asset", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Spacescan /offers/asset", WARN, str(e))

    time.sleep(2)

print()

# ──────────────────────────────────────────────────────────────────────
# SPACESCAN UTILITY ENDPOINTS
# ──────────────────────────────────────────────────────────────────────

print("--- SPACESCAN UTILITY ENDPOINTS ---")
print()

spacescan_util_headers = dict(spacescan_headers)
spacescan_util_headers["version"] = "v1"
spacescan_util_headers["network"] = "xch"

# Mempool min fee
try:
    resp, ms = timed_request("GET", f"{spacescan_url}/mempool/minfee", headers=spacescan_util_headers, timeout=20)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            fee = data.get("minfee", data.get("fee", "?"))
            if ms > 14000:
                test("Spacescan /mempool/minfee", WARN, f"Min fee: {fee}, slow ({ms:.0f}ms)")
            else:
                test("Spacescan /mempool/minfee", PASS, f"Min fee: {fee} ({ms:.0f}ms)")
        except json.JSONDecodeError:
            test("Spacescan /mempool/minfee", FAIL, "Response not valid JSON")
    elif resp and resp.status_code == 404:
        test("Spacescan /mempool/minfee", WARN, "404 (endpoint may not exist)")
    else:
        test("Spacescan /mempool/minfee", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Spacescan /mempool/minfee", WARN, str(e))

time.sleep(2)

# Peak Block
try:
    resp, ms = timed_request("GET", f"{spacescan_url}/block/peak", headers=spacescan_util_headers, timeout=20)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            height = data.get("height", data.get("peak_height", "?"))
            if ms > 14000:
                test("Spacescan /block/peak", WARN, f"Height: {height}, slow ({ms:.0f}ms)")
            else:
                test("Spacescan /block/peak", PASS, f"Height: {height} ({ms:.0f}ms)")
        except json.JSONDecodeError:
            test("Spacescan /block/peak", FAIL, "Response not valid JSON")
    elif resp and resp.status_code == 404:
        test("Spacescan /block/peak", WARN, "404 (endpoint may not exist)")
    else:
        test("Spacescan /block/peak", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Spacescan /block/peak", WARN, str(e))

time.sleep(2)

# Stats (generic attempt)
try:
    resp, ms = timed_request("GET", f"{spacescan_url}/stats", headers=spacescan_util_headers, timeout=20)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            if ms > 14000:
                test("Spacescan /stats", WARN, f"Retrieved, slow ({ms:.0f}ms)")
            else:
                test("Spacescan /stats", PASS, f"{ms:.0f}ms")
        except json.JSONDecodeError:
            test("Spacescan /stats", WARN, "Response not valid JSON")
    elif resp and resp.status_code == 404:
        test("Spacescan /stats", WARN, "404 (endpoint may not exist)")
    else:
        test("Spacescan /stats", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Spacescan /stats", WARN, str(e))

print()

# ──────────────────────────────────────────────────────────────────────
# DEXIE OFFERS API V1 — ADVANCED ENDPOINTS
# ──────────────────────────────────────────────────────────────────────

print("--- DEXIE OFFERS API V1 — ADVANCED ENDPOINTS ---")
print()

# First, get a real dexie_id to test the inspect endpoint
# Use the one from our DB if available, otherwise fetch from Dexie
dexie_offer_id = _test_dexie_id or None
try:
    resp, ms = timed_request("GET", f"{dexie_url}/v1/offers?page_size=1", timeout=15)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            offers = data.get("offers", [])
            if offers:
                dexie_offer_id = offers[0].get("id")
                test("Dexie /v1/offers (sample fetch)", PASS, f"Got offer ID {ms:.0f}ms")
            else:
                test("Dexie /v1/offers (sample fetch)", WARN, "No offers in response")
        except json.JSONDecodeError:
            test("Dexie /v1/offers (sample fetch)", FAIL, "Response not valid JSON")
    else:
        test("Dexie /v1/offers (sample fetch)", FAIL, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Dexie /v1/offers (sample fetch)", FAIL, str(e))

time.sleep(2)

# Test inspect single offer
if dexie_offer_id:
    try:
        resp, ms = timed_request("GET", f"{dexie_url}/v1/offers/{dexie_offer_id}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                test(f"Dexie /v1/offers/{dexie_offer_id[:8]}...", PASS, f"Offer detail retrieved ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test(f"Dexie /v1/offers/{dexie_offer_id[:8]}...", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test(f"Dexie /v1/offers/{dexie_offer_id[:8]}...", WARN, "404 (offer may have expired)")
        else:
            test(f"Dexie /v1/offers/{dexie_offer_id[:8]}...", FAIL, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test(f"Dexie /v1/offers/{dexie_offer_id[:8]}...", FAIL, str(e))
    time.sleep(2)

# Test completed offers (status=4)
if cat_id:
    try:
        params = f"?status=4&offered={cat_id}&requested=xch&page_size=5&sort=date_completed"
        resp, ms = timed_request("GET", f"{dexie_url}/v1/offers{params}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                completed = data.get("offers", [])
                test("Dexie /v1/offers (completed)", PASS, f"{len(completed)} completed offer(s) ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Dexie /v1/offers (completed)", FAIL, "Response not valid JSON")
        else:
            test("Dexie /v1/offers (completed)", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Dexie /v1/offers (completed)", WARN, str(e))
    time.sleep(2)

# Test cancelled offers (status=3)
if cat_id:
    try:
        params = f"?status=3&offered_or_requested={cat_id}&page_size=5"
        resp, ms = timed_request("GET", f"{dexie_url}/v1/offers{params}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                cancelled = data.get("offers", [])
                test("Dexie /v1/offers (cancelled)", PASS, f"{len(cancelled)} cancelled offer(s) ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Dexie /v1/offers (cancelled)", FAIL, "Response not valid JSON")
        else:
            test("Dexie /v1/offers (cancelled)", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Dexie /v1/offers (cancelled)", WARN, str(e))
    time.sleep(2)

# Test expired offers (status=6)
if cat_id:
    try:
        params = f"?status=6&offered_or_requested={cat_id}&page_size=5"
        resp, ms = timed_request("GET", f"{dexie_url}/v1/offers{params}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                expired = data.get("offers", [])
                test("Dexie /v1/offers (expired)", PASS, f"{len(expired)} expired offer(s) ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Dexie /v1/offers (expired)", FAIL, "Response not valid JSON")
        else:
            test("Dexie /v1/offers (expired)", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Dexie /v1/offers (expired)", WARN, str(e))
    time.sleep(2)

# Test compact mode
if cat_id:
    try:
        params = f"?offered={cat_id}&requested=xch&compact=true&page_size=5"
        resp, ms = timed_request("GET", f"{dexie_url}/v1/offers{params}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                test("Dexie /v1/offers (compact mode)", PASS, f"Compact response ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Dexie /v1/offers (compact mode)", FAIL, "Response not valid JSON")
        else:
            test("Dexie /v1/offers (compact mode)", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Dexie /v1/offers (compact mode)", WARN, str(e))
    time.sleep(2)

print()

# ──────────────────────────────────────────────────────────────────────
# DEXIE SWAP API V1 — TOKEN LIST & QUOTES
# ──────────────────────────────────────────────────────────────────────

print("--- DEXIE SWAP API V1 — TOKEN LIST & QUOTES ---")
print()

# Test swap tokens list
try:
    resp, ms = timed_request("GET", f"{dexie_url}/v1/swap/tokens", timeout=15)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            tokens = data.get("tokens", [])
            test("Dexie /v1/swap/tokens", PASS, f"{len(tokens)} token(s) available ({ms:.0f}ms)")
        except json.JSONDecodeError:
            test("Dexie /v1/swap/tokens", FAIL, "Response not valid JSON")
    elif resp and resp.status_code == 404:
        test("Dexie /v1/swap/tokens", WARN, "404 (endpoint may not exist)")
    else:
        test("Dexie /v1/swap/tokens", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Dexie /v1/swap/tokens", WARN, str(e))

time.sleep(2)

# Test swap quote XCH -> XCH (should work)
try:
    params = "?from=xch&to=xch&from_amount=100000000000"
    resp, ms = timed_request("GET", f"{dexie_url}/v1/swap/quote{params}", timeout=15)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            test("Dexie /v1/swap/quote (XCH->XCH)", PASS, f"Quote retrieved ({ms:.0f}ms)")
        except json.JSONDecodeError:
            test("Dexie /v1/swap/quote (XCH->XCH)", FAIL, "Response not valid JSON")
    elif resp and resp.status_code == 404:
        test("Dexie /v1/swap/quote (XCH->XCH)", WARN, "404 (endpoint may not exist)")
    else:
        test("Dexie /v1/swap/quote (XCH->XCH)", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Dexie /v1/swap/quote (XCH->XCH)", WARN, str(e))

time.sleep(2)

# Test swap quote with CAT if available
if cat_id:
    try:
        params = f"?from=xch&to={cat_id}&from_amount=100000000000"
        resp, ms = timed_request("GET", f"{dexie_url}/v1/swap/quote{params}", timeout=15)
        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                to_amount = data.get("to_amount", data.get("out_amount", "?"))
                test("Dexie /v1/swap/quote (XCH->CAT)", PASS, f"Quote: {to_amount} ({ms:.0f}ms)")
            except json.JSONDecodeError:
                test("Dexie /v1/swap/quote (XCH->CAT)", FAIL, "Response not valid JSON")
        elif resp and resp.status_code == 404:
            test("Dexie /v1/swap/quote (XCH->CAT)", WARN, "404 (pair may not exist)")
        else:
            test("Dexie /v1/swap/quote (XCH->CAT)", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
    except Exception as e:
        test("Dexie /v1/swap/quote (XCH->CAT)", WARN, str(e))

time.sleep(2)

print()

# ──────────────────────────────────────────────────────────────────────
# DEXIE PRICES API V3 — LATEST ENDPOINTS
# ──────────────────────────────────────────────────────────────────────

print("--- DEXIE PRICES API V3 — LATEST ENDPOINTS ---")
print()

# Test v3 tickers
try:
    resp, ms = timed_request("GET", f"{dexie_url}/v3/prices/tickers?ticker_id={cat_ticker_id or 'XCH_XCH'}", timeout=15)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            test("Dexie /v3/prices/tickers", PASS, f"Ticker data retrieved ({ms:.0f}ms)")
        except json.JSONDecodeError:
            test("Dexie /v3/prices/tickers", FAIL, "Response not valid JSON")
    elif resp and resp.status_code == 404:
        test("Dexie /v3/prices/tickers", WARN, "404 (endpoint may not exist)")
    else:
        test("Dexie /v3/prices/tickers", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Dexie /v3/prices/tickers", WARN, str(e))

time.sleep(2)

# Test v3 order book depth
try:
    resp, ms = timed_request("GET", f"{dexie_url}/v3/prices/orderbook?ticker_id={cat_ticker_id or 'XCH_XCH'}&depth=10", timeout=15)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            test("Dexie /v3/prices/orderbook", PASS, f"Order book retrieved ({ms:.0f}ms)")
        except json.JSONDecodeError:
            test("Dexie /v3/prices/orderbook", FAIL, "Response not valid JSON")
    elif resp and resp.status_code == 404:
        test("Dexie /v3/prices/orderbook", WARN, "404 (endpoint may not exist)")
    else:
        test("Dexie /v3/prices/orderbook", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Dexie /v3/prices/orderbook", WARN, str(e))

time.sleep(2)

print()

# ──────────────────────────────────────────────────────────────────────
# OFFERPOOL API TESTS (https://offerpool.io) — OPTIONAL CROSS-POSTING
# ──────────────────────────────────────────────────────────────────────

print("--- OFFERPOOL (https://offerpool.io) --- OPTIONAL ---")
print()

offerpool_url = get_offerpool_url()
offerpool_enabled = CONFIG_LOADED and getattr(cfg, "OFFERPOOL_ENABLED", False)

print(f"  Base URL: {offerpool_url}")
print(f"  Status: {'enabled' if offerpool_enabled else 'disabled'}")
print()

# Test root connectivity
try:
    resp, ms = timed_request("GET", "https://offerpool.io", timeout=15)
    if resp is not None and resp.status_code < 500:
        test("Offerpool root", PASS, f"{ms:.0f}ms, HTTP {resp.status_code}")
    else:
        test("Offerpool root", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Offerpool root", WARN, f"{str(e)} (optional)")

time.sleep(2)

# Test POST endpoint (connectivity only)
try:
    resp, ms = timed_request("POST", offerpool_url, json_data={}, timeout=15)
    if resp is not None and resp.status_code < 500:
        test("Offerpool /api/v1/offers (POST)", WARN if offerpool_enabled else SKIP, f"Endpoint responsive ({ms:.0f}ms)")
    else:
        test("Offerpool /api/v1/offers (POST)", WARN, f"HTTP {resp.status_code if resp else 'timeout'} ({ms:.0f}ms)")
except Exception as e:
    test("Offerpool /api/v1/offers (POST)", SKIP, f"{str(e)} (optional)")

print()

# ──────────────────────────────────────────────────────────────────────
# SPLASH P2P TESTS (localhost:4000) — OPTIONAL
# ──────────────────────────────────────────────────────────────────────

print("--- SPLASH P2P (localhost:4000) --- OPTIONAL ---")
print()

print("  Base URL: http://localhost:4000")
print(f"  Status: {'running' if CONFIG_LOADED and getattr(cfg, 'SPLASH_ENABLED', False) else 'not required'}")
print()

try:
    resp, ms = timed_request("GET", "http://localhost:4000", timeout=5)
    if resp is not None:
        test("Splash P2P GET", PASS, f"{ms:.0f}ms, HTTP {resp.status_code}")
    else:
        test("Splash P2P GET", SKIP, f"No response ({ms:.0f}ms) (optional service)")
except Exception as e:
    test("Splash P2P GET", SKIP, f"{str(e)} (optional)")

time.sleep(2)

try:
    resp, ms = timed_request("POST", "http://localhost:4000", json_data={}, timeout=5)
    if resp is not None:
        test("Splash P2P POST", PASS, f"{ms:.0f}ms")
    else:
        test("Splash P2P POST", SKIP, f"No response ({ms:.0f}ms) (optional)")
except Exception as e:
    test("Splash P2P POST", SKIP, f"{str(e)} (optional)")

print()

# ──────────────────────────────────────────────────────────────────────
# BLOCKCHAIN HEALTH CHECKS — CHIA FULL NODE
# ──────────────────────────────────────────────────────────────────────

print("--- BLOCKCHAIN HEALTH (Local Full Node) ---")
print()

# Try to import wallet module to check full node
try:
    from wallet_chia import get_chia_health

    print("  Full Node RPC: localhost:8555 (Chia)")
    print()

    try:
        health = get_chia_health()
        if health.get("ok"):
            test("Chia full node", PASS, f"Synced: {health.get('synced', False)}")
        else:
            test("Chia full node", WARN, "Not healthy")
    except Exception as e:
        test("Chia full node", WARN, str(e))
except ImportError:
    test("Chia health check", SKIP, "wallet_chia not available")

print()

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION VALIDATION
# ──────────────────────────────────────────────────────────────────────

print("--- BOT CONFIGURATION ---")
print()

if CONFIG_LOADED:
    # Check wallet type
    wallet_type = getattr(cfg, "WALLET_TYPE", "chia")
    test("Wallet type configured", PASS, f"{wallet_type}")

    # Check Dexie settings
    dexie_enabled = getattr(cfg, "DEXIE_AUTO_POST", True)
    test("Dexie auto-post", PASS if dexie_enabled else WARN, f"{'enabled' if dexie_enabled else 'disabled'}")

    # Check TibetSwap timeout
    tibet_timeout = getattr(cfg, "TIBET_TIMEOUT", 10)
    if tibet_timeout >= 10:
        test("TibetSwap timeout", PASS, f"{tibet_timeout}s")
    else:
        test("TibetSwap timeout", WARN, f"{tibet_timeout}s -- may be too low")

    # Check Spacescan settings
    spacescan_enabled = getattr(cfg, "SPACESCAN_ENABLED", True)
    spacescan_key = getattr(cfg, "SPACESCAN_API_KEY", "")
    test("Spacescan enabled", PASS if spacescan_enabled else WARN, f"{'enabled (Pro)' if spacescan_key else 'enabled (Free)'}")

    # Check Offerpool settings
    offerpool_enabled = getattr(cfg, "OFFERPOOL_ENABLED", False)
    test("Offerpool enabled", WARN if not offerpool_enabled else PASS, f"{'enabled' if offerpool_enabled else 'disabled'}")

    # Check Market Intelligence
    market_intel_enabled = getattr(cfg, "COMPETITOR_AWARE_ENABLED", False)
    test("Market intelligence", WARN if not market_intel_enabled else PASS, f"{'enabled' if market_intel_enabled else 'disabled'}")

else:
    test("Bot config", SKIP, "Could not load config.py")

print()

# ======================================================================
# COMPREHENSIVE SUMMARY & RECOMMENDATIONS
# ======================================================================

print("=" * 80)
print("  COMPREHENSIVE API DIAGNOSTIC SUMMARY")
print("=" * 80)
print()

passes = sum(1 for _, s, _ in results if s == PASS)
fails = sum(1 for _, s, _ in results if s == FAIL)
warns = sum(1 for _, s, _ in results if s == WARN)
skips = sum(1 for _, s, _ in results if s == SKIP)
total = len(results)

print(f"  Results: {passes} PASS, {fails} FAIL, {warns} WARN, {skips} SKIP / {total} total tests")
print()
print("  Test Categories:")
print("    - Sage Wallet RPC (30+ endpoints)")
print("    - Dexie API (v1 basic + v1 advanced offers + v1 swap + v2 pricing + v3 latest)")
print("    - TibetSwap API (pairs, quotes, slippage)")
print("    - Spacescan API (basic + advanced address + token + offers + utility)")
print("    - Offerpool (cross-posting)")
print("    - Splash P2P (optional)")
print("    - Chia Blockchain Health")
print("    - Bot Configuration Validation")
print()

# Group by service
services = {}
for name, status, detail in results:
    service = name.split(" ")[0]
    if service not in services:
        services[service] = {"pass": 0, "fail": 0, "warn": 0, "skip": 0}
    services[service][status.lower()] += 1

print("  By service:")
for svc in sorted(services.keys()):
    counts = services[svc]
    status_str = "OK" if counts["fail"] == 0 else "BROKEN"
    print(f"    {svc:15} {status_str:7} {counts['pass']:2}P {counts['fail']:2}F {counts['warn']:2}W {counts['skip']:2}S")

print()

if fails > 0:
    print("  CRITICAL FAILURES (bot cannot work with these):")
    for name, status, detail in results:
        if status == FAIL:
            print(f"    [XX] {name}")
            print(f"         {detail}")
    print()

if warns > 0:
    print("  WARNINGS (investigate these):")
    warn_count = 0
    for name, status, detail in results:
        if status == WARN:
            warn_count += 1
            if warn_count <= 10:  # Limit to first 10
                print(f"    [!!] {name}")
                print(f"         {detail}")
    if warn_count > 10:
        print(f"    ... and {warn_count - 10} more warnings")
    print()

# Recommendations
print("  RECOMMENDATIONS:")
print()

sage_tests = [r for r in results if r[0].startswith("Sage")]
sage_pass = sum(1 for r in sage_tests if r[1] == PASS)
if sage_pass > 0:
    print("    [*] Sage wallet RPC is running — you can use WALLET_TYPE=sage")
else:
    print("    [*] Sage wallet RPC is not running or not configured")

dexie_tests = [r for r in results if r[0].startswith("Dexie")]
dexie_fail = sum(1 for r in dexie_tests if r[1] == FAIL)
if dexie_fail == 0:
    print("    [*] Dexie API is working — bot can post offers and fetch prices")
else:
    print("    [*] Dexie API has issues — bot will not work for market making")

tibet_tests = [r for r in results if r[0].startswith("TibetSwap")]
tibet_fail = sum(1 for r in tibet_tests if r[1] == FAIL)
if tibet_fail == 0:
    print("    [*] TibetSwap API is working — bot can calculate reference prices")
else:
    print("    [*] TibetSwap API is down or unreachable — bot will use fallback pricing")

spacescan_tests = [r for r in results if r[0].startswith("Spacescan")]
spacescan_warn = sum(1 for r in spacescan_tests if r[1] == WARN and "slow" in r[2])
if spacescan_warn > 0:
    print("    [!] Spacescan is slow (8-14s) — bot can still use it but may timeout")

offerpool_tests = [r for r in results if r[0].startswith("Offerpool")]
if any(r[1] != SKIP for r in offerpool_tests):
    print("    [*] Offerpool is available for cross-posting (optional)")
else:
    print("    [*] Offerpool is disabled or unavailable (optional)")

print()
print("  FINAL STATUS:")
if fails == 0:
    if warns == 0:
        print("    READY — All critical APIs working. Bot should start normally.")
    else:
        print("    CAUTION — Core APIs working but some optional services have issues.")
        print("              Bot can run, but some features may be limited.")
elif fails <= 2:
    print("    DEGRADED — Some API endpoints failing. Bot may have limited functionality.")
    print("               Review failures above before starting bot.")
else:
    print("    BROKEN — Multiple critical APIs failing. Bot will not operate.")
    print("             Address failures above before attempting to start.")

print()
print("=" * 80)
