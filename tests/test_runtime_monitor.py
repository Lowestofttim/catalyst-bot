import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

dotenv_stub = types.ModuleType("dotenv")
dotenv_stub.load_dotenv = lambda *args, **kwargs: None
dotenv_stub.set_key = lambda *args, **kwargs: None
sys.modules.setdefault("dotenv", dotenv_stub)

from runtime_monitor import RuntimeMonitor


class _FakeEvent:
    def wait(self, timeout=None):
        return True


class _FakeOfferManager:
    def __init__(self, fresh=True):
        self._meta = {
            "fresh": fresh,
            "using_cache": not fresh,
            "consecutive_failures": 0 if fresh else 2,
            "last_error": "" if fresh else "wallet unavailable",
        }

    def get_wallet_sync_meta(self):
        return dict(self._meta)


class _FakeMarketIntel:
    def __init__(self, summary=None, snapshot=None):
        self._summary = summary or {
            "best_bid": "0.00012000",
            "best_ask": "0.00012100",
            "orderbook_age_secs": 5,
            "orderbook_errors": 0,
            "orderbook_refreshes": 3,
        }
        self._snapshot = snapshot or {
            "buy_count": 50,
            "sell_count": 50,
            "our_buy_count": 30,
            "our_sell_count": 30,
            "our_best_bid": "0.00011990",
            "our_best_ask": "0.00012110",
        }

    def refresh_orderbook(self, force=False):
        return dict(self._summary)

    def get_market_summary(self):
        return dict(self._summary)

    def get_orderbook_snapshot(self):
        return dict(self._snapshot)


class _FakeCoinManager:
    def __init__(self, status=None, free_counts=None):
        self._status = status or {
            "xch_locked_coins": 30,
            "cat_locked_coins": 30,
            "prep_running": False,
            "topup_running": False,
            "inventory": {},
        }
        self._free_counts = free_counts or {
            "xch_spendable": 65,
            "cat_spendable": 40,
            "xch_free": 35,
            "cat_free": 10,
        }

    def get_status(self):
        return dict(self._status)

    def get_free_coin_counts(self, wallet_buy=0, wallet_sell=0):
        return dict(self._free_counts)


class _FakeDexieManager:
    def __init__(self, queue_size=0):
        self._queue_size = queue_size

    def get_stats(self):
        return {"queue_size": self._queue_size}


class _FakeBot:
    def __init__(self, *, wallet_buys=30, wallet_sells=30, fresh=True,
                 market_snapshot=None, coin_status=None, free_counts=None,
                 queue_size=0):
        self._startup_complete = _FakeEvent()
        self._running = True
        self._bot_state = {"open_buys": wallet_buys, "open_sells": wallet_sells}
        self._loop_count = 10
        self._last_loop_duration = 12.5
        self._current_mid_price = "0.00012050"
        self._start_time = time.time() - 300
        self.offer_manager = _FakeOfferManager(fresh=fresh)
        self.market_intel = _FakeMarketIntel(snapshot=market_snapshot)
        self.coin_manager = _FakeCoinManager(status=coin_status, free_counts=free_counts)
        self.dexie_manager = _FakeDexieManager(queue_size=queue_size)
        self.alerts = []
        self.cleared = []

    def _emit_alert(self, alert_id, severity, title, message):
        self.alerts.append((alert_id, severity, title, message))

    def _clear_alert(self, alert_id):
        self.cleared.append(alert_id)


def _open_offer_rows(buys, sells):
    rows = []
    rows.extend({"side": "buy", "tier": "mid"} for _ in range(buys))
    rows.extend({"side": "sell", "tier": "mid"} for _ in range(sells))
    return rows


def _tiered_open_offer_rows(buy_tiers=None, sell_tiers=None):
    rows = []
    buy_tiers = buy_tiers or {}
    sell_tiers = sell_tiers or {}
    for tier, count in buy_tiers.items():
        rows.extend({"side": "buy", "tier": tier} for _ in range(count))
    for tier, count in sell_tiers.items():
        rows.extend({"side": "sell", "tier": tier} for _ in range(count))
    return rows


class RuntimeMonitorTests(unittest.TestCase):
    def test_app_lifecycle_monitor_idles_cleanly_while_bot_is_stopped(self):
        bot = _FakeBot()
        bot._running = False
        monitor = RuntimeMonitor(bot)
        monitor.reset_session()

        with patch("runtime_monitor.log_event"):
            monitor.start()
            time.sleep(0.05)
            state = monitor.get_state()
            monitor.stop()

        self.assertEqual(state.get("status"), "idle")
        self.assertEqual(state.get("note"), "Waiting for bot start")

    def test_flags_dexie_visibility_gap_when_live_market_lags_wallet(self):
        bot = _FakeBot(
            market_snapshot={
                "buy_count": 50,
                "sell_count": 50,
                "our_buy_count": 27,
                "our_sell_count": 27,
                "our_best_bid": "0.00011990",
                "our_best_ask": "0.00012110",
            }
        )
        monitor = RuntimeMonitor(bot)
        monitor.reset_session()
        monitor._last_post_activity_at = time.time() - 600

        with patch("runtime_monitor.get_events_since", return_value=[]), \
             patch("runtime_monitor.get_open_offers", return_value=_open_offer_rows(30, 30)), \
             patch("runtime_monitor.log_event") as log_event_mock, \
             patch.object(monitor, "_resolve_superlog_path", return_value=""):
            monitor._run_once()
            monitor._run_once()

        state = monitor.get_state()
        active_codes = {item["code"] for item in state["active_conditions"]}
        self.assertIn("dexie_visibility_gap", active_codes)
        self.assertEqual(state["status"], "warning")
        self.assertTrue(any(call.args[1] == "bot_health_dexie_gap" for call in log_event_mock.call_args_list))

    def test_ignores_dexie_visibility_gap_when_orderbook_side_is_truncated(self):
        bot = _FakeBot(
            wallet_buys=40,
            wallet_sells=40,
            market_snapshot={
                "buy_count": 50,
                "sell_count": 50,
                "page_size": 50,
                "buy_truncated": True,
                "sell_truncated": False,
                "our_buy_count": 13,
                "our_sell_count": 40,
                "our_best_bid": "0.00011990",
                "our_best_ask": "0.00012110",
            }
        )
        monitor = RuntimeMonitor(bot)
        monitor.reset_session()
        monitor._last_post_activity_at = time.time() - 600

        with patch("runtime_monitor.get_events_since", return_value=[]), \
             patch("runtime_monitor.get_open_offers", return_value=_open_offer_rows(40, 40)), \
             patch("runtime_monitor.log_event") as log_event_mock, \
             patch.object(monitor, "_resolve_superlog_path", return_value=""):
            monitor._run_once()
            monitor._run_once()

        state = monitor.get_state()
        active_codes = {item["code"] for item in state["active_conditions"]}
        self.assertNotIn("dexie_visibility_gap", active_codes)
        self.assertFalse(any(call.args[1] == "bot_health_dexie_gap" for call in log_event_mock.call_args_list))

    def test_flags_topup_lag_when_coin_headroom_does_not_improve(self):
        bot = _FakeBot(
            coin_status={
                "xch_locked_coins": 30,
                "cat_locked_coins": 30,
                "prep_running": False,
                "topup_running": True,
                "inventory": {},
            },
            free_counts={
                "xch_spendable": 40,
                "cat_spendable": 40,
                "xch_free": 5,
                "cat_free": 5,
            },
        )
        monitor = RuntimeMonitor(bot)
        monitor.reset_session()
        monitor._topup_started_at = time.time() - 1200
        monitor._topup_baseline = {"xch_free": 5, "cat_free": 5}

        with patch("runtime_monitor.get_events_since", return_value=[]), \
             patch("runtime_monitor.get_open_offers", return_value=_open_offer_rows(30, 30)), \
             patch("runtime_monitor.log_event") as log_event_mock, \
             patch.object(monitor, "_resolve_superlog_path", return_value=""):
            monitor._run_once()

        state = monitor.get_state()
        active_codes = {item["code"] for item in state["active_conditions"]}
        self.assertIn("topup_lag", active_codes)
        self.assertTrue(any(call.args[1] == "bot_health_topup_lag" for call in log_event_mock.call_args_list))

    def test_flags_ladder_shape_drift_when_totals_are_full_but_extremes_missing(self):
        bot = _FakeBot(wallet_buys=30, wallet_sells=30)
        monitor = RuntimeMonitor(bot)
        monitor.reset_session()
        monitor._last_post_activity_at = time.time() - 600

        skewed_rows = _tiered_open_offer_rows(
            buy_tiers={"inner": 6, "mid": 15, "outer": 9},
            sell_tiers={"inner": 6, "mid": 17, "outer": 7},
        )

        with patch("runtime_monitor.get_events_since", return_value=[]), \
             patch("runtime_monitor.get_open_offers", return_value=skewed_rows), \
             patch("runtime_monitor.log_event") as log_event_mock, \
             patch.object(monitor, "_resolve_superlog_path", return_value=""), \
             patch("runtime_monitor.cfg.TIER_ENABLED", True), \
             patch("runtime_monitor.cfg.INNER_TIER_COUNT", 6), \
             patch("runtime_monitor.cfg.MID_TIER_COUNT", 12), \
             patch("runtime_monitor.cfg.OUTER_TIER_COUNT", 6), \
             patch("runtime_monitor.cfg.EXTREME_TIER_COUNT", 6):
            monitor._run_once()
            monitor._run_once()

        state = monitor.get_state()
        active_codes = {item["code"] for item in state["active_conditions"]}
        self.assertIn("ladder_shape_drift", active_codes)
        self.assertEqual(state["status"], "warning")

    def test_parses_repeated_slow_superlog_calls_into_perf_alert(self):
        bot = _FakeBot()
        monitor = RuntimeMonitor(bot)
        monitor.reset_session()

        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as handle:
            tmp_path = handle.name

        try:
            with patch.object(monitor, "_resolve_superlog_path", return_value=tmp_path):
                monitor._ingest_superlog()  # bootstrap to EOF
                with open(tmp_path, "a", encoding="utf-8") as handle:
                    handle.write(
                        "[   1.000s] [00:00:01.000] [bot-loop        ] [ WARN] [OFFER       ] "
                        "<<< sync_from_wallet :: time_ms=2555.9 | thread=bot-loop\n"
                    )
                    handle.write(
                        "[   2.000s] [00:00:02.000] [bot-loop        ] [ WARN] [OFFER       ] "
                        "<<< sync_from_wallet :: time_ms=2588.1 | thread=bot-loop\n"
                    )
                    handle.write(
                        "[   3.000s] [00:00:03.000] [bot-loop        ] [ WARN] [OFFER       ] "
                        "<<< sync_from_wallet :: time_ms=2610.4 | thread=bot-loop\n"
                    )
                monitor._ingest_superlog()

            with patch("runtime_monitor.get_open_offers", return_value=_open_offer_rows(30, 30)):
                snapshot = monitor._collect_snapshot()
            with patch("runtime_monitor.log_event"):
                active = monitor._evaluate(snapshot)

            active_codes = {item["code"] for item in active}
            self.assertIn("slow_runtime", active_codes)
            self.assertEqual(snapshot["performance"]["active_methods"][0]["method"], "sync_from_wallet")
        finally:
            os.remove(tmp_path)


if __name__ == "__main__":
    unittest.main()
