"""Install instrumented wrappers around key bot methods for observability

At startup, `install_all_hooks()` walks the offer, fill, dexie, coin, risk,
sniper, price, db, wallet, splash, and bot-loop subsystems and replaces
selected methods with timed wrappers. On exit each wrapper emits TRACE for
fast calls, INFO for slow calls, WARN for very slow calls, and ERROR when
the wrapped method raised — so the in-memory ring buffer in `super_log` has
the context needed to reconstruct what happened before any failure.

Key responsibilities:
    - Patch instance methods in-place via `functools.wraps`
    - Apply per-category slow thresholds (method / network / wallet)
    - Classify each call's exit level based on duration and outcome
    - Re-raise unchanged after logging so normal control flow is preserved

Call `install_all_hooks()` exactly once, after every subsystem has been
constructed. Safe to call before `init_super_log` — the shim falls back to
a no-op if `super_log` is unavailable.
"""

import time
import threading
from functools import wraps

try:
    from super_log import slog
except ImportError:
    def slog(cat, msg, data=None, level="info"): pass

# Methods slower than this threshold are logged at INFO level;
# methods slower than 5× this threshold are logged at WARN level.
SLOW_METHOD_MS = 500
# Network-facing categories get higher thresholds (HTTP / RPC latency).
SLOW_NETWORK_MS = 2000   # Dexie HTTP, TibetSwap, offer creation via Sage
# Wallet RPC calls get a longer threshold (they talk to network)
SLOW_WALLET_MS = 5000


def _wrap_method(obj, method_name, category, log_args=False, arg_names=None,
                 slow_ms=SLOW_METHOD_MS):
    """Wrap a method with level-aware logging.

    Normal runs → TRACE entry + TRACE exit (ring buffer only)
    Slow runs → TRACE entry + WARN exit (written to file)
    Errors → ERROR (written to file + context dump)
    """
    original = getattr(obj, method_name, None)
    if original is None or not callable(original):
        return

    @wraps(original)
    def wrapper(*args, **kwargs):
        thread = threading.current_thread().name
        extra = {"thread": thread}
        if log_args and args and arg_names:
            for i, name in enumerate(arg_names):
                if i + 1 < len(args):  # skip self
                    val = args[i + 1]
                    if isinstance(val, (str, int, float, bool)):
                        extra[name] = str(val)[:50]
                    elif isinstance(val, (list, set)):
                        extra[name] = f"len={len(val)}"
                    elif isinstance(val, dict):
                        extra[name] = f"keys={len(val)}"

        # Entry → TRACE (ring buffer only)
        slog(category, f">>> {method_name}", extra, level="trace")
        start = time.time()
        try:
            result = original(*args, **kwargs)
            elapsed_ms = (time.time() - start) * 1000

            result_info = {"time_ms": f"{elapsed_ms:.1f}", "thread": thread}
            if isinstance(result, (list, set)):
                result_info["result_len"] = len(result)
            elif isinstance(result, dict):
                result_info["result_keys"] = len(result)
            elif isinstance(result, bool):
                result_info["result"] = str(result)
            elif isinstance(result, tuple) and len(result) <= 4:
                result_info["result"] = str(result)[:80]

            # 3-tier: TRACE → INFO (>slow_ms) → WARN (>5×slow_ms)
            if elapsed_ms > slow_ms * 5:
                lvl = "warn"
            elif elapsed_ms > slow_ms:
                lvl = "info"
            else:
                lvl = "trace"
            slog(category, f"<<< {method_name}", result_info, level=lvl)
            return result
        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            slog(category, f"!!! {method_name} ERROR: {e}",
                 {"time_ms": f"{elapsed_ms:.1f}", "thread": thread}, level="error")
            raise

    setattr(obj, method_name, wrapper)


def _wrap_function(module, func_name, category, log_args=False, arg_names=None,
                   slow_ms=SLOW_METHOD_MS):
    """Wrap a module-level function with level-aware logging."""
    original = getattr(module, func_name, None)
    if original is None or not callable(original):
        return

    @wraps(original)
    def wrapper(*args, **kwargs):
        thread = threading.current_thread().name
        extra = {"thread": thread}
        if log_args and args and arg_names:
            for i, name in enumerate(arg_names):
                if i < len(args):
                    val = args[i]
                    if isinstance(val, (str, int, float, bool)):
                        extra[name] = str(val)[:50]
                    elif isinstance(val, (list, set)):
                        extra[name] = f"len={len(val)}"

        slog(category, f">>> {func_name}", extra, level="trace")
        start = time.time()
        try:
            result = original(*args, **kwargs)
            elapsed_ms = (time.time() - start) * 1000
            result_info = {"time_ms": f"{elapsed_ms:.1f}", "thread": thread}
            if isinstance(result, (list, set)):
                result_info["result_len"] = len(result)
            elif isinstance(result, dict):
                result_info["result_keys"] = len(result)
            elif isinstance(result, bool):
                result_info["result"] = str(result)

            # 3-tier: TRACE → INFO (>slow_ms) → WARN (>5×slow_ms)
            if elapsed_ms > slow_ms * 5:
                lvl = "warn"
            elif elapsed_ms > slow_ms:
                lvl = "info"
            else:
                lvl = "trace"
            slog(category, f"<<< {func_name}", result_info, level=lvl)
            return result
        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            slog(category, f"!!! {func_name} ERROR: {e}",
                 {"time_ms": f"{elapsed_ms:.1f}", "thread": thread}, level="error")
            raise

    setattr(module, func_name, wrapper)


def install_all_hooks():
    """Install logging hooks on all bot modules. Call once after imports."""
    slog("HOOKS", "Installing super_log hooks on all modules...")
    hooked = 0

    # ---- OFFER MANAGER ----
    try:
        import offer_manager as om
        cls = om.OfferManager
        for method in ["create_offer_with_retry", "create_ladder", "cancel_offers",
                        "cancel_all", "cleanup_expired", "detect_expiring_offers",
                        "retry_failed_cancels", "sync_from_wallet", "should_requote",
                        "requote_side"]:
            _wrap_method(cls, method, "OFFER", slow_ms=SLOW_NETWORK_MS)
            hooked += 1
        slog("HOOKS", "  offer_manager: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  offer_manager: FAILED — {e}", level="warn")

    # ---- FILL TRACKER ----
    try:
        from fill_tracker import FillTracker
        count = 0
        for method in ["detect_fills", "match_round_trips", "_record_fill",
                        "_check_mass_disappearance"]:
            _wrap_method(FillTracker, method, "FILL")
            count += 1
        hooked += count
        slog("HOOKS", "  fill_tracker: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  fill_tracker: FAILED — {e}", level="warn")

    # ---- DEXIE MANAGER ----
    try:
        from dexie_manager import DexieManager
        count = 0
        for method in ["queue_post", "flush_queue", "_post_single",
                        "repost_active_offers", "prune_mappings"]:
            _wrap_method(DexieManager, method, "DEXIE", slow_ms=SLOW_NETWORK_MS)
            count += 1
        hooked += count
        slog("HOOKS", "  dexie_manager: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  dexie_manager: FAILED — {e}", level="warn")

    # ---- COIN MANAGER ----
    try:
        from coin_manager import CoinManager
        count = 0
        for method in ["snapshot_coins", "check_runtime_health", "needs_topup",
                        "needs_coin_prep", "start_topup", "start_coin_prep",
                        "check_coin_prep_status", "coin_readiness_report",
                        "get_startup_advisory", "reconcile_with_wallet",
                        "update_coin_counts", "is_busy"]:
            _wrap_method(CoinManager, method, "COIN")
            count += 1
        hooked += count
        slog("HOOKS", "  coin_manager: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  coin_manager: FAILED — {e}", level="warn")

    # ---- RISK MANAGER ----
    try:
        from risk_manager import RiskManager
        count = 0
        for method in ["update_inventory", "record_snapshot", "get_adjusted_spread",
                        "check_circuit_breakers", "should_enable_side",
                        "update_arb_gap", "update_fill_rate"]:
            _wrap_method(RiskManager, method, "RISK")
            count += 1
        hooked += count
        slog("HOOKS", "  risk_manager: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  risk_manager: FAILED — {e}", level="warn")

    # ---- SNIPER ----
    try:
        from sniper import Sniper
        count = 0
        for method in ["try_snipe", "_calculate_snipe_size", "_should_snipe_side",
                        "_create_snipe_offer", "prune_active_snipes"]:
            _wrap_method(Sniper, method, "SNIPER")
            count += 1
        hooked += count
        slog("HOOKS", "  sniper: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  sniper: FAILED — {e}", level="warn")

    # ---- PRICE ENGINE ----
    try:
        from price_engine import PriceEngine
        count = 0
        for method in ["get_price", "get_volatility", "get_tibet_pool_info",
                        "get_tibet_quote", "get_pool_depth_ratio",
                        "invalidate_tibet_cache", "get_dynamic_limits"]:
            _wrap_method(PriceEngine, method, "PRICE", slow_ms=SLOW_NETWORK_MS)
            count += 1
        hooked += count
        slog("HOOKS", "  price_engine: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  price_engine: FAILED — {e}", level="warn")

    # ---- DATABASE (module-level functions) ----
    try:
        import database as db
        count = 0
        for func in ["add_offer", "update_offer_status", "update_offer_dexie",
                      "batch_cancel_stale_offers", "recover_unknown_offers",
                      "record_fill", "match_round_trip", "record_inventory_snapshot",
                      "reconcile_coins_with_wallet", "link_offers_to_locked_coins",
                      "get_open_offers", "get_trade_dexie_map", "get_net_position"]:
            _wrap_function(db, func, "DB_FUNC")
            count += 1
        hooked += count
        slog("HOOKS", "  database: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  database: FAILED — {e}", level="warn")

    # ---- WALLET (module-level functions, longer slow threshold for RPC) ----
    try:
        import wallet
        count = 0
        for func in ["get_wallet_balance", "get_open_offers_rpc",
                      "get_spendable_coins_rpc", "create_offer_rpc",
                      "cancel_offer_rpc", "get_chia_health"]:
            if hasattr(wallet, func):
                _wrap_function(wallet, func, "WALLET", slow_ms=SLOW_WALLET_MS)
                count += 1
        hooked += count
        slog("HOOKS", "  wallet: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  wallet: FAILED — {e}", level="warn")

    # ---- SPLASH NODE ----
    try:
        from splash_node import SplashNode
        count = 0
        for method in ["start", "stop", "post_offer"]:
            if hasattr(SplashNode, method):
                _wrap_method(SplashNode, method, "SPLASH")
                count += 1
        hooked += count
        slog("HOOKS", "  splash_node: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  splash_node: FAILED — {e}", level="warn")

    # ---- BOT LOOP ----
    try:
        from bot_loop import BotLoop
        count = 0
        # _run_one_cycle includes a full Sage get_offers RPC (~4s for 80 offers),
        # so it gets a 6s threshold rather than the default 500ms to avoid noise.
        _wrap_method(BotLoop, "_run_one_cycle", "LOOP", slow_ms=6000)
        count += 1
        for method in ["_startup_sync", "_handle_requoting",
                        "_create_offers_if_needed", "_handle_coins",
                        "_handle_housekeeping", "_repost_active_offers_to_dexie",
                        "graceful_config_change"]:
            _wrap_method(BotLoop, method, "LOOP", slow_ms=SLOW_NETWORK_MS)
            count += 1
        hooked += count
        slog("HOOKS", "  bot_loop: hooked", level="debug")
    except Exception as e:
        slog("HOOKS", f"  bot_loop: FAILED — {e}", level="warn")

    slog("HOOKS", f"Total: {hooked} methods/functions hooked")
    return hooked

