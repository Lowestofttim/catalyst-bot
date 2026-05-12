import os
import tempfile
from decimal import Decimal

import database
import bot_loop
from config import cfg


def _reset_temp_db(path):
    database.DB_PATH = path
    if hasattr(database._local, "conn"):
        conn = getattr(database._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        database._local.conn = None
    database.init_database()


def test_recover_unknown_offers_reports_recovered_trade_ids(monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _reset_temp_db(db_path)
        cat_id = "catasset"
        trade_id = "recovered-trade-id"
        monkeypatch.setattr(cfg, "INNER_SIZE_XCH", Decimal("2.0"), raising=False)
        monkeypatch.setattr(cfg, "MID_SIZE_XCH", Decimal("3.0"), raising=False)
        monkeypatch.setattr(cfg, "OUTER_SIZE_XCH", Decimal("4.0"), raising=False)
        monkeypatch.setattr(cfg, "EXTREME_SIZE_XCH", Decimal("5.0"), raising=False)
        monkeypatch.setattr(cfg, "SNIPER_ENABLED", False, raising=False)
        monkeypatch.setattr(cfg, "CAT_DECIMALS", 3, raising=False)

        stats = database.recover_unknown_offers(
            [
                {
                    "trade_id": trade_id,
                    "summary": {
                        "offered": {"xch": 2_000_000_000_000},
                        "requested": {cat_id: 20_000_000},
                    },
                    "valid_times": {},
                }
            ],
            cat_id,
        )

        assert stats["recovered"] == 1
        assert stats["trade_ids"] == [trade_id]
    finally:
        if hasattr(database._local, "conn"):
            conn = getattr(database._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            database._local.conn = None
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_public_post_flush_reclaims_unsafe_offers_before_dexie(monkeypatch):
    events = []

    class FakeDexie:
        def __init__(self):
            self._queue = [{"trade_id": "unsafe"}]

        def flush_queue(self):
            events.append("dexie_flush")
            return {"posted": 0, "failed": 0, "skipped": 0}

    class FakeSplash:
        def __init__(self):
            self._queue = [{"trade_id": "unsafe"}]

        def flush_queue(self):
            events.append("splash_flush")
            return {"posted": 0, "failed": 0, "skipped": 0}

    loop = bot_loop.BotLoop.__new__(bot_loop.BotLoop)
    loop.dexie_manager = FakeDexie()
    loop.splash_manager = FakeSplash()
    loop._set_cycle_step = lambda step: events.append(step)
    loop._reclaim_oversized_locked_offers = lambda: events.append("reclaim") or True

    monkeypatch.setattr(bot_loop.cfg, "DEXIE_AUTO_POST", True, raising=False)
    monkeypatch.setattr(bot_loop.cfg, "SPLASH_ENABLED", True, raising=False)

    loop._flush_public_offer_queues()

    assert events.index("reclaim") < events.index("dexie_flush")
    assert events.index("reclaim") < events.index("splash_flush")


def test_public_post_flush_skips_reclaim_when_no_public_queue(monkeypatch):
    events = []

    class FakeDexie:
        _queue = []

        def flush_queue(self):
            events.append("dexie_flush")
            return {"posted": 0, "failed": 0, "skipped": 0}

    class FakeSplash:
        _queue = []

        def flush_queue(self):
            events.append("splash_flush")
            return {"posted": 0, "failed": 0, "skipped": 0}

    loop = bot_loop.BotLoop.__new__(bot_loop.BotLoop)
    loop.dexie_manager = FakeDexie()
    loop.splash_manager = FakeSplash()
    loop._set_cycle_step = lambda step: events.append(step)
    loop._reclaim_oversized_locked_offers = lambda: events.append("reclaim") or True

    monkeypatch.setattr(bot_loop.cfg, "DEXIE_AUTO_POST", True, raising=False)
    monkeypatch.setattr(bot_loop.cfg, "SPLASH_ENABLED", True, raising=False)

    loop._flush_public_offer_queues()

    assert "reclaim" not in events
    assert "dexie_flush" in events
    assert "splash_flush" in events
