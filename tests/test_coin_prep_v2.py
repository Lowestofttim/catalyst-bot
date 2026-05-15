#!/usr/bin/env python3
"""
Comprehensive Coin Prep Test V2 — validates the MULTI_SEND approach for BOTH XCH and CAT.

This tests the NEW coin prep strategy where all pool coins are created
in a SINGLE multi_send transaction per asset type, eliminating the tier coin
consumption bug where Sage's coin selection would eat tier coins for exact-match.

Tests (in order):
  Phase 1 — Prerequisites
    1. Sage wallet connectivity (RPC alive, synced)
    2. XCH coin query + balance check
    3. CAT coin query + balance check

  Phase 2 — Multi-Send (XCH + CAT simultaneously)
    4. XCH multi_send with 2 payments to self (creates 2 XCH pool coins)
    5. CAT multi_send with 2 payments to self (creates 2 CAT pool coins)
    6. XCH pool coins appear and become spendable
    7. CAT pool coins appear and become spendable

  Phase 3 — Split All Pool Coins
    8.  Split XCH pool A using coin_ids
    9.  Split XCH pool B using coin_ids
   10.  Split CAT pool A using coin_ids
   11.  Split CAT pool B using coin_ids

  Phase 4 — Final Verification
   12. All expected XCH split coins present and spendable
   13. All expected CAT split coins present and spendable

  Phase 5 — Cleanup
   14. (optional) Reconsolidate

Uses SMALL amounts so nothing gets damaged:
  XCH: 0.02 + 0.03 = 0.05 XCH total
  CAT: 20 + 30 = 50 CAT mojos total (very small)

Usage:
  python test_coin_prep_v2.py              # Run all tests
  python test_coin_prep_v2.py --dry-run    # Check connectivity only, no transactions
  python test_coin_prep_v2.py --skip-cleanup  # Don't reconsolidate at end

REQUIRES: Sage wallet running on localhost:9257 with sufficient XCH and CAT balance.
"""

import sys
import os
import time
import traceback
from datetime import datetime
from decimal import Decimal

# Add project dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─── CONFIG ─────────────────────────────────────────────────────
# XCH Pools
XCH_POOL_A_MOJOS = 20_000_000_000  # 0.02 XCH
XCH_POOL_A_SPLIT = 2  # Split into 2 × 0.01 XCH
XCH_POOL_B_MOJOS = 30_000_000_000  # 0.03 XCH
XCH_POOL_B_SPLIT = 3  # Split into 3 × 0.01 XCH
XCH_TOTAL_MOJOS = XCH_POOL_A_MOJOS + XCH_POOL_B_MOJOS

# CAT Pools — use small amounts (CAT mojos, NOT XCH mojos)
# CAT decimals = 3, so 1000 mojos = 1 token. We use 20+30 = 50 mojos (tiny).
CAT_POOL_A_MOJOS = 20  # 0.02 CAT tokens
CAT_POOL_A_SPLIT = 2  # Split into 2 × 10 mojos
CAT_POOL_B_MOJOS = 30  # 0.03 CAT tokens
CAT_POOL_B_SPLIT = 3  # Split into 3 × 10 mojos
CAT_TOTAL_MOJOS = CAT_POOL_A_MOJOS + CAT_POOL_B_MOJOS

XCH_WALLET_ID = 1


# CAT wallet ID — read from .env, fallback to 5
def _get_cat_wallet_id():
    """Read CAT_WALLET_ID from .env (same logic as config.py)."""
    from dotenv import load_dotenv

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(env_path)
    for key in ["CAT_WALLET_ID", "CHIA_WALLET_ID_MZ", "CHIA_WALLET_ID_CAT"]:
        val = os.getenv(key, "")
        if val.strip():
            try:
                return int(val.strip())
            except ValueError:
                continue
    return 5  # default


CAT_WALLET_ID = _get_cat_wallet_id()

MIN_XCH_BALANCE_MOJOS = 100_000_000_000  # Need at least 0.1 XCH
MIN_CAT_BALANCE_MOJOS = 100  # Need at least 100 CAT mojos (0.1 token)

DRY_RUN = "--dry-run" in sys.argv
SKIP_CLEANUP = "--skip-cleanup" in sys.argv


# ─── TEST FRAMEWORK ────────────────────────────────────────────
class TestRunner:
    def __init__(self):
        self.tests = []
        self.start_time = time.time()
        # XCH state
        self.xch_pool_a_id = None
        self.xch_pool_b_id = None
        # CAT state
        self.cat_pool_a_id = None
        self.cat_pool_b_id = None

    def log(self, msg):
        elapsed = time.time() - self.start_time
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{elapsed:7.1f}s] [{ts}] {msg}")

    def pass_test(self, num, name, detail=""):
        self.tests.append(("PASS", num, name))
        self.log(f"  ✅ PASS #{num}: {name}" + (f" — {detail}" if detail else ""))

    def fail_test(self, num, name, detail=""):
        self.tests.append(("FAIL", num, name))
        self.log(f"  ❌ FAIL #{num}: {name}" + (f" — {detail}" if detail else ""))

    def skip_test(self, num, name, detail=""):
        self.tests.append(("SKIP", num, name))
        self.log(f"  ⏭️ SKIP #{num}: {name}" + (f" — {detail}" if detail else ""))

    def summary(self):
        passed = sum(1 for s, _, _ in self.tests if s == "PASS")
        failed = sum(1 for s, _, _ in self.tests if s == "FAIL")
        skipped = sum(1 for s, _, _ in self.tests if s == "SKIP")
        total = len(self.tests)
        elapsed = time.time() - self.start_time

        self.log(f"\n{'=' * 60}")
        self.log(
            f"TEST RESULTS: {passed}/{total} passed, {failed} failed, {skipped} skipped"
        )
        self.log(f"Total time: {elapsed:.1f}s")
        self.log(f"{'=' * 60}")

        for status, num, name in self.tests:
            icon = "✅" if status == "PASS" else ("❌" if status == "FAIL" else "⏭️")
            self.log(f"  {icon} #{num}: {name}")

        if failed > 0:
            self.log(f"\n⚠️  {failed} test(s) FAILED — review output above")
        else:
            self.log("\n🎉 All tests passed!")

        return failed == 0


# ─── HELPERS ──────────────────────────────────────────────────
def get_coins(wallet_id, label=""):
    """Get all spendable coins from wallet via RPC.

    wallet_sage.get_spendable_coins_rpc returns a dict like:
      {"success": True, "records": [...], "confirmed_records": [...]}
    Each record has: {"coin": {"parent_coin_info":..., "puzzle_hash":..., "amount":...}, "coin_id":...}

    We flatten these into simple dicts: {"coin_id": "...", "amount": 12345}
    """
    from wallet_sage import get_spendable_coins_rpc

    result = get_spendable_coins_rpc(wallet_id)
    if result is None:
        return []
    if not isinstance(result, dict):
        return []
    if not result.get("success"):
        return []

    records = result.get("confirmed_records") or result.get("records") or []
    coins = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        coin_data = rec.get("coin", {})
        amount = coin_data.get("amount", 0)
        coin_id = rec.get("coin_id", "") or rec.get("name", "")

        if not coin_id:
            coin_id = coin_data.get("coin_id", "") or coin_data.get("name", "")

        if amount > 0:
            coins.append(
                {
                    "coin_id": coin_id,
                    "amount": amount,
                    "parent": coin_data.get("parent_coin_info", ""),
                    "puzzle_hash": coin_data.get("puzzle_hash", ""),
                }
            )

    return coins


def find_coins_by_amount(coins, target_mojos):
    """Find all coins matching an exact mojo amount."""
    return [c for c in coins if c.get("amount", 0) == target_mojos]


def poll_for_pools(runner, wallet_id, pool_specs, label, timeout_s=180):
    """Poll until ALL expected pool coins exist AND are spendable.

    Args:
        wallet_id: Wallet to query
        pool_specs: list of (name, amount_mojos) tuples, e.g. [("Pool A", 20000000000), ("Pool B", 30000000000)]
        label: Human-readable label for logging
        timeout_s: Max wait time

    Returns:
        dict of {name: coin_id} if all found and spendable, else None
    """
    from wallet_sage import are_coins_spendable

    runner.log(
        f"\n   🔍 Polling for {label} pool coins to appear and become spendable (up to {timeout_s}s)..."
    )
    result_ids = {}

    for poll in range(timeout_s // 5):
        coins = get_coins(wallet_id, f"poll-{label}")
        all_found = True
        candidate_ids = []

        for pool_name, pool_amount in pool_specs:
            matching = find_coins_by_amount(coins, pool_amount)
            if not matching:
                all_found = False
                break
            candidate_ids.append(
                (pool_name, matching[0].get("coin_id", ""), pool_amount)
            )

        if all_found and candidate_ids:
            # Check all are spendable
            clean_ids = [cid.replace("0x", "") for _, cid, _ in candidate_ids if cid]
            try:
                all_spendable = all(
                    are_coins_spendable([cid]) for cid in clean_ids if cid
                )
            except Exception:
                all_spendable = False

            if all_spendable:
                for pool_name, coin_id, _ in candidate_ids:
                    result_ids[pool_name] = coin_id
                runner.log(
                    f"      ✅ All {label} pool coins spendable after {poll * 5}s"
                )
                return result_ids
            elif poll > 0 and poll % 4 == 0:
                runner.log(
                    f"      ⏳ {poll * 5}s — {label} coins visible but not yet spendable"
                )
        elif poll > 0 and poll % 4 == 0:
            found_count = sum(
                1 for pn, pa in pool_specs if find_coins_by_amount(coins, pa)
            )
            runner.log(
                f"      ⏳ {poll * 5}s — {label} pool coins: {found_count}/{len(pool_specs)} visible"
            )
        time.sleep(5)

    runner.log(f"      ❌ {label} pool coins not all spendable after {timeout_s}s!")
    return None


def poll_for_split(
    runner, wallet_id, pool_coin_id, num_pieces, piece_size, label, timeout_s=180
):
    """Poll until a pool coin is consumed and split coins appear + are spendable.

    Returns:
        list of split coin_ids if confirmed, else None
    """
    from wallet_sage import are_coins_spendable

    runner.log(
        f"      🔍 Polling: {label} — pool gone + {num_pieces} × {piece_size:,} mojos spendable..."
    )

    for poll in range(timeout_s // 5):
        coins = get_coins(wallet_id, f"split-{label}")

        # Check pool coin consumed
        pool_still_exists = any(c.get("coin_id", "") == pool_coin_id for c in coins)

        # Check split coins appeared
        split_coins = find_coins_by_amount(coins, piece_size)

        if not pool_still_exists and len(split_coins) >= num_pieces:
            # Verify spendable
            split_ids = [
                c.get("coin_id", "").replace("0x", "") for c in split_coins[:num_pieces]
            ]
            try:
                all_spendable = all(
                    are_coins_spendable([sid]) for sid in split_ids if sid
                )
            except Exception:
                all_spendable = False

            if all_spendable:
                runner.log(f"      ✅ {label} complete after {poll * 5}s")
                return [c.get("coin_id", "") for c in split_coins[:num_pieces]]
            elif poll > 0 and poll % 4 == 0:
                runner.log(
                    f"      ⏳ {poll * 5}s — {label} split coins visible but not yet spendable"
                )
        elif poll > 0 and poll % 4 == 0:
            runner.log(
                f"      ⏳ {poll * 5}s — {label} pool exists: {pool_still_exists}, "
                f"split coins: {len(split_coins)}/{num_pieces}"
            )
        time.sleep(5)

    runner.log(f"      ❌ {label} not confirmed after {timeout_s}s!")
    return None


# ─── PHASE 1: PREREQUISITES ───────────────────────────────────
def phase1_prerequisites(runner):
    """Test 1-3: Sage connectivity, XCH + CAT coin queries, balance checks."""
    runner.log(f"\n{'=' * 60}")
    runner.log("PHASE 1: Prerequisites")
    runner.log(f"{'=' * 60}")

    # Test 1: Sage wallet connectivity
    try:
        from wallet_sage import get_wallet_sync_status

        sync = get_wallet_sync_status()
        sync_state = (sync or {}).get("sync_state", "unknown")
        if sync and sync.get("synced"):
            runner.pass_test(
                1,
                "Sage wallet connected and synced",
                f"height={sync.get('height', '?')}",
            )
        elif sync and sync_state == "unknown" and sync.get("reachable"):
            runner.pass_test(
                1, "Sage wallet connected (sync state unknown)", f"status={sync}"
            )
        elif sync:
            runner.fail_test(1, "Sage wallet NOT synced", f"status={sync}")
            return False
        else:
            runner.fail_test(1, "Sage wallet not responding")
            return False
    except Exception as e:
        runner.fail_test(1, "Sage wallet connectivity", str(e))
        return False

    # Test 2: XCH coins + balance
    try:
        xch_coins = get_coins(XCH_WALLET_ID, "xch-check")
        xch_total = sum(c.get("amount", 0) for c in xch_coins)
        xch_display = Decimal(xch_total) / Decimal("1000000000000")
        if xch_total >= MIN_XCH_BALANCE_MOJOS:
            runner.pass_test(
                2,
                "XCH balance sufficient",
                f"{len(xch_coins)} coins, {xch_display:.4f} XCH",
            )
        else:
            runner.fail_test(
                2, "XCH balance too low", f"{xch_display:.4f} XCH (need 0.1+)"
            )
            return False
    except Exception as e:
        runner.fail_test(2, "XCH coin query", str(e))
        return False

    # Test 3: CAT coins + balance
    try:
        cat_coins = get_coins(CAT_WALLET_ID, "cat-check")
        cat_total = sum(c.get("amount", 0) for c in cat_coins)
        if cat_total >= MIN_CAT_BALANCE_MOJOS:
            runner.pass_test(
                3,
                "CAT balance sufficient",
                f"{len(cat_coins)} coins, {cat_total:,} CAT mojos (wallet {CAT_WALLET_ID})",
            )
        else:
            runner.fail_test(
                3,
                "CAT balance too low",
                f"{cat_total} CAT mojos (need {MIN_CAT_BALANCE_MOJOS}+), wallet {CAT_WALLET_ID}",
            )
            return False
    except Exception as e:
        runner.fail_test(3, "CAT coin query", str(e))
        return False

    return True


# ─── PHASE 2: MULTI-SEND (XCH + CAT) ────────────────────────
def phase2_multi_send(runner):
    """Test 4-7: Multi-send creates pool coins for BOTH XCH and CAT."""
    runner.log(f"\n{'=' * 60}")
    runner.log("PHASE 2: Multi-Send (XCH + CAT pool coins in two transactions)")
    runner.log(f"{'=' * 60}")

    if DRY_RUN:
        for t in range(4, 8):
            runner.skip_test(t, f"(dry run) multi_send test #{t}")
        return True

    from wallet_sage import (
        get_next_address,
        send_transaction_multi,
        send_cat_multi,
        are_coins_spendable,
    )

    # Get receive address (same for both XCH and CAT)
    addr_result = get_next_address(XCH_WALLET_ID, new_address=False)
    if not addr_result or not addr_result.get("address"):
        runner.fail_test(4, "Could not get receive address")
        return False
    address = addr_result["address"]
    runner.log(f"   Receive address: {address[:20]}...")

    # Snapshot before
    xch_before = get_coins(XCH_WALLET_ID, "xch-before")
    cat_before = get_coins(CAT_WALLET_ID, "cat-before")
    runner.log(f"   XCH before: {len(xch_before)} coins")
    runner.log(f"   CAT before: {len(cat_before)} coins")

    # ── Test 4: XCH multi_send ──
    runner.log(
        f"\n   📤 XCH multi_send: {XCH_POOL_A_MOJOS:,} + {XCH_POOL_B_MOJOS:,} = {XCH_TOTAL_MOJOS:,} mojos"
    )
    try:
        xch_payments = [
            {"address": address, "amount": XCH_POOL_A_MOJOS},
            {"address": address, "amount": XCH_POOL_B_MOJOS},
        ]
        result = send_transaction_multi(xch_payments, fee_mojos=0)
        if result is None or (isinstance(result, dict) and result.get("error")):
            err = (
                result.get("error", "None returned")
                if isinstance(result, dict)
                else "None returned"
            )
            runner.fail_test(4, "XCH multi_send failed", err[:200])
            return False
        runner.pass_test(4, "XCH multi_send submitted")
    except Exception as e:
        runner.fail_test(4, "XCH multi_send exception", str(e))
        return False

    # ── Test 5: CAT multi_send ──
    runner.log(
        f"   📤 CAT multi_send: {CAT_POOL_A_MOJOS:,} + {CAT_POOL_B_MOJOS:,} = {CAT_TOTAL_MOJOS:,} CAT mojos"
    )
    try:
        cat_payments = [
            {"address": address, "amount": CAT_POOL_A_MOJOS},
            {"address": address, "amount": CAT_POOL_B_MOJOS},
        ]
        result = send_cat_multi(cat_payments, fee_mojos=0)
        if result is None or (isinstance(result, dict) and result.get("error")):
            err = (
                result.get("error", "None returned")
                if isinstance(result, dict)
                else "None returned"
            )
            runner.fail_test(5, "CAT multi_send failed", err[:200])
            return False
        runner.pass_test(5, "CAT multi_send submitted")
    except Exception as e:
        runner.fail_test(5, "CAT multi_send exception", str(e))
        return False

    # ── Test 6+7: Poll for BOTH XCH and CAT pool coins simultaneously ──
    runner.log(
        "\n   🔍 Polling for XCH + CAT pool coins simultaneously (up to 180s)..."
    )
    xch_confirmed = False
    cat_confirmed = False
    timeout_s = 180

    for poll in range(timeout_s // 5):
        # ── Check XCH if not yet confirmed ──
        if not xch_confirmed:
            xch_coins = get_coins(XCH_WALLET_ID, "poll-xch-pools")
            xch_a_matches = find_coins_by_amount(xch_coins, XCH_POOL_A_MOJOS)
            xch_b_matches = find_coins_by_amount(xch_coins, XCH_POOL_B_MOJOS)

            if xch_a_matches and xch_b_matches:
                a_id = xch_a_matches[0].get("coin_id", "").replace("0x", "")
                b_id = xch_b_matches[0].get("coin_id", "").replace("0x", "")
                try:
                    a_ok = are_coins_spendable([a_id]) if a_id else False
                    b_ok = are_coins_spendable([b_id]) if b_id else False
                except Exception:
                    a_ok = b_ok = False

                if a_ok and b_ok:
                    runner.xch_pool_a_id = xch_a_matches[0].get("coin_id", "")
                    runner.xch_pool_b_id = xch_b_matches[0].get("coin_id", "")
                    xch_confirmed = True
                    runner.log(f"      ✅ XCH pools spendable after {poll * 5}s")
                    runner.pass_test(
                        6,
                        "XCH pool coins spendable",
                        f"A ({XCH_POOL_A_MOJOS:,}): {runner.xch_pool_a_id[:16]}..., "
                        f"B ({XCH_POOL_B_MOJOS:,}): {runner.xch_pool_b_id[:16]}...",
                    )

        # ── Check CAT if not yet confirmed ──
        if not cat_confirmed:
            cat_coins = get_coins(CAT_WALLET_ID, "poll-cat-pools")
            cat_a_matches = find_coins_by_amount(cat_coins, CAT_POOL_A_MOJOS)
            cat_b_matches = find_coins_by_amount(cat_coins, CAT_POOL_B_MOJOS)

            if cat_a_matches and cat_b_matches:
                a_id = cat_a_matches[0].get("coin_id", "").replace("0x", "")
                b_id = cat_b_matches[0].get("coin_id", "").replace("0x", "")
                try:
                    a_ok = are_coins_spendable([a_id]) if a_id else False
                    b_ok = are_coins_spendable([b_id]) if b_id else False
                except Exception:
                    a_ok = b_ok = False

                if a_ok and b_ok:
                    runner.cat_pool_a_id = cat_a_matches[0].get("coin_id", "")
                    runner.cat_pool_b_id = cat_b_matches[0].get("coin_id", "")
                    cat_confirmed = True
                    runner.log(f"      ✅ CAT pools spendable after {poll * 5}s")
                    runner.pass_test(
                        7,
                        "CAT pool coins spendable",
                        f"A ({CAT_POOL_A_MOJOS:,}): {runner.cat_pool_a_id[:16]}..., "
                        f"B ({CAT_POOL_B_MOJOS:,}): {runner.cat_pool_b_id[:16]}...",
                    )

        # Both done?
        if xch_confirmed and cat_confirmed:
            break

        # Progress update
        if poll > 0 and poll % 4 == 0:
            status_xch = "✅" if xch_confirmed else "⏳"
            status_cat = "✅" if cat_confirmed else "⏳"
            runner.log(f"      ⏳ {poll * 5}s — XCH: {status_xch}, CAT: {status_cat}")

        time.sleep(5)

    if not xch_confirmed:
        runner.fail_test(6, "XCH pool coins not spendable after 180s")
        return False
    if not cat_confirmed:
        runner.fail_test(7, "CAT pool coins not spendable after 180s")
        return False

    return True


# ─── PHASE 3: SPLIT ALL POOLS (PARALLEL) ─────────────────────
def phase3_split_all(runner):
    """Test 8-11: Submit ALL 4 splits at once, then poll for all to confirm."""
    runner.log(f"\n{'=' * 60}")
    runner.log("PHASE 3: Split All Pool Coins — PARALLEL (2 XCH + 2 CAT)")
    runner.log(f"{'=' * 60}")

    if DRY_RUN:
        for t in range(8, 12):
            runner.skip_test(t, f"(dry run) split test #{t}")
        return True

    from wallet_sage import split_coins_rpc, are_coins_spendable

    # ── Split definitions ──
    splits = [
        (
            8,
            XCH_WALLET_ID,
            runner.xch_pool_a_id,
            XCH_POOL_A_SPLIT,
            XCH_POOL_A_MOJOS,
            "XCH Pool A",
        ),
        (
            9,
            XCH_WALLET_ID,
            runner.xch_pool_b_id,
            XCH_POOL_B_SPLIT,
            XCH_POOL_B_MOJOS,
            "XCH Pool B",
        ),
        (
            10,
            CAT_WALLET_ID,
            runner.cat_pool_a_id,
            CAT_POOL_A_SPLIT,
            CAT_POOL_A_MOJOS,
            "CAT Pool A",
        ),
        (
            11,
            CAT_WALLET_ID,
            runner.cat_pool_b_id,
            CAT_POOL_B_SPLIT,
            CAT_POOL_B_MOJOS,
            "CAT Pool B",
        ),
    ]

    # ── Step 1: Submit ALL splits first ──
    runner.log("\n   📤 Submitting all 4 splits...")
    pending_splits = []  # (test_num, wallet_id, pool_coin_id, num_pieces, piece_size, label)

    for test_num, wallet_id, pool_coin_id, num_pieces, pool_mojos, label in splits:
        if not pool_coin_id:
            runner.skip_test(test_num, f"{label} — no pool coin from phase 2")
            continue

        piece_size = pool_mojos // num_pieces
        coin_id_clean = pool_coin_id.replace("0x", "")
        runner.log(
            f"   ✂️ {label}: {pool_coin_id[:16]}... → {num_pieces} × {piece_size:,} mojos"
        )

        try:
            result = split_coins_rpc(
                wallet_id=wallet_id,
                target_coin_id=coin_id_clean,
                num_coins=num_pieces,
                amount_per_coin=0,
                fee_mojos=0,
            )
            if result is None or (isinstance(result, dict) and result.get("error")):
                err = (
                    result.get("error", "None returned")
                    if isinstance(result, dict)
                    else "None returned"
                )
                runner.fail_test(test_num, f"{label} split submit failed", err[:200])
                continue
            pending_splits.append(
                (test_num, wallet_id, pool_coin_id, num_pieces, piece_size, label)
            )
        except Exception as e:
            runner.fail_test(test_num, f"{label} split submit exception", str(e))
            continue

    if not pending_splits:
        runner.log("   ❌ No splits were submitted successfully")
        return False

    runner.log(
        f"\n   ✅ {len(pending_splits)} splits submitted — now polling for ALL to confirm..."
    )

    # ── Step 2: Poll for ALL splits in one loop ──
    confirmed = set()  # test_nums that are confirmed
    timeout_s = 180

    for poll in range(timeout_s // 5):
        # Check each pending split that hasn't confirmed yet
        for (
            test_num,
            wallet_id,
            pool_coin_id,
            num_pieces,
            piece_size,
            label,
        ) in pending_splits:
            if test_num in confirmed:
                continue

            coins = get_coins(wallet_id, f"split-{label}")

            # Pool coin gone?
            pool_still_exists = any(c.get("coin_id", "") == pool_coin_id for c in coins)

            # Split coins appeared?
            split_coins = find_coins_by_amount(coins, piece_size)

            if not pool_still_exists and len(split_coins) >= num_pieces:
                # Verify spendable
                split_ids = [
                    c.get("coin_id", "").replace("0x", "")
                    for c in split_coins[:num_pieces]
                ]
                try:
                    all_spendable = all(
                        are_coins_spendable([sid]) for sid in split_ids if sid
                    )
                except Exception:
                    all_spendable = False

                if all_spendable:
                    runner.log(f"      ✅ {label} confirmed after {poll * 5}s")
                    runner.pass_test(
                        test_num,
                        f"{label} split confirmed",
                        f"{num_pieces} × {piece_size:,} mojos, all spendable",
                    )
                    confirmed.add(test_num)

        # All done?
        if len(confirmed) == len(pending_splits):
            runner.log(f"\n   🎉 All {len(confirmed)} splits confirmed!")
            break

        # Progress update
        if poll > 0 and poll % 4 == 0:
            remaining = [
                label for tn, _, _, _, _, label in pending_splits if tn not in confirmed
            ]
            runner.log(
                f"      ⏳ {poll * 5}s — {len(confirmed)}/{len(pending_splits)} confirmed, "
                f"waiting on: {', '.join(remaining)}"
            )

        time.sleep(5)

    # Mark any unconfirmed as failed
    for (
        test_num,
        wallet_id,
        pool_coin_id,
        num_pieces,
        piece_size,
        label,
    ) in pending_splits:
        if test_num not in confirmed:
            runner.fail_test(
                test_num, f"{label} split not confirmed after {timeout_s}s"
            )

    return True


# ─── PHASE 4: FINAL VERIFICATION ─────────────────────────────
def phase4_verify(runner):
    """Test 12-13: Verify all expected split coins are present."""
    runner.log(f"\n{'=' * 60}")
    runner.log("PHASE 4: Final Verification")
    runner.log(f"{'=' * 60}")

    if DRY_RUN:
        for t in range(12, 14):
            runner.skip_test(t, f"(dry run) verify test #{t}")
        return True

    from wallet_sage import are_coins_spendable

    # Test 12: XCH split coins
    xch_piece_size = (
        XCH_POOL_A_MOJOS // XCH_POOL_A_SPLIT
    )  # Should be same for both pools
    expected_xch_pieces = XCH_POOL_A_SPLIT + XCH_POOL_B_SPLIT
    xch_coins = get_coins(XCH_WALLET_ID, "final-xch")
    xch_pieces = find_coins_by_amount(xch_coins, xch_piece_size)

    if len(xch_pieces) >= expected_xch_pieces:
        # Verify all spendable
        piece_ids = [
            c.get("coin_id", "").replace("0x", "")
            for c in xch_pieces[:expected_xch_pieces]
        ]
        try:
            all_ok = all(are_coins_spendable([pid]) for pid in piece_ids if pid)
        except Exception:
            all_ok = False

        if all_ok:
            runner.pass_test(
                12,
                f"XCH: {len(xch_pieces)} × {xch_piece_size:,} mojos — all spendable",
                f"(expected {expected_xch_pieces})",
            )
        else:
            runner.fail_test(
                12, f"XCH: {len(xch_pieces)} pieces found but not all spendable"
            )
    else:
        runner.fail_test(
            12, f"XCH: expected {expected_xch_pieces} pieces, found {len(xch_pieces)}"
        )

    # Test 13: CAT split coins
    cat_piece_size = CAT_POOL_A_MOJOS // CAT_POOL_A_SPLIT
    expected_cat_pieces = CAT_POOL_A_SPLIT + CAT_POOL_B_SPLIT
    cat_coins = get_coins(CAT_WALLET_ID, "final-cat")
    cat_pieces = find_coins_by_amount(cat_coins, cat_piece_size)

    if len(cat_pieces) >= expected_cat_pieces:
        piece_ids = [
            c.get("coin_id", "").replace("0x", "")
            for c in cat_pieces[:expected_cat_pieces]
        ]
        try:
            all_ok = all(are_coins_spendable([pid]) for pid in piece_ids if pid)
        except Exception:
            all_ok = False

        if all_ok:
            runner.pass_test(
                13,
                f"CAT: {len(cat_pieces)} × {cat_piece_size:,} mojos — all spendable",
                f"(expected {expected_cat_pieces})",
            )
        else:
            runner.fail_test(
                13, f"CAT: {len(cat_pieces)} pieces found but not all spendable"
            )
    else:
        runner.fail_test(
            13, f"CAT: expected {expected_cat_pieces} pieces, found {len(cat_pieces)}"
        )

    return True


# ─── PHASE 5: CLEANUP ─────────────────────────────────────────
def phase5_cleanup(runner):
    """Test 14: Reconsolidate test coins."""
    runner.log(f"\n{'=' * 60}")
    runner.log("PHASE 5: Cleanup")
    runner.log(f"{'=' * 60}")

    if DRY_RUN or SKIP_CLEANUP:
        runner.skip_test(14, "Cleanup", "dry-run or --skip-cleanup flag")
        return True

    runner.log(
        "   ℹ️ Test coins left in wallet (XCH: 5 × 0.01 pieces, CAT: 5 × 10 mojos)"
    )
    runner.log("   ℹ️ Use --skip-cleanup to inspect, or run coin prep to consolidate")
    runner.pass_test(14, "Cleanup noted — small test coins left for inspection")
    return True


# ─── MAIN ──────────────────────────────────────────────────────
def main():
    runner = TestRunner()

    runner.log(f"{'=' * 60}")
    runner.log("🧪 COIN PREP V2 TEST — XCH + CAT Multi-Send Approach")
    runner.log(f"{'=' * 60}")
    runner.log(f"   XCH Pool A: {XCH_POOL_A_MOJOS:,} mojos → {XCH_POOL_A_SPLIT} pieces")
    runner.log(f"   XCH Pool B: {XCH_POOL_B_MOJOS:,} mojos → {XCH_POOL_B_SPLIT} pieces")
    runner.log(f"   CAT Pool A: {CAT_POOL_A_MOJOS:,} mojos → {CAT_POOL_A_SPLIT} pieces")
    runner.log(f"   CAT Pool B: {CAT_POOL_B_MOJOS:,} mojos → {CAT_POOL_B_SPLIT} pieces")
    runner.log(f"   CAT wallet ID: {CAT_WALLET_ID}")
    if DRY_RUN:
        runner.log("   MODE: DRY RUN (no transactions)")
    runner.log(f"{'=' * 60}")

    try:
        if not phase1_prerequisites(runner):
            runner.log("\n⛔ Phase 1 failed — cannot continue")
            runner.summary()
            return 1

        if not phase2_multi_send(runner):
            runner.log("\n⛔ Phase 2 failed — cannot split without pool coins")
            runner.summary()
            return 1

        phase3_split_all(runner)
        phase4_verify(runner)
        phase5_cleanup(runner)

    except KeyboardInterrupt:
        runner.log("\n⛔ Interrupted by user!")
    except Exception as e:
        runner.log(f"\n⛔ Unexpected error: {e}")
        traceback.print_exc()

    all_passed = runner.summary()
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
