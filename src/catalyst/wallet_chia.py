"""Official Chia wallet RPC client for offers, coins, and transfers

Talks to a locally running Chia wallet daemon (default ``https://localhost:9256``)
and full node (default ``https://localhost:8555``) over mutual TLS using the
node's self-signed certificates. Exposes the canonical wallet API consumed by
the trading loop via ``wallet.py`` and is also imported directly by
``api_server``, ``doctor``, and the test suite for Chia-specific workflows.

Key responsibilities:
    - Offer lifecycle: list, create, inspect, and cancel offers (single + batch)
    - Coin queries: XCH and CAT UTXOs, balances, and split/combine operations
    - Full-node reads for mempool and coin-record verification
    - Transaction fee estimation and sending XCH / CATs to addresses

SSL verification is intentionally disabled for localhost self-signed certs and
``InsecureRequestWarning`` is silenced at import time. Batch cancel operations
are serialised with adaptive backoff to stay within wallet RPC limits.
"""

import os
import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import List, Dict, Optional
from decimal import Decimal, ROUND_DOWN
from tx_fees import get_effective_transaction_fee_mojos

# Silence warnings for localhost self-signed cert
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

WALLET_URL = os.getenv("CHIA_WALLET_RPC_URL", "https://localhost:9256").rstrip("/")
CERT_PATH = os.getenv("CHIA_WALLET_CERT")
KEY_PATH = os.getenv("CHIA_WALLET_KEY")

# Derive full node RPC certs from wallet cert path
# wallet: .../ssl/wallet/private_wallet.crt -> full_node: .../ssl/full_node/private_full_node.crt
FULL_NODE_URL = os.getenv("CHIA_FULL_NODE_RPC_URL", "https://localhost:8555").rstrip("/")
if CERT_PATH:
    _ssl_base = os.path.dirname(os.path.dirname(CERT_PATH))  # .../ssl/
    FULL_NODE_CERT = os.path.join(_ssl_base, "full_node", "private_full_node.crt")
    FULL_NODE_KEY = os.path.join(_ssl_base, "full_node", "private_full_node.key")
else:
    FULL_NODE_CERT = None
    FULL_NODE_KEY = None

WALLET_ID_XCH = int(os.getenv("CHIA_WALLET_ID_XCH", "1"))
# CAT wallet ID is now dynamic — passed by api_server.py at runtime

HEADERS = {"Content-Type": "application/json"}

WALLET_DEBUG = os.getenv("WALLET_DEBUG", "false").lower() == "true"

# TLS: Chia uses self-signed certs for localhost RPC that don't include
# 'localhost' in their Subject Alternative Names, so verify=False is required.
# This is standard practice for all Chia RPC clients.  # nosec B501
_TLS_VERIFY = False  # nosec B501

# Optimized: Retry strategy, sized for single-host localhost connection
# IMPORTANT: connect=0 means NO retries on connection refused — when Chia is down,
# retrying immediately just wastes 10+ seconds per call. The bot loop handles recovery.
session = requests.Session()
retries = Retry(
    total=2,
    connect=0,              # Don't retry connection refused (Chia down = down)
    backoff_factor=0.5,
    status_forcelist=[500, 502, 503, 504]
)
session.mount("https://", HTTPAdapter(max_retries=retries, pool_connections=1, pool_maxsize=5))

# Quiet mode: suppress RPC error logging (used during Chia restart)
_quiet_mode = False

def set_quiet_mode(quiet: bool):
    """Enable/disable RPC error suppression (e.g. during Chia restart)."""
    global _quiet_mode
    _quiet_mode = quiet


def rpc(endpoint: str, payload: dict, timeout: int = 10):
    """Make RPC call to Chia wallet"""
    url = f"{WALLET_URL}/{endpoint}"
    start = time.time()
    
    try:
        # Tuple timeout: (connect_timeout, read_timeout)
        # Connect should be fast for localhost — 3s is generous.
        # Read timeout uses the caller's value (default 10s).
        resp = session.post(
            url,
            json=payload,
            cert=(CERT_PATH, KEY_PATH),
            headers=HEADERS,
            verify=_TLS_VERIFY,
            timeout=(3, timeout),
        )
        resp.raise_for_status()
        
        if WALLET_DEBUG:
            elapsed = time.time() - start
            if elapsed > 1.0:
                print(f"⏱️  {endpoint} took {elapsed:.2f}s")
        
        return resp.json()
    except Exception as e:
        elapsed = time.time() - start
        if not _quiet_mode:
            print(f"❌ Wallet RPC error calling {endpoint} (after {elapsed:.2f}s): {e}")
        return None


def get_transaction(transaction_id: str, timeout: int = 10) -> Optional[Dict]:
    """Get transaction status by ID.

    Chia wallet RPC: get_transaction
    Returns the transaction record including confirmation status.

    The response includes:
    - confirmed: bool (True when on-chain)
    - confirmed_at_height: int (block height, 0 if unconfirmed)
    - additions: list of new coins created by this transaction
    - removals: list of coins spent by this transaction

    Args:
        transaction_id: The transaction ID (hex string, with or without 0x prefix)
        timeout: RPC timeout in seconds

    Returns:
        Transaction record dict, or None on error
    """
    # Ensure 0x prefix
    if transaction_id and not transaction_id.startswith("0x"):
        transaction_id = "0x" + transaction_id

    result = rpc("get_transaction", {
        "transaction_id": transaction_id,
    }, timeout=timeout)

    if result and result.get("success"):
        return result.get("transaction") or result.get("transaction_record") or result
    return result


def full_node_rpc(endpoint: str, payload: dict, timeout: int = 5):
    """Make RPC call to Chia full node (port 8555)"""
    if not FULL_NODE_CERT or not os.path.exists(FULL_NODE_CERT):
        return None
    url = f"{FULL_NODE_URL}/{endpoint}"
    try:
        resp = session.post(
            url, json=payload,
            cert=(FULL_NODE_CERT, FULL_NODE_KEY),
            headers=HEADERS, verify=_TLS_VERIFY, timeout=(3, timeout),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


# ==================== CHIA HEALTH MONITORING ====================

def get_wallet_sync_status() -> dict:
    """Check wallet sync status via RPC.
    Returns: {"reachable": bool, "synced": bool, "syncing": bool}
    """
    try:
        result = rpc("get_sync_status", {}, timeout=5)
        if result and result.get("success"):
            return {
                "reachable": True,
                "synced": result.get("synced", False),
                "syncing": result.get("syncing", False),
            }
        # RPC responded but not successful
        return {"reachable": True, "synced": False, "syncing": False}
    except Exception:
        return {"reachable": False, "synced": False, "syncing": False}


def get_full_node_sync_status() -> dict:
    """Check full node sync status via RPC on port 8555.
    Returns: {"reachable": bool, "synced": bool, "syncing": bool, "peak_height": int}
    """
    try:
        result = full_node_rpc("get_blockchain_state", {})
        if result and result.get("success"):
            state = result.get("blockchain_state", {})
            sync = state.get("sync", {})
            return {
                "reachable": True,
                "synced": sync.get("synced", False),
                "syncing": sync.get("sync_mode", False),
                "peak_height": state.get("peak", {}).get("height", 0) if state.get("peak") else 0,
            }
        return {"reachable": True, "synced": False, "syncing": False, "peak_height": 0}
    except Exception:
        return {"reachable": False, "synced": False, "syncing": False, "peak_height": 0}


def get_chia_health() -> dict:
    """Combined health check — wallet + full node.
    Returns comprehensive health status dict.
    """
    wallet = get_wallet_sync_status()
    node = get_full_node_sync_status()
    
    # Determine overall status
    if not wallet["reachable"] and not node["reachable"]:
        status = "unreachable"
    elif not wallet["reachable"]:
        status = "wallet_down"
    elif not node["reachable"]:
        status = "node_down"
    elif wallet["synced"] and node["synced"]:
        status = "healthy"
    elif wallet["syncing"] or node["syncing"]:
        status = "syncing"
    elif not wallet["synced"]:
        status = "wallet_not_synced"
    elif not node["synced"]:
        status = "node_not_synced"
    else:
        status = "unknown"
    
    return {
        "status": status,
        "wallet": wallet,
        "node": node,
        "healthy": status == "healthy",
        "timestamp": time.time(),
    }


# ============================================================================
# CHIA NODE / DASHBOARD QUERIES
# ============================================================================

def get_blockchain_state_full() -> Optional[Dict]:
    """Get full blockchain state including height, difficulty, space, mempool.
    Used by the Chia Dashboard for node status display.
    """
    result = full_node_rpc("get_blockchain_state", {}, timeout=5)
    if result and result.get("success"):
        state = result.get("blockchain_state", {})
        peak = state.get("peak", {}) or {}
        sync = state.get("sync", {})
        return {
            "success": True,
            "peak_height": peak.get("height", 0),
            "peak_timestamp": peak.get("timestamp", 0),
            "difficulty": state.get("difficulty", 0),
            "space_bytes": state.get("space", 0),
            "mempool_size": state.get("mempool_size", 0),
            "synced": sync.get("synced", False),
            "syncing": sync.get("sync_mode", False),
            "sync_tip_height": sync.get("sync_tip_height", 0),
            "sync_progress_height": sync.get("sync_progress_height", 0),
        }
    return None


def get_peer_connections() -> Optional[List[Dict]]:
    """Get list of connected peers from the full node.
    Returns simplified peer info for dashboard display.
    """
    result = full_node_rpc("get_connections", {}, timeout=5)
    if result and result.get("success"):
        connections = result.get("connections", [])
        peers = []
        for c in connections:
            peers.append({
                "node_id": c.get("node_id", "")[:16],
                "peer_host": c.get("peer_host", ""),
                "peer_port": c.get("peer_port", 0),
                "type": c.get("type", 0),  # 1=full_node, 2=harvester, 3=farmer, 4=timelord, 5=wallet
                "bytes_read": c.get("bytes_read", 0),
                "bytes_written": c.get("bytes_written", 0),
                "peak_height": c.get("peak_height", 0),
                "creation_time": c.get("creation_time", 0),
            })
        return peers
    return None


def get_transactions_list(wallet_id: int, start: int = 0, end: int = 50,
                          sort_key: str = "CONFIRMED_AT_HEIGHT",
                          reverse: bool = True) -> Optional[Dict]:
    """Get transaction history for a wallet.
    Used by the Chia Dashboard transactions tab.

    Args:
        wallet_id: Wallet ID (1=XCH, 2+=CATs)
        start: Offset for pagination
        end: End index for pagination
        sort_key: Sort field (CONFIRMED_AT_HEIGHT or RELEVANCE)
        reverse: Newest first if True

    Returns:
        Dict with 'transactions' list and 'wallet_id'
    """
    result = rpc("get_transactions", {
        "wallet_id": wallet_id,
        "start": start,
        "end": end,
        "sort_key": sort_key,
        "reverse": reverse,
    }, timeout=15)
    if result and result.get("success"):
        return {
            "success": True,
            "transactions": result.get("transactions", []),
            "wallet_id": wallet_id,
        }
    return None


def get_transaction_count(wallet_id: int) -> int:
    """Get total number of transactions for a wallet."""
    result = rpc("get_transaction_count", {
        "wallet_id": wallet_id,
    }, timeout=10)
    if result and result.get("success"):
        return result.get("count", 0)
    return 0


def get_all_coins_for_wallet(wallet_id: int) -> Optional[List[Dict]]:
    """Get ALL coins for a wallet including locked ones.
    Returns both spendable and pending coins for the dashboard coins tab.
    """
    # Note: Do NOT pass min/max_coin_amount: 0 — Chia RPC treats 0
    # as a literal filter (max size = 0 mojos), not "no limit".
    # Omitting these params returns all coins regardless of size.
    result = rpc("get_spendable_coins", {
        "wallet_id": wallet_id,
    }, timeout=15)
    if result and result.get("success"):
        confirmed = result.get("confirmed_records", [])
        unconfirmed_additions = result.get("unconfirmed_additions", [])
        unconfirmed_removals = result.get("unconfirmed_removals", [])
        return {
            "confirmed": confirmed,
            "pending_additions": unconfirmed_additions,
            "pending_removals": unconfirmed_removals,
        }
    return None


# ============================================================================
# COIN MANAGEMENT - NEW SMART FUNCTIONS
# ============================================================================

def get_spendable_coins(wallet_id: int, min_amount_mojos: int = 0, 
                       max_amount_mojos: int = None) -> Optional[Dict]:
    """
    Query spendable coins within a size range
    
    Args:
        wallet_id: Wallet to query
        min_amount_mojos: Minimum coin size (inclusive)
        max_amount_mojos: Maximum coin size (inclusive), None = no limit
    
    Returns:
        Response with 'records' list of coin records
    """
    payload = {
        "wallet_id": wallet_id,
        "min_coin_amount": str(min_amount_mojos) if min_amount_mojos > 0 else None,
    }
    
    if max_amount_mojos:
        payload["max_coin_amount"] = str(max_amount_mojos)
    
    # Remove None values
    payload = {k: v for k, v in payload.items() if v is not None}
    
    return rpc("get_spendable_coins", payload, timeout=15)


def get_spendable_coins_rpc(wallet_id: int) -> Optional[Dict]:
    """
    Get spendable coins for a wallet using RPC
    
    Returns:
        Response with coin records, or None on failure
    """
    payload = {
        "wallet_id": wallet_id,
        "min_coin_amount": 0,
    }
    return rpc("get_spendable_coins", payload, timeout=10)


def get_spendable_coin_count(wallet_id: int) -> int:
    """Return the number of spendable coins for a wallet."""
    result = get_spendable_coins_rpc(wallet_id)
    if not result:
        return 0
    return len(result.get("confirmed_records", []))


def split_coins_rpc(wallet_id: int, target_coin_id: str, num_coins: int,
                    amount_per_coin: int, fee_mojos: int = 0,
                    is_cat: bool = False) -> Optional[Dict]:
    """
    Split a single coin into multiple coins using native RPC.

    NOTE: The response always shows sent_to=0 peers, but the transaction
    DOES broadcast in practice (~10s confirmation on clean coins).
    However, it can silently fail if the target coin is in a pending
    state (e.g. right after consolidation/pool creation). The
    coin_prep_worker uses this as primary with CLI fallback + 60s
    timeout to catch silent failures.

    Args:
        wallet_id: Wallet ID
        target_coin_id: The coin ID to split (0x...)
        num_coins: How many new coins to create (max 500)
        amount_per_coin: Size of each coin in mojos (XCH) or CAT base units
        fee_mojos: Transaction fee in mojos (default 0)
        is_cat: True if splitting a CAT wallet

    Returns:
        Transaction result from RPC
    """
    payload = {
        "wallet_id": wallet_id,
        "target_coin_id": target_coin_id,
        "number_of_coins": num_coins,
        "amount_per_coin": amount_per_coin,
        "fee": fee_mojos,
    }
    return rpc("split_coins", payload, timeout=30)


def split_coins_bulk(wallet_id: int, num_coins: int, coin_size_mojos: int,
                    fee_mojos: int = 0, reserve_multiplier: float = 2.0,
                    is_cat: bool = False, cat_decimals: int = 3) -> Optional[Dict]:
    """
    Split wallet balance using NATIVE split_coins RPC
    
    🚀 FAST: Creates all coins in ONE blockchain transaction!
    🎯 SMART: Leaves a reserve coin for future expansion (Close the Gap button)
    
    Strategy:
    1. Find largest spendable coin
    2. Calculate how much to split vs reserve
    3. Use split_coins RPC to create all coins at once
    4. Remainder stays as a large coin for future offers!
    
    Args:
        wallet_id: Wallet to split
        num_coins: How many coins to create
        coin_size_mojos: Size of each coin in mojos (for XCH) or tokens (for CAT)
        fee_mojos: Transaction fee (default 0)
        reserve_multiplier: Keep this much extra as reserve (default 2.0 = 200%)
        is_cat: True if this is a CAT wallet
        cat_decimals: Decimals for CAT (default 3)
    
    Returns:
        Transaction result with success status
    """
    print(f"💰 Smart coin splitting for wallet {wallet_id}...")
    
    # Get spendable coins
    coins_result = get_spendable_coins_rpc(wallet_id)
    
    if not coins_result or not coins_result.get("success"):
        return {"success": False, "error": "Failed to get spendable coins"}
    
    coin_records = coins_result.get("confirmed_records", [])
    
    if not coin_records:
        return {"success": False, "error": "No spendable coins found"}
    
    # Debug: Show what we got
    print(f"   🔍 DEBUG: Got {len(coin_records)} coin records from wallet")
    if coin_records:
        print("   🔍 DEBUG: First coin record structure:")
        import json
        print(json.dumps(coin_records[0], indent=4))
    
    for i, rec in enumerate(coin_records[:3]):  # Show first 3
        amt = rec["coin"]["amount"]
        spent = rec.get("spent_block_index", 0)
        print(f"      Coin {i}: amount={amt} ({amt/1e12:.4f} XCH), spent_block={spent}")
    
    # Filter to only truly spendable coins (spent_block_index == 0)
    unspent_records = [r for r in coin_records if r.get("spent_block_index", 0) == 0]
    
    if not unspent_records:
        print("   ⚠️  All coins are pending spend - waiting for confirmations...")
        return {"success": False, "error": "All coins pending spend - retry after confirmation"}
    
    print(f"   📊 Found {len(unspent_records)} unspent coins (filtered from {len(coin_records)} total)")
    
    # Find largest UNSPENT coin
    largest_coin = max(unspent_records, key=lambda c: c["coin"]["amount"])
    coin_amount = largest_coin["coin"]["amount"]
    
    # Get the coin ID — prefer wallet-provided 'name' field
    parent = largest_coin["coin"]["parent_coin_info"]
    puzzle = largest_coin["coin"]["puzzle_hash"]
    amount = largest_coin["coin"]["amount"]

    # DEBUG: Show the exact coin we're trying to split
    print("   🎯 Selected coin to split:")
    print(f"      parent: {parent}")
    print(f"      puzzle: {puzzle}")
    print(f"      amount: {amount}")

    # Strategy 1: Use wallet-provided coin name (most reliable)
    coin_id = largest_coin["coin"].get("name", "") or largest_coin.get("name", "")

    if coin_id:
        if not coin_id.startswith("0x"):
            coin_id = "0x" + coin_id
        print(f"   🔑 Using wallet-provided coin_id: {coin_id}")
    else:
        # Strategy 2: Compute using Chia's variable-length int encoding
        if not parent.startswith("0x"):
            parent = "0x" + parent
        if not puzzle.startswith("0x"):
            puzzle = "0x" + puzzle

        import hashlib
        parent_bytes = bytes.fromhex(parent.replace("0x", ""))
        puzzle_bytes = bytes.fromhex(puzzle.replace("0x", ""))
        # Chia uses variable-length signed encoding, NOT fixed 8 bytes
        byte_count = (amount.bit_length() + 8) >> 3 if amount > 0 else 1
        amount_bytes = amount.to_bytes(byte_count, 'big', signed=True) if amount > 0 else b'\x00'

        coin_id = "0x" + hashlib.sha256(parent_bytes + puzzle_bytes + amount_bytes).hexdigest()
        print(f"   🔑 Computed coin_id: {coin_id}")
    
    # For CATs, convert amounts properly
    if is_cat:
        # coin_size_mojos is actually token amount for CATs
        token_amount = coin_size_mojos
        coin_scale = 10 ** cat_decimals
        coin_amount_tokens = coin_amount / coin_scale
        
        print(f"   📊 Largest CAT coin: {coin_amount_tokens:.2f} tokens ({coin_amount} mojos)")
        print(f"   🎯 Target: {num_coins} coins × {token_amount:.2f} tokens = {num_coins * token_amount:.2f} total")
        
        # Calculate reserve
        needed = num_coins * token_amount
        reserve_amount = needed * reserve_multiplier
        
        if coin_amount_tokens < needed:
            return {
                "success": False, 
                "error": f"Insufficient balance: have {coin_amount_tokens:.2f}, need {needed:.2f}"
            }
        
        # Check if we have enough for reserve
        if coin_amount_tokens >= reserve_amount:
            print(f"   💎 Will keep {coin_amount_tokens - needed:.2f} tokens as reserve for future offers!")
        else:
            print("   ⚠️  Using all available tokens (no reserve possible)")
        
        amount_per_coin = token_amount
        
    else:
        # XCH wallet
        coin_amount_xch = coin_amount / 1e12
        total_needed_mojos = num_coins * coin_size_mojos
        total_needed_xch = total_needed_mojos / 1e12
        
        print(f"   📊 Largest XCH coin: {coin_amount_xch:.4f} XCH ({coin_amount} mojos)")
        print(f"   🎯 Target: {num_coins} coins × {coin_size_mojos / 1e12:.4f} XCH = {total_needed_xch:.4f} total")
        
        # Calculate reserve
        reserve_mojos = total_needed_mojos * reserve_multiplier
        
        if coin_amount < total_needed_mojos:
            return {
                "success": False,
                "error": f"Insufficient balance: have {coin_amount_xch:.4f} XCH, need {total_needed_xch:.4f} XCH"
            }
        
        # Check if we have enough for reserve
        if coin_amount >= reserve_mojos:
            reserve_xch = (coin_amount - total_needed_mojos) / 1e12
            print(f"   💎 Will keep {reserve_xch:.4f} XCH as reserve for future offers!")
        else:
            print("   ⚠️  Using all available XCH (no reserve possible)")
        
        amount_per_coin = coin_size_mojos
    
    print(f"   🎲 Splitting coin {coin_id[:18]}...")
    print("   📦 Trying split_coins RPC (fast method)...")
    
    # Call split_coins RPC
    result = split_coins_rpc(
        wallet_id=wallet_id,
        target_coin_id=coin_id,
        num_coins=num_coins,
        amount_per_coin=amount_per_coin,
        fee_mojos=fee_mojos,
        is_cat=is_cat
    )
    
    if result and result.get("success"):
        print("   ✅ Split transaction submitted!")
        print("   ⏳ Waiting for blockchain confirmation...")
        return {
            "success": True,
            "coins_created": num_coins,
            "transaction_id": result.get("transaction_id"),
        }
    else:
        error = result.get("error", "Unknown error") if result else "RPC call failed"
        print(f"   ⚠️  split_coins failed: {error}")
        print("   🔄 Falling back to sequential send_transaction method...")
        
        # FALLBACK: Use send_transaction to create coins sequentially
        # This is slower but more reliable when split_coins can't find the coin
        try:
            # Get a receive address
            addr_result = get_next_address(wallet_id=wallet_id, new_address=False)
            if not addr_result or not addr_result.get("success"):
                return {"success": False, "error": "Could not get receive address"}
            
            address = addr_result["address"]
            
            # Send multiple small transactions to ourselves
            print(f"   📤 Creating {num_coins} coins via self-payments...")
            success_count = 0
            
            # Create in small batches to avoid overwhelming the wallet
            batch_size = 5
            for i in range(0, num_coins, batch_size):
                batch_end = min(i + batch_size, num_coins)
                batch_count = batch_end - i
                
                for j in range(batch_count):
                    tx_result = send_transaction(
                        wallet_id=wallet_id,
                        amount_mojos=int(amount_per_coin) if not is_cat else int(amount_per_coin * (10 ** cat_decimals)),
                        address=address,
                        fee_mojos=get_effective_transaction_fee_mojos()
                    )
                    
                    if tx_result and tx_result.get("success"):
                        success_count += 1
                    else:
                        print(f"   ⚠️  Transaction {i+j+1} failed")
                        break
                
                # Small delay between batches
                if batch_end < num_coins:
                    import time
                    time.sleep(0.5)
                    print("   ⏳ Waiting 60s for change coin to confirm...")
                    time.sleep(60)  # Wait for blockchain confirmation
                    print(f"   ⏳ Progress: {success_count}/{num_coins} coins created...")
            
            if success_count > 0:
                print(f"   ✅ Created {success_count}/{num_coins} coins via fallback method!")
                return {
                    "success": True,
                    "coins_created": success_count,
                    "method": "sequential_fallback"
                }
            else:
                return {
                    "success": False,
                    "error": "All fallback transactions failed"
                }
                
        except Exception as e:
            return {
                "success": False,
                "error": f"Fallback method failed: {e}"
            }


def wait_for_coin_confirmations(wallet_id: int, target_coin_size_mojos: int,
                                target_count: int, tolerance: float = 0.25,
                                max_wait_seconds: int = 300,
                                poll_interval: int = 10,
                                progress_callback=None) -> bool:
    """
    Wait for coins to confirm after splitting
    
    Args:
        wallet_id: Wallet to monitor
        target_coin_size_mojos: Expected coin size
        target_count: How many coins we're waiting for
        tolerance: Size variation tolerance
        max_wait_seconds: Maximum time to wait
        poll_interval: How often to check (seconds)
        progress_callback: Optional function(confirmed_count, target_count)
    
    Returns:
        True if enough coins confirmed, False if timeout
    """
    start_time = time.time()
    
    while (time.time() - start_time) < max_wait_seconds:
        confirmed = count_suitable_coins(wallet_id, target_coin_size_mojos, tolerance=tolerance)
        
        if progress_callback:
            progress_callback(confirmed, target_count)
        
        if confirmed >= target_count:
            return True
        
        time.sleep(poll_interval)
    
    return False



def get_balances_parallel(wallet_ids: list = None):
    """Fetch multiple wallet balances in parallel"""
    if wallet_ids is None:
        wallet_ids = [WALLET_ID_XCH]
    
    results = {}
    with ThreadPoolExecutor(max_workers=len(wallet_ids)) as executor:
        future_to_id = {
            executor.submit(get_wallet_balance, wid): wid 
            for wid in wallet_ids
        }
        
        for future in as_completed(future_to_id):
            wallet_id = future_to_id[future]
            try:
                results[wallet_id] = future.result()
            except Exception as e:
                print(f"❌ Failed to get balance for wallet {wallet_id}: {e}")
                results[wallet_id] = None
    
    return results


def get_wallets():
    return rpc("get_wallets", {})


def get_wallet_balance(wallet_id: int):
    return rpc("get_wallet_balance", {"wallet_id": wallet_id})


def get_next_address(wallet_id: int = WALLET_ID_XCH, new_address: bool = True):
    """Get next address for a wallet"""
    return rpc("get_next_address", {"wallet_id": wallet_id, "new_address": new_address})


def send_transaction(wallet_id: int, amount_mojos: int, address: str, fee_mojos: int = 0):
    """Send transaction (works for XCH and CAT wallets)"""
    payload = {
        "wallet_id": int(wallet_id),
        "amount": int(amount_mojos),
        "address": str(address),
        "fee": int(fee_mojos),
    }
    return rpc("send_transaction", payload)


def send_transaction_multi(payments: list, fee_mojos: int = 0):
    """Send multiple payments in one transaction"""
    # Try with "additions" first
    payload1 = {"wallet_id": WALLET_ID_XCH, "additions": payments, "fee": int(fee_mojos)}
    res = rpc("send_transaction_multi", payload1)
    if res and res.get("success"):
        return res

    # Fallback: try with "payments"
    payload2 = {"wallet_id": WALLET_ID_XCH, "payments": payments, "fee": int(fee_mojos)}
    res = rpc("send_transaction_multi", payload2)
    if res and res.get("success"):
        return res

    return None


def create_offer(offer_dict: dict, validate_only: bool = True, max_time: int = None,
                  _reuse_puzhash: bool = False,
                  min_coin_amount: int = None, max_coin_amount: int = None,
                  coin_ids: list = None):
    """Create offer for tokens

    Args:
        offer_dict: Wallet ID -> amount mapping (negative = offering, positive = requesting)
        validate_only: If True, only validate the offer without creating it
        max_time: Optional UNIX timestamp after which the offer auto-expires on-chain.
                  Provides passive protection against stale offers being taken.
        reuse_puzhash: Reuse existing puzzle hashes instead of generating new ones.
                       Keeps the wallet leaner and faster when creating many offers.
        min_coin_amount: Minimum coin size the wallet should select (mojos).
                         Prevents the wallet from using dust coins.
        max_coin_amount: Maximum coin size the wallet should select (mojos).
                         Prevents the wallet from using large reserve coins for small offers.
    """
    payload = {
        "wallet_id": WALLET_ID_XCH,
        "offer": offer_dict,
        "validate_only": validate_only,
    }
    if max_time is not None:
        payload["max_time"] = int(max_time)
    if min_coin_amount is not None:
        payload["min_coin_amount"] = min_coin_amount
    if max_coin_amount is not None:
        payload["max_coin_amount"] = max_coin_amount
    return rpc("create_offer_for_ids", payload, timeout=15)


def cancel_offer(trade_id: str, secure: bool = True, timeout: int = 60,
                 fee_mojos: Optional[int] = None):
    payload = {"trade_id": trade_id, "secure": secure}
    if secure:
        resolved_fee = (
            max(0, int(fee_mojos))
            if fee_mojos is not None
            else get_effective_transaction_fee_mojos()
        )
        payload["fee"] = int(resolved_fee)
    return rpc("cancel_offer", payload, timeout=timeout)


def is_offer_time_expired(offer: dict) -> bool:
    """Check if an offer has expired based on its valid_times.max_time field.
    
    The Chia wallet does NOT automatically transition expired offers from
    PENDING_ACCEPT to EXPIRED status. They remain 'open' indefinitely.
    This function checks the actual expiry timestamp.
    
    Returns True if the offer's max_time has passed, False otherwise.
    """
    valid_times = offer.get("valid_times") or {}
    max_time = valid_times.get("max_time", 0)
    if max_time and max_time > 0:
        return int(time.time()) > max_time
    return False


def get_offer_expiry_info(offer: dict) -> dict:
    """Get expiry timing info for an offer.
    
    Returns dict with:
        max_time: Unix timestamp of expiry (0 if none)
        expired: True if past expiry
        seconds_remaining: Seconds until expiry (negative if expired)
    """
    valid_times = offer.get("valid_times") or {}
    max_time = valid_times.get("max_time", 0)
    now = int(time.time())
    
    if not max_time or max_time <= 0:
        return {"max_time": 0, "expired": False, "seconds_remaining": float('inf')}
    
    return {
        "max_time": max_time,
        "expired": now > max_time,
        "seconds_remaining": max_time - now,
    }


def cleanup_expired_offers(log_fn=None) -> int:
    """Cancel any offers whose max_time has passed to free locked coins.
    
    Uses secure=False (off-chain cancel) because expired offers can't be 
    taken anyway — the max_time constraint is baked into the on-chain puzzle.
    This just cleans up the wallet's local state instantly without needing
    a blockchain transaction or fee.
    
    Args:
        log_fn: Optional logging function(level, message)
        
    Returns:
        Number of offers cancelled
    """
    def _log(level, msg):
        if log_fn:
            log_fn(level, msg)
    
    offers = get_all_offers(include_completed=False, start=0, end=200)
    if not offers:
        return 0
    
    now = int(time.time())
    cancelled = 0
    expired_found = 0
    
    for offer in offers:
        if not isinstance(offer, dict):
            continue
            
        valid_times = offer.get("valid_times") or {}
        max_time = valid_times.get("max_time", 0)
        
        if max_time and max_time > 0 and now > max_time:
            expired_found += 1
            trade_id = offer.get("trade_id", "")
            expired_ago = now - max_time
            trade_id_short = str(trade_id)[:16]
            
            _log("info", f"  Cancelling expired offer {trade_id_short}... "
                         f"(expired {expired_ago}s / {expired_ago//60}m ago)")
            
            # secure=False is safe for expired offers — they can't be taken
            # on-chain anyway. This just removes from wallet's local list.
            result = cancel_offer(str(trade_id), secure=False)
            if result and result.get("success"):
                cancelled += 1
            else:
                _log("warning", f"  Failed to cancel {trade_id_short}: {result}")
            
            # Small delay between cancels to avoid overwhelming wallet RPC
            time.sleep(0.3)
    
    if expired_found > 0:
        _log("success" if cancelled > 0 else "warning",
             f"Expired offer cleanup: found {expired_found}, "
             f"cancelled {cancelled}")
    
    return cancelled


def get_all_offers(include_completed: bool = True, start: int = 0, end: int = 50):
    """Get all offers"""
    payload = {
        "include_completed": include_completed,
        "start": start,
        "end": end,
        "reverse": True,
    }
    res = rpc("get_all_offers", payload, timeout=8)
    if not res or not res.get("success"):
        return None

    # Handle different response formats
    offers_list = res.get("trades")
    if offers_list is None:
        offers_list = res.get("offers")
    if offers_list is None:
        offers_list = res.get("trade_records")
    if offers_list is None:
        maybe = res.get("data") or {}
        offers_list = maybe.get("trades") or maybe.get("offers") or []

    if offers_list is None:
        offers_list = []

    if not isinstance(offers_list, list):
        return []

    return offers_list


def get_offer_bech32(trade_id: str) -> str:
    """Get the bech32 offer string for a specific trade_id.
    
    Calls the Chia 'get_offer' RPC with file_contents=True to retrieve
    the full offer data needed for re-posting to Dexie.
    
    Returns:
        The bech32 offer string (starts with 'offer1...'), or None on failure.
    """
    res = rpc("get_offer", {
        "trade_id": trade_id,
        "file_contents": True
    }, timeout=10)
    
    if not res or not res.get("success"):
        return None
    
    # The offer bech32 is in the 'offer' field of the response
    offer_str = res.get("offer")
    if offer_str and isinstance(offer_str, str) and offer_str.startswith("offer1"):
        return offer_str
    
    # Some Chia versions nest it under trade_record
    trade_record = res.get("trade_record") or {}
    offer_str = trade_record.get("offer")
    if offer_str and isinstance(offer_str, str) and offer_str.startswith("offer1"):
        return offer_str
    
    return None


def _is_open_status(status_val, offer_record=None) -> bool:
    """Determine if an offer status represents an open/active offer.

    Chia TradeStatus integer enum:
        0 = PENDING_ACCEPT  (open — offer created, waiting for taker)
        1 = PENDING_CONFIRM (open — taker accepted, in mempool)
        2 = PENDING_CANCEL  (transitioning — cancel submitted, not confirmed)
        3 = CANCELLED       (closed — cancel confirmed)
        4 = CONFIRMED       (closed — trade completed on chain)
        5 = FAILED          (closed — something went wrong)

    Args:
        status_val: The status field from the offer record (int or string)
        offer_record: Optional full offer dict — if provided, also checks
                      valid_times.max_time for time-based expiry
    """
    # Time-based expiry check (CRITICAL: wallet doesn't auto-expire!)
    if offer_record and is_offer_time_expired(offer_record):
        return False

    if status_val is None:
        return False
    if isinstance(status_val, int):
        # Only 0 (PENDING_ACCEPT) and 1 (PENDING_CONFIRM) are truly open
        # 2 (PENDING_CANCEL) is transitioning to closed — treat as closed
        # 3 (CANCELLED), 4 (CONFIRMED), 5 (FAILED) are definitely closed
        return status_val <= 1

    status = str(status_val).upper()
    OPEN_STATUSES = {"PENDING_ACCEPT", "PENDING_CONFIRM", "PENDING", "IN_PROGRESS", "OPEN"}
    CLOSED_STATUSES = {"PENDING_CANCEL", "CANCELLED", "CANCELED", "CONFIRMED", "FAILED", "EXPIRED", "COMPLETED", "SUCCESS"}

    if status in CLOSED_STATUSES:
        return False
    return status in OPEN_STATUSES


def classify_offers_from_list(offers_list: list, asset_id_mz: str):
    """Classify offers from a pre-fetched list"""
    open_buy = []
    open_sell = []
    closed_offers = []

    # Status distribution logging — helps diagnose classification issues
    STATUS_NAMES = {0: "PENDING_ACCEPT", 1: "PENDING_CONFIRM", 2: "PENDING_CANCEL",
                    3: "CANCELLED", 4: "CONFIRMED", 5: "FAILED"}
    status_counts = {}

    for tr in offers_list:
        if not isinstance(tr, dict):
            continue

        status_val = tr.get("status")
        summary = tr.get("summary") or {}
        offered = summary.get("offered") or {}
        requested = summary.get("requested") or {}

        # Track status distribution
        status_label = STATUS_NAMES.get(status_val, str(status_val)) if isinstance(status_val, int) else str(status_val)
        status_counts[status_label] = status_counts.get(status_label, 0) + 1

        is_open = _is_open_status(status_val, offer_record=tr)

        # Classify by type
        is_buy = "xch" in offered and asset_id_mz in requested
        is_sell = asset_id_mz in offered and "xch" in requested

        if is_open:
            if is_buy:
                open_buy.append(tr)
            elif is_sell:
                open_sell.append(tr)
        else:
            if is_buy or is_sell:
                closed_offers.append(tr)

    # Log classification summary
    total = len(offers_list)
    status_str = ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items()))
    summary = (f"{total} wallet offers → "
               f"{len(open_buy)} open buys, {len(open_sell)} open sells, "
               f"{len(closed_offers)} closed | Statuses: {status_str}")
    print(f"📊 [CLASSIFY] {summary}", flush=True)
    try:
        from database import log_event as _log_classify
        _log_classify("info", "offer_classify", summary)
    except Exception:
        pass

    return open_buy, open_sell, closed_offers


def classify_open_offers_for_pair(asset_id_mz: str):
    """LEGACY: Keep for backwards compatibility"""
    offers_list = get_all_offers(include_completed=True)
    if offers_list is None:
        print("⚠️  Could not fetch offers from wallet RPC.")
        return [], []

    open_buy, open_sell, _ = classify_offers_from_list(offers_list, asset_id_mz)
    return open_buy, open_sell


def cancel_offers_batch(trade_ids: list, secure: bool = True, max_workers: int = 3,
                        fee_mojos: Optional[int] = None,
                        skip_confirmation: bool = False):
    """Cancel multiple offers — always serialized to avoid overwhelming wallet RPC.
    
    Parallel cancellation was causing failures when the wallet was under load
    (e.g. after creating many new offers in overlap strategy). Sequential with
    brief delays is more reliable and the speed difference is negligible.
    """
    results = {}
    
    if not trade_ids:
        return results
    
    if len(trade_ids) > 10:
        print(f"📋 Cancelling {len(trade_ids)} offers sequentially (large batch)...")
    
    for i, tid in enumerate(trade_ids):
        try:
            # Use longer timeout for large batches — wallet is under load
            timeout = 120 if len(trade_ids) > 10 else 60
            result = cancel_offer(tid, secure, timeout=timeout)
            results[tid] = result or {"success": False, "error": "RPC returned None"}

            # Chia wallet doesn't special-case "offer already gone" like Sage.
            # If the error indicates the offer no longer exists (filled, expired,
            # already cancelled), treat it as success — the goal (offer is gone)
            # is achieved. Without this, offer_manager queues infinite retries.
            if result and not result.get("success"):
                err_str = str(result.get("error", "")).lower()
                if any(phrase in err_str for phrase in
                       ("not found", "no offer", "unknown trade", "already",
                        "cannot cancel", "not pending")):
                    result["success"] = True
                    result["already_gone"] = True

            if result and result.get("success"):
                if len(trade_ids) > 10 and (i + 1) % 10 == 0:
                    print(f"   ✅ Cancelled {i+1}/{len(trade_ids)}")
            else:
                error = (result or {}).get("error", "unknown")
                print(f"   ❌ Failed {tid[:16]}...: {error}")
            # Brief delay between cancels to let wallet breathe
            # Shorter delays for small batches (wallet handles these fine),
            # longer for large batches to avoid overwhelming the RPC.
            import time
            if len(trade_ids) > 10:
                time.sleep(1.0)
            elif len(trade_ids) > 5:
                time.sleep(0.5)
            else:
                time.sleep(0.3)
        except Exception as e:
            print(f"❌ Failed to cancel offer {tid}: {e}")
            results[tid] = {"success": False, "error": str(e)}
    
    return results


# ==================== COIN MANAGEMENT HELPERS ====================

def cat_to_mojos(amount: Decimal, decimals: int) -> int:
    """Convert CAT amount to mojos"""
    scale = Decimal(10) ** Decimal(decimals)
    return int((amount * scale).to_integral_value(ROUND_DOWN))


def xch_to_mojos(amount) -> int:
    """Convert XCH amount (Decimal or numeric) to mojos (1 XCH = 1e12 mojos)."""
    return int((Decimal(str(amount)) * Decimal(10) ** 12).to_integral_value(ROUND_DOWN))


def mojos_to_xch(mojos: int) -> Decimal:
    """Convert mojos to XCH Decimal."""
    return Decimal(int(mojos)) / (Decimal(10) ** 12)


def mojos_to_cat(mojos: int, decimals: int) -> Decimal:
    """Convert CAT mojos to Decimal amount."""
    scale = Decimal(10) ** Decimal(decimals)
    return Decimal(int(mojos)) / scale


def count_suitable_coins(wallet_id: int, target_size_mojos: int, 
                        is_cat: bool = False, decimals: int = 3,
                        tolerance: float = 0.1) -> int:
    """
    Count how many coins are suitable for trading
    
    A coin is suitable if it's within tolerance of target size
    (e.g., 10% tolerance means 0.18-0.22 XCH coins are suitable for 0.2 XCH target)
    """
    coins_result = get_spendable_coins_rpc(wallet_id)
    
    if not coins_result or not coins_result.get("success"):
        return 0
    
    coin_records = coins_result.get("confirmed_records", [])
    
    min_size = int(target_size_mojos * (1 - tolerance))
    max_size = int(target_size_mojos * (1 + tolerance))
    
    suitable = 0
    for record in coin_records:
        amount = record["coin"]["amount"]
        if min_size <= amount <= max_size:
            suitable += 1
    
    return suitable



def get_owned_coins(wallet_id: int) -> Optional[Dict]:
    """Stub function for Chia wallet.

    Chia wallet doesn't have a filter_mode="owned" RPC endpoint like Sage does.
    This stub returns None, which signals to coin_manager.py to use the original
    coin mark-gone logic (no Sage-hidden coin filtering).

    Args:
        wallet_id: Wallet ID (unused in stub)

    Returns:
        None (Chia wallet doesn't support this query)
    """
    return None


def get_selectable_coins_map(wallet_id: int) -> Optional[Dict]:
    """Stub function for Chia wallet.

    Chia wallet doesn't have a dedicated filter_mode="selectable" map function.
    This stub returns None.

    Args:
        wallet_id: Wallet ID (unused in stub)

    Returns:
        None (Chia wallet doesn't support this query)
    """
    return None

