# vulture whitelist - names that are intentionally "unused" from Python's
# perspective but are called from JavaScript, the Flask framework, or pytest
# infrastructure.
#
# Usage: python -m vulture src/catalyst scripts desktop_app.py build.py \
#   scripts/vulture_whitelist.py --min-confidence 90

# --- src-layout bootstrap (auto-inserted) ---
import os as _os
import sys as _sys

_sys.path.insert(
    0,
    _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), "..", "src", "catalyst"
    ),
)
# --- end bootstrap ---

from vulture.utils import whitelist_item
from whitelist_utils import Whitelist

# PyWebView API methods
# All public methods of AppBridge are called via window.pywebview.api.* from
# bot_gui.html. Vulture sees them as unused Python functions.
import app_bridge  # noqa: F401

for _name in dir(app_bridge.AppBridge):
    if not _name.startswith("_"):
        whitelist_item(app_bridge.AppBridge, _name)

# Flask route functions
# Every function decorated with @app.route is called by Flask's router, not
# directly from Python code. Re-exported blueprint route functions are also
# kept on api_server for app_bridge.py and older tests.
_api_server = Whitelist()
_api_server.api_splash_incoming
_api_server.api_splash_incoming_list
_api_server.api_splash_node_output
_api_server.api_splash_setup_release
_api_server.api_config_export_env
_api_server.api_config_history
_api_server.api_health_runtime
_api_server.api_self_test
_api_server.api_superlog_archive
_api_server.api_superlog_download
_api_server.api_superlog_level
_api_server.api_superlog_stats
_api_server.api_watchdog_cancel_mismatched_offers
_api_server.api_watchdog_shape_fix_abort
_api_server.api_watchdog_shape_fix_status
_api_server.api_session_resume_chosen
_api_server.api_wallets_detect
_api_server.api_wallets_switch
_api_server.api_amm_price
_api_server.api_coinset_stats
_api_server.api_debug_coinprep
_api_server.api_debug_pricing
_api_server.api_debug_sage_single_offer_test
_api_server.api_debug_tibet_test
_api_server.api_market_dbx
_api_server.api_tibet_price
_api_server.api_full_node_status
_api_server.api_wallet_retry_sage_connect
_api_server.api_deposit_advisory_allocate
_api_server.api_dexie_v3_pairs
_api_server.api_token_overview
_api_server.api_coin_prep
_api_server.api_db_backup
_api_server.api_log_event
_api_server.api_fills_arb_wallets
_api_server.api_fills_classified
_api_server.api_market_fill_intel
_api_server.api_open_offer_count
_api_server.api_pnl_reset
_api_server.api_pnl_reset_preview
_api_server.api_reset_full
_api_server.api_reset_offer_history
_api_server.api_bot_stop
_api_server.api_diagnostics_api_stats
_api_server.api_events

# reaction_strategy.CycleBudget
# Defined in reaction_strategy.py but not yet wired to a caller.
# Tracked in spawn_queue 01-03 for removal decision.
from reaction_strategy import CycleBudget  # noqa: F401

whitelist_item(CycleBudget, "cancels_used")
whitelist_item(CycleBudget, "creates_used")
whitelist_item(CycleBudget, "requotes_used")
whitelist_item(CycleBudget, "max_cancels")
whitelist_item(CycleBudget, "max_creates")
whitelist_item(CycleBudget, "max_requotes")
