#!/usr/bin/env python3
"""
Hidden Coin Test — definitively proves whether Sage's "selectable"
filter bug exists or not.

CONTROLLED TEST with 6 stages:
  Stage 1: Baseline — 1 XCH coin, 1 CAT coin, 0 offers. Compare owned vs selectable.
  Stage 2: Split XCH 1→3. Wait for FULL on-chain confirm. Compare again.
  Stage 3: Split CAT 1→3. Wait for FULL on-chain confirm. Compare again.
  Stage 4: Create BUY + SELL offers. Wait, compare — expect legit locked coins.
           KEY TEST: does the REQUESTED side also lose coins? (= the bug)
  Stage 5: Split MORE coins while offers are open. Wait for confirm. Compare.
  Stage 6: Cancel offers. Wait for on-chain confirm. Compare — should all be free.

Uses:
  - get_selectable_coins_only()    — TRUE selectable, no workaround
  - get_pending_transactions()     — wait for tx to confirm on chain
  - raw rpc("get_coins", ...)      — direct owned/selectable comparison
  - create_offer() with coin_ids   — proven working method from test_offer_create
  - split_coins_rpc()              — proven working method

Usage:  python test_hidden_coins.py
"""

import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import wallet_sage as ws
from wallet_sage import (
    rpc, get_all_offers, split_coins_rpc, cancel_offer, create_offer,
    get_selectable_coins_only, get_pending_transactions,
)


def _env_int(name, default=0):
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


CAT_ASSET_ID = os.getenv("CAT_ASSET_ID", "")
XCH_WALLET_ID = ws.WALLET_ID_XCH
CAT_WALLET_ID = _env_int("CAT_WALLET_ID") or _env_int("CHIA_WALLET_ID_MZ") or 5

POLL_INTERVAL = 5     # seconds between polls
POLL_TIMEOUT = 180    # max wait for on-chain confirm (splits can take 60-90s after pending clears)


# ── TEST FRAMEWORK ────────────────────────────────────────────

class T:
    """Simple test tracker with timestamps."""
    passed = 0
    failed = 0
    start = time.time()

    @staticmethod
    def log(msg):
        elapsed = time.time() - T.start
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{elapsed:7.1f}s] [{ts}] {msg}", flush=True)

    @staticmethod
    def ok(label, detail=""):
        T.passed += 1
        T.log(f"  ✅ {label}{' — ' + detail if detail else ''}")

    @staticmethod
    def fail(label, detail=""):
        T.failed += 1
        T.log(f"  ❌ {label}{' — ' + detail if detail else ''}")


# ── TRANSACTION CONFIRMATION ──────────────────────────────────

def wait_for_pending_clear(label, timeout=POLL_TIMEOUT):
    """Wait until get_pending_transactions returns empty (= all confirmed).

    This is the proper way to know a split/cancel/offer is truly on chain.
    """
    start = time.time()
    while time.time() - start < timeout:
        pending = get_pending_transactions()
        if not pending or len(pending) == 0:
            T.log(f"    {label}: all pending transactions confirmed ({time.time()-start:.0f}s)")
            return True
        T.log(f"    {label}: {len(pending)} pending tx(s)... ({time.time()-start:.0f}s)")
        time.sleep(POLL_INTERVAL)
    T.log(f"    ⏰ {label}: timeout after {timeout}s — still pending")
    return False


def wait_for_selectable_count(wallet_id, expected_count, label, timeout=POLL_TIMEOUT):
    """Wait until selectable coin count reaches expected.

    Uses get_selectable_coins_only() — the TRUE selectable count
    with NO workaround applied. This confirms coins are genuinely
    on chain and spendable.
    """
    start = time.time()
    last_count = 0
    while time.time() - start < timeout:
        result = get_selectable_coins_only(wallet_id)
        if result and isinstance(result, dict):
            records = result.get("records") or []
            last_count = len(records)
            if last_count >= expected_count:
                T.log(f"    {label}: confirmed {last_count} selectable coins ({time.time()-start:.0f}s)")
                return True
        T.log(f"    {label}: {last_count}/{expected_count} selectable ({time.time()-start:.0f}s)")
        time.sleep(POLL_INTERVAL)
    T.log(f"    ⏰ {label}: timeout — got {last_count}, needed {expected_count}")
    return False


def confirm_transaction(label, wallet_id=None, expected_count=None, timeout=POLL_TIMEOUT):
    """Full transaction confirmation: pending clear + coin count verification.

    Step 1: Wait for pending transactions to clear (fast — usually 2-5s)
    Step 2: Wait for coin count to reach expected (slower — coins take time
            to appear in 'selectable' even after pending clears)

    IMPORTANT: Step 2 uses the FULL timeout because Chia blocks take ~52s
    and coins don't become selectable until they're confirmed in a block.
    The previous 30s timeout was too short and caused false "hidden" results.
    """
    T.log(f"  ⏳ Confirming: {label}...")

    # Step 1: Wait for no pending tx
    pending_ok = wait_for_pending_clear(label, timeout)
    if not pending_ok:
        T.fail(f"{label} confirmation", "Pending transactions didn't clear")
        return False

    # Step 2: Verify coin count — use FULL timeout, not a short one.
    # Coins need to be included in a block (~52s) before they appear
    # as selectable. Pending clearing just means the tx was broadcast.
    if wallet_id is not None and expected_count is not None:
        count_ok = wait_for_selectable_count(wallet_id, expected_count, label, timeout=timeout)
        if not count_ok:
            T.fail(f"{label} coin count", f"Expected {expected_count} selectable coins")
            return False

    T.ok(f"{label} confirmed on chain")
    return True


def wait_for_owned_equals_selectable(asset_id, label, timeout=POLL_TIMEOUT):
    """Wait until owned count == selectable count for an asset.

    This is the definitive "everything is settled" check.
    Use between stages to ensure no stale coins from earlier operations.
    """
    is_cat = bool(asset_id)
    asset_label = "CAT" if is_cat else "XCH"
    start = time.time()
    while time.time() - start < timeout:
        owned = raw_get_coins(asset_id, "owned")
        selectable = raw_get_coins(asset_id, "selectable")
        if len(owned) == len(selectable):
            T.log(f"    {label} {asset_label}: owned == selectable "
                  f"({len(owned)} coins, {time.time()-start:.0f}s)")
            return True
        T.log(f"    {label} {asset_label}: owned={len(owned)} sel={len(selectable)} "
              f"({time.time()-start:.0f}s)")
        time.sleep(POLL_INTERVAL)
    T.log(f"    ⏰ {label} {asset_label}: timeout — owned={len(owned)} "
          f"sel={len(selectable)}")
    return False


# ── COIN HELPERS ──────────────────────────────────────────────

def raw_get_coins(asset_id, filter_mode):
    """Direct RPC call to get_coins with a specific filter mode.

    Returns raw Sage coin dicts (NOT Chia-normalized).
    This bypasses ALL workarounds so we see the real difference.
    """
    payload = {
        "asset_id": asset_id if asset_id else None,
        "offset": 0,
        "limit": 500,
        "sort_mode": "amount",
        "filter_mode": filter_mode,
        "ascending": False,
    }
    res = rpc("get_coins", payload, timeout=15)
    if not res or not isinstance(res, dict):
        return []
    coins = res.get("coins") or res.get("records") or res.get("data") or []
    if not coins:
        for k, v in res.items():
            if isinstance(v, list) and len(v) > 0:
                coins = v
                break
    return coins


def get_coin_id(coin):
    """Extract coin ID from a raw Sage coin."""
    return (coin.get("coin_id") or coin.get("id") or
            coin.get("coinId") or coin.get("name") or "unknown")


def get_coin_amount(coin):
    """Extract amount from a raw Sage coin."""
    return int(coin.get("amount", coin.get("value", 0)))


def fmt(mojos, is_cat=False):
    """Format mojos to readable string."""
    if is_cat:
        return f"{int(mojos):,} CAT mojos"
    return f"{int(mojos)/1e12:.6f} XCH"


def get_spendable_records(wallet_id):
    """Get spendable coins using get_selectable_coins_only (NO workaround).

    Returns (count, list_of_chia_compatible_records).
    Each record: {"coin": {"amount": ..., ...}, "coin_id": "..."}
    """
    result = get_selectable_coins_only(wallet_id)
    if result and isinstance(result, dict):
        records = result.get("records") or result.get("confirmed_records") or []
        return len(records), records
    return 0, []


def compare_owned_vs_selectable(stage, asset_label, asset_id, expect_hidden=0):
    """Core comparison: fetch owned and selectable separately via raw RPC.

    Returns (hidden_count, hidden_coin_ids).
    """
    is_cat = bool(asset_id)

    owned = raw_get_coins(asset_id, "owned")
    selectable = raw_get_coins(asset_id, "selectable")

    # Build ID → amount maps
    owned_map = {}
    for c in owned:
        cid = get_coin_id(c).lower().replace("0x", "")
        owned_map[cid] = get_coin_amount(c)

    selectable_map = {}
    for c in selectable:
        cid = get_coin_id(c).lower().replace("0x", "")
        selectable_map[cid] = get_coin_amount(c)

    hidden_ids = set(owned_map.keys()) - set(selectable_map.keys())
    extra_ids = set(selectable_map.keys()) - set(owned_map.keys())

    T.log(f"  {asset_label}: owned={len(owned)}, selectable={len(selectable)}, "
          f"diff={len(hidden_ids)}")

    # Show all coins with status
    for cid in sorted(owned_map.keys(), key=lambda x: owned_map[x], reverse=True):
        if cid in selectable_map:
            tag = "  FREE  "
        else:
            tag = "⚠ HIDDEN"
        T.log(f"    [{tag}]  {cid[:16]}...  {fmt(owned_map[cid], is_cat)}")

    if extra_ids:
        for cid in extra_ids:
            T.log(f"    [?? EXTRA]  {cid[:16]}...  {fmt(selectable_map[cid], is_cat)}  "
                  f"(in selectable but NOT in owned — unexpected)")

    # Verdict for this comparison
    if len(hidden_ids) == expect_hidden:
        T.ok(f"{stage} {asset_label}",
             f"{len(hidden_ids)} hidden (expected {expect_hidden})")
    else:
        T.fail(f"{stage} {asset_label}",
               f"{len(hidden_ids)} hidden but expected {expect_hidden}")

    return len(hidden_ids), hidden_ids


# ── MAIN ──────────────────────────────────────────────────────

def main():
    T.log(f"\n{'='*60}")
    T.log("  SAGE HIDDEN COIN TEST — DEFINITIVE")
    T.log(f"{'='*60}")

    if not CAT_ASSET_ID:
        T.log("  ❌ CAT_ASSET_ID not set in .env")
        return

    T.log(f"  XCH wallet: {XCH_WALLET_ID}")
    T.log(f"  CAT wallet: {CAT_WALLET_ID}")
    T.log(f"  CAT asset:  {CAT_ASSET_ID[:20]}...")

    # ── PRE-CHECK: pending transactions? ─────────────────────
    T.log("\n  Pre-check: any pending transactions?")
    pending = get_pending_transactions()
    if pending and len(pending) > 0:
        T.log(f"  ⚠️  {len(pending)} pending tx(s) — waiting for them to clear first...")
        wait_for_pending_clear("Pre-check", timeout=POLL_TIMEOUT)
    else:
        T.log("  ✅ No pending transactions.")

    # ── PRE-CHECK: open offers? ──────────────────────────────
    T.log("\n  Pre-check: any open offers?")
    offers = get_all_offers(include_completed=True, start=0, end=500)
    open_count = 0
    if offers:
        OPEN_SET = {"PENDING_ACCEPT", "PENDING_CONFIRM", "PENDING",
                     "IN_PROGRESS", "OPEN", "ACTIVE"}
        for o in offers:
            s = o.get("status", "")
            if isinstance(s, int) and s <= 1:
                open_count += 1
            elif str(s).upper() in OPEN_SET:
                open_count += 1

    if open_count > 0:
        T.log(f"  ⚠️  {open_count} open offers — they lock coins legitimately.")
        T.log("     Cancel them and re-run for a clean baseline.")
        T.log("     Continuing but Stage 1 results may show expected diffs.\n")
    else:
        T.log("  ✅ No open offers — clean starting state.\n")

    # ══════════════════════════════════════════════════════════
    # STAGE 1: BASELINE
    # ══════════════════════════════════════════════════════════
    T.log(f"{'─'*60}")
    T.log("  STAGE 1: BASELINE (no splits, no offers)")
    T.log(f"{'─'*60}")

    xch_h1, _ = compare_owned_vs_selectable("Stage 1:", "XCH", None, expect_hidden=0)
    cat_h1, _ = compare_owned_vs_selectable("Stage 1:", "CAT", CAT_ASSET_ID, expect_hidden=0)

    # ══════════════════════════════════════════════════════════
    # STAGE 2: SPLIT XCH 1 → 3
    # ══════════════════════════════════════════════════════════
    T.log(f"\n{'─'*60}")
    T.log("  STAGE 2: SPLIT XCH (1 coin → 3 coins)")
    T.log(f"{'─'*60}")

    xch_count_before, xch_records = get_spendable_records(XCH_WALLET_ID)
    T.log(f"  Current selectable XCH coins: {xch_count_before}")

    if xch_count_before < 1:
        T.fail("Stage 2: No XCH coins to split")
    else:
        # Find biggest coin
        biggest = max(xch_records, key=lambda r: r.get("coin", {}).get("amount", 0))
        big_id = biggest.get("coin_id", "")
        big_amt = biggest.get("coin", {}).get("amount", 0)
        T.log(f"  Splitting {big_id[:16]}... ({fmt(big_amt)}) into 3 coins")

        result = split_coins_rpc(
            wallet_id=XCH_WALLET_ID,
            target_coin_id=big_id,
            num_coins=3,
            amount_per_coin=0,
            fee_mojos=0,
            is_cat=False
        )

        if result and not result.get("error"):
            T.ok("XCH split submitted")

            # FULL CONFIRMATION: pending clear + coin count
            expected_xch = xch_count_before - 1 + 3
            confirmed = confirm_transaction(
                "XCH split",
                wallet_id=XCH_WALLET_ID,
                expected_count=expected_xch
            )

            if confirmed:
                # Also verify owned == selectable before comparing
                T.log("  Verifying XCH owned == selectable (fully settled)...")
                wait_for_owned_equals_selectable(None, "Stage 2")
                T.log("  Comparing owned vs selectable after XCH split:")
                xch_h2, _ = compare_owned_vs_selectable("Stage 2:", "XCH", None, expect_hidden=0)
            else:
                T.log("  ⚠️  Split not confirmed — waiting for owned == selectable...")
                wait_for_owned_equals_selectable(None, "Stage 2")
                T.log("  Comparing after wait:")
                compare_owned_vs_selectable("Stage 2:", "XCH", None, expect_hidden=0)
        else:
            T.fail("XCH split", f"Failed: {result}")

    # ══════════════════════════════════════════════════════════
    # STAGE 3: SPLIT CAT 1 → 3
    # ══════════════════════════════════════════════════════════
    T.log(f"\n{'─'*60}")
    T.log("  STAGE 3: SPLIT CAT (1 coin → 3 coins)")
    T.log(f"{'─'*60}")

    cat_count_before, cat_records = get_spendable_records(CAT_WALLET_ID)
    T.log(f"  Current selectable CAT coins: {cat_count_before}")

    if cat_count_before < 1:
        T.fail("Stage 3: No CAT coins to split")
    else:
        biggest = max(cat_records, key=lambda r: r.get("coin", {}).get("amount", 0))
        big_id = biggest.get("coin_id", "")
        big_amt = biggest.get("coin", {}).get("amount", 0)
        T.log(f"  Splitting {big_id[:16]}... ({fmt(big_amt, is_cat=True)}) into 3 coins")

        result = split_coins_rpc(
            wallet_id=CAT_WALLET_ID,
            target_coin_id=big_id,
            num_coins=3,
            amount_per_coin=0,
            fee_mojos=0,
            is_cat=True
        )

        if result and not result.get("error"):
            T.ok("CAT split submitted")

            expected_cat = cat_count_before - 1 + 3
            confirmed = confirm_transaction(
                "CAT split",
                wallet_id=CAT_WALLET_ID,
                expected_count=expected_cat
            )

            if confirmed:
                T.log("  Verifying CAT owned == selectable (fully settled)...")
                wait_for_owned_equals_selectable(CAT_ASSET_ID, "Stage 3")
                T.log("  Comparing owned vs selectable after CAT split:")
                cat_h3, _ = compare_owned_vs_selectable("Stage 3:", "CAT", CAT_ASSET_ID, expect_hidden=0)
            else:
                T.log("  ⚠️  Split not confirmed — waiting for owned == selectable...")
                wait_for_owned_equals_selectable(CAT_ASSET_ID, "Stage 3")
                T.log("  Comparing after wait:")
                compare_owned_vs_selectable("Stage 3:", "CAT", CAT_ASSET_ID, expect_hidden=0)
        else:
            T.fail("CAT split", f"Failed: {result}")

    # ══════════════════════════════════════════════════════════
    # PRE-STAGE-4: ENSURE EVERYTHING IS FULLY SETTLED
    # ══════════════════════════════════════════════════════════
    T.log(f"\n{'─'*60}")
    T.log("  PRE-STAGE 4: SETTLING CHECK")
    T.log("  Making sure ALL prior splits are fully confirmed")
    T.log(f"{'─'*60}")

    # Wait for both XCH and CAT to have owned == selectable
    # This catches any lingering unconfirmed coins from Stages 2+3
    T.log("  Waiting for XCH to fully settle...")
    xch_settled = wait_for_owned_equals_selectable(None, "Pre-Stage-4")
    T.log("  Waiting for CAT to fully settle...")
    cat_settled = wait_for_owned_equals_selectable(CAT_ASSET_ID, "Pre-Stage-4")

    if xch_settled and cat_settled:
        T.ok("All coins fully settled before Stage 4")
    else:
        T.fail("Pre-Stage 4 settling", "Coins still not equal after timeout — results may be unreliable")

    # ══════════════════════════════════════════════════════════
    # STAGE 4: CREATE BUY + SELL OFFERS
    # ══════════════════════════════════════════════════════════
    T.log(f"\n{'─'*60}")
    T.log("  STAGE 4: CREATE OFFERS (BUY + SELL)")
    T.log("  Then check: does the OTHER side also lose coins?")
    T.log(f"{'─'*60}")

    # Get current selectable coins (NO workaround)
    xch_pre, xch_recs_pre = get_spendable_records(XCH_WALLET_ID)
    cat_pre, cat_recs_pre = get_spendable_records(CAT_WALLET_ID)
    T.log(f"  Before offers: {xch_pre} XCH coins, {cat_pre} CAT coins (selectable)")

    # ── BUY OFFER: offer smallest XCH, request absurd CAT ────
    smallest_xch = min(xch_recs_pre, key=lambda r: r.get("coin", {}).get("amount", 0))
    sm_xch_id = smallest_xch.get("coin_id", "")
    sm_xch_amt = smallest_xch.get("coin", {}).get("amount", 0)
    T.log(f"  Using smallest XCH: {sm_xch_id[:16]}... ({fmt(sm_xch_amt)})")

    buy_dict = {
        XCH_WALLET_ID: -sm_xch_amt,
        CAT_WALLET_ID: 999_999_999,  # absurd — never fills
    }
    T.log("  Creating BUY offer...")
    buy_result = create_offer(buy_dict, validate_only=False, max_time=0, coin_ids=[sm_xch_id])
    buy_trade_id = None
    if buy_result and isinstance(buy_result, dict) and not buy_result.get("error"):
        buy_trade_id = buy_result.get("trade_id", "")
        T.ok("BUY offer created", f"trade_id={buy_trade_id[:16]}..." if buy_trade_id else "")
    else:
        T.fail("BUY offer creation", str(buy_result))

    # Wait for BUY to fully confirm before creating SELL
    if buy_trade_id:
        confirm_transaction("BUY offer")

    # ── SELL OFFER: offer smallest CAT, request absurd XCH ───
    # Re-fetch CAT coins (they may have changed if BUY locked something)
    cat_pre2, cat_recs_pre2 = get_spendable_records(CAT_WALLET_ID)
    T.log(f"  Selectable CAT coins after BUY: {cat_pre2}")

    smallest_cat = min(cat_recs_pre2, key=lambda r: r.get("coin", {}).get("amount", 0))
    sm_cat_id = smallest_cat.get("coin_id", "")
    sm_cat_amt = smallest_cat.get("coin", {}).get("amount", 0)
    T.log(f"  Using smallest CAT: {sm_cat_id[:16]}... ({fmt(sm_cat_amt, is_cat=True)})")

    sell_dict = {
        CAT_WALLET_ID: -sm_cat_amt,
        XCH_WALLET_ID: 999_000_000_000_000,  # 999 XCH — absurd
    }
    T.log("  Creating SELL offer...")
    sell_result = create_offer(sell_dict, validate_only=False, max_time=0, coin_ids=[sm_cat_id])
    sell_trade_id = None
    if sell_result and isinstance(sell_result, dict) and not sell_result.get("error"):
        sell_trade_id = sell_result.get("trade_id", "")
        T.ok("SELL offer created", f"trade_id={sell_trade_id[:16]}..." if sell_trade_id else "")
    else:
        T.fail("SELL offer creation", str(sell_result))

    # Wait for SELL to fully confirm
    if sell_trade_id:
        confirm_transaction("SELL offer")

    # ── KEY COMPARISON ────────────────────────────────────────
    T.log("\n  ═══ KEY TEST ═══")
    T.log("  BUY offer locked 1 XCH coin → expect exactly 1 hidden XCH")
    T.log("  SELL offer locked 1 CAT coin → expect exactly 1 hidden CAT")
    T.log("  IF MORE than 1 hidden per side → SAGE BUG IS REAL")
    T.log("  ═════════════════")

    xch_h4, xch_hidden_ids = compare_owned_vs_selectable(
        "Stage 4:", "XCH", None, expect_hidden=1)
    cat_h4, cat_hidden_ids = compare_owned_vs_selectable(
        "Stage 4:", "CAT", CAT_ASSET_ID, expect_hidden=1)

    if xch_h4 > 1:
        T.log(f"  🔍 XCH: {xch_h4} hidden instead of 1 — "
              f"SELL offer's requested side IS hiding XCH coins!")
    if cat_h4 > 1:
        T.log(f"  🔍 CAT: {cat_h4} hidden instead of 1 — "
              f"BUY offer's requested side IS hiding CAT coins!")

    # ══════════════════════════════════════════════════════════
    # STAGE 5: SPLIT MORE COINS WHILE OFFERS ARE OPEN
    # ══════════════════════════════════════════════════════════
    T.log(f"\n{'─'*60}")
    T.log("  STAGE 5: SPLIT WHILE OFFERS OPEN")
    T.log("  Tests if new coins from splits also get hidden")
    T.log(f"{'─'*60}")

    # ── Split one more XCH coin ──────────────────────────────
    xch_now, xch_recs_now = get_spendable_records(XCH_WALLET_ID)
    T.log(f"  Current selectable XCH: {xch_now}")

    if xch_now >= 2:
        biggest_free = max(xch_recs_now, key=lambda r: r.get("coin", {}).get("amount", 0))
        bf_id = biggest_free.get("coin_id", "")
        bf_amt = biggest_free.get("coin", {}).get("amount", 0)
        T.log(f"  Splitting XCH {bf_id[:16]}... ({fmt(bf_amt)}) into 2 coins")

        result = split_coins_rpc(
            wallet_id=XCH_WALLET_ID,
            target_coin_id=bf_id,
            num_coins=2,
            amount_per_coin=0,
            fee_mojos=0,
            is_cat=False
        )

        if result and not result.get("error"):
            T.ok("XCH split (with offers open) submitted")
            expected = xch_now - 1 + 2
            confirmed = confirm_transaction(
                "XCH split (offers open)",
                wallet_id=XCH_WALLET_ID,
                expected_count=expected
            )
        else:
            T.fail("XCH split (with offers open)", f"Failed: {result}")
    else:
        T.log(f"  Only {xch_now} selectable XCH — skipping split")

    # ── Split one more CAT coin ──────────────────────────────
    cat_now, cat_recs_now = get_spendable_records(CAT_WALLET_ID)
    T.log(f"  Current selectable CAT: {cat_now}")

    if cat_now >= 2:
        biggest_free = max(cat_recs_now, key=lambda r: r.get("coin", {}).get("amount", 0))
        bf_id = biggest_free.get("coin_id", "")
        bf_amt = biggest_free.get("coin", {}).get("amount", 0)
        T.log(f"  Splitting CAT {bf_id[:16]}... ({fmt(bf_amt, is_cat=True)}) into 2 coins")

        result = split_coins_rpc(
            wallet_id=CAT_WALLET_ID,
            target_coin_id=bf_id,
            num_coins=2,
            amount_per_coin=0,
            fee_mojos=0,
            is_cat=True
        )

        if result and not result.get("error"):
            T.ok("CAT split (with offers open) submitted")
            expected = cat_now - 1 + 2
            confirmed = confirm_transaction(
                "CAT split (offers open)",
                wallet_id=CAT_WALLET_ID,
                expected_count=expected
            )
        else:
            T.fail("CAT split (with offers open)", f"Failed: {result}")
    else:
        T.log(f"  Only {cat_now} selectable CAT — skipping split")

    # Wait for splits to fully settle before comparing.
    # We can't use wait_for_owned_equals_selectable here because
    # the offers legitimately lock 1 coin per side. Instead we
    # check that selectable count matches what we expect.
    T.log("\n  Waiting for Stage 5 splits to fully settle...")
    xch_expected_free, _ = get_spendable_records(XCH_WALLET_ID)
    cat_expected_free, _ = get_spendable_records(CAT_WALLET_ID)
    # Give extra time for any pending coins to land
    wait_for_selectable_count(XCH_WALLET_ID, xch_expected_free, "Stage 5 XCH settle", timeout=POLL_TIMEOUT)
    wait_for_selectable_count(CAT_WALLET_ID, cat_expected_free, "Stage 5 CAT settle", timeout=POLL_TIMEOUT)

    T.log("\n  Comparing after splits with offers still open:")
    T.log("  (expect 1 hidden per side = the offer-locked coin)")
    xch_h5, _ = compare_owned_vs_selectable("Stage 5:", "XCH", None, expect_hidden=1)
    cat_h5, _ = compare_owned_vs_selectable("Stage 5:", "CAT", CAT_ASSET_ID, expect_hidden=1)

    # ══════════════════════════════════════════════════════════
    # STAGE 6: CANCEL OFFERS — EVERYTHING SHOULD BE FREE
    # ══════════════════════════════════════════════════════════
    T.log(f"\n{'─'*60}")
    T.log("  STAGE 6: CANCEL OFFERS (all coins should be free)")
    T.log(f"{'─'*60}")

    if buy_trade_id:
        T.log(f"  Cancelling BUY offer {buy_trade_id[:16]}...")
        cancel_result = cancel_offer(buy_trade_id)
        if cancel_result:
            T.ok("BUY cancel submitted")
        else:
            T.fail("BUY cancel", "cancel_offer returned None")

    # Sequential cancels — 1s delay between (Chia rule)
    time.sleep(1)

    if sell_trade_id:
        T.log(f"  Cancelling SELL offer {sell_trade_id[:16]}...")
        cancel_result = cancel_offer(sell_trade_id)
        if cancel_result:
            T.ok("SELL cancel submitted")
        else:
            T.fail("SELL cancel", "cancel_offer returned None")

    # Wait for both cancels to fully confirm on chain
    T.log("  Waiting for cancels to confirm on chain...")
    confirm_transaction("Cancel offers")

    # Wait for all coins to be free — owned must equal selectable
    T.log("  Verifying all coins are free (owned == selectable)...")
    T.log("  Waiting for XCH...")
    wait_for_owned_equals_selectable(None, "Stage 6")
    T.log("  Waiting for CAT...")
    wait_for_owned_equals_selectable(CAT_ASSET_ID, "Stage 6")

    xch_h6, _ = compare_owned_vs_selectable("Stage 6:", "XCH", None, expect_hidden=0)
    cat_h6, _ = compare_owned_vs_selectable("Stage 6:", "CAT", CAT_ASSET_ID, expect_hidden=0)

    # ══════════════════════════════════════════════════════════
    # VERDICT
    # ══════════════════════════════════════════════════════════
    T.log(f"\n{'='*60}")
    T.log("  VERDICT")
    T.log(f"{'='*60}")
    T.log(f"  Results: {T.passed} passed, {T.failed} failed")

    if T.failed == 0:
        T.log("\n  ✅ ALL STAGES PASSED — NO BUG FOUND")
        T.log("     owned == selectable at every stage where it should.")
        T.log("     → Safe to close the GitHub issue.")
        T.log("     → The workaround in wallet_sage.py can be removed.")
    else:
        # Check specifically for the "both sides" bug
        bug_confirmed = False
        try:
            if xch_h4 > 1 or cat_h4 > 1:
                bug_confirmed = True
        except NameError:
            pass

        if bug_confirmed:
            T.log("\n  ❌ BUG CONFIRMED")
            T.log("     Sage hides coins on BOTH sides of an offer.")
            T.log("     → GitHub issue is valid — keep the workaround.")
        else:
            T.log("\n  ⚠️  Some tests failed — see details above.")
            T.log("     May need manual investigation.")

    T.log(f"{'='*60}\n")


if __name__ == "__main__":
    main()
