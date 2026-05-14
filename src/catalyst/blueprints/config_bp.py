"""Configuration, settings-validation and session-resume routes.

Nine routes:
  * `/api/config` GET + POST (single-key and bulk forms), `/api/config/reload`,
    `/api/config/apply`, `/api/config/live` — config read/write.
  * `/api/fees/status` — blockchain fee snapshot.
  * `/api/settings/defaults`, `/api/settings/validate` — GUI settings form.
  * `/api/check-resume` — resume-modal bootstrap.

Named `config_bp` (not `config`) to avoid shadowing the top-level
`config.py` module that provides the singleton `cfg`.
"""

from __future__ import annotations

import json as _json
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, request

import api_server
from config import cfg
from database import log_event
from tx_fees import get_fee_settings_snapshot


bp = Blueprint("config_bp", __name__)


def _api_server():
    """Return the currently loaded api_server module.

    Some tests reload api_server while Flask blueprints remain imported.
    Looking it up lazily keeps these routes attached to the live module state.
    """
    try:
        owner = current_app.config.get("_CATALYST_API_SERVER_MODULE")
        return owner or sys.modules.get("api_server", api_server)
    except RuntimeError:
        return sys.modules.get("api_server", api_server)


def _apply_sage_change_address_setting() -> dict:
    """Apply the opt-in Sage change-address setting immediately when possible."""
    try:
        from wallet import get_wallet_type, get_next_address
        if get_wallet_type() != "sage":
            return {"attempted": False, "success": False, "error": "wallet_not_sage"}
        if not getattr(cfg, "SAGE_SET_CHANGE_ADDRESS", False):
            return {"attempted": False, "success": False, "error": "setting_disabled"}

        addr_result = get_next_address(new_address=False)
        if not addr_result or not addr_result.get("success") or not addr_result.get("address"):
            return {"attempted": True, "success": False, "error": "wallet_address_unavailable"}

        cfg.WALLET_ADDRESS = addr_result["address"]
        from wallet_sage import set_change_address as _sage_set_change_address
        result = _sage_set_change_address(cfg.WALLET_ADDRESS)
        if result and result.get("success"):
            log_event("success", "sage_change_address_set",
                      f"Sage change address set to {cfg.WALLET_ADDRESS[:20]}... "
                      f"for fingerprint {result.get('fingerprint')}")
            return {"attempted": True, **result}

        error = (result or {}).get("error", "unknown_error")
        log_event("warning", "sage_change_address_failed",
                  f"Could not set Sage change address via API: {error}")
        return {"attempted": True, "success": False, "error": "change_address_failed"}
    except Exception as e:
        log_event("warning", "sage_change_address_failed",
                  f"Error applying Sage change address via API: {e}")
        return {"attempted": True, "success": False, "error": "Change address failed"}


def _resume_last_active_label(offers: list) -> str:
    """Return a human-readable 'last active' string from the most recent offer timestamp."""
    best = None
    for o in offers:
        ts = o.get("creation_timestamp") or o.get("created_at") or ""
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if best is None or dt > best:
                best = dt
        except Exception:
            pass
    if best is None:
        return "Previous session"
    now = datetime.now(timezone.utc)
    diff = now - best
    minutes = int(diff.total_seconds() // 60)
    if minutes < 2:
        return "Active just now"
    if minutes < 60:
        return f"Last active {minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"Last active {hours}h ago"
    days = hours // 24
    return f"Last active {days}d ago"


# Security-sensitive keys blocked from API modification. Can only be
# changed by editing .env directly.
_BLOCKED_KEYS = {
    "CHIA_WALLET_CERT", "CHIA_WALLET_KEY", "WALLET_FINGERPRINT",
    "SPACESCAN_API_KEY", "SAGE_CERT_PATH", "SAGE_KEY_PATH", "SAGE_DATA_DIR",
    "SAGE_FINGERPRINT",
    "CHIA_WALLET_RPC_URL", "CHIA_FULL_NODE_RPC_URL", "SAGE_RPC_URL",
    "DEXIE_API_BASE", "TIBET_API_BASE",
    "SPLASH_SUBMIT_URL", "COINSET_API_URL",
    "SPACESCAN_PRO_URL", "SPACESCAN_FREE_URL",
    "WALLET_TYPE",
    "CAT_ASSET_ID",
    # CAT_WALLET_ID is derived at runtime from CAT_ASSET_ID via Sage's
    # get_cats RPC (see wallet_sage.get_wallets). Persisting it to .env
    # makes it stale every time the user switches fingerprints, which then
    # causes a 10-second window at startup where _active_cat points at the
    # wrong wallet_id before the resolver overwrites it.
    "CAT_WALLET_ID",
}


_KEY_MAP = {
    "spread_bps": "SPREAD_BPS",
    "loop_seconds": "LOOP_SECONDS",
    "default_trade_xch": "DEFAULT_TRADE_XCH",
    "max_active_buy": "MAX_ACTIVE_BUY",
    "max_active_sell": "MAX_ACTIVE_SELL",
    "auto_requote": "AUTO_REQUOTE",
    "requote_bps": "REQUOTE_BPS",
    "requote_cooldown": "REQUOTE_COOLDOWN_SECS",
    "requote_batch_size": "REQUOTE_BATCH_SIZE",
    "tibet_shock_cancel_trigger_pct": "TIBET_SHOCK_CANCEL_TRIGGER_PCT",
    "tibet_shock_cancel_mid_pct": "TIBET_SHOCK_CANCEL_MID_PCT",
    "tibet_shock_cancel_outer_pct": "TIBET_SHOCK_CANCEL_OUTER_PCT",
    "xch_reserve": "XCH_RESERVE",
    "cat_reserve": "CAT_RESERVE",
    "dry_run": "DRY_RUN",
    "enable_buy": "ENABLE_BUY",
    "enable_sell": "ENABLE_SELL",
    "topup_pool_pct": "TOPUP_POOL_PCT",
    "topup_pool_xch": "TOPUP_POOL_XCH",
    "topup_pool_cat": "TOPUP_POOL_CAT",
    "dynamic_limit_pct": "DYNAMIC_LIMIT_PCT",
    "max_step_change_fraction": "MAX_STEP_CHANGE_FRACTION",
    "min_mid": "HARD_MIN_PRICE_XCH",
    "max_mid": "HARD_MAX_PRICE_XCH",
    "price_strategy": "PRICE_STRATEGY",
    "arb_threshold_bps": "ARB_ALERT_THRESHOLD_BPS",
    "offer_expiry_minutes": "OFFER_EXPIRY_SECS",
    "dynamic_spread_enabled": "DYNAMIC_SPREAD_ENABLED",
    "base_spread_bps": "BASE_SPREAD_BPS",
    "min_edge_bps": "MIN_EDGE_BPS",
    "min_spread_bps": "MIN_SPREAD_BPS",
    "max_spread_bps": "MAX_SPREAD_BPS",
    "volatility_window_hours": "VOLATILITY_WINDOW_HOURS",
    "market_toxicity_enabled": "MARKET_TOXICITY_ENABLED",
    "toxicity_protection_level": "TOXICITY_PROTECTION_LEVEL",
    "toxicity_widen_start": "TOXICITY_WIDEN_START",
    "toxicity_elevated_start": "TOXICITY_ELEVATED_START",
    "toxicity_throttle_start": "TOXICITY_THROTTLE_START",
    "toxicity_cancel_start": "TOXICITY_CANCEL_START",
    "toxicity_throttle_secs": "TOXICITY_THROTTLE_SECS",
    "toxicity_decay_per_loop": "TOXICITY_DECAY_PER_LOOP",
    "toxicity_max_spread_multiplier": "TOXICITY_MAX_SPREAD_MULTIPLIER",
    "toxicity_min_throttle_signals": "TOXICITY_MIN_THROTTLE_SIGNALS",
    "toxicity_cancel_enabled": "TOXICITY_CANCEL_ENABLED",
    "inventory_enabled": "INVENTORY_ENABLED",
    "skew_intensity": "SKEW_INTENSITY",
    "max_position_xch": "MAX_POSITION_XCH",
    "liquidity_mode": "LIQUIDITY_MODE",
    "tier_enabled": "TIER_ENABLED",
    "buy_ladder_reversed": "BUY_LADDER_REVERSED",
    "inner_size_xch": "INNER_SIZE_XCH",
    "mid_size_xch": "MID_SIZE_XCH",
    "outer_size_xch": "OUTER_SIZE_XCH",
    "extreme_size_xch": "EXTREME_SIZE_XCH",
    "buy_inner_size_xch":   "BUY_INNER_SIZE_XCH",
    "buy_mid_size_xch":     "BUY_MID_SIZE_XCH",
    "buy_outer_size_xch":   "BUY_OUTER_SIZE_XCH",
    "buy_extreme_size_xch": "BUY_EXTREME_SIZE_XCH",
    "sell_inner_size_xch":   "SELL_INNER_SIZE_XCH",
    "sell_mid_size_xch":     "SELL_MID_SIZE_XCH",
    "sell_outer_size_xch":   "SELL_OUTER_SIZE_XCH",
    "sell_extreme_size_xch": "SELL_EXTREME_SIZE_XCH",
    "inner_tier_count": "INNER_TIER_COUNT",
    "mid_tier_count": "MID_TIER_COUNT",
    "outer_tier_count": "OUTER_TIER_COUNT",
    "extreme_tier_count": "EXTREME_TIER_COUNT",
    "buy_inner_tier_count": "BUY_INNER_TIER_COUNT",
    "buy_mid_tier_count": "BUY_MID_TIER_COUNT",
    "buy_outer_tier_count": "BUY_OUTER_TIER_COUNT",
    "buy_extreme_tier_count": "BUY_EXTREME_TIER_COUNT",
    "sell_inner_tier_count": "SELL_INNER_TIER_COUNT",
    "sell_mid_tier_count": "SELL_MID_TIER_COUNT",
    "sell_outer_tier_count": "SELL_OUTER_TIER_COUNT",
    "sell_extreme_tier_count": "SELL_EXTREME_TIER_COUNT",
    "inner_tier_spare_count": "INNER_TIER_SPARE_COUNT",
    "mid_tier_spare_count": "MID_TIER_SPARE_COUNT",
    "outer_tier_spare_count": "OUTER_TIER_SPARE_COUNT",
    "extreme_tier_spare_count": "EXTREME_TIER_SPARE_COUNT",
    "buy_inner_tier_spare_count": "BUY_INNER_TIER_SPARE_COUNT",
    "buy_mid_tier_spare_count": "BUY_MID_TIER_SPARE_COUNT",
    "buy_outer_tier_spare_count": "BUY_OUTER_TIER_SPARE_COUNT",
    "buy_extreme_tier_spare_count": "BUY_EXTREME_TIER_SPARE_COUNT",
    "sell_inner_tier_spare_count": "SELL_INNER_TIER_SPARE_COUNT",
    "sell_mid_tier_spare_count": "SELL_MID_TIER_SPARE_COUNT",
    "sell_outer_tier_spare_count": "SELL_OUTER_TIER_SPARE_COUNT",
    "sell_extreme_tier_spare_count": "SELL_EXTREME_TIER_SPARE_COUNT",
    "competitor_aware_enabled": "COMPETITOR_AWARE_ENABLED",
    "dbx_max_spread_bps": "DBX_MAX_SPREAD_BPS",
    "coin_prep_multiplier": "COIN_PREP_MULTIPLIER",
    "coin_prep_headroom_pct": "COIN_PREP_HEADROOM_PCT",
    "transaction_fee_mode": "TRANSACTION_FEE_MODE",
    "transaction_fee_xch": "TRANSACTION_FEE_XCH",
    "transaction_fee_target_secs": "TRANSACTION_FEE_TARGET_SECS",
    "transaction_fee_estimate_cost": "TRANSACTION_FEE_ESTIMATE_COST",
    "fee_prep_count": "FEE_PREP_COUNT",
    "fee_coin_size_xch": "FEE_COIN_SIZE_XCH",
    "sniper_enabled": "SNIPER_ENABLED",
    "sniper_size_xch": "SNIPER_SIZE_XCH",
    "sniper_prep_count": "SNIPER_PREP_COUNT",
    "sniper_rearm_price_move_bps": "SNIPER_REARM_PRICE_MOVE_BPS",
    "sniper_rearm_gap_move_bps": "SNIPER_REARM_GAP_MOVE_BPS",
    "splash_enabled": "SPLASH_ENABLED",
    "enable_coin_prep": "ENABLE_COIN_PREP",
    "enable_runtime_coin_health": "ENABLE_RUNTIME_COIN_HEALTH",
    "sage_set_change_address": "SAGE_SET_CHANGE_ADDRESS",
}


@bp.route("/api/config")
def api_config_get():
    """Get all configuration (excludes secrets)."""
    cfg = api_server.cfg
    return jsonify(cfg.to_dict())


@bp.route("/api/fees/status")
def api_fees_status():
    """Get fee settings plus the current effective/suggested fee snapshot."""
    return jsonify({"success": True, **api_server.get_fee_settings_snapshot()})


@bp.route("/api/config", methods=["POST"])
def api_config_update():
    """Update configuration settings.

    Accepts two formats:
      Single:  {"key": "SPREAD_BPS", "value": "800"}
      Bulk:    {"spread_bps": 800, "loop_seconds": 90, ...}
    """
    bot = api_server.bot
    cfg = api_server.cfg
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400

    if "key" in data and "value" in data:
        # --- Single key-value format ---
        key = data["key"]
        value = data["value"]
        if key in _BLOCKED_KEYS:
            return jsonify({"success": False, "error": f"Cannot modify {key} via API"}), 403

        _bot_running = bool(bot and bot.is_running())
        if key == "LIQUIDITY_MODE":
            _allowed = ("two_sided", "buy_only", "sell_only")
            if str(value).lower().strip() not in _allowed:
                return jsonify({
                    "success": False,
                    "error": f"LIQUIDITY_MODE must be one of: {', '.join(_allowed)}"
                }), 400
            if _bot_running:
                return jsonify({
                    "success": False,
                    "error": "LIQUIDITY_MODE cannot be changed while bot is running — stop the bot first"
                }), 409

        if key == "TOXICITY_PROTECTION_LEVEL":
            _allowed = ("gentle", "balanced", "defensive")
            if str(value).lower().strip() not in _allowed:
                return jsonify({
                    "success": False,
                    "error": f"TOXICITY_PROTECTION_LEVEL must be one of: {', '.join(_allowed)}"
                }), 400

        if key == "SPREAD_BPS":
            try:
                _bps_val = int(float(value))
            except (ValueError, TypeError):
                return jsonify({"success": False, "error": "SPREAD_BPS must be a positive integer"}), 400
            if _bps_val <= 0:
                return jsonify({
                    "success": False,
                    "error": "SPREAD_BPS must be a positive integer (got %d)" % _bps_val
                }), 400

        ok = cfg.update(key, str(value), source="api_settings_save")
        if ok:
            extra = None
            if key == "SAGE_SET_CHANGE_ADDRESS" and str(value).strip().lower() in ("true", "1", "yes", "on"):
                extra = _apply_sage_change_address_setting()
            safe_value = "***" if api_server._is_sensitive_key(key) else value
            log_event("info", "config_changed", f"Config updated: {key} = {safe_value}")
            response = {"success": True, "status": "updated", "key": key,
                        "change_address_result": extra}
            event_payload = {"key": key, "value": safe_value}
            notice = api_server._get_live_requote_notice([key])
            if notice:
                response["apply_mode"] = notice["apply_mode"]
                response["warning"] = notice["warning"]
                event_payload["apply_mode"] = notice["apply_mode"]
                event_payload["warning"] = notice["warning"]
            api_server.events.emit("config_changed", event_payload)
            return jsonify(response)
        return jsonify({"success": False, "error": f"Failed to update {key}"}), 500

    # --- Bulk format ---
    _bot_running = bool(bot and bot.is_running())
    updated = []
    errors = []
    validation_warnings = []
    for gui_key, value in data.items():
        env_key = _KEY_MAP.get(gui_key, gui_key.upper())
        if env_key in _BLOCKED_KEYS:
            continue

        if env_key == "LIQUIDITY_MODE":
            _allowed = ("two_sided", "buy_only", "sell_only")
            if str(value).lower().strip() not in _allowed:
                errors.append(f"LIQUIDITY_MODE must be one of: {', '.join(_allowed)}")
                continue
            if _bot_running:
                errors.append(
                    "LIQUIDITY_MODE cannot be changed while bot is running — stop the bot first"
                )
                continue

        if env_key == "TOXICITY_PROTECTION_LEVEL":
            _allowed = ("gentle", "balanced", "defensive")
            if str(value).lower().strip() not in _allowed:
                errors.append(f"TOXICITY_PROTECTION_LEVEL must be one of: {', '.join(_allowed)}")
                continue

        if env_key == "SPREAD_BPS":
            try:
                _bps_val = int(float(value))
            except (ValueError, TypeError):
                errors.append("SPREAD_BPS must be a positive integer")
                continue
            if _bps_val <= 0:
                errors.append("SPREAD_BPS must be a positive integer (got %d)" % _bps_val)
                continue
            _max_bps = getattr(cfg, "MAX_SPREAD_BPS", 0) or 0
            if _max_bps and _bps_val > _max_bps:
                validation_warnings.append(
                    f"SPREAD_BPS ({_bps_val}) exceeds MAX_SPREAD_BPS ({_max_bps}) — "
                    "dynamic spread will always clamp to the maximum"
                )

        ok = cfg.update(env_key, str(value), source="api_settings_save")
        if ok:
            updated.append(env_key)
        else:
            errors.append(env_key)

    _mode_after = (getattr(cfg, "LIQUIDITY_MODE", "two_sided") or "two_sided").lower()
    _sniper_after = getattr(cfg, "SNIPER_ENABLED", False)
    if _sniper_after and _mode_after in ("sell_only", "buy_only"):
        validation_warnings.append(
            f"SNIPER_ENABLED=true has no effect in {_mode_after} mode — "
            "sniper requires both sides to place opposing probe offers"
        )

    _buy_slots = getattr(cfg, "MAX_ACTIVE_BUY_OFFERS", 0) or 0
    _sell_slots = getattr(cfg, "MAX_ACTIVE_SELL_OFFERS", 0) or 0
    if _buy_slots == 0 and _sell_slots == 0 and (updated or errors):
        validation_warnings.append(
            "MAX_ACTIVE_BUY and MAX_ACTIVE_SELL are both 0 — "
            "bot will loop but create no offers"
        )

    response = {
        "success": len(errors) == 0,
        "status": "updated",
        "updated": updated,
        "errors": errors,
        "warnings": validation_warnings or None,
        "change_address_result": None,
    }

    if updated:
        # Legacy key clearing — HARD_* takes precedence over MAX_MID / MIN_MID.
        legacy_cleared = []
        if "HARD_MAX_PRICE_XCH" in updated:
            if cfg.update("MAX_MID", ""):
                legacy_cleared.append("MAX_MID")
        if "HARD_MIN_PRICE_XCH" in updated:
            if cfg.update("MIN_MID", ""):
                legacy_cleared.append("MIN_MID")
        if legacy_cleared:
            log_event("info", "legacy_keys_cleared",
                      f"Cleared legacy price rail keys: {', '.join(legacy_cleared)} "
                      f"(superseded by HARD_MAX/MIN_PRICE_XCH)")
            updated.extend(legacy_cleared)

        log_event("info", "config_changed", f"Bulk config updated: {', '.join(updated)}")
        event_payload = {"keys": updated}
        notice = api_server._get_live_requote_notice(updated)
        if notice:
            response["apply_mode"] = notice["apply_mode"]
            response["warning"] = notice["warning"]
            event_payload["apply_mode"] = notice["apply_mode"]
            event_payload["warning"] = notice["warning"]
        api_server.events.emit("config_changed", event_payload)

    extra = None
    if ("SAGE_SET_CHANGE_ADDRESS" in updated and
            str(getattr(cfg, "SAGE_SET_CHANGE_ADDRESS", False)).lower() == "true"):
        extra = _apply_sage_change_address_setting()

    response["change_address_result"] = extra
    return jsonify(response)


@bp.route("/api/config/reload", methods=["POST"])
def api_config_reload():
    """Reload config from .env file."""
    cfg = api_server.cfg
    cfg.reload()
    api_server.events.emit("config_changed", {"action": "full_reload"})
    return jsonify({"status": "reloaded"})


@bp.route("/api/config/apply", methods=["POST"])
def api_config_apply():
    """Apply config changes gracefully while bot is running (V1 parity)."""
    bot = api_server.bot
    cfg = api_server.cfg
    if not bot:
        return jsonify({"error": "Bot not initialised"}), 500

    if not bot.is_running():
        cfg.reload()
        return jsonify({"status": "reloaded", "message": "Bot not running — config reloaded directly"})

    result = bot.graceful_config_change()
    api_server.events.emit("config_changed", {"action": "graceful_apply", "result": result})
    return jsonify(result)


@bp.route("/api/config/live", methods=["POST"])
def api_config_live():
    """Live control endpoint — update a single config key and optionally
    trigger a graceful apply.  Used by the Live Controls bar in the GUI.

    Body: {"key": "BASE_SPREAD_BPS", "value": "600", "graceful": true}
    """
    bot = api_server.bot
    cfg = api_server.cfg
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400
    if "key" not in data or "value" not in data:
        return jsonify({"success": False, "error": "Missing key/value"}), 400

    raw_key = str(data["key"])
    key = _KEY_MAP.get(raw_key, raw_key.upper())
    value = str(data["value"])
    graceful = data.get("graceful", False)

    if key in _BLOCKED_KEYS:
        return jsonify({"success": False, "error": f"Cannot modify {key} via live controls"}), 403

    if key == "LIQUIDITY_MODE":
        _allowed = ("two_sided", "buy_only", "sell_only")
        if str(value).lower().strip() not in _allowed:
            return jsonify({
                "success": False,
                "error": f"LIQUIDITY_MODE must be one of: {', '.join(_allowed)}"
            }), 400
        if bot and bot.is_running():
            return jsonify({
                "success": False,
                "error": "LIQUIDITY_MODE cannot be changed while bot is running — stop the bot first"
            }), 409

    ok = cfg.update(key, value, source="gui_live_control")
    if not ok:
        return jsonify({"success": False, "error": f"Failed to update {key}"}), 500

    log_event("info", "config_live", f"Live control: {key} = {value}")
    response = {"success": True, "key": key}

    if bot:
        try:
            bot_status = (bot.get_state() or {}).get("status", "")
            if bot_status == "recovering":
                response["recovery_warning"] = (
                    "Bot is currently in recovery mode \u2014 config changes may not "
                    "take effect until recovery completes."
                )
        except Exception:
            pass
    event_payload = {"key": key, "value": value, "source": "live_controls"}

    if bot and bot.is_running() and key in api_server._LIVE_REQUOTE_ONLY_KEYS:
        warning = (
            "Saved without live offer migration — existing offers stay live and "
            "the change will take effect on future requotes and new offers."
        )
        response["apply_mode"] = "next_requote"
        response["warning"] = warning
        event_payload["apply_mode"] = "next_requote"
        event_payload["warning"] = warning

    api_server.events.emit("config_changed", event_payload)

    if graceful and key in api_server._LIVE_REQUOTE_ONLY_KEYS:
        response["graceful"] = {
            "status": "skipped",
            "message": "Live migration is disabled for this control; existing offers were left in place.",
        }
        return jsonify(response)

    if graceful and bot and bot.is_running():
        try:
            result = bot.graceful_config_change()
            api_server.events.emit("config_changed", {"action": "graceful_apply", "result": result})
            response["graceful"] = result
            return jsonify(response)
        except Exception as e:
            log_event("error", "api_error", f"Config apply graceful error: {e}")
            response["graceful_error"] = "Apply failed — see debug log"
            return jsonify(response)

    return jsonify(response)


@bp.route("/api/settings/defaults")
def api_settings_defaults():
    """Get default settings (current config as defaults for GUI)."""
    cfg = api_server.cfg
    d = api_server._serialize_dict(cfg.to_dict())
    d["success"] = True
    return jsonify(d)


@bp.route("/api/settings/validate", methods=["POST"])
def api_settings_validate():
    """Validate config settings before saving."""
    bot = api_server.bot
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400
    errors = []
    warnings = []

    def _get_first(*keys):
        for key in keys:
            if key in data:
                return data.get(key)
        return None

    def _decimal_value(*keys):
        raw = _get_first(*keys)
        if raw in (None, ""):
            return None
        try:
            return Decimal(str(raw))
        except Exception:
            return None

    def _bool_value(*keys):
        raw = _get_first(*keys)
        if raw is None:
            return None
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    if "SPREAD_BPS" in data:
        try:
            spread = float(data["SPREAD_BPS"])
            if spread < 10:
                warnings.append("Spread below 0.1% is very tight — high risk of adverse selection")
            if spread > 2000:
                warnings.append("Spread above 20% — offers unlikely to fill")
        except ValueError:
            errors.append("SPREAD_BPS must be a number")

    if "NUM_OFFERS" in data:
        try:
            n = int(data["NUM_OFFERS"])
            if n < 1:
                errors.append("NUM_OFFERS must be at least 1")
            if n > 50:
                warnings.append("More than 50 offers per side requires many coins")
        except ValueError:
            errors.append("NUM_OFFERS must be an integer")

    fee_mode = str(data.get("transaction_fee_mode", "auto") or "auto").strip().lower()
    if fee_mode not in {"auto", "manual"}:
        errors.append("Transaction fee mode must be auto or manual")

    try:
        fee_xch = Decimal(str(data.get("transaction_fee_xch", "0") or "0"))
        if fee_xch < 0:
            errors.append("Transaction fee must be zero or greater")
    except Exception:
        errors.append("Transaction fee must be a valid XCH amount")
        fee_xch = Decimal("0")

    try:
        fee_count = int(data.get("fee_prep_count", 0) or 0)
        if fee_count < 0:
            errors.append("Fee prep count must be zero or greater")
    except Exception:
        errors.append("Fee prep count must be an integer")
        fee_count = 0

    try:
        fee_coin_size = Decimal(str(data.get("fee_coin_size_xch", "0") or "0"))
        if fee_coin_size < 0:
            errors.append("Fee coin size must be zero or greater")
    except Exception:
        errors.append("Fee coin size must be a valid XCH amount")
        fee_coin_size = Decimal("0")

    if fee_count > 0 and fee_coin_size <= 0:
        errors.append("Fee coin size must be greater than zero when fee prep count is enabled")

    if fee_mode == "manual" and fee_xch > 0 and fee_coin_size > 0 and fee_coin_size <= fee_xch:
        warnings.append("Fee coin size should usually be larger than the manual fee so change can recycle into the fee pool")

    base_spread = _decimal_value("base_spread_bps", "BASE_SPREAD_BPS")
    min_edge = _decimal_value("min_edge_bps", "MIN_EDGE_BPS")
    min_spread = _decimal_value("min_spread_bps", "MIN_SPREAD_BPS")
    max_spread = _decimal_value("max_spread_bps", "MAX_SPREAD_BPS")
    vol_window = _decimal_value("volatility_window_hours", "VOLATILITY_WINDOW_HOURS")
    skew_intensity = _decimal_value("skew_intensity", "SKEW_INTENSITY")
    max_position = _decimal_value("max_position_xch", "MAX_POSITION_XCH")
    default_trade_xch = _decimal_value("default_trade_xch", "DEFAULT_TRADE_XCH")
    sniper_rearm_price_move = _decimal_value("sniper_rearm_price_move_bps", "SNIPER_REARM_PRICE_MOVE_BPS")
    sniper_rearm_gap_move = _decimal_value("sniper_rearm_gap_move_bps", "SNIPER_REARM_GAP_MOVE_BPS")
    shock_trigger_pct = _decimal_value("tibet_shock_cancel_trigger_pct", "TIBET_SHOCK_CANCEL_TRIGGER_PCT")
    dynamic_enabled = _bool_value("dynamic_spread_enabled", "DYNAMIC_SPREAD_ENABLED")
    inventory_enabled = _bool_value("inventory_enabled", "INVENTORY_ENABLED")
    competitor_enabled = _bool_value("competitor_aware_enabled", "COMPETITOR_AWARE_ENABLED")

    if base_spread is not None:
        if base_spread <= 0:
            errors.append("Base spread must be greater than zero")
        elif base_spread < Decimal("200"):
            warnings.append("Base spread below 2% is very aggressive for live market making")
        elif base_spread > Decimal("1500"):
            warnings.append("Base spread above 15% is very wide and can stall fills")

    if min_edge is not None and min_edge < 0:
        errors.append("Inner edge must be zero or greater")

    if min_spread is not None:
        if min_spread <= 0:
            errors.append("Min spread must be greater than zero")

    if max_spread is not None:
        if max_spread <= 0:
            errors.append("Max spread must be greater than zero")

    if min_spread is not None and max_spread is not None and max_spread < min_spread:
        errors.append("Max spread must be greater than or equal to min spread")

    if min_edge is not None:
        required_outer = min_edge * Decimal("1.5")
        if max_spread is not None and max_spread < required_outer:
            errors.append("Max spread must be at least 1.5× the inner edge")
        if min_spread is not None and min_spread < required_outer:
            warnings.append("Min spread is below the ladder safety floor and will be clamped up at runtime")
        if base_spread is not None and base_spread < required_outer:
            warnings.append("Base spread is below the ladder safety floor and will be clamped up at runtime")

    if base_spread is not None and min_spread is not None and base_spread < min_spread:
        warnings.append("Base spread is below min spread and will be clamped up at runtime")
    if base_spread is not None and max_spread is not None and base_spread > max_spread:
        warnings.append("Base spread is above max spread and will be clamped down at runtime")

    if vol_window is not None:
        if vol_window <= 0:
            errors.append("Volatility window must be greater than zero")
        elif vol_window < Decimal("1"):
            warnings.append("Volatility window below 1 hour will make spreads very reactive")
        elif vol_window > Decimal("24"):
            warnings.append("Volatility window above 24 hours will make spreads slow to adapt")

    if skew_intensity is not None:
        if skew_intensity < 0:
            errors.append("Skew intensity must be zero or greater")
        elif skew_intensity > 1:
            errors.append("Skew intensity must be 1.0 or lower")
        elif skew_intensity > Decimal("0.7"):
            warnings.append("Skew intensity above 0.7 is aggressive and can swing buy/sell spreads sharply")

    if max_position is not None:
        if max_position < 0:
            errors.append("Max position must be zero or greater")
        elif max_position == 0:
            warnings.append("Max position set to 0 disables position-limit protection")
            if inventory_enabled:
                warnings.append("Inventory management is enabled, but max position 0 effectively disables skew and side protection")
        elif default_trade_xch is not None and default_trade_xch > 0 and max_position < default_trade_xch:
            warnings.append("Max position is smaller than one normal trade size, so inventory protection may trip very quickly")
        elif bot and getattr(bot, "risk_manager", None):
            try:
                current_mid = getattr(bot, "_current_mid_price", Decimal("0")) or Decimal("0")
                if current_mid <= 0 and getattr(bot, "price_engine", None):
                    current_mid = Decimal(str(bot.price_engine.get_last_price() or 0))
                current_pos_cat = Decimal(
                    str(bot.risk_manager.get_inventory_state().get("net_position_cat", "0"))
                )
                current_pos_xch = abs(current_pos_cat * current_mid) if current_mid > 0 else Decimal("0")
                if current_pos_xch > 0:
                    if current_pos_xch > max_position:
                        warnings.append(
                            f"Current position is already {current_pos_xch:.2f} XCH, above the new max position"
                        )
                    elif current_pos_xch >= max_position * Decimal("0.8"):
                        warnings.append(
                            f"Current position is {current_pos_xch:.2f} XCH, close to the new max position"
                        )
            except Exception:
                pass

    if sniper_rearm_price_move is not None:
        if sniper_rearm_price_move < 0:
            errors.append("Sniper re-arm price move must be zero or greater")
        elif sniper_rearm_price_move == 0:
            warnings.append("Sniper re-arm price move of 0% makes sniper re-arm on every qualifying gap")
        elif sniper_rearm_price_move < Decimal("25"):
            warnings.append("Sniper re-arm price move below 0.25% may create frequent tiny probes")

    if sniper_rearm_gap_move is not None:
        if sniper_rearm_gap_move < 0:
            errors.append("Sniper re-arm arb gap move must be zero or greater")
        elif sniper_rearm_gap_move == 0:
            warnings.append("Sniper re-arm arb gap move of 0% makes sniper re-arm on every qualifying gap")
        elif sniper_rearm_gap_move < Decimal("25"):
            warnings.append("Sniper re-arm arb gap move below 0.25% may create frequent tiny probes")

    if shock_trigger_pct is not None:
        if shock_trigger_pct < 0:
            errors.append("Tibet shock cancel threshold must be zero or greater")
        elif shock_trigger_pct == 0:
            pass
        elif shock_trigger_pct < Decimal("0.5"):
            warnings.append("Tibet shock cancel threshold below 0.5% may churn offers on normal pool noise")
        elif shock_trigger_pct > Decimal("20"):
            warnings.append("Tibet shock cancel threshold above 20% may react too late to stale offers")

    if dynamic_enabled is False and (inventory_enabled or competitor_enabled):
        warnings.append(
            "Dynamic spreads off only disables volatility, fill-rate, arb-gap, and pool-depth scaling; "
            "inventory skew and competitor nudges still apply if those features stay enabled"
        )

    return jsonify({
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    })


@bp.route("/api/check-resume")
def api_check_resume():
    """Check if there are existing offers from a previous session.

    Returns can_resume + offer details so the GUI can show a resume modal.
    """
    server = _api_server()
    bot = server.bot
    cfg = server.cfg
    if bot and getattr(bot, "_loop_count", 0) > 0:
        return jsonify({"can_resume": False, "has_session": True,
                        "buy_count": 0, "sell_count": 0, "reason": "bot_already_running"})
    if server._fresh_start_is_set():
        return jsonify({"can_resume": False, "has_session": False,
                        "buy_count": 0, "sell_count": 0, "reason": "fresh_start_chosen"})
    try:
        from wallet import get_all_offers, classify_offers_from_list
        active_cat = server._active_cat
        asset_id = active_cat.get("asset_id") or (cfg.CAT_ASSET_ID if hasattr(cfg, "CAT_ASSET_ID") else "")
        offers = get_all_offers(include_completed=False, start=0, end=200)
        if not offers:
            return jsonify({"can_resume": False, "has_session": False,
                            "buy_count": 0, "sell_count": 0, "reason": "no offers"})

        open_buy, open_sell, _ = classify_offers_from_list(offers, asset_id)
        total = len(open_buy) + len(open_sell)
        can_resume = total > 0

        saved = {}
        if hasattr(cfg, "DEFAULT_TRADE_XCH"):
            saved["trade_xch"] = str(cfg.DEFAULT_TRADE_XCH)
        if hasattr(cfg, "MAX_ACTIVE_BUY"):
            saved["max_buy"] = int(cfg.MAX_ACTIVE_BUY)
        if hasattr(cfg, "MAX_ACTIVE_SELL"):
            saved["max_sell"] = int(cfg.MAX_ACTIVE_SELL)
        if hasattr(cfg, "SPREAD_BPS"):
            saved["spread_bps"] = float(cfg.SPREAD_BPS)
        saved["cat_name"] = active_cat.get("name") or getattr(cfg, "CAT_NAME", "CAT")
        saved["cat_asset_id"] = active_cat.get("asset_id") or getattr(cfg, "CAT_ASSET_ID", "")
        saved["cat_wallet_id"] = active_cat.get("wallet_id") or getattr(cfg, "CAT_WALLET_ID", None)
        saved["cat_decimals"] = active_cat.get("decimals") or getattr(cfg, "CAT_DECIMALS", 3)
        saved["cat_ticker_id"] = active_cat.get("ticker_id") or getattr(cfg, "CAT_TICKER_ID", "")

        # Detect gap closer activity via (1) open boost offers + (2) recent events.
        gap_closer_info = {"active": False, "count": 0}
        try:
            from database import get_connection
            db = get_connection()

            boost_count = 0
            try:
                from database import get_open_offers
                boost_offers = get_open_offers(cat_asset_id=asset_id)
                boost_count = sum(1 for o in boost_offers if o.get("tier") == "boost")
                print(f"[RESUME] DB open boost offers: {boost_count}", flush=True)
            except Exception:
                pass

            gc_event = None
            gc_deactivated = False
            try:
                row = db.execute(
                    "SELECT event_type, data, timestamp FROM events "
                    "WHERE event_type IN ('gap_closer_step', 'gap_closer_arbed', "
                    "  'gap_closer_activated', 'gap_closer_deactivated') "
                    "ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
                if row:
                    evt_type = row[0]
                    evt_data_str = row[1]
                    evt_ts = row[2]
                    print(f"[RESUME] Latest gap closer event: {evt_type} at {evt_ts}", flush=True)

                    if evt_type == "gap_closer_deactivated":
                        gc_deactivated = True
                    else:
                        try:
                            evt_time = datetime.fromisoformat(evt_ts.replace("Z", "+00:00"))
                            age = datetime.now(timezone.utc) - evt_time
                            if age < timedelta(hours=2):
                                gc_event = evt_data_str
                                print(f"[RESUME] Gap closer was active ({age.seconds//60}min ago)", flush=True)
                            else:
                                print(f"[RESUME] Gap closer event too old ({age})", flush=True)
                        except Exception:
                            gc_event = evt_data_str
            except Exception as e:
                print(f"[RESUME] Event check error: {e}", flush=True)

            if boost_count > 0 or (gc_event and not gc_deactivated):
                gap_closer_info["active"] = True
                gap_closer_info["count"] = max(boost_count, 2)

                if gc_event:
                    try:
                        evt_data = _json.loads(gc_event) if isinstance(gc_event, str) else gc_event
                        if evt_data and isinstance(evt_data, dict):
                            if evt_data.get("spread_bps"):
                                gap_closer_info["last_spread_bps"] = int(evt_data["spread_bps"])
                            if evt_data.get("arb_floor_bps"):
                                gap_closer_info["arb_floor_bps"] = int(evt_data["arb_floor_bps"])
                            if evt_data.get("steps_taken"):
                                gap_closer_info["steps_taken"] = int(evt_data["steps_taken"])
                    except Exception:
                        pass

                print(f"[RESUME] Gap closer info: {gap_closer_info}", flush=True)
        except Exception as e:
            print(f"[RESUME] Gap closer detection error: {e}", flush=True)

        return jsonify({
            "can_resume": can_resume,
            "has_session": can_resume,
            "buy_count": len(open_buy),
            "sell_count": len(open_sell),
            "offer_count": total,
            "saved_settings": saved,
            "active_cat": {
                "asset_id": saved.get("cat_asset_id") or "",
                "wallet_id": saved.get("cat_wallet_id"),
                "decimals": saved.get("cat_decimals"),
                "ticker_id": saved.get("cat_ticker_id") or "",
                "name": saved.get("cat_name") or "CAT",
            },
            "gap_closer": gap_closer_info,
            "last_active": _resume_last_active_label(open_buy + open_sell),
        })
    except Exception as e:
        log_event("error", "api_error", f"Resume session check failed: {e}", {"endpoint": request.path})
        return jsonify({"can_resume": False, "has_session": False,
                        "error": "Internal server error", "code": "SERVER_ERROR"})
