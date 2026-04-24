"""CAT discovery, selection, refresh + deposit-advisory + Dexie v3 pairs.

Routes:
  * `/api/deposit-advisory/allocate` — apply an external-deposit split
    between `TOPUP_POOL_*` and `*_RESERVE`.
  * `/api/token_overview` — Dexie asset description + website lookup.
  * `/api/dexie/v3-pairs` — exposed Dexie v3 pairs with summary stats.
  * `/api/cats` — discover wallet CATs and match against Dexie pairs.
  * `/api/cat/select` — persist active-CAT choice to .env and _active_cat.
  * `/api/cat/refresh` — force a config reload.
  * `/api/balances/refresh` — fetch fresh wallet balances.

Helpers `_normalize_asset_id` and `_get_dexie_pairs` live here since
`/api/cats` is the only caller.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Dict

from flask import Blueprint, jsonify, request

import api_server
from config import cfg
from database import log_event
from super_log import slog


bp = Blueprint("cat", __name__)


def _normalize_asset_id(asset_id: str) -> str:
    """Normalize asset ID for matching — remove 0x, lowercase, strip trailing 00 pairs."""
    if not asset_id:
        return ""
    cleaned = asset_id.lower().replace("0x", "")
    while cleaned.endswith("00") and len(cleaned) > 60:
        cleaned = cleaned[:-2]
    return cleaned


def _get_dexie_pairs() -> list:
    """Fetch all trading pairs from Dexie API.

    GET /v2/prices/tickers (no params) → all tickers → filter _XCH pairs.
    """
    try:
        import requests as _req
        dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
        url = f"{dexie_base}/v2/prices/tickers"
        response = _req.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()
        tickers = data.get("tickers", [])

        pairs = []
        for ticker in tickers:
            ticker_id = ticker.get("ticker_id", "")
            if "_XCH" in ticker_id and ticker_id != "XCH_USDT":
                base_name = ticker_id.replace("_XCH", "")
                pairs.append({
                    "ticker_id": ticker_id,
                    "name": ticker.get("base_name", base_name),
                    "asset_id": ticker.get("base_id", ""),
                    "price": float(ticker.get("current_avg_price", 0) or 0),
                    "volume_24h": float(ticker.get("target_volume", 0) or 0),
                    "vol_7d_xch":   float(ticker.get("target_volume_7d",  0) or 0),
                    "vol_30d_xch":  float(ticker.get("target_volume_30d", 0) or 0),
                    "price_high_7d": float(ticker.get("high_7d",  0) or 0),
                    "price_low_7d":  float(ticker.get("low_7d",   0) or 0),
                })

        pairs.sort(key=lambda x: x["volume_24h"], reverse=True)
        print(f"[CATS] Fetched {len(pairs)} Dexie pairs")
        return pairs
    except Exception as e:
        print(f"[CATS] Failed to fetch Dexie pairs: {e}")
        return []


@bp.route("/api/deposit-advisory/allocate", methods=["POST"])
def api_deposit_advisory_allocate():
    """Apply an allocation decision for a detected external deposit.

    Body:
        {
          "coin_id":        "0x...",    # required
          "wallet_type":    "xch" | "cat",
          "amount_mojos":   int,
          "to_pool_pct":    0-100,
          "budget_cfg":     "TOPUP_POOL_XCH" | "TOPUP_POOL_CAT",
          "reserve_cfg":    "XCH_RESERVE" | "CAT_RESERVE"
        }

    Adds `amount × to_pool_pct / 100` to the topup budget and the rest to
    the hard reserve floor.
    """
    cfg = api_server.cfg
    from database import get_setting as _get_setting, set_setting as _set_setting
    slog("GUI_ACTION", ">>> BUTTON: Deposit Advisory Allocate")

    try:
        payload = request.get_json(silent=True) or {}
        coin_id = str(payload.get("coin_id") or "").strip().lower()
        wallet_type = str(payload.get("wallet_type") or "").strip().lower()
        amount_mojos = int(payload.get("amount_mojos") or 0)
        to_pool_pct = float(payload.get("to_pool_pct") or 0)
        budget_cfg = str(payload.get("budget_cfg") or "").strip()
        reserve_cfg = str(payload.get("reserve_cfg") or "").strip()

        if not coin_id or wallet_type not in ("xch", "cat") or amount_mojos <= 0:
            return jsonify({"success": False, "error": "invalid_payload"}), 400
        if budget_cfg not in ("TOPUP_POOL_XCH", "TOPUP_POOL_CAT"):
            return jsonify({"success": False, "error": "invalid_budget_cfg"}), 400
        if reserve_cfg not in ("XCH_RESERVE", "CAT_RESERVE"):
            return jsonify({"success": False, "error": "invalid_reserve_cfg"}), 400
        if not (0.0 <= to_pool_pct <= 100.0):
            return jsonify({"success": False, "error": "pct_out_of_range"}), 400

        if wallet_type == "cat":
            scale = Decimal(10) ** Decimal(str(getattr(cfg, "CAT_DECIMALS", 3)))
        else:
            scale = Decimal("1000000000000")

        amount_asset = Decimal(amount_mojos) / scale
        to_pool_asset = amount_asset * Decimal(str(to_pool_pct)) / Decimal("100")
        to_reserve_asset = amount_asset - to_pool_asset

        current_budget = Decimal(str(getattr(cfg, budget_cfg, 0) or 0))
        current_reserve = Decimal(str(getattr(cfg, reserve_cfg, 0) or 0))
        new_budget = current_budget + to_pool_asset
        new_reserve = current_reserve + to_reserve_asset

        if wallet_type == "cat":
            budget_str = f"{new_budget:.3f}"
            reserve_str = f"{new_reserve:.3f}"
        else:
            budget_str = f"{new_budget:.8f}".rstrip("0").rstrip(".")
            reserve_str = f"{new_reserve:.8f}".rstrip("0").rstrip(".")
            if not budget_str:
                budget_str = "0"
            if not reserve_str:
                reserve_str = "0"

        # cfg.update() on TOPUP_POOL_* resets the session spend counter
        # to zero as a side effect — correct for Smart Settings, wrong
        # here: a deposit is ADDITIVE. Snapshot & restore.
        spend_key = ("topup_pool_cat_spent_mojos" if wallet_type == "cat"
                     else "topup_pool_xch_spent_mojos")
        pre_spent = 0
        try:
            pre_spent = int(str(_get_setting(spend_key, "0") or "0"))
        except Exception:
            pre_spent = 0

        budget_ok = cfg.update(budget_cfg, budget_str,
                               source="deposit_advisory",
                               note=f"deposit {coin_id[:16]}... to pool")
        reserve_ok = cfg.update(reserve_cfg, reserve_str,
                                source="deposit_advisory",
                                note=f"deposit {coin_id[:16]}... to reserve")

        if to_pool_pct > 0 and budget_ok:
            try:
                _set_setting(spend_key, str(pre_spent))
            except Exception:
                pass
        if not budget_ok and not reserve_ok:
            return jsonify({"success": False, "error": "config_write_failed"}), 500

        # Mark this coin as advised so the health check stops re-prompting.
        try:
            raw = _get_setting("deposit_advisory_advised_coins", "") or ""
            existing = [s.strip() for s in raw.split(",") if s.strip()]
            if coin_id not in existing:
                existing.append(coin_id)
                _set_setting("deposit_advisory_advised_coins", ",".join(existing))
        except Exception:
            pass

        try:
            api_server.alerts.clear(f"deposit_advisory_{coin_id}")
        except Exception:
            pass

        log_event("info", "deposit_advisory_allocated",
                  f"Deposit {coin_id[:16]}... allocated: "
                  f"{to_pool_asset} to {budget_cfg} (now {budget_str}), "
                  f"{to_reserve_asset} to {reserve_cfg} (now {reserve_str})")

        return jsonify({
            "success": True,
            "coin_id": coin_id,
            "to_pool_asset": str(to_pool_asset),
            "to_reserve_asset": str(to_reserve_asset),
            "new_budget": budget_str,
            "new_reserve": reserve_str,
        })
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/token_overview")
def api_token_overview():
    """Return description + website for a token from Dexie v1/assets."""
    dexie_asset_id = (request.args.get("dexie_asset_id") or "").strip().lower()
    if not dexie_asset_id:
        return jsonify({"success": False, "description": "", "website": ""})
    try:
        import requests as _req
        dexie_base = getattr(cfg, "DEXIE_API_BASE", "https://api.dexie.space")
        for page in range(1, 4):
            resp = _req.get(
                f"{dexie_base}/v1/assets",
                params={"page": page, "page_size": 100},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            assets = data.get("assets", [])
            for asset in assets:
                if asset.get("id", "").lower() == dexie_asset_id:
                    return jsonify({
                        "success": True,
                        "description": asset.get("description", ""),
                        "website": asset.get("website", ""),
                    })
            if len(assets) < 100:
                break
        return jsonify({"success": True, "description": "", "website": ""})
    except Exception as e:
        print(f"[TOKEN_OVERVIEW] Failed for {dexie_asset_id[:12]}: {e}")
        return jsonify({"success": False, "description": "", "website": "", "error": str(e)})


@bp.route("/api/dexie/v3-pairs")
def api_dexie_v3_pairs():
    """Expose Dexie v3 pairs (with summary stats) to the GUI."""
    bot = api_server.bot
    if not bot or not getattr(bot, "dexie_manager", None):
        return jsonify({"pairs": [], "error": "bot not initialised"}), 503
    try:
        pairs = bot.dexie_manager.fetch_v3_pairs() or []
        return jsonify({"pairs": pairs, "count": len(pairs)})
    except Exception as e:
        return api_server._api_error(e, request.path)


@bp.route("/api/cats")
def api_cats():
    """Discover CAT tokens by matching wallet CATs against Dexie pairs."""
    bot = api_server.bot
    cfg = api_server.cfg
    cats = []
    try:
        from wallet import get_wallets
        result = get_wallets()
        wallet_cats = {}

        if result and result.get("success"):
            wallets = result.get("wallets") or []
            for w in wallets:
                wtype = w.get("type", 0)
                if wtype == 6 or str(wtype) == "6" or str(wtype).upper() == "CAT":
                    wallet_id = w.get("id", 0)
                    name = w.get("name", "Unknown CAT")
                    asset_id = w.get("data", "") or w.get("asset_id", "")
                    if isinstance(asset_id, str) and len(asset_id) > 64:
                        asset_id = asset_id[:64]
                    if asset_id:
                        wallet_cats[asset_id] = {
                            "wallet_id": wallet_id,
                            "name": name,
                            "asset_id": asset_id,
                        }
                        print(f"[CATS] Found wallet CAT: {name} (Wallet {wallet_id}, Asset: {asset_id[:16]}...)")

        print(f"[CATS] Total wallet CATs found: {len(wallet_cats)}")
        log_event("info", "cat_discovery", f"Found {len(wallet_cats)} CAT tokens in wallet")

        dexie_pairs = _get_dexie_pairs()
        print(f"[CATS] Found {len(dexie_pairs)} Dexie pairs")

        # Enrich each matched CAT with v3 pair stats when available.
        v3_by_ticker: Dict[str, Dict] = {}
        try:
            if bot and getattr(bot, "dexie_manager", None):
                v3_pairs = bot.dexie_manager.fetch_v3_pairs() or []
                for vp in v3_pairs:
                    try:
                        tid = str(vp.get("ticker_id") or vp.get("ticker") or "").upper()
                        if tid:
                            v3_by_ticker[tid] = vp
                    except Exception:
                        continue
                if v3_by_ticker:
                    print(f"[CATS] Enriched with {len(v3_by_ticker)} v3 pair stats")
        except Exception as _v3_err:
            print(f"[CATS] v3 enrichment skipped: {_v3_err}")

        matched_count = 0
        for pair in dexie_pairs:
            raw_asset_id = pair.get("asset_id", "")
            normalized_dexie = _normalize_asset_id(raw_asset_id)

            wallet_info = None
            for wallet_asset, wallet_data in wallet_cats.items():
                normalized_wallet = _normalize_asset_id(wallet_asset)
                if (wallet_asset.lower() == raw_asset_id.lower() or
                        normalized_wallet == normalized_dexie):
                    wallet_info = wallet_data
                    break

            if wallet_info:
                matched_count += 1
                print(f"[CATS] Matched: {pair['name']} ({pair['ticker_id']}) in wallet {wallet_info['wallet_id']}")
                # Use the Dexie base_id (full 64-char) for the icon URL.
                # icons.dexie.space serves .webp ONLY — .png returns a placeholder.
                dexie_asset_id = pair.get("asset_id", "") or wallet_info["asset_id"]
                icon_url = (f"https://icons.dexie.space/{dexie_asset_id}.webp"
                            if dexie_asset_id else "")
                _v3 = v3_by_ticker.get(pair["ticker_id"].upper(), {})
                cats.append({
                    "asset_id": wallet_info["asset_id"],
                    "dexie_asset_id": dexie_asset_id,
                    "icon_url": icon_url,
                    "name": pair["name"],
                    "ticker": pair["ticker_id"].replace("_XCH", ""),
                    "ticker_id": pair["ticker_id"],
                    "wallet_id": wallet_info["wallet_id"],
                    "decimals": 3,
                    "category": "ready",
                    "volume_24h":    pair.get("volume_24h", 0),
                    "price":         pair.get("price", 0),
                    "vol_7d_xch":    pair.get("vol_7d_xch", 0),
                    "vol_30d_xch":   pair.get("vol_30d_xch", 0),
                    "price_high_7d": pair.get("price_high_7d", 0),
                    "price_low_7d":  pair.get("price_low_7d", 0),
                    "v3_last_price":      _v3.get("last_price"),
                    "v3_base_volume":     _v3.get("base_volume"),
                    "v3_target_volume":   _v3.get("target_volume"),
                    "v3_high":            _v3.get("high"),
                    "v3_low":             _v3.get("low"),
                    "v3_bid":             _v3.get("bid"),
                    "v3_ask":             _v3.get("ask"),
                })

        print(f"[CATS] Matched {matched_count} wallet CATs with Dexie pairs")
        log_event("success", "cat_discovery",
                  f"Matched {matched_count} wallet CATs with Dexie trading pairs")

        matched_assets = {c["asset_id"].lower() for c in cats}
        for asset_id, wdata in wallet_cats.items():
            if asset_id.lower() not in matched_assets:
                ticker = wdata["name"].split(" ")[0] if wdata["name"] else asset_id[:8]
                cats.append({
                    "asset_id": asset_id,
                    "name": wdata["name"],
                    "ticker": ticker,
                    "ticker_id": f"{ticker}_XCH",
                    "wallet_id": wdata["wallet_id"],
                    "decimals": 3,
                    "category": "wallet_only",
                    "volume_24h": 0,
                })

    except Exception as e:
        print(f"[CATS] Error in CAT discovery: {e}")
        import traceback
        traceback.print_exc()

    # Fallback: if everything failed, use configured CAT from .env
    if not cats:
        cat_id = cfg.CAT_ASSET_ID
        if cat_id:
            cat_name = getattr(cfg, 'CAT_NAME', 'CAT')
            cat_ticker = getattr(cfg, 'CAT_TICKER_ID', cat_id[:8])
            cats.append({
                "asset_id": cat_id,
                "name": cat_name,
                "ticker": cat_ticker,
                "ticker_id": cat_ticker,
                "wallet_id": getattr(cfg, 'CAT_WALLET_ID', 2),
                "decimals": getattr(cfg, 'CAT_DECIMALS', 3),
                "category": "ready",
                "volume_24h": 0,
            })

    return jsonify({"success": True, "cats": cats})


@bp.route("/api/cat/select", methods=["POST"])
def api_cat_select():
    """Select active CAT token — stores wallet_id so balance lookups work."""
    bot = api_server.bot
    cfg = api_server.cfg
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Invalid request body"}), 400
    asset_id = data.get("asset_id", "")
    wallet_id = data.get("wallet_id")
    name = data.get("name", "")
    decimals = data.get("decimals", 3)
    ticker_id = data.get("ticker_id", "")

    # Validate asset_id format (64 lowercase hex chars) BEFORE writing to .env.
    if asset_id:
        asset_id = str(asset_id).strip()
        if len(asset_id) != 64 or not all(c in '0123456789abcdefABCDEF' for c in asset_id):
            return jsonify({
                "success": False,
                "error": "CAT asset_id must be exactly 64 hex characters",
            }), 400
        asset_id = asset_id.lower()

        # Defensive guard: reject asset_id that isn't actually in the Sage wallet.
        try:
            from wallet import get_wallets as _get_wallets
            _wallets_resp = _get_wallets()
            if isinstance(_wallets_resp, dict) and _wallets_resp.get("success") is not False:
                _wallets = _wallets_resp.get("wallets") or []
                _known_asset_ids = set()
                for _w in _wallets:
                    _aid = str(_w.get("asset_id") or "").lower().replace("0x", "")
                    if len(_aid) == 64:
                        _known_asset_ids.add(_aid)
                if _known_asset_ids and asset_id not in _known_asset_ids:
                    return jsonify({
                        "success": False,
                        "error": (f"CAT asset_id not found in Sage wallet "
                                  f"(wallet has {len(_known_asset_ids)} known CAT"
                                  f"{'s' if len(_known_asset_ids) != 1 else ''}). "
                                  f"Receive the CAT first, or double-check the ID."),
                    }), 400
        except Exception as _wallet_check_err:
            log_event("warning", "cat_select_wallet_check_failed",
                      f"Could not verify asset_id against wallet "
                      f"({_wallet_check_err}) — proceeding without existence check")

    if name and len(str(name)) > 128:
        return jsonify({"success": False, "error": "CAT name too long"}), 400
    if ticker_id and len(str(ticker_id)) > 64:
        return jsonify({"success": False, "error": "Ticker ID too long"}), 400
    if decimals is not None:
        try:
            decimals = int(decimals)
            if decimals < 0 or decimals > 18:
                return jsonify({"success": False, "error": "Invalid decimals"}), 400
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "Invalid decimals"}), 400

    # Safety: never change the trading pair while the bot is running.
    try:
        if bot is not None and bot.is_running():
            return jsonify({
                "success": False,
                "error": "Stop the bot before changing the trading pair. "
                         "Switching CAT mid-run would cause offers for the wrong token."
            }), 409
    except Exception:
        pass

    with api_server._active_cat_lock:
        api_server._active_cat["asset_id"] = asset_id
        api_server._active_cat["name"] = name
        api_server._active_cat["decimals"] = int(decimals) if decimals else 3
        api_server._active_cat["ticker_id"] = ticker_id
        if wallet_id is not None:
            api_server._active_cat["wallet_id"] = int(wallet_id)

    # Persist to .env so it survives restarts.
    # CAT_WALLET_ID is intentionally NOT saved — it's resolved dynamically.
    if asset_id:
        cfg.update("CAT_ASSET_ID", asset_id)
    if name:
        cfg.update("CAT_NAME", name)
    if decimals:
        cfg.update("CAT_DECIMALS", str(int(decimals)))
    if ticker_id:
        cfg.update("CAT_TICKER_ID", ticker_id)

    # Reset risk manager so stale inventory/CB state doesn't leak into the new CAT.
    if bot is not None:
        try:
            if hasattr(bot, "risk_manager") and bot.risk_manager:
                bot.risk_manager.reset_session()
                log_event("info", "cat_switch_risk_reset",
                          f"Risk manager reset for CAT change to {name}")
        except Exception as e:
            log_event("warning", "cat_switch_risk_reset_failed",
                      f"Could not reset risk manager on CAT switch: {e}")

    # Auto-resolve TIBET_PAIR_ID for the newly selected CAT in the background.
    if asset_id:
        def _resolve_new_cat_tibet():
            try:
                import cat_resolver as _cr
                _cr._cache = None
                _cr._last_resolve_at = 0
                cfg.update("TIBET_PAIR_ID", "")
                meta = _cr.resolve_and_apply(cfg)
                if meta.get("pair_id"):
                    log_event("info", "cat_tibet_pair_resolved",
                              f"TIBET_PAIR_ID auto-resolved for {name}: "
                              f"{meta['pair_id'][:20]}...")
                    print(f"[CAT SELECT] TIBET_PAIR_ID resolved: {meta['pair_id'][:20]}...")
                else:
                    log_event("info", "cat_tibet_pair_not_found",
                              f"CAT {name} ({asset_id[:12]}...) has no TibetSwap pair — "
                              f"AMM monitoring disabled for this token")
            except Exception as e:
                log_event("warning", "cat_tibet_resolve_error",
                          f"TIBET_PAIR_ID auto-resolve failed after CAT select: {e}")
        threading.Thread(target=_resolve_new_cat_tibet, daemon=True,
                         name="cat-tibet-resolve").start()

    # Notify the Sage wallet adapter so _get_cat_asset_id() returns the new
    # asset ID immediately — without waiting for .env to be re-read.
    try:
        from wallet_sage import notify_cat_asset_id_changed
        notify_cat_asset_id_changed(asset_id)
    except Exception:
        pass

    print(f"🔄 CAT selected: {name} (wallet_id={wallet_id}, asset={asset_id[:12]}...)")
    log_event("info", "cat_selected", f"Trading pair selected: {name} (wallet {wallet_id})")
    return jsonify({"success": True, "asset_id": asset_id, "wallet_id": wallet_id})


@bp.route("/api/cat/refresh", methods=["POST"])
def api_cat_refresh():
    """Refresh CAT token list (re-read from config)."""
    cfg = api_server.cfg
    cfg.reload()
    return jsonify({"success": True})


@bp.route("/api/balances/refresh", methods=["POST"])
def api_balances_refresh():
    """Force refresh wallet balances and return them."""
    try:
        xch_bal = {"spendable": 0, "total": 0}
        cat_bal = {"spendable": 0, "total": 0}
        try:
            from wallet import get_wallet_balance, WALLET_ID_XCH
            xr = get_wallet_balance(WALLET_ID_XCH)
            if xr and xr.get("success"):
                wb = xr.get("wallet_balance") or {}
                xch_bal["total"] = api_server._safe_float(wb.get("confirmed_wallet_balance", 0)) / 1e12
                xch_bal["spendable"] = api_server._safe_float(wb.get("spendable_balance", 0)) / 1e12
            cat_wid = api_server._active_cat.get("wallet_id") or getattr(cfg, 'CAT_WALLET_ID', 2)
            cat_dec = api_server._active_cat.get("decimals") or getattr(cfg, 'CAT_DECIMALS', 3)
            cr = get_wallet_balance(cat_wid)
            if cr and cr.get("success"):
                wb = cr.get("wallet_balance") or {}
                cat_bal["total"] = api_server._safe_float(wb.get("confirmed_wallet_balance", 0)) / (10 ** cat_dec)
                cat_bal["spendable"] = api_server._safe_float(wb.get("spendable_balance", 0)) / (10 ** cat_dec)
        except Exception:
            pass
        return jsonify({
            "success": True,
            "balances": {
                "xch": xch_bal,
                "cat": cat_bal,
            }
        })
    except Exception as e:
        return api_server._api_error(e, request.path)
