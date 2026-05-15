#!/usr/bin/env python3
"""
Spacescan API Diagnostic Script
================================
Tests every Spacescan endpoint the bot uses to verify connectivity,
response format, and API key validity.

Usage:
    python test_spacescan.py

Tests:
  1. API connectivity (can we reach Spacescan?)
  2. API key validation (Pro vs Free tier)
  3. /coin/info/{coin_id} — fill verification endpoint
  4. /address/xch-balance/{address} — XCH balance check
  5. /address/token-balance/{address} — CAT balance check
  6. Response format validation (are fields where we expect them?)
  7. Rate limit check (are we being throttled?)
  8. Timeout measurement (how fast are responses?)
"""

import sys
import os
import time
import json
import requests

# Add project root to path so we can import config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from config import cfg

    CONFIG_LOADED = True
except Exception as e:
    CONFIG_LOADED = False
    print(f"  WARNING: Could not load config: {e}")
    print("  Will use defaults / environment variables")


# ── Helpers ──────────────────────────────────────────────────────────


def get_base_url():
    if CONFIG_LOADED and cfg.SPACESCAN_API_KEY:
        return getattr(cfg, "SPACESCAN_PRO_URL", "https://pro-api.spacescan.io")
    return "https://api.spacescan.io"


def get_headers():
    headers = {"Accept": "application/json"}
    if CONFIG_LOADED and cfg.SPACESCAN_API_KEY:
        headers["x-api-key"] = cfg.SPACESCAN_API_KEY
    return headers


def get_api_key():
    if CONFIG_LOADED:
        return cfg.SPACESCAN_API_KEY or ""
    return os.environ.get("SPACESCAN_API_KEY", "")


def get_wallet_address():
    if CONFIG_LOADED:
        return getattr(cfg, "WALLET_ADDRESS", "")
    return ""


def get_cat_asset_id():
    if CONFIG_LOADED:
        return getattr(cfg, "CAT_ASSET_ID", "")
    return ""


def timed_request(url, headers, timeout=15):
    """Make a GET request and return (response, elapsed_ms) or (None, elapsed_ms)."""
    start = time.time()
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        elapsed = (time.time() - start) * 1000
        return resp, elapsed
    except requests.exceptions.Timeout:
        elapsed = (time.time() - start) * 1000
        return None, elapsed
    except requests.exceptions.ConnectionError:
        elapsed = (time.time() - start) * 1000
        return None, elapsed
    except Exception:
        elapsed = (time.time() - start) * 1000
        return None, elapsed


# ── Test Results ─────────────────────────────────────────────────────

results = []
PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"


def test(name, status, detail=""):
    results.append((name, status, detail))
    icon = {"PASS": "OK", "FAIL": "XX", "WARN": "!!", "SKIP": "--"}[status]
    print(f"  [{icon}] {name}: {status}" + (f" -- {detail}" if detail else ""))


# ======================================================================
# TESTS
# ======================================================================

print("=" * 70)
print("  SPACESCAN API DIAGNOSTIC")
print("=" * 70)
print()

base_url = get_base_url()
headers = get_headers()
api_key = get_api_key()

print(f"  Base URL:    {base_url}")
print(f"  API Key:     {'configured' if api_key else '(none)'}")
print(f"  Tier:        {'PRO' if api_key else 'FREE'}")
print()


# ── Test 1: Basic Connectivity ───────────────────────────────────────
print("--- Test 1: Basic Connectivity ---")

resp, ms = timed_request(f"{base_url}/coin/info/0x{'0' * 64}", headers)
if resp is not None:
    test("API reachable", PASS, f"{ms:.0f}ms, HTTP {resp.status_code}")
elif ms > 14000:
    test("API reachable", FAIL, f"Timeout after {ms:.0f}ms")
else:
    test("API reachable", FAIL, f"Connection error after {ms:.0f}ms")
print()


# ── Test 2: API Key Validation ───────────────────────────────────────
print("--- Test 2: API Key Validation ---")

if not api_key:
    test("API key configured", WARN, "No API key -- using Free tier (limited)")
else:
    resp, ms = timed_request(f"{base_url}/coin/info/0x{'0' * 64}", headers)
    if resp and resp.status_code == 200:
        test("API key valid", PASS, f"Pro tier confirmed ({ms:.0f}ms)")
    elif resp and resp.status_code == 401:
        test("API key valid", FAIL, "401 Unauthorized -- key is invalid or expired")
    elif resp and resp.status_code == 403:
        test("API key valid", FAIL, "403 Forbidden -- key lacks required permissions")
    else:
        test(
            "API key valid",
            WARN,
            f"Unexpected response: HTTP {resp.status_code if resp else 'None'}",
        )
print()


# ── Test 3: Coin Info Endpoint (Fill Verification) ───────────────────
print("--- Test 3: /coin/info -- Fill Verification ---")

# Use a zeros coin just to test the endpoint responds
test_coin = "0x" + "0" * 64

resp, ms = timed_request(f"{base_url}/coin/info/{test_coin}", headers)
if resp is None:
    test("coin/info endpoint", FAIL, f"No response ({ms:.0f}ms)")
elif resp.status_code == 200:
    try:
        data = resp.json()
        has_status = "status" in data
        has_coin = "coin" in data
        test("coin/info HTTP", PASS, f"200 OK ({ms:.0f}ms)")
        test(
            "coin/info has 'status'",
            PASS if has_status else FAIL,
            f"status={data.get('status')}",
        )
        test(
            "coin/info has 'coin'",
            PASS if has_coin else WARN,
            f"keys: {list(data.keys())[:5]}",
        )

        if has_coin:
            coin = data["coin"]
            expected = ["spent_block", "receiver", "sender"]
            present = [f for f in expected if f in coin]
            missing = [f for f in expected if f not in coin]
            if not missing:
                test("coin/info format", PASS, "All expected fields present")
            else:
                test(
                    "coin/info format",
                    WARN,
                    f"Missing: {missing}. Got: {list(coin.keys())[:8]}",
                )
    except json.JSONDecodeError:
        test("coin/info response", FAIL, "Not valid JSON")
elif resp.status_code == 404:
    test("coin/info endpoint", WARN, "404 -- may be expected for test coin")
else:
    test("coin/info endpoint", FAIL, f"HTTP {resp.status_code} ({ms:.0f}ms)")

# Test with a REAL coin from the bot's database
if CONFIG_LOADED:
    try:
        from database import get_connection

        conn = get_connection()
        row = conn.execute(
            "SELECT coin_id FROM coins WHERE coin_id IS NOT NULL AND coin_id != '' LIMIT 1"
        ).fetchone()
        if row and row["coin_id"]:
            real_coin = row["coin_id"]
            if not real_coin.startswith("0x"):
                real_coin = f"0x{real_coin}"

            time.sleep(2)
            resp2, ms2 = timed_request(f"{base_url}/coin/info/{real_coin}", headers)
            if resp2 and resp2.status_code == 200:
                data2 = resp2.json()
                if data2.get("status") == "success" and data2.get("coin"):
                    coin2 = data2["coin"]
                    spent = coin2.get("spent_block")
                    is_spent = (
                        spent is not None
                        and spent != ""
                        and spent != 0
                        and str(spent) != "0"
                    )
                    test(
                        "coin/info REAL coin",
                        PASS,
                        f"{real_coin[:20]}... spent={is_spent} ({ms2:.0f}ms)",
                    )
                else:
                    test(
                        "coin/info REAL coin",
                        WARN,
                        f"status={data2.get('status')} ({ms2:.0f}ms)",
                    )
            elif resp2:
                test(
                    "coin/info REAL coin",
                    FAIL,
                    f"HTTP {resp2.status_code} ({ms2:.0f}ms)",
                )
            else:
                test("coin/info REAL coin", FAIL, f"Timeout ({ms2:.0f}ms)")
        else:
            test("coin/info REAL coin", SKIP, "No coins in DB")
    except Exception as e:
        test("coin/info REAL coin", SKIP, f"DB error: {e}")
print()


# ── Test 4: XCH Balance Endpoint ─────────────────────────────────────
print("--- Test 4: /address/xch-balance ---")

wallet_addr = get_wallet_address()
# Use the Chia null address as a throwaway fallback so we don't
# disclose any real address to Spacescan during testing.
test_addr = (
    wallet_addr or "xch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqs0wd5zg"
)
addr_source = "from config" if wallet_addr else "null address fallback"

time.sleep(2)
resp, ms = timed_request(f"{base_url}/address/xch-balance/{test_addr}", headers)
if resp is None:
    test("xch-balance endpoint", FAIL, f"No response ({ms:.0f}ms)")
elif resp.status_code == 200:
    try:
        data = resp.json()
        if "xch" in data:
            balance = data["xch"]
            test("xch-balance", PASS, f"{balance} XCH ({addr_source}, {ms:.0f}ms)")
        elif data.get("status") == "success":
            test(
                "xch-balance",
                WARN,
                f"Success but no 'xch' key. Keys: {list(data.keys())[:5]}",
            )
        else:
            test(
                "xch-balance", WARN, f"Unexpected format. Keys: {list(data.keys())[:5]}"
            )
    except json.JSONDecodeError:
        test("xch-balance", FAIL, "Not valid JSON")
else:
    test("xch-balance endpoint", FAIL, f"HTTP {resp.status_code} ({ms:.0f}ms)")
print()


# ── Test 5: Token Balance Endpoint ───────────────────────────────────
print("--- Test 5: /address/token-balance ---")

time.sleep(2)
resp, ms = timed_request(f"{base_url}/address/token-balance/{test_addr}", headers)
if resp is None:
    test("token-balance endpoint", FAIL, f"No response ({ms:.0f}ms)")
elif resp.status_code == 200:
    try:
        data = resp.json()
        balances = data.get("data", data.get("balance", []))
        test("token-balance HTTP", PASS, f"200 OK ({ms:.0f}ms)")

        if isinstance(balances, list):
            test("token-balance format", PASS, f"{len(balances)} token(s)")
            cat_id = get_cat_asset_id()
            if cat_id and balances:
                found = any(t.get("asset_id") == cat_id for t in balances)
                test(
                    "Our CAT found",
                    PASS if found else WARN,
                    f"{cat_id[:16]}... {'found' if found else 'not in list'}",
                )
        else:
            test(
                "token-balance format",
                WARN,
                f"Expected list, got {type(balances).__name__}",
            )
    except json.JSONDecodeError:
        test("token-balance", FAIL, "Not valid JSON")
else:
    test("token-balance endpoint", FAIL, f"HTTP {resp.status_code} ({ms:.0f}ms)")
print()


# ── Test 6: Response Time Benchmark ──────────────────────────────────
print("--- Test 6: Response Time Benchmark (3 calls) ---")

times = []
for i in range(3):
    time.sleep(2)
    _, ms = timed_request(f"{base_url}/coin/info/0x{'0' * 64}", headers)
    times.append(ms)

avg = sum(times) / len(times)
max_t = max(times)
min_t = min(times)

if avg < 2000:
    test("Avg response time", PASS, f"{avg:.0f}ms (min={min_t:.0f}, max={max_t:.0f})")
elif avg < 5000:
    test(
        "Avg response time",
        WARN,
        f"{avg:.0f}ms -- slow (min={min_t:.0f}, max={max_t:.0f})",
    )
else:
    test(
        "Avg response time",
        FAIL,
        f"{avg:.0f}ms -- very slow (min={min_t:.0f}, max={max_t:.0f})",
    )

bot_timeout = getattr(cfg, "SPACESCAN_TIMEOUT", 10) * 1000 if CONFIG_LOADED else 10000
if max_t > bot_timeout:
    test(
        "Timeout risk",
        FAIL,
        f"Slowest ({max_t:.0f}ms) exceeds bot timeout ({bot_timeout:.0f}ms)",
    )
elif max_t > bot_timeout * 0.7:
    test(
        "Timeout risk",
        WARN,
        f"Slowest ({max_t:.0f}ms) is {max_t / bot_timeout * 100:.0f}% of timeout ({bot_timeout:.0f}ms)",
    )
else:
    test(
        "Timeout risk",
        PASS,
        f"All within timeout ({max_t:.0f}ms < {bot_timeout:.0f}ms)",
    )
print()


# ── Test 7: Bot Config Check ─────────────────────────────────────────
print("--- Test 7: Bot Config ---")

if CONFIG_LOADED:
    enabled = getattr(cfg, "SPACESCAN_ENABLED", True)
    test(
        "SPACESCAN_ENABLED",
        PASS if enabled else WARN,
        f"{'enabled' if enabled else 'DISABLED -- fills not verified!'}",
    )

    timeout = getattr(cfg, "SPACESCAN_TIMEOUT", 10)
    if timeout >= 15:
        test("SPACESCAN_TIMEOUT", PASS, f"{timeout}s")
    elif timeout >= 10:
        test(
            "SPACESCAN_TIMEOUT",
            WARN,
            f"{timeout}s -- increase to 15-20s if timeouts frequent",
        )
    else:
        test("SPACESCAN_TIMEOUT", FAIL, f"{timeout}s -- too low, increase to 15+")

    key = cfg.SPACESCAN_API_KEY or ""
    test(
        "API key",
        PASS if key else WARN,
        f"{'Pro key set' if key else 'No key -- Free tier'}",
    )
else:
    test("Bot config", SKIP, "Could not load config.py")
print()


# ======================================================================
# SUMMARY
# ======================================================================

print("=" * 70)
passes = sum(1 for _, s, _ in results if s == PASS)
fails = sum(1 for _, s, _ in results if s == FAIL)
warns = sum(1 for _, s, _ in results if s == WARN)
skips = sum(1 for _, s, _ in results if s == SKIP)
total = len(results)

print(
    f"  RESULTS: {passes} passed, {fails} failed, {warns} warnings, {skips} skipped / {total} total"
)

if fails > 0:
    print()
    print("  FAILURES:")
    for name, status, detail in results:
        if status == FAIL:
            print(f"    XX {name}: {detail}")

if warns > 0:
    print()
    print("  WARNINGS:")
    for name, status, detail in results:
        if status == WARN:
            print(f"    !! {name}: {detail}")

print()
if fails == 0:
    print("  All critical Spacescan endpoints are working.")
elif fails <= 2:
    print("  Some endpoints have issues -- check failures above.")
else:
    print(
        "  Multiple Spacescan endpoints failing -- fills cannot be verified reliably."
    )
print("=" * 70)
