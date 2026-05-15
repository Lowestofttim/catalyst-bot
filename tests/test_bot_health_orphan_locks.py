"""Tests for bot_health.check_orphan_locks() — finds DB-locked coins
whose trade_id points to no open offer and frees them.
"""

import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock


def _ensure_stubs():
    if "dotenv" not in sys.modules:
        d = types.ModuleType("dotenv")
        d.load_dotenv = lambda *a, **kw: None
        d.set_key = lambda *a, **kw: None
        sys.modules["dotenv"] = d
    if "requests" not in sys.modules:
        r = types.ModuleType("requests")

        class _Resp:
            status_code = 200

            def json(self):
                return {}

            def raise_for_status(self):
                pass

        class _Session:
            headers = {}

            def get(self, *a, **kw):
                return _Resp()

            def mount(self, *a, **kw):
                pass

        r.get = lambda *a, **kw: _Resp()
        r.Session = _Session
        r.exceptions = types.SimpleNamespace(
            Timeout=Exception, ConnectionError=Exception
        )
        a = types.ModuleType("requests.adapters")
        a.HTTPAdapter = object
        r.adapters = a
        sys.modules["requests"] = r
        sys.modules["requests.adapters"] = a
    if "urllib3" not in sys.modules:
        u = types.ModuleType("urllib3")
        u.Retry = object
        u.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
        u.disable_warnings = lambda *a, **kw: None
        sys.modules["urllib3"] = u


_ensure_stubs()
import bot_health  # noqa: E402


class _ModuleStubMixin:
    _STUBBED_NAMES = ("database", "dexie_manager", "wallet")

    def setUp(self):
        self._saved = {
            n: sys.modules[n] for n in self._STUBBED_NAMES if n in sys.modules
        }
        bot_health._last_run_lock_ts = 0.0
        bot_health._last_report = None

    def tearDown(self):
        for n in self._STUBBED_NAMES:
            sys.modules.pop(n, None)
            if n in self._saved:
                sys.modules[n] = self._saved[n]


def _orphan(coin_id, last_seen=None, trade_id=None, wt="cat"):
    return {
        "coin_id": coin_id,
        "wallet_type": wt,
        "amount_mojos": 1_000_000,
        "trade_id": trade_id,
        "assigned_tier": "inner",
        "last_seen": last_seen,
    }


class CheckOrphanLocksTests(_ModuleStubMixin, unittest.TestCase):
    def _patch_db(self, orphans):
        fake = types.ModuleType("database")
        cur = MagicMock()
        cur.fetchall.return_value = [orphans] if isinstance(orphans, dict) else orphans
        conn = MagicMock()
        conn.execute.return_value = cur
        fake.get_connection = lambda: conn
        fake.free_coin = MagicMock(return_value=True)
        sys.modules["database"] = fake
        return fake

    def test_no_orphans_returns_pass(self):
        self._patch_db([])
        c = bot_health.check_orphan_locks(auto_repair=True)
        self.assertEqual(c.status, "pass")
        self.assertEqual(c.anomaly_count, 0)

    def test_old_orphan_is_freed(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        orphans = [_orphan("0xcoin1", last_seen=old, trade_id="dead_tid")]
        fake_db = self._patch_db(orphans)
        c = bot_health.check_orphan_locks(auto_repair=True)
        self.assertEqual(c.repaired_count, 1)
        fake_db.free_coin.assert_called_once_with("0xcoin1")
        self.assertIn("freed_orphan_lock", c.repair_log[0])

    def test_recent_orphan_is_left_alone(self):
        # 30s ago — within the grace window (default 300s)
        recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        orphans = [_orphan("0xcoin1", last_seen=recent, trade_id="maybe_in_flight")]
        fake_db = self._patch_db(orphans)
        c = bot_health.check_orphan_locks(auto_repair=True)
        # No action — within grace
        fake_db.free_coin.assert_not_called()
        self.assertEqual(c.anomaly_count, 0)
        self.assertIn("recent orphan", c.message)

    def test_auto_repair_false_does_not_mutate(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        orphans = [_orphan("0xcoin1", last_seen=old)]
        fake_db = self._patch_db(orphans)
        c = bot_health.check_orphan_locks(auto_repair=False)
        self.assertEqual(c.anomaly_count, 1)
        self.assertEqual(c.repaired_count, 0)
        fake_db.free_coin.assert_not_called()

    def test_orphan_with_no_last_seen_treated_as_actionable(self):
        """Coin without a last_seen timestamp can't be aged — but it's still
        an orphan, so we treat it as actionable rather than skip forever."""
        orphans = [_orphan("0xcoin1", last_seen=None)]
        fake_db = self._patch_db(orphans)
        c = bot_health.check_orphan_locks(auto_repair=True)
        self.assertEqual(c.repaired_count, 1)


class CheckStaleDexiePostsTests(_ModuleStubMixin, unittest.TestCase):
    def _patch_db(self, rows):
        fake = types.ModuleType("database")
        cur = MagicMock()
        cur.fetchall.return_value = rows
        conn = MagicMock()
        conn.execute.return_value = cur
        fake.get_connection = lambda: conn
        sys.modules["database"] = fake
        return fake

    def _patch_dexie_manager(self):
        fake = types.ModuleType("dexie_manager")
        fake.queue_post = MagicMock()
        sys.modules["dexie_manager"] = fake
        return fake

    def test_no_stale_posts(self):
        self._patch_db([])
        self._patch_dexie_manager()
        c = bot_health.check_stale_dexie_posts(auto_repair=True)
        self.assertEqual(c.status, "pass")
        self.assertEqual(c.anomaly_count, 0)

    def test_stale_post_is_requeued(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        rows = [
            {
                "trade_id": "tid1",
                "side": "buy",
                "tier": "inner",
                "offer_bech32": "offer1...",
                "created_at": old,
            }
        ]
        self._patch_db(rows)
        dx = self._patch_dexie_manager()
        c = bot_health.check_stale_dexie_posts(auto_repair=True)
        self.assertEqual(c.repaired_count, 1)
        dx.queue_post.assert_called_once_with("offer1...", "tid1")
        self.assertIn("requeued_dexie_post", c.repair_log[0])

    def test_recent_offer_not_requeued(self):
        recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        rows = [
            {
                "trade_id": "tid1",
                "side": "buy",
                "tier": "inner",
                "offer_bech32": "offer1...",
                "created_at": recent,
            }
        ]
        self._patch_db(rows)
        dx = self._patch_dexie_manager()
        c = bot_health.check_stale_dexie_posts(auto_repair=True)
        # Offer is too fresh — give the normal post path time to drain
        dx.queue_post.assert_not_called()
        self.assertEqual(c.anomaly_count, 0)


if __name__ == "__main__":
    unittest.main()
