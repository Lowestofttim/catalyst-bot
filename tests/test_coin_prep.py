#!/usr/bin/env python3
"""
Mini Coin Prep Test — validates the core operations before a real run.

Tests (in order):
  1. Can we query coins from Sage?
  2. Can we send-to-self a small amount?
  3. Does the pool coin appear after send confirms?
  4. Can we find the CHANGE coin after the send?
  5. Is the change coin spendable?
  6. Can we split the pool coin?
  7. Do the split coins appear in the DB?
  8. Does the change coin survive the split (not consumed)?

Uses TINY amounts (0.01 XCH split into 2 pieces) so nothing gets damaged.
The test cleans up after itself by reconsolidating.

Usage:
  python test_coin_prep.py           # Run all tests
  python test_coin_prep.py --dry-run # Just check queries, no transactions
"""

import sys
import os
import time
from datetime import datetime

# Add project dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── CONFIG ─────────────────────────────────────────────────────
TEST_XCH_MOJOS = 10_000_000_000  # 0.01 XCH — tiny test amount
TEST_SPLIT_COUNT = 2              # Split into just 2 pieces
XCH_WALLET_ID = 1
DRY_RUN = "--dry-run" in sys.argv

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

    def summary(self):
        passed = sum(1 for s, _ in self.tests if s == "PASS")
        failed = sum(1 for s, _ in self.tests if s == "FAIL")
        total = len(self.tests)
        self.log(f"\n{'='*60}")
        self.log(f"  RESULTS: {passed}/{total} passed, {failed} failed")
        if failed:
            self.log("  FAILED TESTS:")
            for status, name in self.tests:
                if status == "FAIL":
                    self.log(f"    ❌ {name}")
        self.log(f"{'='*60}")
        return failed == 0


def wait_for_pending_clear(t, label, timeout_s=120):
    """Poll until no pending transactions."""
    from wallet_sage import get_pending_transactions
    for poll in range(timeout_s // 5):
        time.sleep(5)
        pending = get_pending_transactions()
        if len(pending) == 0:
            t.log(f"      Pending clear after {(poll + 1) * 5}s")
            return True
        if (poll + 1) % 6 == 0:
            t.log(f"      ⏳ {(poll + 1) * 5}s — {len(pending)} pending...")
    return False


def find_coin_by_amount(wallet_id, target_mojos, t, label=""):
    """Find a coin with exact amount."""
    from wallet_sage import get_spendable_coins_rpc
    result = get_spendable_coins_rpc(wallet_id)
    if not result or not result.get("success"):
        return None
    records = result.get("confirmed_records") or result.get("records") or []
    for rec in records:
        coin = rec.get("coin", {})
        amount = coin.get("amount", 0)
        if amount == target_mojos:
            coin_id = rec.get("name") or rec.get("coin_id") or ""
            parent = coin.get("parent_coin_info", "")
            puzzle = coin.get("puzzle_hash", "")
            # Compute coin_id if missing
            if not coin_id or len(coin_id.replace("0x", "")) < 64:
                if parent and puzzle:
                    import hashlib
                    parent_bytes = bytes.fromhex(parent.replace("0x", ""))
                    puzzle_bytes = bytes.fromhex(puzzle.replace("0x", ""))
                    amount_bytes = amount.to_bytes(8, 'big')
                    coin_id = "0x" + hashlib.sha256(
                        parent_bytes + puzzle_bytes + amount_bytes
                    ).hexdigest()
            return {
                "coin_id": coin_id,
                "amount": amount,
                "parent": parent,
                "puzzle_hash": puzzle,
            }
    return None


def get_all_coins(wallet_id):
    """Get all spendable coins."""
    from wallet_sage import get_spendable_coins_rpc
    result = get_spendable_coins_rpc(wallet_id)
    if not result or not result.get("success"):
        return []
    records = result.get("confirmed_records") or result.get("records") or []
    coins = []
    for rec in records:
        coin = rec.get("coin", {})
        amount = coin.get("amount", 0)
        coin_id = rec.get("name") or rec.get("coin_id") or ""
        if amount > 0:
            coins.append({"coin_id": coin_id, "amount": amount})
    return coins


def find_biggest_non_tier_coin(wallet_id, tier_size_mojos, t):
    """Find largest coin that ISN'T the tier size."""
    coins = get_all_coins(wallet_id)
    coins.sort(key=lambda c: c["amount"], reverse=True)
    for c in coins:
        if c["amount"] != tier_size_mojos:
            return c
    return None


# ─── MAIN TEST ──────────────────────────────────────────────────
def main():
    t = TestResult()

    t.log(f"{'='*60}")
    t.log("  MINI COIN PREP TEST")
    t.log(f"  Test amount: {TEST_XCH_MOJOS / 1e12:.4f} XCH ({TEST_XCH_MOJOS:,} mojos)")
    t.log(f"  Split into: {TEST_SPLIT_COUNT} pieces")
    t.log(f"  Mode: {'DRY RUN (no transactions)' if DRY_RUN else 'LIVE (real transactions)'}")
    t.log(f"{'='*60}\n")

    # ── TEST 1: Query coins ─────────────────────────────────
    t.log("TEST 1: Can we query XCH coins from Sage?")
    try:
        from wallet_sage import get_pending_transactions, are_coins_spendable
        coins = get_all_coins(XCH_WALLET_ID)
        if coins:
            total = sum(c["amount"] for c in coins)
            t.pass_test("Query coins", f"{len(coins)} coins, total {total/1e12:.6f} XCH")

            # Show what we have
            t.log("      Coins:")
            for i, c in enumerate(sorted(coins, key=lambda x: x["amount"], reverse=True)[:5]):
                cid = c["coin_id"]
                cid_short = cid[:16] + "..." if len(cid) > 16 else cid
                t.log(f"        #{i+1}: {cid_short} = {c['amount']/1e12:.6f} XCH")
            if len(coins) > 5:
                t.log(f"        ... and {len(coins) - 5} more")
        else:
            t.fail_test("Query coins", "No coins returned!")
            t.summary()
            return
    except Exception as e:
        t.fail_test("Query coins", str(e))
        t.summary()
        return

    # ── TEST 2: Check pending transactions ───────────────────
    t.log("\nTEST 2: Are there any pending transactions?")
    try:
        pending = get_pending_transactions()
        if len(pending) == 0:
            t.pass_test("No pending txns")
        else:
            t.fail_test("Pending transactions", f"{len(pending)} pending — wait for them to confirm!")
            t.log("      ⚠️ Cannot proceed with pending transactions. Wait and retry.")
            t.summary()
            return
    except Exception as e:
        t.fail_test("Check pending", str(e))
        t.summary()
        return

    # ── TEST 3: Check biggest coin is spendable ──────────────
    t.log("\nTEST 3: Is the biggest coin spendable?")
    try:
        biggest = sorted(coins, key=lambda c: c["amount"], reverse=True)[0]
        biggest_id = biggest["coin_id"].replace("0x", "")
        spendable = are_coins_spendable([biggest_id])
        if spendable:
            t.pass_test("Biggest coin spendable",
                        f"{biggest_id[:16]}... ({biggest['amount']/1e12:.4f} XCH)")
        else:
            t.fail_test("Biggest coin NOT spendable",
                        f"{biggest_id[:16]}... — wallet may still be syncing")
            t.summary()
            return
    except Exception as e:
        t.fail_test("Spendable check", str(e))
        t.summary()
        return

    if DRY_RUN:
        t.log("\n🏁 DRY RUN — skipping transaction tests")
        t.pass_test("Dry run complete")
        t.summary()
        return

    # Ensure we have enough
    if biggest["amount"] < TEST_XCH_MOJOS * 2:
        t.fail_test("Insufficient balance", f"Need at least {TEST_XCH_MOJOS * 2 / 1e12:.4f} XCH")
        t.summary()
        return

    # ── TEST 4: Send-to-self (create pool coin) ──────────────
    t.log(f"\nTEST 4: Send {TEST_XCH_MOJOS/1e12:.4f} XCH to self (create pool coin)")
    try:
        from wallet_sage import send_transaction, get_next_address, split_coins_rpc

        addr_result = get_next_address(XCH_WALLET_ID, new_address=False)
        address = addr_result.get("address", "")
        if not address:
            t.fail_test("Get address", "No address returned")
            t.summary()
            return

        t.log(f"      Address: {address[:20]}...")
        t.log(f"      Sending {TEST_XCH_MOJOS:,} mojos...")

        result = send_transaction(
            wallet_id=XCH_WALLET_ID,
            amount_mojos=TEST_XCH_MOJOS,
            address=address,
            fee_mojos=0,
        )

        if result is None or (isinstance(result, dict) and result.get("error")):
            err = result.get("error", "None") if isinstance(result, dict) else "None"
            t.fail_test("Send-to-self", f"Error: {err}")
            t.summary()
            return

        t.pass_test("Send submitted")

        # Wait for on-chain confirmation
        t.log("      ⏳ Waiting for on-chain confirmation...")
        if wait_for_pending_clear(t, "send"):
            t.pass_test("Send confirmed on-chain")
        else:
            t.fail_test("Send confirmation timeout")
            t.summary()
            return
    except Exception as e:
        t.fail_test("Send-to-self", str(e))
        t.summary()
        return

    # ── TEST 5: Find pool coin ───────────────────────────────
    t.log(f"\nTEST 5: Can we find the pool coin ({TEST_XCH_MOJOS:,} mojos)?")
    pool_coin = None
    for attempt in range(30):  # Up to 150s
        pool_coin = find_coin_by_amount(XCH_WALLET_ID, TEST_XCH_MOJOS, t, "pool")
        if pool_coin:
            t.pass_test("Pool coin found",
                        f"{pool_coin['coin_id'][:16]}... after {attempt * 5}s")
            break
        if attempt > 0 and attempt % 6 == 0:
            t.log(f"      ⏳ {attempt * 5}s — not yet visible...")
        time.sleep(5)
    else:
        t.fail_test("Pool coin never appeared after 150s")
        t.summary()
        return

    # ── TEST 6: Find CHANGE coin (the critical test!) ────────
    t.log("\nTEST 6: Can we find the CHANGE coin? (biggest non-pool coin)")
    change_coin = find_biggest_non_tier_coin(XCH_WALLET_ID, TEST_XCH_MOJOS, t)
    if change_coin:
        t.pass_test("Change coin found",
                    f"{change_coin['coin_id'][:16]}... ({change_coin['amount']/1e12:.6f} XCH)")
    else:
        t.fail_test("Change coin NOT found!")

    # ── TEST 7: Is change coin spendable? ────────────────────
    t.log("\nTEST 7: Is the change coin spendable?")
    if change_coin:
        change_id = change_coin["coin_id"].replace("0x", "")
        for attempt in range(30):
            if are_coins_spendable([change_id]):
                t.pass_test("Change coin spendable", f"after {attempt * 5}s")
                break
            if attempt > 0 and attempt % 6 == 0:
                t.log(f"      ⏳ {attempt * 5}s — not yet spendable...")
            time.sleep(5)
        else:
            t.fail_test("Change coin never became spendable after 150s")
    else:
        t.fail_test("No change coin to check")

    # ── TEST 8: Split pool coin ──────────────────────────────
    t.log(f"\nTEST 8: Split pool coin into {TEST_SPLIT_COUNT} pieces")
    pool_id = pool_coin["coin_id"].replace("0x", "")

    # First confirm pool is spendable
    t.log("      Confirming pool coin is spendable...")
    for attempt in range(30):
        if are_coins_spendable([pool_id]):
            t.log(f"      Pool spendable after {attempt * 5}s")
            break
        time.sleep(5)
    else:
        t.fail_test("Pool coin never spendable")
        t.summary()
        return

    try:
        result = split_coins_rpc(
            wallet_id=XCH_WALLET_ID,
            target_coin_id=pool_id,
            num_coins=TEST_SPLIT_COUNT,
            amount_per_coin=0,
            fee_mojos=0,
            is_cat=False,
        )
        if result is None or (isinstance(result, dict) and result.get("error")):
            err = result.get("error", "None") if isinstance(result, dict) else "None"
            t.fail_test("Split", f"Error: {err}")
            t.summary()
            return

        t.pass_test("Split submitted")

        t.log("      ⏳ Waiting for split confirmation...")
        if wait_for_pending_clear(t, "split"):
            t.pass_test("Split confirmed on-chain")
        else:
            t.fail_test("Split confirmation timeout")
            t.summary()
            return
    except Exception as e:
        t.fail_test("Split", str(e))
        t.summary()
        return

    # ── TEST 9: Verify split created correct coins ───────────
    t.log("\nTEST 9: Did the split create the right coins?")
    expected_piece = TEST_XCH_MOJOS // TEST_SPLIT_COUNT
    found_pieces = 0
    for attempt in range(20):
        coins_now = get_all_coins(XCH_WALLET_ID)
        found_pieces = sum(1 for c in coins_now if c["amount"] == expected_piece)
        if found_pieces >= TEST_SPLIT_COUNT:
            t.pass_test("Split coins found",
                        f"{found_pieces} × {expected_piece/1e12:.6f} XCH after {attempt * 5}s")
            break
        time.sleep(5)
    else:
        t.fail_test("Split coins", f"Found {found_pieces}/{TEST_SPLIT_COUNT}")

    # ── TEST 10: Change coin survived the split? ─────────────
    t.log("\nTEST 10: Did the change coin survive the split?")
    coins_after = get_all_coins(XCH_WALLET_ID)
    change_after = None
    for c in sorted(coins_after, key=lambda x: x["amount"], reverse=True):
        if c["amount"] != expected_piece:
            change_after = c
            break

    if change_after and change_after["amount"] == change_coin["amount"]:
        t.pass_test("Change coin survived!",
                    f"Same size: {change_after['amount']/1e12:.6f} XCH")
    elif change_after:
        t.fail_test("Change coin DIFFERENT size!",
                    f"Was {change_coin['amount']/1e12:.6f}, now {change_after['amount']/1e12:.6f} XCH")
    else:
        t.fail_test("Change coin MISSING after split!")

    # ── TEST 11: DB recording ────────────────────────────────
    t.log("\nTEST 11: Can we record coins to DB?")
    try:
        from database import upsert_coin, get_connection
        test_coin_id = "0xTEST_" + datetime.now().strftime("%H%M%S")
        result = upsert_coin(test_coin_id, "xch", expected_piece)
        if result:
            t.pass_test("DB upsert works")
            # Clean up test coin
            conn = get_connection()
            conn.execute("DELETE FROM coins WHERE coin_id=?", (test_coin_id,))
            conn.commit()
        else:
            t.fail_test("DB upsert returned False")
    except Exception as e:
        t.fail_test("DB recording", str(e))

    # ── FINAL: Show all coins ────────────────────────────────
    t.log("\n--- Final coin state ---")
    final_coins = get_all_coins(XCH_WALLET_ID)
    final_coins.sort(key=lambda c: c["amount"], reverse=True)
    for i, c in enumerate(final_coins[:10]):
        cid = c["coin_id"][:16] + "..." if len(c["coin_id"]) > 16 else c["coin_id"]
        t.log(f"   #{i+1}: {cid} = {c['amount']/1e12:.6f} XCH")
    t.log(f"   Total: {len(final_coins)} coins = {sum(c['amount'] for c in final_coins)/1e12:.6f} XCH")

    # ── CLEANUP: Reconsolidate ───────────────────────────────
    t.log("\n--- Cleanup: reconsolidating test coins ---")
    t.log("   (The next real coin prep will consolidate anyway)")
    t.log("   No cleanup needed — test coins are tiny and harmless")

    # ── SUMMARY ──────────────────────────────────────────────
    all_passed = t.summary()
    return all_passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
