import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd() / "src" / "catalyst"))

import bot_loop


class BotLoopDailyReconcileTests(unittest.TestCase):
    def test_daily_reconcile_scopes_backfill_to_fresh_run_cutoff(self):
        cutoff = "2026-04-30 19:09:43"
        host = types.SimpleNamespace(_last_daily_reconcile_at=0)
        calls = []

        def fake_backfill(*args, **kwargs):
            calls.append((args, kwargs))
            return []

        with (
            patch.object(bot_loop.cfg, "RUN_HISTORY_CUTOFF", cutoff, create=True),
            patch.object(bot_loop.cfg, "SPACESCAN_ENABLED", False),
            patch.object(
                bot_loop, "backfill_verified_fills_from_offers", fake_backfill
            ),
            patch.object(bot_loop, "get_all_offers", return_value=[]),
            patch("database.get_open_offers", return_value=[]),
            patch.object(bot_loop, "log_event"),
        ):
            bot_loop.BotLoop._maybe_run_daily_reconcile(host)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1].get("since"), cutoff)

    def test_state_recent_fills_scoped_to_fresh_run_cutoff(self):
        cutoff = "2026-04-30 19:09:43"
        host = object.__new__(bot_loop.BotLoop)
        host._init_complete = True
        host._state_lock = threading.RLock()
        host._bot_state = {"running": True}
        host._last_loop_duration = 0
        host._chia_health = {}
        host._watcher_lock = threading.RLock()
        host._watcher_data = {}
        host._current_mid_price = "0"
        host.coin_manager = types.SimpleNamespace(get_status=lambda: {})
        host.risk_manager = types.SimpleNamespace(get_inventory_state=lambda: {})
        host.dexie_manager = types.SimpleNamespace(get_stats=lambda: {})
        host.fill_tracker = types.SimpleNamespace(
            get_fill_counts=lambda: {"buy": 0, "sell": 0}
        )
        host.sniper = types.SimpleNamespace(get_stats=lambda: {})
        host.market_intel = types.SimpleNamespace(get_stats=lambda: {})
        host.runtime_monitor = types.SimpleNamespace(get_state=lambda: {})
        host.splash_manager = types.SimpleNamespace(get_stats=lambda: {})
        host.splash_node = types.SimpleNamespace(get_status=lambda: {})
        host.coinset_client = types.SimpleNamespace(get_stats=lambda: {})
        host._recovery_state = {}
        calls = []

        def fake_get_fills(*args, **kwargs):
            calls.append((args, kwargs))
            return [{"trade_id": "fresh-only"}]

        with (
            patch.object(bot_loop.cfg, "CAT_ASSET_ID", "asset-test", create=True),
            patch.object(bot_loop.cfg, "RUN_HISTORY_CUTOFF", cutoff, create=True),
            patch.object(bot_loop.cfg, "LOOP_SECONDS", 30, create=True),
            patch.object(bot_loop.cfg, "DRY_RUN", False, create=True),
            patch("database.get_fills", fake_get_fills),
            patch.object(bot_loop, "get_stats", return_value={}),
            patch.object(bot_loop.BotLoop, "_get_requote_diagnostics", return_value={}),
            patch.object(bot_loop.BotLoop, "get_splash_receive_stats", return_value={}),
        ):
            state = bot_loop.BotLoop.get_state(host)

        self.assertEqual(state["fills"]["recent"], [{"trade_id": "fresh-only"}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1].get("since"), cutoff)


if __name__ == "__main__":
    unittest.main()
