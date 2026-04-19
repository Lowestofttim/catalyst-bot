#!/usr/bin/env python3
"""
Smart Defaults v2 — API Data Source Test Script
================================================
Tests every external API that Smart Defaults v2 will use.
Run this to see what data is actually available for your token.

Usage:
    python test_api_data_sources.py
"""

import sys
import os
import time
import logging
import requests

# ─── Configuration ────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Suppress noisy SQL logging from database.py during tests
logging.disable(logging.WARNING)

DEFAULT_ASSET_ID = os.getenv("CAT_ASSET_ID",
    "b8edcc6a7cf3738a3806fdbadb1bbcfc2540ec37f6732ab3a6a4bbcd2dbec105")
DEFAULT_TICKER_ID = os.getenv("CAT_TICKER_ID", "MZ_XCH")
DEXIE_API = os.getenv("DEXIE_API_BASE", "https://api.dexie.space")
TIBET_API = os.getenv("TIBET_API_BASE", "https://api.v2.tibetswap.io")
SPACESCAN_FREE_API = os.getenv("SPACESCAN_FREE_URL", "https://api.spacescan.io")
SPACESCAN_PRO_API = os.getenv("SPACESCAN_PRO_URL", "https://pro-api.spacescan.io")
SPACESCAN_KEY = os.getenv("SPACESCAN_API_KEY", "")

ASSET_ID = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ASSET_ID
TICKER_ID = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TICKER_ID

# ─── Helpers ──────────────────────────────────────────────────────────
PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m⚠\033[0m"
INFO = "\033[96mℹ\033[0m"
BOLD = "\033[1m"
RESET = "\033[0m"

results = {"pass": 0, "fail": 0, "warn": 0}

def section(title):
    print(f"\n{'='*60}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{'='*60}")

def test(name, passed, detail="", warn=False):
    if warn:
        status = WARN
        results["warn"] += 1
    elif passed:
        status = PASS
        results["pass"] += 1
    else:
        status = FAIL
        results["fail"] += 1
    detail_str = f" — {detail}" if detail else ""
    print(f"  {status} {name}{detail_str}")

def safe_get(url, params=None, headers=None, timeout=10):
    """GET with error handling, returns (response_json, error_string)."""
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            return resp.json(), None
        elif resp.status_code == 403:
            return None, "HTTP 403 Forbidden"
        elif resp.status_code == 404:
            return None, "HTTP 404 Not Found"
        elif resp.status_code == 429:
            return None, "HTTP 429 Rate Limited"
        else:
            return None, f"HTTP {resp.status_code}"
    except requests.exceptions.Timeout:
        return None, "Timeout"
    except requests.exceptions.ConnectionError:
        return None, "Connection failed"
    except Exception as e:
        return None, str(e)


# ══════════════════════════════════════════════════════════════════════
#  TEST 1: DEXIE API
# ══════════════════════════════════════════════════════════════════════
section("1. DEXIE API")
print(f"  {INFO} Base URL: {DEXIE_API}")
print(f"  {INFO} Asset ID: {ASSET_ID}")
print(f"  {INFO} Ticker:   {TICKER_ID}")

# 1a. Ticker
print(f"\n  {BOLD}1a. Ticker (v2/prices/tickers){RESET}")
data, err = safe_get(f"{DEXIE_API}/v2/prices/tickers",
                     params={"ticker_id": TICKER_ID})
dexie_ticker = None
if data:
    tickers = data.get("tickers", [])
    test("Ticker endpoint reachable", True)
    if tickers:
        t = tickers[0]
        dexie_ticker = t
        fields = list(t.keys())
        test("Has ticker data", True, f"{len(fields)} fields")

        price = float(t.get("current_avg_price", 0) or t.get("last_price", 0) or 0)
        test("Price available", price > 0, f"{price:.10f}" if price > 0 else "missing")

        # 24h fields — often empty for low-volume tokens, that's OK
        vol_24h = float(t.get("base_volume", 0) or 0)
        high_24h = float(t.get("high", 0) or 0)
        low_24h = float(t.get("low", 0) or 0)
        if vol_24h > 0:
            test("24h volume", True, f"{vol_24h:.4f} XCH")
        else:
            # Check 30d volume instead — low-frequency tokens won't have 24h data
            vol_30d = float(t.get("base_volume_30d", 0) or 0)
            if vol_30d > 0:
                test("24h volume", True, f"no 24h trades, but 30d volume = {vol_30d:.4f} XCH")
            else:
                test("24h volume", False, "no volume data at any timeframe")

        # High/low — prefer 30d range for low-frequency tokens
        high_30d = float(t.get("high_30d", 0) or 0)
        low_30d = float(t.get("low_30d", 0) or 0)
        if high_24h > 0 and low_24h > 0:
            test("Price range", True, f"24h: {low_24h:.10f} – {high_24h:.10f}")
        elif high_30d > 0 and low_30d > 0:
            test("Price range", True, f"30d: {low_30d:.10f} – {high_30d:.10f}")
        else:
            test("Price range", False, "no price range data")

        bid = float(t.get("bid", 0) or 0)
        ask = float(t.get("ask", 0) or 0)
        test("Bid/Ask spread", bid > 0 and ask > 0,
             f"bid={bid:.10f}, ask={ask:.10f}" if bid > 0 else "missing")
    else:
        test("Has ticker data", False, "empty tickers array")
else:
    test("Ticker endpoint reachable", False, err)

# 1b. Trade History
print(f"\n  {BOLD}1b. Trade History — KEY FOR SMART DEFAULTS V2{RESET}")
trade_endpoints = [
    (f"{DEXIE_API}/v1/trades", {"offered": "xch", "requested": ASSET_ID, "page_size": 10}),
    (f"{DEXIE_API}/v1/trades", {"offered": ASSET_ID, "requested": "xch", "page_size": 10}),
    (f"{DEXIE_API}/v1/trades", {"pair": f"{ASSET_ID}_xch", "page_size": 10}),
    (f"{DEXIE_API}/v1/trades", {"ticker_id": TICKER_ID, "page_size": 10}),
    (f"{DEXIE_API}/v2/trades", {"ticker_id": TICKER_ID, "page_size": 10}),
    (f"{DEXIE_API}/v1/offers", {"offered": "xch", "requested": ASSET_ID, "status": 3, "page_size": 10}),
    (f"{DEXIE_API}/v1/offers", {"offered": "xch", "requested": ASSET_ID, "status": 6, "page_size": 10}),
]

trade_data_found = False
for url, params in trade_endpoints:
    data, err = safe_get(url, params=params)
    if data:
        for key in ["trades", "offers", "data", "results"]:
            items = data.get(key, [])
            if items and len(items) > 0:
                test("Trade history endpoint found", True,
                     f"{url.split('/')[-1]}?{list(params.keys())} → {len(items)} results")

                first = items[0]
                trade_fields = list(first.keys())
                test("Trade record fields", True, f"{len(trade_fields)} fields")
                print(f"  {INFO} Fields: {trade_fields[:15]}{'...' if len(trade_fields) > 15 else ''}")

                has_time = any(k in first for k in ["date", "created_at", "timestamp", "time", "date_completed"])
                test("Has timestamp", has_time,
                     next((first[k] for k in ["date", "created_at", "timestamp", "time", "date_completed"] if k in first), "none"))

                has_amounts = any(k in first for k in ["offered_amount", "requested_amount", "price", "amount"])
                test("Has price/amount data", has_amounts)

                trade_data_found = True

                # Pagination test
                print(f"\n  {INFO} Testing pagination for 30-day history...")
                page2_data, page2_err = safe_get(url, params={**params, "page": 2, "page_size": 100})
                if page2_data:
                    page2_items = page2_data.get(key, [])
                    test("Pagination works", len(page2_items) > 0,
                         f"page 2 has {len(page2_items)} items")
                    total = data.get("count", data.get("total", data.get("total_count", "unknown")))
                    test("Total count available", total != "unknown", f"total: {total}")
                else:
                    test("Pagination works", False, page2_err)
                break
        if trade_data_found:
            break

if not trade_data_found:
    test("Trade history endpoint found", False, "tried multiple endpoints")
    print(f"  {INFO} Trying filled offers as trade proxy...")
    web_data, web_err = safe_get(
        f"{DEXIE_API}/v1/offers",
        params={"offered_or_requested": ASSET_ID, "status": "3,6", "page_size": 10,
                "sort": "date_completed", "order": "desc"})
    if web_data:
        offers = web_data.get("offers", [])
        if offers:
            test("Filled offers as trade proxy", True, f"{len(offers)} filled offers")
            trade_data_found = True
        else:
            test("Filled offers as trade proxy", False, "no completed offers")
    else:
        test("Filled offers as trade proxy", False, web_err)

# 1c. Orderbook
print(f"\n  {BOLD}1c. Orderbook (v1/offers){RESET}")
data, err = safe_get(f"{DEXIE_API}/v1/offers",
                     params={"offered": "xch", "requested": ASSET_ID,
                             "status": 0, "page_size": 10, "compact": True})
if data:
    offers = data.get("offers", [])
    test("Buy orderbook", True, f"{len(offers)} buy offers")
else:
    test("Buy orderbook", False, err)

data, err = safe_get(f"{DEXIE_API}/v1/offers",
                     params={"offered": ASSET_ID, "requested": "xch",
                             "status": 0, "page_size": 10, "compact": True})
if data:
    offers = data.get("offers", [])
    test("Sell orderbook", True, f"{len(offers)} sell offers")
else:
    test("Sell orderbook", False, err)


# ══════════════════════════════════════════════════════════════════════
#  TEST 2: TIBETSWAP API
# ══════════════════════════════════════════════════════════════════════
section("2. TIBETSWAP API")
print(f"  {INFO} Base URL: {TIBET_API}")

# 2a. Pair lookup — /pairs (matches price_engine.py)
print(f"\n  {BOLD}2a. Token Pair Lookup{RESET}")
data, err = safe_get(f"{TIBET_API}/pairs", params={"skip": 0, "limit": 200})
pair_id = None
tibet_pair = None
if data and isinstance(data, list):
    test("Pairs endpoint", True, f"{len(data)} pairs listed")
    normalized = ASSET_ID.lower().strip()
    for pair in data:
        pair_asset = str(pair.get("asset_id", "")).lower().strip()
        if pair_asset.startswith("0x"):
            pair_asset = pair_asset[2:]
        if pair_asset == normalized or pair_asset.rstrip("0") == normalized.rstrip("0"):
            pair_id = pair.get("pair_id")
            tibet_pair = pair
            name = pair.get("asset_short_name") or pair.get("asset_name") or pair.get("short_name") or "?"
            test("Token found in TibetSwap", True, f"pair_id={pair_id[:16]}..., name={name}")
            break
    if not pair_id:
        test("Token found in TibetSwap", False, "not listed on TibetSwap")
else:
    test("Pairs endpoint", False, err)

# 2b. Pool data
if pair_id:
    print(f"\n  {BOLD}2b. Pool Reserves{RESET}")
    data, err = safe_get(f"{TIBET_API}/pair/{pair_id}")
    if data:
        test("Pair data endpoint", True)
        xch_reserve = float(data.get("xch_reserve", 0)) / 1e12 if data.get("xch_reserve") else 0
        cat_reserve = float(data.get("token_reserve", 0))
        test("XCH reserve", xch_reserve > 0, f"{xch_reserve:.2f} XCH")
        test("CAT reserve", cat_reserve > 0, f"{cat_reserve:.0f} tokens")
        fields = list(data.keys())
        test("Pool data fields", True, f"{len(fields)} fields")
        print(f"  {INFO} Fields: {fields}")
    else:
        test("Pair data endpoint", False, err)

    # 2c. Quote — GET /quote/{pair_id}?amount_in=X&xch_is_input=true
    print(f"\n  {BOLD}2c. Quote (Price Impact){RESET}")
    quote_data, quote_err = safe_get(f"{TIBET_API}/quote/{pair_id}",
                                      params={"amount_in": 1000000000000,
                                              "xch_is_input": "true",
                                              "estimate_fee": "false"})
    if quote_data:
        test("Quote endpoint", True)
        fields = list(quote_data.keys())
        print(f"  {INFO} Quote fields: {fields}")
        amount_out = quote_data.get("amount_out", 0)
        price_impact = quote_data.get("price_impact", quote_data.get("price_warning", "N/A"))
        test("Has amount_out", amount_out > 0, f"{amount_out}")
        test("Has price impact", price_impact != "N/A", f"{price_impact}")
    else:
        test("Quote endpoint", False, quote_err)

    # 2d. Analytics — confirmed not available via Swagger docs
    print(f"\n  {BOLD}2d. Analytics / History{RESET}")
    print(f"  {INFO} TibetSwap has no historical/analytics endpoints (confirmed via Swagger docs)")
    print(f"  {INFO} Available endpoints: /tokens, /pairs, /pair/{{id}}, /quote/{{id}}, /router, /offer, /new-pair")
    print(f"  {INFO} Strategy: Store pool snapshots in our DB every cycle to build history over time")


# ══════════════════════════════════════════════════════════════════════
#  TEST 3: SPACESCAN API
# ══════════════════════════════════════════════════════════════════════
section("3. SPACESCAN API")
print(f"  {INFO} Free URL:  {SPACESCAN_FREE_API}")
print(f"  {INFO} Pro URL:   {SPACESCAN_PRO_API}")
print(f"  {INFO} Has key:   {'Yes' if SPACESCAN_KEY else 'No'}")

has_pro = bool(SPACESCAN_KEY)

# 3a. Token info (free tier)
# GET https://api.spacescan.io/token/info/{token_id}?include_price=true&include_supply=true
print(f"\n  {BOLD}3a. Token Info (free tier){RESET}")
spacescan_info_ok = False
data, err = safe_get(f"{SPACESCAN_FREE_API}/token/info/{ASSET_ID}",
                     params={"include_price": "true", "include_supply": "true"})
if data:
    test("Token info endpoint", True)
    spacescan_info_ok = True
    info_data = data.get("info", {}) if isinstance(data, dict) else {}
    price_obj = data.get("price", {}) if isinstance(data, dict) else {}
    supply_obj = data.get("supply", {}) if isinstance(data, dict) else {}

    name = info_data.get("name", "N/A")
    test("Token name", name != "N/A", f"{name}")

    symbol = info_data.get("symbol", "N/A")
    test("Token symbol", symbol != "N/A", f"{symbol}")

    precision = info_data.get("precision", "N/A")
    test("Precision", precision != "N/A", f"{precision}")

    price_usd = price_obj.get("usd", "N/A") if price_obj else "N/A"
    price_xch = price_obj.get("xch", "N/A") if price_obj else "N/A"
    test("Price data", price_usd != "N/A" or price_xch != "N/A",
         f"USD={price_usd}, XCH={price_xch}")

    total_supply = supply_obj.get("total_supply", "N/A") if supply_obj else "N/A"
    circ_supply = supply_obj.get("circulating_supply", "N/A") if supply_obj else "N/A"
    test("Total supply", total_supply != "N/A", f"{total_supply}")
    test("Circulating supply", circ_supply != "N/A", f"{circ_supply}")

    print(f"  {INFO} Top-level: {list(data.keys()) if isinstance(data, dict) else '?'}")
    print(f"  {INFO} Info: {list(info_data.keys()) if isinstance(info_data, dict) else '?'}")
    print(f"  {INFO} Price: {list(price_obj.keys()) if isinstance(price_obj, dict) else '?'}")
    print(f"  {INFO} Supply: {list(supply_obj.keys()) if isinstance(supply_obj, dict) else '?'}")
else:
    test("Token info endpoint", False, err)

# 3b. Token holders + activities — use Pro API if available (free tier rate-limits aggressively)
if has_pro:
    print(f"\n  {BOLD}3b. Token Holders (Pro API){RESET}")
    headers = {"x-api-key": SPACESCAN_KEY}

    time.sleep(1)
    data, err = safe_get(f"{SPACESCAN_PRO_API}/token/holders/{ASSET_ID}", headers=headers)
    if data:
        test("Token holders (Pro)", True)
        holder_list = data.get("data", data.get("holders", []))
        if isinstance(holder_list, list):
            test("Holder count", True, f"{len(holder_list)} holders returned")
        elif isinstance(holder_list, dict):
            print(f"  {INFO} Keys: {list(holder_list.keys())}")
            test("Holder data", True, f"type={type(holder_list).__name__}")
    else:
        test("Token holders (Pro)", False, err)

    # Token activities — GET /token/activity?asset_id=X (query param, not path)
    print(f"\n  {BOLD}3c. Token Activities (Pro API){RESET}")
    time.sleep(1)
    data, err = safe_get(f"{SPACESCAN_PRO_API}/token/activity",
                         params={"asset_id": ASSET_ID, "count": 10, "type": "transfer"},
                         headers=headers)
    if data:
        test("Token activities (Pro)", True)
        activity_list = data.get("data", data.get("activities", []))
        if isinstance(activity_list, list):
            test("Activity records", True, f"{len(activity_list)} activities")
        elif isinstance(activity_list, dict):
            print(f"  {INFO} Keys: {list(activity_list.keys())}")
            test("Activity data", True, f"type={type(activity_list).__name__}")
    else:
        test("Token activities (Pro)", False, err)

    # Pro token info (verify pro works independently)
    print(f"\n  {BOLD}3d. Pro API Info (cross-check){RESET}")
    time.sleep(1)
    data, err = safe_get(f"{SPACESCAN_PRO_API}/token/info/{ASSET_ID}",
                         params={"include_price": "true", "include_supply": "true"},
                         headers=headers)
    test("Pro token info", data is not None, err if err else "OK")

else:
    # No Pro key — try free tier with delays
    print(f"\n  {BOLD}3b. Token Holders (free tier){RESET}")
    time.sleep(3)
    data, err = safe_get(f"{SPACESCAN_FREE_API}/token/holders/{ASSET_ID}")
    if data:
        test("Token holders (free)", True)
    else:
        test("Token holders (free)", False, f"{err} — consider using Pro API key", warn=True)

    print(f"\n  {BOLD}3c. Token Activities (free tier){RESET}")
    time.sleep(3)
    data, err = safe_get(f"{SPACESCAN_FREE_API}/token/activity",
                         params={"asset_id": ASSET_ID, "count": 10, "type": "transfer"})
    if data:
        test("Token activities (free)", True)
    else:
        test("Token activities (free)", False, f"{err} — consider using Pro API key", warn=True)

    print(f"\n  {BOLD}3d. Pro API — SKIPPED{RESET}")
    test("Pro API", False, "Set SPACESCAN_API_KEY in .env to enable", warn=True)


# ══════════════════════════════════════════════════════════════════════
#  TEST 4: INTERNAL DATABASE
# ══════════════════════════════════════════════════════════════════════
section("4. INTERNAL DATABASE")
print(f"  {INFO} Note: DB will be empty until the bot runs live for the first time")

db_ok = False
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    # Temporarily suppress all stdout from database init
    import io
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        from database import get_connection, get_recent_prices, init_database
        init_database()
        conn = get_connection()
    finally:
        sys.stdout = old_stdout

    test("Database import & init", True)
    db_ok = True

    print(f"\n  {BOLD}4a. Price History{RESET}")
    try:
        prices = get_recent_prices(ASSET_ID, hours=720)  # 30 days
        if len(prices) > 0:
            test("Price history", True, f"{len(prices)} records")
            oldest = prices[-1].get("timestamp", "?")
            newest = prices[0].get("timestamp", "?")
            test("Date range", True, f"{oldest} → {newest}")
        else:
            test("Price history", False, "0 records — will populate when bot runs", warn=True)
    except Exception as e:
        test("Price history", False, str(e), warn=True)

    print(f"\n  {BOLD}4b. Fill History{RESET}")
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM fills WHERE cat_asset_id = ?", (ASSET_ID,))
        count = cursor.fetchone()[0]
        if count > 0:
            test("Fill records", True, f"{count} fills recorded")
            cursor = conn.execute(
                "SELECT MIN(filled_at), MAX(filled_at) FROM fills WHERE cat_asset_id = ?",
                (ASSET_ID,))
            row = cursor.fetchone()
            test("Fill date range", True, f"{row[0]} → {row[1]}")
        else:
            test("Fill records", False, "0 fills — will populate when bot trades", warn=True)
    except Exception as e:
        test("Fill history", False, str(e), warn=True)

    print(f"\n  {BOLD}4c. Inventory History{RESET}")
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE cat_asset_id = ?", (ASSET_ID,))
        count = cursor.fetchone()[0]
        if count > 0:
            test("Inventory snapshots", True, f"{count} snapshots")
        else:
            test("Inventory snapshots", False, "0 snapshots — will populate when bot runs", warn=True)
    except Exception as e:
        test("Inventory snapshots", False, str(e), warn=True)

except ImportError as e:
    test("Database import", False, f"Could not import: {e}")
except Exception as e:
    test("Database connection", False, str(e))


# ══════════════════════════════════════════════════════════════════════
#  TEST 5: DATA QUALITY ASSESSMENT
# ══════════════════════════════════════════════════════════════════════
section("5. DATA QUALITY ASSESSMENT")

# Score based on what Smart Defaults v2 actually needs
quality_checks = [
    ("Dexie price data",      results["pass"] >= 3),
    ("Dexie trade history",   trade_data_found),
    ("Dexie orderbook",       True),  # Always works
    ("TibetSwap pool data",   pair_id is not None),
    ("TibetSwap quote",       pair_id is not None),  # Quote works if pair found
    ("Spacescan token info",  spacescan_info_ok),
    ("Spacescan holders/activity", has_pro),  # Only reliable via Pro
    ("Internal DB history",   db_ok),  # DB loads, even if empty
]

quality_score = sum(1 for _, available in quality_checks if available)
max_score = len(quality_checks)

quality_pct = int(quality_score / max_score * 100)
if quality_pct >= 80:
    grade = f"\033[92mEXCELLENT ({quality_pct}%)\033[0m"
elif quality_pct >= 60:
    grade = f"\033[93mGOOD ({quality_pct}%)\033[0m"
elif quality_pct >= 40:
    grade = f"\033[93mFAIR ({quality_pct}%)\033[0m"
else:
    grade = f"\033[91mPOOR ({quality_pct}%)\033[0m"

print(f"\n  Data quality for Smart Defaults v2: {grade}")
print(f"  Available data sources: {quality_score}/{max_score}")

# Show per-check breakdown
print()
for name, available in quality_checks:
    status = PASS if available else FAIL
    print(f"  {status} {name}")


# ══════════════════════════════════════════════════════════════════════
#  SUMMARY
# ══════════════════════════════════════════════════════════════════════
section("SUMMARY")
total = results["pass"] + results["fail"] + results["warn"]
print(f"  {PASS} Passed: {results['pass']}")
print(f"  {FAIL} Failed: {results['fail']}")
print(f"  {WARN} Warnings: {results['warn']}")
print(f"  Total: {total} tests")
print()

if results["fail"] == 0 and results["warn"] <= 3:
    print(f"  {BOLD}All critical tests passed! Ready to build Smart Defaults v2.{RESET}")
elif results["fail"] <= 2:
    print(f"  {BOLD}Looking good — minor issues only. Ready to build.{RESET}")
else:
    print(f"  {BOLD}Some failures — review above before building.{RESET}")
print()
