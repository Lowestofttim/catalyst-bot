"""
Wallet Adapter — Selects Sage or Chia wallet backend.

This thin adapter reads WALLET_TYPE from the environment and re-exports
all public functions from either wallet_sage.py or wallet_chia.py.

Usage in .env:
    WALLET_TYPE=sage    (default — Sage light wallet on port 9257)
    WALLET_TYPE=chia    (official Chia wallet on port 9256, legacy)

All other modules continue to do:
    from wallet import get_all_offers, create_offer, ...

Zero changes needed in any other file. The adapter handles the switch.
"""

import os
from dotenv import load_dotenv

load_dotenv()

WALLET_TYPE = os.getenv("WALLET_TYPE", "sage").strip().lower()

if WALLET_TYPE == "chia":
    print("🔄 [Wallet] Using Chia wallet backend (port 9256)")
    try:
        from database import log_event as _log_wallet
        _log_wallet("info", "wallet_backend", "Using Chia wallet backend (port 9256)")
    except Exception:
        pass
    from wallet_chia import (  # noqa: F401
        # Constants
        WALLET_ID_XCH,
        WALLET_URL,
        CERT_PATH,
        KEY_PATH,
        HEADERS,
        WALLET_DEBUG,
        # Full node (Chia only)
        FULL_NODE_URL,
        FULL_NODE_CERT,
        FULL_NODE_KEY,
        # Core RPC
        rpc,
        full_node_rpc,
        set_quiet_mode,
        session,
        # Health monitoring
        get_wallet_sync_status,
        get_full_node_sync_status,
        get_chia_health,
        # Coin management
        get_spendable_coins,
        count_suitable_coins,
        get_spendable_coins_rpc,
        split_coins_rpc,
        split_coins_bulk,
        wait_for_coin_confirmations,
        get_transaction,
        prepare_coins_for_offers_v2,
        prepare_coins_for_trading,
        # Chia-specific coin queries (stubs for compatibility)
        get_owned_coins,
        get_selectable_coins_map,
        # Balance & address
        get_wallet_balance,
        get_balances_parallel,
        get_wallets,
        get_next_address,
        send_transaction,
        send_transaction_multi,
        # Offer management
        create_offer,
        cancel_offer,
        is_offer_time_expired,
        get_offer_expiry_info,
        cleanup_expired_offers,
        get_all_offers,
        get_offer_bech32,
        classify_offers_from_list,
        classify_open_offers_for_pair,
        cancel_offers_batch,
        # Helpers
        cat_to_mojos,
        # Chia Dashboard queries
        get_blockchain_state_full,
        get_peer_connections,
        get_transactions_list,
        get_transaction_count,
        get_all_coins_for_wallet,
    )
    def get_owned_coins_detailed(wallet_id: int):
        """Chia backend compatibility stub for Sage-only detailed owned coins."""
        return None
    # Chia's spendable RPC is already the exact selectable view.
    get_exact_spendable_coins_rpc = get_spendable_coins_rpc
else:
    # Default: Sage (or unknown type falls back to Sage)
    def _safe_console(msg: str) -> None:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(msg.encode("ascii", "replace").decode("ascii"), flush=True)

    if WALLET_TYPE != "sage":
        _safe_console(f"[Wallet] Unknown WALLET_TYPE '{WALLET_TYPE}', defaulting to 'sage'")
    _safe_console("[Wallet] Using Sage light wallet backend (port 9257)")
    try:
        from database import log_event as _log_wallet
        _log_wallet("info", "wallet_backend", "Using Sage light wallet backend (port 9257)")
    except Exception:
        pass
    from wallet_sage import (  # noqa: F401
        # Constants
        WALLET_ID_XCH,
        WALLET_URL,
        CERT_PATH,
        KEY_PATH,
        HEADERS,
        WALLET_DEBUG,
        # Core RPC
        rpc,
        full_node_rpc,
        set_quiet_mode,
        # Health monitoring
        get_wallet_sync_status,
        get_full_node_sync_status,
        get_chia_health,
        # Coin management
        get_spendable_coins,
        count_suitable_coins,
        get_spendable_coins_rpc,
        split_coins_rpc,
        split_coins_bulk,
        wait_for_coin_confirmations,
        get_transaction,
        prepare_coins_for_offers_v2,
        prepare_coins_for_trading,
        # Sage-specific coin queries (owned + selectable maps)
        get_owned_coins,
        get_owned_coins_detailed,
        get_selectable_coins_map,
        get_selectable_coins_only as get_exact_spendable_coins_rpc,
        # Balance & address
        get_wallet_balance,
        get_balances_parallel,
        get_wallets,
        get_next_address,
        send_transaction,
        send_transaction_multi,
        # Offer management
        create_offer,
        cancel_offer,
        is_offer_time_expired,
        get_offer_expiry_info,
        cleanup_expired_offers,
        get_all_offers,
        get_offer_bech32,
        classify_offers_from_list,
        classify_open_offers_for_pair,
        cancel_offers_batch,
        # Helpers
        cat_to_mojos,
        # Chia Dashboard queries
        get_blockchain_state_full,
        get_peer_connections,
        get_transactions_list,
        get_transaction_count,
        get_all_coins_for_wallet,
        # Sage-specific: offer cleanup (delete from Sage's local DB)
        delete_offer as sage_delete_offer,
        delete_offers_batch as sage_delete_offers_batch,
    )


def get_wallet_type() -> str:
    """Return which wallet backend is active: 'chia' or 'sage'."""
    return WALLET_TYPE
