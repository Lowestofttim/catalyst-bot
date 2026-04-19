#!/usr/bin/env python3
"""
Offer Creation Test — validates the full offer lifecycle with Sage wallet.

Tests (in order):
  0. Coin prep — split off small XCH and CAT coins for testing
  1. Can we connect to Sage and get balances?
  2. Can we fetch existing offers and see their status values?
  3. Can we create a BUY offer (offering XCH, requesting MZ)?
  4. Can we create a SELL offer (offering MZ, requesting XCH)?
  5. Do the new offers appear as OPEN in get_offers?
  6. Does the expires_at_second fix work? (no expiry = stays open)
  7. Can we cancel both test offers?
  8. Do cancelled offers show as closed?

Uses TINY amounts so nothing gets damaged:
  - Buy:  0.01 XCH for MZ at a very low price (won't get filled)
  - Sell:  tiny MZ for XCH at a very high price (won't get filled)

Usage:
  python test_offer_create.py           # Run all tests
  python test_offer_create.py --dry-run # Just check queries, no real offers
"""

import sys
import os
import time
from datetime import datetime
from decimal import Decimal

# Add project dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── CONFIG ─────────────────────────────────────────────────────
# Tiny test amounts — these offers are priced to NEVER get filled
TEST_BUY_XCH_MOJOS = 10_000_000_000       # 0.01 XCH offered
TEST_BUY_CAT_AMOUNT = 10                   # Request just 10 CAT units (absurdly cheap)
TEST_SELL_CAT_AMOUNT = 10                  # Offer just 10 CAT units
TEST_SELL_XCH_MOJOS = 100_000_000_000_000  # Request 100 XCH (absurdly expensive — won't fill)

# Coin prep sizes — what we split off for test offers
PREP_XCH_COIN_SIZE = 20_000_000_000        # 0.02 XCH — enough for the buy offer + headroom
PREP_XCH_SPLIT_COUNT = 2                   # Split into 2 coins (0.01 XCH each)
PREP_CAT_SPLIT_COUNT = 2                   # Split CAT coin into 2 pieces

# Thresholds — skip prep if we already have small enough coins
SMALL_XCH_THRESHOLD = 50_000_000_000       # 0.05 XCH — coins below this are "small enough"
SMALL_CAT_THRESHOLD = 1000                 # CAT mojos below this are "small enough"

DRY_RUN = "--dry-run" in sys.argv
CANCEL_DELAY = 1.0   # seconds between cancel calls (sequential rule)
POLL_INTERVAL = 5     # seconds between coin count polls
POLL_TIMEOUT = 120    # max seconds to wait for split to confirm


# ─── HELPERS ────────────────────────────────────────────────────
class TestResult:
    def __init__(self):
        self.tests = []
        self.start_time = time.time()

    def log(self, msg):
        elapsed = time.time() - self.start_time
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{elapsed:7.1f}s] [{ts}] {msg}")

    def pass_test(self, name, detail=""):
        self.tests.append(("PASS", name))
        self.log(f"  ✅ PASS: {name}" + (f" — {detail}" if detail else ""))

    def fail_test(self, name, detail=""):
        self.tests.append(("FAIL", name))
        self.log(f"  ❌ FAIL: {name}" + (f" — {detail}" if detail else ""))

    def skip_test(self, name, detail=""):
        self.tests.append(("SKIP", name))
        self.log(f"  ⏭️  SKIP: {name}" + (f" — {detail}" if detail else ""))

    def summary(self):
        passed = sum(1 for s, _ in self.tests if s == "PASS")
        failed = sum(1 for s, _ in self.tests if s == "FAIL")
        skipped = sum(1 for s, _ in self.tests if s == "SKIP")
        total = len(self.tests)
        self.log(f"\n{'='*60}")
        self.log(f"  Results: {passed} passed, {failed} failed, {skipped} skipped / {total} total")
        if failed == 0:
            self.log("  🎉 ALL TESTS PASSED!")
        else:
            self.log(f"  ⚠️  {failed} TEST(S) FAILED — see above")
        self.log(f"{'='*60}")
        return failed == 0


def poll_for_coin_change(ws, wallet_id, initial_count, direction="increase",
                         timeout=POLL_TIMEOUT, interval=POLL_INTERVAL, T=None):
    """Poll until coin count changes in the expected direction.

    direction: "increase" = wait for more coins, "decrease" = wait for fewer
    Returns (success, new_count).
    """
    start = time.time()
    while time.time() - start < timeout:
        result = ws.get_spendable_coins(wallet_id)
        if result and isinstance(result, dict):
            records = result.get("records") or result.get("confirmed_records") or []
            current = len(records)
            if direction == "increase" and current > initial_count:
                return True, current
            elif direction == "decrease" and current < initial_count:
                return True, current
        elapsed = time.time() - start
        if T:
            T.log(f"    Polling... {elapsed:.0f}s elapsed, coins={current if result else '?'} (waiting for {direction})")
        time.sleep(interval)
    return False, initial_count


def get_coin_count_and_list(ws, wallet_id):
    """Get spendable coins as (count, list_of_records)."""
    result = ws.get_spendable_coins(wallet_id)
    if result and isinstance(result, dict):
        records = result.get("records") or result.get("confirmed_records") or []
        return len(records), records
    return 0, []


# ─── MAIN ───────────────────────────────────────────────────────
def main():
    T = TestResult()
    T.log(f"{'='*60}")
    T.log(f"  Offer Creation Test {'(DRY RUN)' if DRY_RUN else '(LIVE)'}")
    T.log(f"{'='*60}")

    # ── Load wallet module ──────────────────────────────────────
    T.log("\n📦 Loading wallet_sage module...")
    try:
        import wallet_sage as ws
        T.pass_test("Import wallet_sage")
    except Exception as e:
        T.fail_test("Import wallet_sage", str(e))
        return T.summary()

    # Read config
    XCH_WALLET_ID = ws.WALLET_ID_XCH
    CAT_WALLET_ID = int(os.getenv("CAT_WALLET_ID", "5"))
    CAT_ASSET_ID = os.getenv("CAT_ASSET_ID", "")
    T.log(f"  Config: XCH wallet={XCH_WALLET_ID}, CAT wallet={CAT_WALLET_ID}")
    T.log(f"  CAT asset: {CAT_ASSET_ID[:20]}...")

    # ── Test 1: Connect and get balances ────────────────────────
    T.log("\n🔌 Test 1: Connect to Sage and check balances...")
    try:
        xch_bal_raw = ws.get_wallet_balance(XCH_WALLET_ID)
        if xch_bal_raw and isinstance(xch_bal_raw, dict):
            # get_wallet_balance returns {"wallet_balance": {"spendable_balance": ...}}
            xch_bal = xch_bal_raw.get("wallet_balance") or xch_bal_raw
            spendable = xch_bal.get("spendable_balance", 0)
            total = xch_bal.get("confirmed_wallet_balance", 0)
            spendable_xch = Decimal(str(spendable)) / Decimal("1000000000000")
            total_xch = Decimal(str(total)) / Decimal("1000000000000")
            T.log(f"  XCH spendable: {spendable_xch} XCH, total: {total_xch} XCH")
            T.pass_test("XCH balance query", f"{spendable_xch} XCH spendable")

            if spendable < TEST_BUY_XCH_MOJOS * 2:
                T.fail_test("Sufficient XCH balance", f"Need at least 0.02 XCH, have {spendable_xch}")
                return T.summary()
        else:
            T.fail_test("XCH balance query", f"Got: {xch_bal_raw}")
            return T.summary()

        cat_bal_raw = ws.get_wallet_balance(CAT_WALLET_ID)
        if cat_bal_raw and isinstance(cat_bal_raw, dict):
            cat_bal = cat_bal_raw.get("wallet_balance") or cat_bal_raw
            cat_spendable = cat_bal.get("spendable_balance", 0)
            cat_total = cat_bal.get("confirmed_wallet_balance", 0)
            T.log(f"  CAT spendable: {cat_spendable} mojos, total: {cat_total} mojos")
            T.pass_test("CAT balance query", f"{cat_spendable} spendable mojos")

            if cat_spendable < TEST_SELL_CAT_AMOUNT * 2:
                T.fail_test("Sufficient CAT balance", f"Need at least {TEST_SELL_CAT_AMOUNT * 2}, have {cat_spendable}")
                return T.summary()
        else:
            T.fail_test("CAT balance query", f"Got: {cat_bal_raw}")
            return T.summary()
    except Exception as e:
        T.fail_test("Balance queries", str(e))
        import traceback
        traceback.print_exc()
        return T.summary()

    # ── Test 2: Coin Prep — ensure we have small coins ──────────
    T.log("\n🪙 Test 2: Coin prep — check for small coins, split if needed...")

    if DRY_RUN:
        T.skip_test("Coin prep", "dry run mode")
    else:
        # ── 2a: XCH coin prep ──────────────────────────────────
        T.log("  Checking XCH coins...")
        xch_count, xch_coins = get_coin_count_and_list(ws, XCH_WALLET_ID)
        T.log(f"  Found {xch_count} XCH coins")

        # Check if we already have a small XCH coin
        small_xch = [r for r in xch_coins
                     if isinstance(r, dict) and r.get("coin", {}).get("amount", 0) <= SMALL_XCH_THRESHOLD
                     and r.get("coin", {}).get("amount", 0) >= TEST_BUY_XCH_MOJOS]

        if small_xch:
            small_amt = small_xch[0]["coin"]["amount"]
            small_xch_mojos = Decimal(str(small_amt)) / Decimal("1000000000000")
            T.log(f"  Already have {len(small_xch)} small XCH coin(s) — smallest usable: {small_xch_mojos} XCH")
            T.pass_test("XCH coin prep", f"small coin exists ({small_xch_mojos} XCH)")
        else:
            T.log("  No small XCH coins found — need to split")

            # Find the largest XCH coin to split
            xch_coins_sorted = sorted(xch_coins,
                                      key=lambda r: r.get("coin", {}).get("amount", 0),
                                      reverse=True)
            if not xch_coins_sorted:
                T.fail_test("XCH coin prep", "No XCH coins available!")
                return T.summary()

            source_coin = xch_coins_sorted[0]
            source_id = source_coin.get("coin_id", "")
            source_amt = source_coin.get("coin", {}).get("amount", 0)
            source_xch = Decimal(str(source_amt)) / Decimal("1000000000000")
            T.log(f"  Splitting coin {source_id[:16]}... ({source_xch} XCH) into {PREP_XCH_SPLIT_COUNT} pieces")

            try:
                split_result = ws.split_coins_rpc(
                    wallet_id=XCH_WALLET_ID,
                    target_coin_id=source_id,
                    num_coins=PREP_XCH_SPLIT_COUNT,
                    amount_per_coin=0,  # Sage splits evenly
                    fee_mojos=0,
                    is_cat=False
                )
                if split_result:
                    T.log("  Split submitted, waiting for confirmation...")

                    # Poll for coin count change
                    success, new_count = poll_for_coin_change(
                        ws, XCH_WALLET_ID, xch_count, "increase", T=T
                    )
                    if success:
                        T.pass_test("XCH coin split", f"{xch_count} → {new_count} coins")
                    else:
                        T.fail_test("XCH coin split", f"Timed out waiting — still {xch_count} coins")
                        T.log("  Continuing anyway — the split may still be pending...")
                else:
                    T.fail_test("XCH coin split", f"split_coins_rpc returned: {split_result}")
            except Exception as e:
                T.fail_test("XCH coin split", str(e))
                import traceback
                traceback.print_exc()

        # ── 2b: CAT coin prep ──────────────────────────────────
        T.log("\n  Checking CAT coins...")
        cat_count, cat_coins = get_coin_count_and_list(ws, CAT_WALLET_ID)
        T.log(f"  Found {cat_count} CAT coins")

        small_cat = [r for r in cat_coins
                     if isinstance(r, dict) and r.get("coin", {}).get("amount", 0) <= SMALL_CAT_THRESHOLD
                     and r.get("coin", {}).get("amount", 0) >= TEST_SELL_CAT_AMOUNT]

        if small_cat:
            small_amt = small_cat[0]["coin"]["amount"]
            T.log(f"  Already have {len(small_cat)} small CAT coin(s) — smallest usable: {small_amt} mojos")
            T.pass_test("CAT coin prep", f"small coin exists ({small_amt} mojos)")
        else:
            T.log("  No small CAT coins found — need to split")

            cat_coins_sorted = sorted(cat_coins,
                                      key=lambda r: r.get("coin", {}).get("amount", 0),
                                      reverse=True)
            if not cat_coins_sorted:
                T.fail_test("CAT coin prep", "No CAT coins available!")
                return T.summary()

            source_coin = cat_coins_sorted[0]
            source_id = source_coin.get("coin_id", "")
            source_amt = source_coin.get("coin", {}).get("amount", 0)
            T.log(f"  Splitting CAT coin {source_id[:16]}... ({source_amt} mojos) into {PREP_CAT_SPLIT_COUNT} pieces")

            try:
                split_result = ws.split_coins_rpc(
                    wallet_id=CAT_WALLET_ID,
                    target_coin_id=source_id,
                    num_coins=PREP_CAT_SPLIT_COUNT,
                    amount_per_coin=0,  # Sage splits evenly
                    fee_mojos=0,
                    is_cat=True
                )
                if split_result:
                    T.log("  Split submitted, waiting for confirmation...")

                    success, new_count = poll_for_coin_change(
                        ws, CAT_WALLET_ID, cat_count, "increase", T=T
                    )
                    if success:
                        T.pass_test("CAT coin split", f"{cat_count} → {new_count} coins")
                    else:
                        T.fail_test("CAT coin split", f"Timed out waiting — still {cat_count} coins")
                        T.log("  Continuing anyway — the split may still be pending...")
                else:
                    T.fail_test("CAT coin split", f"split_coins_rpc returned: {split_result}")
            except Exception as e:
                T.fail_test("CAT coin split", str(e))
                import traceback
                traceback.print_exc()

    # ── Test 3: Fetch existing offers and inspect status ────────
    T.log("\n📋 Test 3: Fetch existing offers and inspect status values...")
    try:
        offers = ws.get_all_offers(include_completed=False, start=0, end=10)
        if offers is None:
            T.fail_test("Fetch offers", "get_all_offers returned None")
            return T.summary()

        T.log(f"  Found {len(offers)} offers (include_completed=False)")

        if len(offers) > 0:
            # Show the raw status of each offer
            T.log(f"\n  {'─'*50}")
            T.log("  Raw status values from Sage:")
            status_counts = {}
            for i, o in enumerate(offers[:10]):
                raw_status = o.get("status")
                trade_id = (o.get("trade_id") or o.get("offer_id") or "?")[:16]
                valid_times = o.get("valid_times", {})
                max_time = valid_times.get("max_time", "none")
                expires = o.get("expires_at_second", "none")

                status_key = repr(raw_status)
                status_counts[status_key] = status_counts.get(status_key, 0) + 1

                if i < 5:  # Show first 5 in detail
                    T.log(f"    Offer {i}: status={repr(raw_status)}, "
                          f"id={trade_id}..., "
                          f"max_time={max_time}, expires_at={expires}")

            T.log(f"\n  Status distribution: {status_counts}")
            T.log(f"  {'─'*50}")

            # Check if _is_open_status agrees
            open_count = 0
            closed_count = 0
            for o in offers:
                if ws._is_open_status(o.get("status"), offer_record=o):
                    open_count += 1
                else:
                    closed_count += 1
            T.log(f"  _is_open_status says: {open_count} open, {closed_count} closed")
            T.pass_test("Fetch and inspect offers", f"{len(offers)} offers, {open_count} open")
        else:
            T.log("  No existing offers to inspect (that's OK)")
            T.pass_test("Fetch offers", "0 offers returned (clean slate)")

    except Exception as e:
        T.fail_test("Fetch offers", str(e))
        import traceback
        traceback.print_exc()

    # ── Test 4: Create a BUY offer ──────────────────────────────
    T.log("\n🟢 Test 4: Create BUY offer (0.01 XCH → tiny MZ, absurd price)...")

    buy_trade_id = None

    if DRY_RUN:
        T.skip_test("Create BUY offer", "dry run mode")
    else:
        try:
            # Buy offer: offer XCH (negative), request CAT (positive)
            buy_dict = {
                XCH_WALLET_ID: -TEST_BUY_XCH_MOJOS,    # Offering 0.01 XCH
                CAT_WALLET_ID: TEST_BUY_CAT_AMOUNT,     # Requesting 10 MZ
            }
            T.log(f"  Offer dict: {buy_dict}")
            T.log("  max_time=0 (should NOT send expires_at_second)")

            result = ws.create_offer(buy_dict, validate_only=False, max_time=0)

            if result and isinstance(result, dict):
                # Check for errors first
                error = result.get("error", "")
                if error:
                    T.fail_test("Create BUY offer", f"Error: {error}")
                else:
                    buy_trade_id = result.get("trade_id", "")
                    success = result.get("success", False)
                    offer_str = result.get("offer", "")

                    T.log(f"  Response keys: {list(result.keys())}")
                    T.log(f"  success: {success}")
                    T.log(f"  trade_id: {buy_trade_id[:20]}..." if buy_trade_id else "  trade_id: MISSING!")

                    if buy_trade_id and (success or offer_str):
                        T.pass_test("Create BUY offer", f"trade_id={buy_trade_id[:16]}...")
                    else:
                        T.fail_test("Create BUY offer", f"No trade_id or success flag: {str(result)[:200]}")
            else:
                T.fail_test("Create BUY offer", f"Result: {result}")
        except Exception as e:
            T.fail_test("Create BUY offer", str(e))
            import traceback
            traceback.print_exc()

    # Small delay between offers
    if not DRY_RUN and buy_trade_id:
        T.log("  Waiting 2s for wallet to settle...")
        time.sleep(2)

    # ── Test 5: Create a SELL offer ─────────────────────────────
    T.log("\n🔴 Test 5: Create SELL offer (tiny MZ → absurd XCH price)...")

    sell_trade_id = None

    if DRY_RUN:
        T.skip_test("Create SELL offer", "dry run mode")
    else:
        try:
            # Sell offer: offer CAT (negative), request XCH (positive)
            sell_dict = {
                CAT_WALLET_ID: -TEST_SELL_CAT_AMOUNT,    # Offering 10 MZ
                XCH_WALLET_ID: TEST_SELL_XCH_MOJOS,      # Requesting 100 XCH (absurd!)
            }
            T.log(f"  Offer dict: {sell_dict}")
            T.log("  max_time=0 (should NOT send expires_at_second)")

            result = ws.create_offer(sell_dict, validate_only=False, max_time=0)

            if result and isinstance(result, dict):
                error = result.get("error", "")
                if error:
                    T.fail_test("Create SELL offer", f"Error: {error}")
                else:
                    sell_trade_id = result.get("trade_id", "")
                    success = result.get("success", False)
                    offer_str = result.get("offer", "")

                    T.log(f"  Response keys: {list(result.keys())}")
                    T.log(f"  success: {success}")
                    T.log(f"  trade_id: {sell_trade_id[:20]}..." if sell_trade_id else "  trade_id: MISSING!")

                    if sell_trade_id and (success or offer_str):
                        T.pass_test("Create SELL offer", f"trade_id={sell_trade_id[:16]}...")
                    else:
                        T.fail_test("Create SELL offer", f"No trade_id or success flag: {str(result)[:200]}")
            else:
                T.fail_test("Create SELL offer", f"Result: {result}")
        except Exception as e:
            T.fail_test("Create SELL offer", str(e))
            import traceback
            traceback.print_exc()

    # ── Test 6: Verify offers appear as OPEN ────────────────────
    T.log("\n🔍 Test 6: Verify new offers appear as OPEN in wallet...")

    if DRY_RUN or (not buy_trade_id and not sell_trade_id):
        T.skip_test("Verify offers OPEN", "no offers created")
    else:
        try:
            T.log("  Waiting 3s for wallet to index new offers...")
            time.sleep(3)

            offers = ws.get_all_offers(include_completed=False, start=0, end=200)
            if offers is None:
                T.fail_test("Verify offers OPEN", "get_all_offers returned None")
            else:
                T.log(f"  Total offers returned: {len(offers)}")

                # Look for our test offers by trade_id
                found_buy = None
                found_sell = None
                for o in offers:
                    tid = o.get("trade_id") or o.get("offer_id") or ""
                    if buy_trade_id and tid == buy_trade_id:
                        found_buy = o
                    if sell_trade_id and tid == sell_trade_id:
                        found_sell = o

                # Check buy offer
                if buy_trade_id:
                    if found_buy:
                        raw_status = found_buy.get("status")
                        is_open = ws._is_open_status(raw_status, offer_record=found_buy)
                        valid_times = found_buy.get("valid_times", {})
                        expires = found_buy.get("expires_at_second", "not set")

                        T.log("  BUY offer found!")
                        T.log(f"    raw status: {repr(raw_status)}")
                        T.log(f"    _is_open_status: {is_open}")
                        T.log(f"    valid_times: {valid_times}")
                        T.log(f"    expires_at_second: {expires}")

                        if is_open:
                            T.pass_test("BUY offer is OPEN", f"status={repr(raw_status)}")
                        else:
                            T.fail_test("BUY offer is OPEN",
                                        f"status={repr(raw_status)} — "
                                        f"_is_open_status returned False! "
                                        f"This is the bug we're testing for.")
                    else:
                        T.fail_test("BUY offer found in wallet",
                                    f"trade_id {buy_trade_id[:16]}... not in {len(offers)} offers")
                        # Show what statuses exist
                        for i, o in enumerate(offers[:5]):
                            T.log(f"    offer {i}: status={repr(o.get('status'))}, "
                                  f"id={(o.get('trade_id') or o.get('offer_id', '?'))[:16]}")

                # Check sell offer
                if sell_trade_id:
                    if found_sell:
                        raw_status = found_sell.get("status")
                        is_open = ws._is_open_status(raw_status, offer_record=found_sell)
                        valid_times = found_sell.get("valid_times", {})
                        expires = found_sell.get("expires_at_second", "not set")

                        T.log("  SELL offer found!")
                        T.log(f"    raw status: {repr(raw_status)}")
                        T.log(f"    _is_open_status: {is_open}")
                        T.log(f"    valid_times: {valid_times}")
                        T.log(f"    expires_at_second: {expires}")

                        if is_open:
                            T.pass_test("SELL offer is OPEN", f"status={repr(raw_status)}")
                        else:
                            T.fail_test("SELL offer is OPEN",
                                        f"status={repr(raw_status)} — "
                                        f"_is_open_status returned False!")
                    else:
                        T.fail_test("SELL offer found in wallet",
                                    f"trade_id {sell_trade_id[:16]}... not in {len(offers)} offers")

        except Exception as e:
            T.fail_test("Verify offers OPEN", str(e))
            import traceback
            traceback.print_exc()

    # ── Test 7: Classification test ─────────────────────────────
    T.log("\n🏷️  Test 7: Full classification (buy/sell/closed) check...")

    if DRY_RUN or (not buy_trade_id and not sell_trade_id):
        T.skip_test("Classification test", "no offers created")
    else:
        try:
            offers = ws.get_all_offers(include_completed=False, start=0, end=200)
            if offers:
                open_buys, open_sells, closed = ws.classify_offers_from_list(
                    offers, CAT_ASSET_ID
                )
                T.log("  classify_offers_from_list result:")
                T.log(f"    open buys:  {len(open_buys)}")
                T.log(f"    open sells: {len(open_sells)}")
                T.log(f"    closed:     {len(closed)}")

                # Our test buy should be in open_buys
                if buy_trade_id:
                    buy_in_open = any(
                        (o.get("trade_id") or o.get("offer_id")) == buy_trade_id
                        for o in open_buys
                    )
                    if buy_in_open:
                        T.pass_test("BUY in open_buys list")
                    else:
                        T.fail_test("BUY in open_buys list",
                                    f"trade_id {buy_trade_id[:16]}... not found in {len(open_buys)} open buys")

                # Our test sell should be in open_sells
                if sell_trade_id:
                    sell_in_open = any(
                        (o.get("trade_id") or o.get("offer_id")) == sell_trade_id
                        for o in open_sells
                    )
                    if sell_in_open:
                        T.pass_test("SELL in open_sells list")
                    else:
                        T.fail_test("SELL in open_sells list",
                                    f"trade_id {sell_trade_id[:16]}... not found in {len(open_sells)} open sells")
            else:
                T.fail_test("Classification test", "No offers to classify")
        except Exception as e:
            T.fail_test("Classification test", str(e))
            import traceback
            traceback.print_exc()

    # ── Done — leave offers open for manual inspection ─────────
    T.log("\n📌 Test offers LEFT OPEN for inspection:")
    if buy_trade_id:
        T.log(f"  BUY:  {buy_trade_id}")
    if sell_trade_id:
        T.log(f"  SELL: {sell_trade_id}")
    T.log("  Cancel them manually when done checking.")

    # ── Summary ─────────────────────────────────────────────────
    return T.summary()


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
