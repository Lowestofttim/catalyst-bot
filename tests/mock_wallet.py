"""
Mock Wallet — Simulation Mode for Testing Without Real Blockchain

Replaces wallet.py function calls with simulated responses so the bot
can run its full loop cycle without needing a Chia node or real funds.

Usage:
    Set MOCK_WALLET=true in .env, then start the bot normally.
    The mock wallet will:
    - Simulate coin balances (XCH and CAT)
    - Create fake offers with realistic trade IDs
    - Simulate random fills (configurable fill rate)
    - Track coin counts as offers are created/cancelled
    - Log all operations for debugging

This is purely for testing the bot logic, GUI, and API layer.
No real blockchain transactions occur.
"""

import time
import random
import hashlib
import threading
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Mock state
# ---------------------------------------------------------------------------


class MockWalletState:
    """Holds all simulated wallet state."""

    def __init__(self):
        self._lock = threading.Lock()

        # Simulated balances (in mojos)
        self.xch_balance: int = 10_000_000_000_000  # 10 XCH
        self.cat_balance: int = 50_000_000  # 50,000 CAT (3 decimals)

        # Simulated coins (list of dicts like Chia returns)
        self.xch_coins: List[Dict] = []
        self.cat_coins: List[Dict] = []

        # Active offers
        self.offers: Dict[str, Dict] = {}  # trade_id -> offer record
        self._offer_counter: int = 0

        # Fill simulation
        self.fill_probability: float = 0.05  # 5% chance per check
        self.fills: List[Dict] = []

        # Initialise coins
        self._init_coins()

    def _init_coins(self):
        """Create initial set of simulated coins."""
        # Create 20 XCH coins of 0.5 XCH each
        for i in range(20):
            self.xch_coins.append(
                self._make_coin(
                    amount=500_000_000_000,  # 0.5 XCH
                    wallet_type="xch",
                    index=i,
                )
            )

        # Create 15 CAT coins of ~3333 CAT each
        for i in range(15):
            self.cat_coins.append(
                self._make_coin(
                    amount=3_333_000,  # 3333 CAT (3 decimals)
                    wallet_type="cat",
                    index=i,
                )
            )

    def _make_coin(self, amount: int, wallet_type: str, index: int) -> Dict:
        """Create a realistic-looking coin record."""
        # Generate deterministic but unique IDs
        seed = f"{wallet_type}_{index}_{amount}_{time.time()}"
        coin_id = "0x" + hashlib.sha256(seed.encode()).hexdigest()
        parent = "0x" + hashlib.sha256(f"parent_{seed}".encode()).hexdigest()
        puzzle = "0x" + hashlib.sha256(f"puzzle_{wallet_type}".encode()).hexdigest()

        return {
            "coin": {
                "parent_coin_info": parent,
                "puzzle_hash": puzzle,
                "amount": amount,
            },
            "id": coin_id,
            "spent_block_index": 0,  # 0 = unspent
            "confirmed_block_index": 1000 + index,
        }

    def _make_trade_id(self) -> str:
        """Generate a realistic trade ID."""
        self._offer_counter += 1
        seed = f"trade_{self._offer_counter}_{time.time()}"
        return "0x" + hashlib.sha256(seed.encode()).hexdigest()

    def _make_offer_bech32(self, trade_id: str) -> str:
        """Generate a fake bech32 offer string."""
        # Real offers start with "offer1" — we use a simplified fake
        return f"offer1mock{hashlib.sha256(trade_id.encode()).hexdigest()[:40]}"


state = MockWalletState()


# ---------------------------------------------------------------------------
# Mock implementations of wallet.py functions
# ---------------------------------------------------------------------------

WALLET_ID_XCH = 1


def rpc(endpoint: str, payload: dict, timeout: int = 10) -> Optional[Dict]:
    """Mock RPC — routes to simulated handlers."""
    # Small delay to simulate network latency
    time.sleep(random.uniform(0.05, 0.15))

    handlers = {
        "get_spendable_coins": _mock_get_spendable_coins,
        "split_coins": _mock_split_coins,
        "get_wallet_balance": _mock_get_wallet_balance,
        "create_offer_for_ids": _mock_create_offer,
        "cancel_offer": _mock_cancel_offer,
        "get_all_offers": _mock_get_all_offers,
        "get_offer": _mock_get_offer,
        "get_wallets": _mock_get_wallets,
        "get_sync_status": _mock_get_sync_status,
        "get_next_address": _mock_get_next_address,
        "log_in": _mock_log_in,
    }

    handler = handlers.get(endpoint)
    if handler:
        return handler(payload)

    # Unknown endpoint — return generic success
    return {"success": True}


def _mock_get_spendable_coins(payload: dict) -> Dict:
    """Return simulated spendable coins."""
    wallet_id = payload.get("wallet_id", 1)

    with state._lock:
        if wallet_id == 1:  # XCH
            coins = [c for c in state.xch_coins if c["spent_block_index"] == 0]
        else:  # CAT
            coins = [c for c in state.cat_coins if c["spent_block_index"] == 0]

    return {
        "success": True,
        "confirmed_records": coins,
        "unconfirmed_records": [],
        "unconfirmed_additions": [],
        "unconfirmed_removals": [],
    }


def _mock_split_coins(payload: dict) -> Dict:
    """Simulate coin splitting."""
    wallet_id = payload.get("wallet_id", 1)
    num_coins = payload.get("number_of_coins", 5)
    amount_per = payload.get("amount_per_coin", 100_000_000_000)

    with state._lock:
        coins = state.xch_coins if wallet_id == 1 else state.cat_coins

        # Find largest coin
        unspent = [c for c in coins if c["spent_block_index"] == 0]
        if not unspent:
            return {"success": False, "error": "No spendable coins"}

        largest = max(unspent, key=lambda c: c["coin"]["amount"])
        total_needed = amount_per * num_coins

        if largest["coin"]["amount"] < total_needed:
            return {"success": False, "error": "Coin too small to split"}

        # "Spend" the original coin
        largest["spent_block_index"] = 9999

        # Create new coins
        wtype = "xch" if wallet_id == 1 else "cat"
        for i in range(num_coins):
            new_coin = state._make_coin(amount_per, wtype, len(coins) + i)
            coins.append(new_coin)

        # Create remainder coin if there's leftover
        remainder = largest["coin"]["amount"] - total_needed
        if remainder > 0:
            rem_coin = state._make_coin(remainder, wtype, len(coins))
            coins.append(rem_coin)

    return {"success": True, "transaction": {"name": "mock_split_tx"}}


def _mock_get_wallet_balance(payload: dict) -> Dict:
    """Return simulated wallet balance."""
    wallet_id = payload.get("wallet_id", 1)

    with state._lock:
        if wallet_id == 1:
            total = sum(
                c["coin"]["amount"]
                for c in state.xch_coins
                if c["spent_block_index"] == 0
            )
            pending = 0
        else:
            total = sum(
                c["coin"]["amount"]
                for c in state.cat_coins
                if c["spent_block_index"] == 0
            )
            pending = 0

    return {
        "success": True,
        "wallet_balance": {
            "confirmed_wallet_balance": total,
            "unconfirmed_wallet_balance": total,
            "spendable_balance": total,
            "pending_change": pending,
            "max_send_amount": total,
            "unspent_coin_count": total,  # Simplified
            "pending_coin_removal_count": 0,
        },
    }


def _mock_create_offer(payload: dict) -> Dict:
    """Simulate offer creation."""
    offer_dict = payload.get("offer", {})
    validate_only = payload.get("validate_only", False)
    max_time = payload.get("max_time", 0)

    if validate_only:
        return {"success": True, "offer": None}

    with state._lock:
        trade_id = state._make_trade_id()
        bech32 = state._make_offer_bech32(trade_id)

        # Determine side from offer_dict
        side = "unknown"
        for wid, amount in offer_dict.items():
            if int(amount) < 0 and str(wid) == "1":
                side = "buy"  # Spending XCH = buying CAT
                break
            elif int(amount) < 0 and str(wid) != "1":
                side = "sell"  # Spending CAT = selling
                break

        offer_record = {
            "trade_id": trade_id,
            "status": 1,  # PENDING_ACCEPT
            "offer": bech32,
            "side": side,
            "offer_dict": offer_dict,
            "created_at_time": int(time.time()),
            "valid_times": {"max_time": max_time} if max_time else {},
            "summary": {
                "offered": [],
                "requested": [],
                "fees": 0,
            },
            "pending": {},
        }

        state.offers[trade_id] = offer_record

    return {
        "success": True,
        "offer": bech32,
        "trade_id": trade_id,
        "trade_record": offer_record,
    }


def _mock_cancel_offer(payload: dict) -> Dict:
    """Simulate offer cancellation."""
    trade_id = payload.get("trade_id", "")

    with state._lock:
        if trade_id in state.offers:
            state.offers[trade_id]["status"] = 5  # CANCELLED
            return {"success": True}

    return {"success": False, "error": "Offer not found"}


def _mock_get_all_offers(payload: dict) -> Dict:
    """Return simulated offer list."""
    include_completed = payload.get("include_completed", True)
    start = payload.get("start", 0)
    end = payload.get("end", 50)

    with state._lock:
        if include_completed:
            offers = list(state.offers.values())
        else:
            # Only active offers (status 1 = PENDING_ACCEPT)
            offers = [o for o in state.offers.values() if o["status"] == 1]

        # Slice
        offers = offers[start:end]

    return {
        "success": True,
        "trades": offers,
    }


def _mock_get_offer(payload: dict) -> Dict:
    """Return a specific offer."""
    trade_id = payload.get("trade_id", "")
    file_contents = payload.get("file_contents", False)

    with state._lock:
        offer = state.offers.get(trade_id)
        if offer:
            result = {"success": True, "trade_record": offer}
            if file_contents:
                result["offer"] = offer.get("offer", "")
            return result

    return {"success": False, "error": "Offer not found"}


def _mock_get_wallets(payload: dict) -> Dict:
    """Return simulated wallet list."""
    return {
        "success": True,
        "wallets": [
            {"id": 1, "name": "Chia Wallet", "type": 0},
            {"id": 2, "name": "Mock CAT", "type": 6, "data": "mock_asset_id_abc123"},
        ],
    }


def _mock_get_sync_status(payload: dict) -> Dict:
    """Return synced status."""
    return {
        "success": True,
        "synced": True,
        "syncing": False,
        "genesis_initialized": True,
    }


def _mock_get_next_address(payload: dict) -> Dict:
    """Return a fake address."""
    return {
        "success": True,
        "address": "xch1mock_address_for_testing_purposes_only_not_real",
    }


def _mock_log_in(payload: dict) -> Dict:
    """Simulate wallet login."""
    return {"success": True, "fingerprint": 1234567890}


# ---------------------------------------------------------------------------
# Convenience wrappers (matching wallet.py's module-level functions)
# ---------------------------------------------------------------------------


def get_spendable_coins_rpc(wallet_id: int) -> Optional[Dict]:
    """Mock version of wallet.get_spendable_coins_rpc."""
    return rpc("get_spendable_coins", {"wallet_id": wallet_id})


def split_coins_rpc(
    wallet_id: int,
    target_coin_id: str,
    num_coins: int,
    amount_per_coin: int,
    fee_mojos: int = 0,
    is_cat: bool = False,
) -> Optional[Dict]:
    """Mock version of wallet.split_coins_rpc."""
    return rpc(
        "split_coins",
        {
            "wallet_id": wallet_id,
            "target_coin_id": target_coin_id,
            "number_of_coins": num_coins,
            "amount_per_coin": amount_per_coin,
            "fee": fee_mojos,
        },
    )


def get_wallet_balance(wallet_id: int) -> Optional[Dict]:
    """Mock version of wallet.get_wallet_balance."""
    return rpc("get_wallet_balance", {"wallet_id": wallet_id})


def create_offer(
    offer_dict: dict, validate_only: bool = True, max_time: int = None
) -> Optional[Dict]:
    """Mock version of wallet.create_offer."""
    payload = {
        "wallet_id": WALLET_ID_XCH,
        "offer": offer_dict,
        "validate_only": validate_only,
    }
    if max_time is not None:
        payload["max_time"] = int(max_time)
    return rpc("create_offer_for_ids", payload)


def cancel_offer(
    trade_id: str, secure: bool = True, timeout: int = 60
) -> Optional[Dict]:
    """Mock version of wallet.cancel_offer."""
    return rpc("cancel_offer", {"trade_id": trade_id, "secure": secure})


def get_all_offers(
    include_completed: bool = True, start: int = 0, end: int = 50
) -> Optional[List]:
    """Mock version of wallet.get_all_offers."""
    res = rpc(
        "get_all_offers",
        {
            "include_completed": include_completed,
            "start": start,
            "end": end,
            "reverse": True,
        },
    )
    if not res or not res.get("success"):
        return None
    return res.get("trades", [])


def get_offer_bech32(trade_id: str) -> str:
    """Mock version — returns fake bech32 for a trade_id."""
    res = rpc("get_offer", {"trade_id": trade_id, "file_contents": True})
    if res and res.get("success"):
        return res.get("offer", "")
    return ""


def is_offer_time_expired(offer: dict) -> bool:
    """Same logic as wallet.py — check valid_times.max_time."""
    valid_times = offer.get("valid_times") or {}
    max_time = valid_times.get("max_time", 0)
    if max_time and max_time > 0:
        return int(time.time()) > max_time
    return False


def get_wallets():
    """Mock version of wallet.get_wallets."""
    return rpc("get_wallets", {})


def set_quiet_mode(quiet: bool):
    """No-op for mock wallet."""
    pass


def cancel_offers_batch(
    trade_ids: list, secure: bool = True, max_workers: int = 3
) -> List[Dict]:
    """Cancel multiple offers sequentially (like real wallet.py)."""
    results = []
    for tid in trade_ids:
        res = cancel_offer(tid, secure=secure)
        results.append({"trade_id": tid, "success": res and res.get("success", False)})
        time.sleep(0.1)  # Simulate delay
    return results


# ---------------------------------------------------------------------------
# Fill simulator (call periodically to simulate random fills)
# ---------------------------------------------------------------------------


def simulate_fills(fill_probability: float = 0.05) -> List[str]:
    """Randomly 'fill' some active offers to simulate market activity.

    Call this from the bot loop or a test harness.
    Returns list of trade_ids that were 'filled'.
    """
    filled = []

    with state._lock:
        active = [tid for tid, o in state.offers.items() if o["status"] == 1]

        for tid in active:
            if random.random() < fill_probability:
                state.offers[tid]["status"] = 4  # CONFIRMED (filled)
                filled.append(tid)

    return filled


def get_mock_stats() -> Dict:
    """Get mock wallet statistics for debugging."""
    with state._lock:
        active_offers = sum(1 for o in state.offers.values() if o["status"] == 1)
        filled_offers = sum(1 for o in state.offers.values() if o["status"] == 4)
        xch_coins = sum(1 for c in state.xch_coins if c["spent_block_index"] == 0)
        cat_coins = sum(1 for c in state.cat_coins if c["spent_block_index"] == 0)

    return {
        "mock_mode": True,
        "active_offers": active_offers,
        "filled_offers": filled_offers,
        "xch_coins": xch_coins,
        "cat_coins": cat_coins,
        "total_offers_created": state._offer_counter,
    }


def reset_mock():
    """Reset all mock state (useful for test reruns)."""
    global state
    state = MockWalletState()
