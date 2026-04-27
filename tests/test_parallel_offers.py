#!/usr/bin/env python3
"""
Test Parallel Offer Creation
==============================
Tests whether Sage can handle concurrent make_offer RPC calls.

This script:
1. Queries available coins from Sage
2. Creates 6 small test offers in parallel (3 buy + 3 sell)
3. Measures time: sequential vs parallel
4. Cancels all test offers afterwards

IMPORTANT: Uses real wallet — creates small 0.1 XCH offers that are
immediately cancelled. Run only when bot is STOPPED.

Usage:
    python test_parallel_offers.py
"""

import sys
import os
import time
import json
import ssl
import http.client
import threading
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from config import cfg
    print(f"  Config loaded: CAT_ASSET_ID={cfg.CAT_ASSET_ID[:16]}...")
except Exception as e:
    print(f"  ERROR: Could not load config: {e}")
    sys.exit(1)


# ── Sage RPC (same as bot) ──────────────────────────────────────────

CERT_PATH = getattr(cfg, "SAGE_CERT_PATH", "")
KEY_PATH = getattr(cfg, "SAGE_KEY_PATH", "")
SAGE_HOST = "localhost"
SAGE_PORT = 9257

def sage_rpc(endpoint, payload, timeout=15):
    """Make RPC call to Sage using http.client+ssl (same as bot)."""
    body = json.dumps(payload).encode("utf-8")
    ctx = ssl._create_unverified_context()
    if CERT_PATH and KEY_PATH and os.path.exists(CERT_PATH):
        ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    conn = http.client.HTTPSConnection(SAGE_HOST, SAGE_PORT, timeout=timeout, context=ctx)
    conn.request("POST", "/" + endpoint.lstrip("/"), body=body,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    conn.close()
    if resp.status == 200:
        return json.loads(data)
    else:
        raise ConnectionError(f"Sage HTTP {resp.status}: {data[:200]}")


# ── Helpers ─────────────────────────────────────────────────────────

def get_xch_coins(limit=10):
    """Get spendable XCH coins."""
    result = sage_rpc("get_coins", {
        "asset_id": None,
        "include_spent": False,
        "offset": 0,
        "limit": limit,
    })
    return result.get("coins", []) if result else []


def get_cat_coins(limit=10):
    """Get spendable CAT coins."""
    result = sage_rpc("get_coins", {
        "asset_id": cfg.CAT_ASSET_ID,
        "include_spent": False,
        "offset": 0,
        "limit": limit,
    })
    return result.get("coins", []) if result else []


def create_offer(side, price_xch, size_xch, coin_id=None):
    """Create a single offer. Returns (trade_id, elapsed_ms) or (None, elapsed_ms)."""
    cat_decimals = cfg.CAT_DECIMALS
    cat_scale = 10 ** cat_decimals
    xch_mojos = int(Decimal(str(size_xch)) * Decimal("1000000000000"))
    cat_amount = Decimal(str(size_xch)) / Decimal(str(price_xch))
    cat_mojos = int(cat_amount * cat_scale)

    if side == "buy":
        offered = [{"asset_id": None, "amount": str(xch_mojos)}]
        requested = [{"asset_id": cfg.CAT_ASSET_ID, "amount": str(cat_mojos)}]
    else:
        offered = [{"asset_id": cfg.CAT_ASSET_ID, "amount": str(cat_mojos)}]
        requested = [{"asset_id": None, "amount": str(xch_mojos)}]

    payload = {
        "offered_assets": offered,
        "requested_assets": requested,
        "fee": "0",
        "auto_import": True,
        "expires_at_second": int(time.time()) + 300,  # 5 min expiry
    }

    # Add coin_ids if provided
    if coin_id:
        bare_id = coin_id.replace("0x", "")
        payload["coin_ids"] = [bare_id]

    start = time.time()
    try:
        result = sage_rpc("make_offer", payload, timeout=30)
        elapsed = (time.time() - start) * 1000

        if result:
            offer_id = result.get("offer_id", "")
            return offer_id, elapsed
        return None, elapsed
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        print(f"    ERROR: {e}")
        return None, elapsed


def cancel_offer(offer_id):
    """Cancel an offer."""
    try:
        sage_rpc("cancel_offer", {
            "offer_id": offer_id,
            "fee": "0",
            "auto_submit": True,
        }, timeout=30)
        return True
    except Exception as e:
        # 404 = already gone, that's fine
        if "404" in str(e):
            return True
        print(f"    Cancel error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════

print("=" * 70)
print("  PARALLEL OFFER CREATION TEST")
print("=" * 70)
print()

# Step 1: Check Sage connectivity
print("--- Step 1: Check Sage ---")
try:
    ver = sage_rpc("get_version", {}, timeout=5)
    version = ver.get("version", "?") if isinstance(ver, dict) else "?"
    print(f"  [OK] Sage v{version} reachable")
except Exception as e:
    print(f"  [FAIL] Cannot reach Sage: {e}")
    sys.exit(1)  # Safe: excluded from pytest via pytest.ini.

# Step 2: Get available coins
print()
print("--- Step 2: Get coins ---")
xch_coins = get_xch_coins(limit=25)
cat_coins = get_cat_coins(limit=25)
print(f"  XCH coins available: {len(xch_coins)}")
print(f"  CAT coins available: {len(cat_coins)}")

NUM_TEST = 10  # Offers per side per test

if len(xch_coins) < NUM_TEST or len(cat_coins) < NUM_TEST:
    print(f"  [SKIP] Need at least {NUM_TEST} coins per side, have {len(xch_coins)} XCH + {len(cat_coins)} CAT")
    print("  Run coin prep first.")
    sys.exit(0)

# Get current price for offer creation
print()
print("--- Step 3: Get price ---")
try:
    # Use TibetSwap price
    import requests
    resp = requests.get("https://api.v2.tibetswap.io/pairs", params={"skip": 0, "limit": 200}, timeout=10)
    pairs = resp.json()
    tibet_price = None
    norm_id = cfg.CAT_ASSET_ID.lower().replace("0x", "")
    for p in pairs:
        if str(p.get("asset_id", "")).lower().replace("0x", "") == norm_id:
            xr = float(p.get("xch_reserve", 0)) / 1e12
            tr = float(p.get("token_reserve", 0)) / (10 ** cfg.CAT_DECIMALS)
            if tr > 0:
                tibet_price = xr / tr
                break
    if tibet_price:
        print(f"  [OK] Tibet price: {tibet_price:.8f} XCH per MZ")
    else:
        print("  [WARN] No Tibet price, using 0.00012")
        tibet_price = 0.00012
except Exception as e:
    print(f"  [WARN] Price fetch failed: {e}, using 0.00012")
    tibet_price = 0.00012

# Step 4: Sequential test (10 buy + 10 sell)
print()
print(f"--- Step 4: Sequential creation ({NUM_TEST} buy + {NUM_TEST} sell = {NUM_TEST*2} offers) ---")
sequential_ids = []
sequential_start = time.time()

buy_price = tibet_price * 0.90   # 10% below market (won't get taken)
sell_price = tibet_price * 1.10  # 10% above market (won't get taken)

# Buy offers
for i in range(NUM_TEST):
    coin = xch_coins[i]
    coin_id = coin.get("coin_id", coin.get("name", ""))
    size = 0.1 + (i * 0.001)  # 0.100, 0.101, ... 0.109

    oid, ms = create_offer("buy", buy_price, size, coin_id=coin_id)
    if oid:
        sequential_ids.append(oid)
        print(f"  [buy {i+1}/{NUM_TEST}] {ms:.0f}ms -- {oid[:16]}...")
    else:
        print(f"  [buy {i+1}/{NUM_TEST}] FAILED -- {ms:.0f}ms")

# Sell offers
for i in range(NUM_TEST):
    coin = cat_coins[i]
    coin_id = coin.get("coin_id", coin.get("name", ""))
    size = 0.1 + (i * 0.001)

    oid, ms = create_offer("sell", sell_price, size, coin_id=coin_id)
    if oid:
        sequential_ids.append(oid)
        print(f"  [sell {i+1}/{NUM_TEST}] {ms:.0f}ms -- {oid[:16]}...")
    else:
        print(f"  [sell {i+1}/{NUM_TEST}] FAILED -- {ms:.0f}ms")

sequential_total = (time.time() - sequential_start) * 1000
seq_created = len(sequential_ids)
print(f"  Sequential total: {sequential_total:.0f}ms ({sequential_total/1000:.1f}s) -- {seq_created}/{NUM_TEST*2} created")

# Cancel all sequential offers
print(f"  Cancelling {seq_created} offers...")
for oid in sequential_ids:
    try:
        cancel_offer(oid)
    except Exception:
        pass
    time.sleep(0.5)
print("  Cancelled. Waiting 10s for wallet to settle...")
time.sleep(10)

# Step 5: Parallel test (10 buy + 10 sell simultaneously)
print()
print(f"--- Step 5: Parallel creation ({NUM_TEST} buy + {NUM_TEST} sell, ThreadPoolExecutor) ---")

# Re-fetch coins (old ones changed after cancel)
xch_coins = get_xch_coins(limit=25)
cat_coins = get_cat_coins(limit=25)
print(f"  XCH coins: {len(xch_coins)}, CAT coins: {len(cat_coins)}")

if len(xch_coins) < NUM_TEST or len(cat_coins) < NUM_TEST:
    print(f"  [SKIP] Not enough coins after cancel — need {NUM_TEST} per side")
    print("  Try again after coins settle.")
else:
    parallel_ids = []
    parallel_lock = threading.Lock()
    parallel_times = []
    parallel_start = time.time()

    def create_one_parallel(side, idx, coin, price, size):
        coin_id = coin.get("coin_id", coin.get("name", ""))
        oid, ms = create_offer(side, price, size, coin_id=coin_id)
        with parallel_lock:
            if oid:
                parallel_ids.append(oid)
            parallel_times.append(ms)
        return side, idx, oid, ms

    # Build task list: 10 buy + 10 sell
    tasks = []
    for i in range(NUM_TEST):
        tasks.append(("buy", i, xch_coins[i], buy_price, 0.1 + (i * 0.001)))
    for i in range(NUM_TEST):
        tasks.append(("sell", i, cat_coins[i], sell_price, 0.1 + (i * 0.001)))

    # Fire all 20 offers with max 5 concurrent workers
    MAX_WORKERS = 5
    print(f"  Firing {len(tasks)} offers with {MAX_WORKERS} concurrent workers...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(create_one_parallel, *t) for t in tasks]
        buy_ok = 0
        sell_ok = 0
        buy_fail = 0
        sell_fail = 0
        for f in as_completed(futures):
            side, idx, oid, ms = f.result()
            if oid:
                if side == "buy":
                    buy_ok += 1
                else:
                    sell_ok += 1
                print(f"  [{side} {idx+1}/{NUM_TEST}] OK {ms:.0f}ms -- {oid[:16]}...")
            else:
                if side == "buy":
                    buy_fail += 1
                else:
                    sell_fail += 1
                print(f"  [{side} {idx+1}/{NUM_TEST}] FAILED {ms:.0f}ms")

    parallel_total = (time.time() - parallel_start) * 1000
    par_created = len(parallel_ids)
    print(f"  Parallel total: {parallel_total:.0f}ms ({parallel_total/1000:.1f}s)")
    print(f"  Success: {buy_ok} buy + {sell_ok} sell = {par_created}/{NUM_TEST*2}")
    if buy_fail + sell_fail > 0:
        print(f"  Failed:  {buy_fail} buy + {sell_fail} sell")
    if parallel_times:
        avg_ms = sum(parallel_times) / len(parallel_times)
        print(f"  Avg per offer: {avg_ms:.0f}ms, Min: {min(parallel_times):.0f}ms, Max: {max(parallel_times):.0f}ms")

    # Cancel parallel offers
    print(f"  Cancelling {par_created} offers...")
    for oid in parallel_ids:
        try:
            cancel_offer(oid)
        except Exception:
            pass
        time.sleep(0.5)
    print("  Cancelled.")

    # Step 6: Results
    print()
    print("=" * 70)
    print("  RESULTS")
    print("=" * 70)
    print(f"  Sequential ({NUM_TEST*2} offers): {sequential_total:.0f}ms ({sequential_total/1000:.1f}s) -- {seq_created} created")
    print(f"  Parallel   ({NUM_TEST*2} offers): {parallel_total:.0f}ms ({parallel_total/1000:.1f}s) -- {par_created} created")

    if sequential_total > 0 and parallel_total > 0:
        speedup = sequential_total / parallel_total
        print(f"  Speedup: {speedup:.1f}x")

        if par_created == NUM_TEST * 2 and speedup > 2:
            print()
            print(f"  [OK] Parallel creation works! {speedup:.1f}x faster with {MAX_WORKERS} workers.")
            print(f"  Recommended: ThreadPoolExecutor(max_workers={MAX_WORKERS}) for production.")
        elif par_created == NUM_TEST * 2 and speedup > 1.3:
            print()
            print(f"  [OK] Parallel works, modest {speedup:.1f}x speedup.")
            print("  Sage may partially serialize. Still worth using.")
        elif par_created < NUM_TEST * 2:
            fail_rate = ((NUM_TEST * 2) - par_created) / (NUM_TEST * 2) * 100
            print()
            print(f"  [WARN] {fail_rate:.0f}% failure rate in parallel mode.")
            if fail_rate > 30:
                print("  Reduce concurrency (try max_workers=2 or 3).")
            else:
                print("  Acceptable — some coin conflicts expected. Retry handles them.")
        else:
            print()
            print("  [OK] Parallel works but minimal speedup. Sage serializes internally.")

    print("=" * 70)
