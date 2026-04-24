"""Python-side bridge exposed to JavaScript inside the PyWebView window

Defines `AppBridge`, whose methods are reachable from the GUI as
`window.pywebview.api.*`. Each bridge method mirrors a Flask route but invokes
the corresponding handler in-process through `test_request_context()`,
bypassing network I/O and the `before_request` hooks. This lets the desktop
GUI share a single implementation with the browser-mode HTTP surface while
avoiding serialization over localhost.

Key responsibilities:
    - Route JS calls to Flask handlers via in-process request contexts
    - Normalize results to JSON-safe shapes using `DecimalEncoder`
    - Catch all exceptions in `@_safe` and return `{"success": False, ...}`
    - Clip excess positional args injected by PyWebView's `undefined` serialization

Security note: because bridge calls do not traverse the HTTP layer, the
loopback and per-run token checks are skipped. The bridge trusts the GUI,
which must escape all server-sourced data before rendering (see `escapeHtml`).
"""

import inspect
import json
import traceback
from decimal import Decimal


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal -> str for lossless PyWebView serialization.

    Using str instead of float avoids precision loss for large mojo integers
    (float64 holds ~15.9 decimal digits; mojo values can exceed that).
    """
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


def _safe(func):
    """
    Decorator that wraps bridge methods in try/except.
    Returns {"success": False, "error": "..."} on any exception.
    This prevents JS from getting cryptic Python tracebacks.

    Also trims excess positional arguments so no-arg bridge methods
    don't crash when the JS side accidentally passes an argument.
    PyWebView serialises a JS ``undefined`` as a positional Python
    argument, which used to blow up every ``def get_foo(self):`` method
    with "takes 1 positional argument but 2 were given". We introspect
    the wrapped function once at decoration time, record the maximum
    number of positional args it accepts, and clip incoming ``args`` to
    that length unless the function declares ``*args`` itself.
    """
    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    _POSITIONAL_KINDS = (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    )
    max_positional = sum(1 for p in params if p.kind in _POSITIONAL_KINDS)
    has_var_positional = any(
        p.kind is inspect.Parameter.VAR_POSITIONAL for p in params
    )

    def wrapper(*args, **kwargs):
        try:
            if not has_var_positional and len(args) > max_positional:
                args = args[:max_positional]
            result = func(*args, **kwargs)
            # Recursively convert any Decimals in the result
            return json.loads(json.dumps(result, cls=DecimalEncoder))
        except Exception:
            traceback.print_exc()
            # Never leak raw exception messages (may contain paths, SQL, RPC URLs)
            # to the frontend — log the detail server-side only.
            return {
                "success": False,
                "error": "Internal error — check bot logs for details",
            }
    wrapper.__name__ = func.__name__
    return wrapper


class AppBridge:
    """
    JS bridge — exposes bot functionality to the PyWebView frontend.

    All public methods (no underscore prefix) are callable from JavaScript as:
        window.pywebview.api.method_name(args)
    """

    def __init__(self):
        """
        Initialize the bridge. Bot and modules are accessed lazily through
        api_server to avoid circular imports.
        """
        self._api = None

    @property
    def api(self):
        """Lazy import of api_server to avoid circular imports."""
        if self._api is None:
            import api_server
            self._api = api_server
        return self._api

    # -----------------------------------------------------------------------
    # Clipboard (WebView2 blocks execCommand paste — read from Python side)
    # -----------------------------------------------------------------------

    def read_clipboard(self):
        try:
            import ctypes
            CF_UNICODETEXT = 13
            # Must set restype to c_void_p — HANDLE is 64-bit on 64-bit Windows
            # and ctypes defaults to c_int (32-bit), truncating the pointer
            GetClipboardData = ctypes.windll.user32.GetClipboardData
            GetClipboardData.restype = ctypes.c_void_p
            GlobalLock = ctypes.windll.kernel32.GlobalLock
            GlobalLock.restype = ctypes.c_void_p
            GlobalLock.argtypes = [ctypes.c_void_p]
            GlobalUnlock = ctypes.windll.kernel32.GlobalUnlock
            GlobalUnlock.argtypes = [ctypes.c_void_p]
            if not ctypes.windll.user32.OpenClipboard(None):
                return {"success": False, "text": "", "error": "OpenClipboard failed"}
            try:
                h = GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return {"success": False, "text": "", "error": "No text in clipboard"}
                ptr = GlobalLock(h)
                if not ptr:
                    return {"success": False, "text": "", "error": "GlobalLock failed"}
                try:
                    text = ctypes.wstring_at(ptr)
                finally:
                    GlobalUnlock(h)
            finally:
                ctypes.windll.user32.CloseClipboard()
            return {"success": True, "text": text}
        except Exception as e:
            return {"success": False, "text": "", "error": str(e)}

    # -----------------------------------------------------------------------
    # Bot Control
    # -----------------------------------------------------------------------

    @_safe
    def start_bot(self, _body=None):
        """Start the bot loop. Maps to POST /api/bot/start.
        Delegates fully to api_server.api_bot_start() which runs all pre-start
        validation (CAT_ASSET_ID, wallet sync, spread sanity, etc.).
        """
        import api_server
        with api_server.app.test_request_context('/api/bot/start', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_bot_start()
        return _unwrap_flask_response(resp)

    @_safe
    def stop_bot(self, _body=None):
        """Stop the bot loop. Maps to POST /api/bot/stop."""
        api = self.api
        bot = api.bot
        if bot is None:
            return {"success": False, "error": "Bot not initialized"}
        bot.stop()
        try:
            api.events.emit("bot_control", {"action": "stopped"})
        except Exception:
            pass
        return {"status": "stopped"}

    @_safe
    def get_bot_state(self):
        """Get full bot state. Maps to GET /api/bot/state."""
        import api_server
        with api_server.app.test_request_context('/api/bot/state'):
            resp = api_server.api_bot_state()
        return _unwrap_flask_response(resp)

    @_safe
    def get_status(self):
        """
        Main polling endpoint. Maps to GET /api/status.
        Polled every 5 seconds by the GUI.
        """
        import api_server
        with api_server.app.test_request_context('/api/status'):
            resp = api_server.api_status()
        return _unwrap_flask_response(resp)

    @_safe
    def get_price(self):
        """Get current price data. Maps to GET /api/bot/price."""
        import api_server
        with api_server.app.test_request_context('/api/bot/price'):
            resp = api_server.api_bot_price()
        return _unwrap_flask_response(resp)

    @_safe
    def shutdown(self, _body=None):
        """Full shutdown. Maps to POST /api/shutdown."""
        import api_server
        with api_server.app.test_request_context('/api/shutdown', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_shutdown()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Config
    # -----------------------------------------------------------------------

    @_safe
    def get_config(self):
        """Get all configuration. Maps to GET /api/config."""
        import api_server
        with api_server.app.test_request_context('/api/config'):
            resp = api_server.api_config_get()
        return _unwrap_flask_response(resp)

    @_safe
    def update_config(self, body=None):
        """
        Update configuration. Maps to POST /api/config.
        body: dict with {key, value} or bulk settings dict.
        """
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/config', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_config_update()
        return _unwrap_flask_response(resp)

    @_safe
    def live_config(self, body=None):
        """Apply a live config change. Maps to POST /api/config/live."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/config/live', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_config_live()
        return _unwrap_flask_response(resp)

    @_safe
    def reload_config(self, _body=None):
        """Reload config from disk. Maps to POST /api/config/reload."""
        import api_server
        with api_server.app.test_request_context('/api/config/reload', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_config_reload()
        return _unwrap_flask_response(resp)

    @_safe
    def apply_config(self, body=None):
        """Apply config changes. Maps to POST /api/config/apply."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/config/apply', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_config_apply()
        return _unwrap_flask_response(resp)

    @_safe
    def validate_config(self):
        """Validate current config. Maps to GET /api/config/validate."""
        import api_server
        with api_server.app.test_request_context('/api/config/validate'):
            resp = api_server.api_config_validate()
        return _unwrap_flask_response(resp)

    @_safe
    def get_fees_status(self):
        """Get fee settings. Maps to GET /api/fees/status."""
        import api_server
        with api_server.app.test_request_context('/api/fees/status'):
            resp = api_server.api_fees_status()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Settings
    # -----------------------------------------------------------------------

    @_safe
    def get_settings_defaults(self):
        """Get default settings. Maps to GET /api/settings/defaults."""
        import api_server
        with api_server.app.test_request_context('/api/settings/defaults'):
            resp = api_server.api_settings_defaults()
        return _unwrap_flask_response(resp)

    @_safe
    def validate_settings(self, body=None):
        """Validate settings before saving. Maps to POST /api/settings/validate."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/settings/validate', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_settings_validate()
        return _unwrap_flask_response(resp)

    @_safe
    def get_smart_defaults(self, params=None):
        """Get smart defaults. Maps to GET /api/smart-defaults."""
        import api_server
        qs = ''
        if params and isinstance(params, dict):
            from urllib.parse import urlencode
            qs = '?' + urlencode(params)
        with api_server.app.test_request_context('/api/smart-defaults' + qs):
            resp = api_server.api_smart_defaults()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Dashboard
    # -----------------------------------------------------------------------

    @_safe
    def get_dashboard(self):
        """Get full dashboard data. Maps to GET /api/dashboard."""
        import api_server
        with api_server.app.test_request_context('/api/dashboard'):
            resp = api_server.api_dashboard()
        return _unwrap_flask_response(resp)

    @_safe
    def get_inventory(self):
        """Get inventory state. Maps to GET /api/inventory."""
        import api_server
        with api_server.app.test_request_context('/api/inventory'):
            resp = api_server.api_inventory()
        return _unwrap_flask_response(resp)

    @_safe
    def get_risk_spreads(self):
        """Get adjusted spreads. Maps to GET /api/risk/spreads."""
        import api_server
        with api_server.app.test_request_context('/api/risk/spreads'):
            resp = api_server.api_risk_spreads()
        return _unwrap_flask_response(resp)

    @_safe
    def get_stats(self):
        """Get trading statistics. Maps to GET /api/stats."""
        import api_server
        with api_server.app.test_request_context('/api/stats'):
            resp = api_server.api_stats()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Offers
    # -----------------------------------------------------------------------

    @_safe
    def get_offers(self):
        """Get active offers. Maps to GET /api/offers."""
        import api_server
        with api_server.app.test_request_context('/api/offers'):
            resp = api_server.api_offers()
        return _unwrap_flask_response(resp)

    @_safe
    def cancel_all_offers(self, _body=None):
        """Cancel all offers. Maps to POST /api/offers/cancel_all."""
        import api_server
        with api_server.app.test_request_context('/api/offers/cancel_all', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_cancel_all()
        return _unwrap_flask_response(resp)

    @_safe
    def get_cancel_all_status(self):
        """Get cancel-all progress. Maps to GET /api/offers/cancel_all/status."""
        import api_server
        with api_server.app.test_request_context('/api/offers/cancel_all/status'):
            resp = api_server.api_cancel_all_status()
        return _unwrap_flask_response(resp)

    @_safe
    def cancel_offer(self, body=None):
        """Cancel a single offer. Maps to POST /api/offers/cancel."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/offers/cancel', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_cancel_offer()
        return _unwrap_flask_response(resp)

    @_safe
    def cleanup_orphans(self, _body=None):
        """Clean up orphaned offers. Maps to POST /api/offers/cleanup_orphans."""
        import api_server
        with api_server.app.test_request_context('/api/offers/cleanup_orphans', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_cleanup_orphans()
        return _unwrap_flask_response(resp)

    @_safe
    def get_offers_diagnostic(self):
        """Get offer diagnostics. Maps to GET /api/offers/diagnostic."""
        import api_server
        with api_server.app.test_request_context('/api/offers/diagnostic'):
            resp = api_server.api_offers_diagnostic()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Fills & PnL
    # -----------------------------------------------------------------------

    @_safe
    def get_fills(self):
        """Get fill history. Maps to GET /api/fills."""
        import api_server
        with api_server.app.test_request_context('/api/fills'):
            resp = api_server.api_fills()
        return _unwrap_flask_response(resp)

    @_safe
    def purge_fills(self, _body=None):
        """Purge fill history. Maps to POST /api/fills/purge."""
        import api_server
        with api_server.app.test_request_context('/api/fills/purge', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_purge_fills()
        return _unwrap_flask_response(resp)

    @_safe
    def export_fills(self):
        """Export fills to CSV. Maps to GET /api/fills/export.
        Returns a data URL in desktop mode so JS can trigger download."""
        import api_server
        import base64
        with api_server.app.test_request_context('/api/fills/export'):
            resp = api_server.api_fills_export()
        # If it's a file response, encode as data URL for JS download
        try:
            if hasattr(resp, 'data'):
                b64 = base64.b64encode(resp.data).decode('utf-8')
                ct = resp.content_type or 'text/csv'
                return {"success": True, "data_url": f"data:{ct};base64,{b64}",
                        "filename": "fills_export.csv"}
        except Exception:
            pass
        return _unwrap_flask_response(resp)

    @_safe
    def get_pnl(self):
        """Get PnL summary. Maps to GET /api/pnl."""
        import api_server
        with api_server.app.test_request_context('/api/pnl'):
            resp = api_server.api_pnl()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Session
    # -----------------------------------------------------------------------

    @_safe
    def fresh_start(self, body=None):
        """Clear session state. Maps to POST /api/session/fresh-start."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/session/fresh-start', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_session_fresh_start()
        return _unwrap_flask_response(resp)

    @_safe
    def check_resume(self):
        """Check if previous session can be resumed. Maps to GET /api/check-resume."""
        import api_server
        with api_server.app.test_request_context('/api/check-resume'):
            resp = api_server.api_check_resume()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Coins
    # -----------------------------------------------------------------------

    @_safe
    def get_coins(self):
        """Get coin status. Maps to GET /api/coins."""
        import api_server
        with api_server.app.test_request_context('/api/coins'):
            resp = api_server.api_coins()
        return _unwrap_flask_response(resp)

    @_safe
    def trigger_topup(self, _body=None):
        """Trigger coin topup. Maps to POST /api/coins/topup."""
        import api_server
        with api_server.app.test_request_context('/api/coins/topup', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_coin_topup()
        return _unwrap_flask_response(resp)

    @_safe
    def get_coin_prep_status(self):
        """Get coin prep status. Maps to GET /api/coin-prep/status."""
        import api_server
        with api_server.app.test_request_context('/api/coin-prep/status'):
            resp = api_server.api_coin_prep_status()
        return _unwrap_flask_response(resp)

    @_safe
    def trigger_coin_prep(self, _body=None):
        """Trigger coin prep. Maps to POST /api/coin-prep/trigger."""
        import api_server
        with api_server.app.test_request_context('/api/coin-prep/trigger', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_coin_prep_trigger()
        return _unwrap_flask_response(resp)

    @_safe
    def reset_coin_prep(self, _body=None):
        """Reset coin prep state. Maps to POST /api/coin-prep/reset."""
        import api_server
        with api_server.app.test_request_context('/api/coin-prep/reset', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_coin_prep_reset()
        return _unwrap_flask_response(resp)

    @_safe
    def verify_coin_prep(self, params=None):
        """Verify coin prep. Maps to GET /api/coin-prep/verify."""
        import api_server
        qs = ''
        if params and isinstance(params, dict):
            from urllib.parse import urlencode
            qs = '?' + urlencode(params)
        with api_server.app.test_request_context('/api/coin-prep/verify' + qs):
            resp = api_server.api_coin_prep_verify()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Boost
    # -----------------------------------------------------------------------

    @_safe
    def get_boost_state(self):
        """Get boost state. Maps to GET /api/boost/state."""
        import api_server
        with api_server.app.test_request_context('/api/boost/state'):
            resp = api_server.api_boost_state()
        return _unwrap_flask_response(resp)

    @_safe
    def activate_boost(self, body=None):
        """Activate boost. Maps to POST /api/boost/activate."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/boost/activate', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_boost_activate()
        return _unwrap_flask_response(resp)

    @_safe
    def deactivate_boost(self, _body=None):
        """Deactivate boost. Maps to POST /api/boost/deactivate."""
        import api_server
        with api_server.app.test_request_context('/api/boost/deactivate', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_boost_deactivate()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Market Intel
    # -----------------------------------------------------------------------

    @_safe
    def get_market_intel(self):
        """Get market intelligence. Maps to GET /api/market/intel."""
        import api_server
        with api_server.app.test_request_context('/api/market/intel'):
            resp = api_server.api_market_intel()
        return _unwrap_flask_response(resp)

    @_safe
    def get_market_summary(self):
        """Get market summary. Maps to GET /api/market/summary."""
        import api_server
        with api_server.app.test_request_context('/api/market/summary'):
            resp = api_server.api_market_summary()
        return _unwrap_flask_response(resp)

    @_safe
    def get_market_slippage(self, params=None):
        """Get market slippage. Maps to GET /api/market/slippage."""
        import api_server
        qs = ''
        if params and isinstance(params, dict):
            from urllib.parse import urlencode
            qs = '?' + urlencode(params)
        with api_server.app.test_request_context('/api/market/slippage' + qs):
            resp = api_server.api_market_slippage()
        return _unwrap_flask_response(resp)

    @_safe
    def get_market_orderbook(self):
        """Get market orderbook. Maps to GET /api/market/orderbook."""
        import api_server
        with api_server.app.test_request_context('/api/market/orderbook'):
            resp = api_server.api_market_orderbook()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Alerts
    # -----------------------------------------------------------------------

    @_safe
    def get_alerts(self):
        """Get active alerts. Maps to GET /api/alerts."""
        import api_server
        with api_server.app.test_request_context('/api/alerts'):
            resp = api_server.api_alerts()
        return _unwrap_flask_response(resp)

    @_safe
    def dismiss_alert(self, body=None):
        """Dismiss an alert. Maps to POST /api/alerts/dismiss."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/alerts/dismiss', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_dismiss_alert()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Logs
    # -----------------------------------------------------------------------

    @_safe
    def get_logs(self, params=None):
        """Get recent logs. Maps to GET /api/logs."""
        import api_server
        qs = ''
        if params and isinstance(params, dict):
            from urllib.parse import urlencode
            qs = '?' + urlencode(params)
        with api_server.app.test_request_context('/api/logs' + qs):
            resp = api_server.api_logs()
        return _unwrap_flask_response(resp)

    @_safe
    def clear_logs(self, _body=None):
        """Clear logs. Maps to POST /api/logs/clear."""
        import api_server
        with api_server.app.test_request_context('/api/logs/clear', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_logs_clear()
        return _unwrap_flask_response(resp)

    @_safe
    def download_logs(self):
        """Download debug bundle. Maps to GET /api/logs/download.

        WebView2 (PyWebView on Windows) does not support the a.download
        anchor trick for data: URLs or blob: URLs — the click is silently
        swallowed and no save dialog appears.  Instead we write the ZIP
        directly to the user's Downloads folder and open it in Explorer /
        Finder / file-manager so they can see it immediately.

        Returns {"success": True, "saved_to": "<abs path>", "filename": "<name>"}
        so the JS can show a toast with the exact file location.
        """
        import api_server
        import re
        import pathlib
        import subprocess
        import sys as _sys
        from datetime import datetime, timezone
        with api_server.app.test_request_context('/api/logs/download'):
            resp = api_server.api_logs_download()
        try:
            if hasattr(resp, 'data') and resp.data:
                # Resolve filename from Content-Disposition header
                filename = None
                try:
                    cd = resp.headers.get('Content-Disposition', '') if hasattr(resp, 'headers') else ''
                    m = re.search(r'filename=([^;]+)', cd or '')
                    if m:
                        filename = m.group(1).strip().strip('"')
                except Exception:
                    filename = None
                if not filename:
                    filename = 'bot_debug_bundle_' + datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S') + '.zip'

                # Save to user Downloads folder (falls back to home dir)
                try:
                    downloads = pathlib.Path.home() / 'Downloads'
                    downloads.mkdir(parents=True, exist_ok=True)
                except Exception:
                    downloads = pathlib.Path.home()
                save_path = downloads / filename
                with open(save_path, 'wb') as fh:
                    fh.write(resp.data)

                # Open the containing folder and highlight the file (best-effort)
                try:
                    if _sys.platform == 'win32':
                        # /select, highlights the file in Explorer
                        subprocess.Popen(['explorer', '/select,', str(save_path)])
                    elif _sys.platform == 'darwin':
                        subprocess.Popen(['open', '-R', str(save_path)])
                    else:
                        subprocess.Popen(['xdg-open', str(downloads)])
                except Exception:
                    pass

                return {"success": True, "saved_to": str(save_path), "filename": filename}
        except Exception:
            pass
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Health & Doctor
    # -----------------------------------------------------------------------

    @_safe
    def get_health(self):
        """Health check. Maps to GET /api/health."""
        import api_server
        with api_server.app.test_request_context('/api/health'):
            resp = api_server.api_health()
        return _unwrap_flask_response(resp)

    @_safe
    def run_doctor(self, params=None):
        """Run preflight checks. Maps to GET /api/doctor."""
        import api_server
        qs = ''
        if params and isinstance(params, dict):
            from urllib.parse import urlencode
            qs = '?' + urlencode(params)
        with api_server.app.test_request_context('/api/doctor' + qs):
            resp = api_server.api_doctor()
        return _unwrap_flask_response(resp)

    @_safe
    def get_reservations(self):
        """Get capacity reservations. Maps to GET /api/reservations."""
        import api_server
        with api_server.app.test_request_context('/api/reservations'):
            resp = api_server.api_reservations()
        return _unwrap_flask_response(resp)

    @_safe
    def get_runtime_diagnostics(self):
        """Get runtime diagnostics. Maps to GET /api/diagnostics/runtime."""
        import api_server
        with api_server.app.test_request_context('/api/diagnostics/runtime'):
            resp = api_server.api_runtime_diagnostics()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Wallet / CAT
    # -----------------------------------------------------------------------

    @_safe
    def get_fingerprint(self):
        """Get wallet fingerprint. Maps to GET /api/fingerprint."""
        import api_server
        with api_server.app.test_request_context('/api/fingerprint'):
            resp = api_server.api_fingerprint()
        return _unwrap_flask_response(resp)

    @_safe
    def get_cats(self):
        """List available CATs. Maps to GET /api/cats."""
        import api_server
        with api_server.app.test_request_context('/api/cats'):
            resp = api_server.api_cats()
        return _unwrap_flask_response(resp)

    @_safe
    def select_cat(self, body=None):
        """Select active CAT. Maps to POST /api/cat/select."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/cat/select', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_cat_select()
        return _unwrap_flask_response(resp)

    @_safe
    def refresh_cat(self, _body=None):
        """Refresh CAT data. Maps to POST /api/cat/refresh."""
        import api_server
        with api_server.app.test_request_context('/api/cat/refresh', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_cat_refresh()
        return _unwrap_flask_response(resp)

    @_safe
    def refresh_balances(self, _body=None):
        """Refresh wallet balances. Maps to POST /api/balances/refresh."""
        import api_server
        with api_server.app.test_request_context('/api/balances/refresh', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_balances_refresh()
        return _unwrap_flask_response(resp)

    @_safe
    def is_sage_running(self):
        """Check if Sage RPC is available. Maps to GET /api/wallet/sage-running."""
        import api_server
        with api_server.app.test_request_context('/api/wallet/sage-running'):
            resp = api_server.api_wallet_sage_running()
        return _unwrap_flask_response(resp)

    @_safe
    def begin_startup(self, body=None):
        """Begin wallet startup. Maps to POST /api/wallet/begin-startup."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/wallet/begin-startup', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_wallet_begin_startup()
        return _unwrap_flask_response(resp)

    @_safe
    def get_startup_status(self):
        """Get wallet startup status. Maps to GET /api/sage/startup-status."""
        import api_server
        with api_server.app.test_request_context('/api/sage/startup-status'):
            resp = api_server.api_chia_startup_status()
        return _unwrap_flask_response(resp)

    @_safe
    def get_fingerprints(self):
        """List wallet fingerprints. Maps to GET /api/sage/fingerprints."""
        import api_server
        with api_server.app.test_request_context('/api/sage/fingerprints'):
            resp = api_server.api_chia_fingerprints()
        return _unwrap_flask_response(resp)

    @_safe
    def start_with_fingerprint(self, body=None):
        """Start with fingerprint. Maps to POST /api/sage/start-with-fingerprint."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/sage/start-with-fingerprint', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_chia_start_with_fingerprint()
        return _unwrap_flask_response(resp)

    @_safe
    def setup_certs(self, body=None):
        """Setup Sage certificates. Maps to POST /api/sage/setup-certs."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/sage/setup-certs', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_sage_setup_certs()
        return _unwrap_flask_response(resp)

    @_safe
    def restart_sage(self, _body=None):
        """Restart Sage daemon. Sage must be restarted manually by the user."""
        return {
            "success": False,
            "message": "Sage must be restarted manually. Close and reopen the Sage application.",
        }

    # -----------------------------------------------------------------------
    # Dexie / Price
    # -----------------------------------------------------------------------

    @_safe
    def get_price_info(self):
        """Get price from all sources. Maps to GET /api/price."""
        import api_server
        with api_server.app.test_request_context('/api/price'):
            resp = api_server.api_price()
        return _unwrap_flask_response(resp)

    @_safe
    def get_dexie_stats(self):
        """Get Dexie statistics. Maps to GET /api/dexie/stats."""
        import api_server
        with api_server.app.test_request_context('/api/dexie/stats'):
            resp = api_server.api_dexie_stats()
        return _unwrap_flask_response(resp)

    @_safe
    def repost_dexie(self, _body=None):
        """Repost to Dexie. Maps to POST /api/dexie/repost."""
        import api_server
        with api_server.app.test_request_context('/api/dexie/repost', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_dexie_repost()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Splash Network
    # -----------------------------------------------------------------------

    @_safe
    def get_splash_stats(self):
        """Get Splash stats. Maps to GET /api/splash/stats."""
        import api_server
        with api_server.app.test_request_context('/api/splash/stats'):
            resp = api_server.api_splash_stats()
        return _unwrap_flask_response(resp)

    @_safe
    def get_splash_node(self):
        """Get Splash node status. Maps to GET /api/splash/node."""
        import api_server
        with api_server.app.test_request_context('/api/splash/node'):
            resp = api_server.api_splash_node()
        return _unwrap_flask_response(resp)

    @_safe
    def check_splash_setup(self):
        """Check Splash setup. Maps to GET /api/splash/setup/check."""
        import api_server
        with api_server.app.test_request_context('/api/splash/setup/check'):
            resp = api_server.api_splash_setup_check()
        return _unwrap_flask_response(resp)

    @_safe
    def download_splash_setup(self, _body=None):
        """Download Splash binary. Maps to POST /api/splash/setup/download."""
        import api_server
        with api_server.app.test_request_context('/api/splash/setup/download', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_splash_setup_download()
        return _unwrap_flask_response(resp)

    @_safe
    def get_splash_setup_progress(self):
        """Get Splash download progress. Maps to GET /api/splash/setup/progress."""
        import api_server
        with api_server.app.test_request_context('/api/splash/setup/progress'):
            resp = api_server.api_splash_setup_progress()
        return _unwrap_flask_response(resp)

    @_safe
    def start_splash_node(self, _body=None):
        """Start Splash node. Maps to POST /api/splash/node/start."""
        import api_server
        with api_server.app.test_request_context('/api/splash/node/start', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_splash_node_start()
        return _unwrap_flask_response(resp)

    @_safe
    def get_splash_receive(self):
        """Get Splash receive stats. Maps to GET /api/splash/receive."""
        import api_server
        with api_server.app.test_request_context('/api/splash/receive'):
            resp = api_server.api_splash_receive()
        return _unwrap_flask_response(resp)

    @_safe
    def set_splash_receive(self, body=None):
        """Set Splash receive enabled. Maps to POST /api/splash/receive."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/splash/receive', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_splash_receive()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Spacescan
    # -----------------------------------------------------------------------

    @_safe
    def get_spacescan_status(self):
        """Get Spacescan status. Maps to GET /api/spacescan/status."""
        import api_server
        with api_server.app.test_request_context('/api/spacescan/status'):
            resp = api_server.api_spacescan_status()
        return _unwrap_flask_response(resp)

    @_safe
    def setup_spacescan(self, body=None):
        """Setup Spacescan. Maps to POST /api/spacescan/setup."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/spacescan/setup', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_spacescan_setup()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # Console
    # -----------------------------------------------------------------------

    @_safe
    def get_console_status(self):
        """Get console status. Maps to GET /api/console/status."""
        import api_server
        with api_server.app.test_request_context('/api/console/status'):
            resp = api_server.api_console_status()
        return _unwrap_flask_response(resp)

    @_safe
    def toggle_console(self, _body=None):
        """Toggle console. Maps to POST /api/console/toggle."""
        import api_server
        with api_server.app.test_request_context('/api/console/toggle', method='POST',
                                                  content_type='application/json',
                                                  data='{}'):
            resp = api_server.api_console_toggle()
        return _unwrap_flask_response(resp)

    # -----------------------------------------------------------------------
    # App Info (desktop-specific)
    # -----------------------------------------------------------------------

    @_safe
    def get_app_info(self):
        """Return app metadata for the titlebar / about dialog."""
        return {
            "name": "CATalyst",
            "version": "4.0.0",
            "mode": "desktop",
            "platform": __import__("sys").platform,
        }

    # -----------------------------------------------------------------------
    # Window management (called from custom titlebar)
    # -----------------------------------------------------------------------

    @_safe
    def confirm_close_window(self):
        """Mark the window as 'confirmed for close' and trigger it.

        The shutdown modal in the GUI calls this after the user clicks
        the final 'Shutdown App' button. It flips the _state.confirmed_close
        flag in desktop_app so the on_closing() hook lets the close go
        through instead of re-opening the modal.

        Safety guard: the bot must already be stopped. This prevents any
        untrusted JS (e.g. via an XSS-injected payload) from destroying
        the window while the bot is actively trading, which would lose
        the user's chance to cancel open offers.
        """
        try:
            # Gate: refuse while the bot is still running — the shutdown
            # modal is expected to have called /api/bot/stop (and waited)
            # before invoking this.
            try:
                import api_server
                if api_server.bot and getattr(api_server.bot, "_running", False):
                    return {
                        "success": False,
                        "error": "Bot is still running. Stop the bot before closing the window.",
                    }
            except Exception:
                # If we can't even check, fall through and let the close
                # proceed — the modal shouldn't have reached this point
                # unless the rest of the shutdown sequence already ran.
                pass

            import desktop_app as _da
            if hasattr(_da, "_state"):
                _da._state["confirmed_close"] = True
                # Persist window geometry now — destroy() may bypass the
                # on_closing hook depending on PyWebView backend.
                try:
                    win = _da._state.get("window")
                    if win and hasattr(_da, "_save_window_state"):
                        _da._save_window_state(win)
                except Exception:
                    pass
            import webview
            if webview.windows:
                webview.windows[0].destroy()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @_safe
    def minimize_window(self):
        """Minimize the window."""
        try:
            import webview
            if webview.windows:
                webview.windows[0].minimize()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @_safe
    def maximize_window(self):
        """Toggle maximize/restore."""
        try:
            import webview
            if webview.windows:
                webview.windows[0].toggle_fullscreen()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @_safe
    def resize_window(self, width=0, height=0):
        """Resize window to absolute width/height."""
        try:
            import webview
            if webview.windows:
                webview.windows[0].resize(int(width), int(height))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @_safe
    def get_window_size(self):
        """Get current window width/height."""
        try:
            import webview
            if webview.windows:
                win = webview.windows[0]
                return {"success": True, "width": win.width, "height": win.height}
            return {"success": False, "error": "No window"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @_safe
    def move_window(self, x=0, y=0):
        """Move window to absolute x/y position."""
        try:
            import webview
            if webview.windows:
                webview.windows[0].move(int(x), int(y))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @_safe
    def get_window_pos(self):
        """Get current window x/y position."""
        try:
            import webview
            if webview.windows:
                win = webview.windows[0]
                return {"success": True, "x": win.x, "y": win.y,
                        "width": win.width, "height": win.height}
            return {"success": False, "error": "No window"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @_safe
    def close_window(self):
        """Close the window."""
        try:
            import webview
            if webview.windows:
                webview.windows[0].destroy()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @_safe
    def open_external(self, body=None):
        """Open external URL in system browser. Maps to POST /api/open-external."""
        import api_server
        body_json = json.dumps(body or {})
        with api_server.app.test_request_context('/api/open-external', method='POST',
                                                  content_type='application/json',
                                                  data=body_json):
            resp = api_server.api_open_external()
        return _unwrap_flask_response(resp)


# ---------------------------------------------------------------------------
# Helper: unwrap a Flask response object to a plain dict
# ---------------------------------------------------------------------------

def _unwrap_flask_response(resp):
    """
    Convert a Flask response (jsonify result, tuple, or Response) to a plain dict
    so PyWebView can serialize it to the JS side.

    Flask route functions return:
      - Response object (from jsonify)
      - tuple: (Response, status_code)
      - tuple: (Response, status_code, headers)
    """
    import json as _json

    # Unwrap tuples
    if isinstance(resp, tuple):
        resp = resp[0]

    # If it's a Response object, extract JSON data
    if hasattr(resp, 'get_json'):
        try:
            data = resp.get_json(force=True, silent=True)
            if data is not None:
                return data
        except Exception:
            pass

    if hasattr(resp, 'data'):
        try:
            return _json.loads(resp.data)
        except Exception:
            pass

    if hasattr(resp, 'json'):
        try:
            return resp.json()
        except Exception:
            pass

    # Last resort — return as-is if it's already dict/list
    if isinstance(resp, (dict, list)):
        return resp

    return {"success": False, "error": f"Could not parse response: {type(resp).__name__}"}

