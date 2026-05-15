"""Slice 03-13 — shutdown + resume: state correct on restart (integration test).

Tests the full shutdown → restart → resume decision flow:

  Shutdown:
    - Bot stops; open offers remain in wallet (persistent); DB retains fills,
      open-offer records, and PnL history.

  Restart (check-resume):
    - /api/check-resume inspects live wallet offers to decide
      whether to show the Resume modal.
    - can_resume=True when wallet has open offers.
    - can_resume=False when no wallet offers, or when fresh_start flag is set.

  Resume chosen:
    - /api/session/resume-chosen clears the fresh_start flag — no data wiped.
    - Fills and offers in DB are preserved.

  Fresh start chosen:
    - /api/session/fresh-start calls _reset_fresh_run_session():
      fills cleared, round-trips cleared, position baseline reset.
    - Subsequent check-resume returns can_resume=False (flag guards it).
    - DB fills count drops to 0 after reset.

Uses real temp SQLite to verify DB-level state changes.
Wallet calls are mocked — only DB and session-flag logic is exercised live.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import database as _db
    import api_server

    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    _db = None
    api_server = None
    _SKIP = str(exc)


_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

_FAKE_ASSET = "a" * 64  # valid 64-hex asset_id
_FAKE_TRADE_ID = "test-shutdown-001"


class _TempDB(unittest.TestCase):
    """Base: redirect database module to a fresh temp SQLite file."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self._tmp_path = self._tmp.name

        self._orig_db_path = _db.DB_PATH
        _db.DB_PATH = self._tmp_path
        self._orig_init_path = _db._db_initialized_path
        _db._db_initialized_path = ""

        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.init_database()

        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.token = api_server._LOCAL_API_TOKEN
        api_server._rate_limit_log.clear()

        # Snapshot globals mutated by _reset_fresh_run_session() so tearDown
        # can restore them and avoid polluting subsequent test modules.
        self._orig_session_start_time = api_server._session_start_time
        self._orig_run_history_cutoff = api_server._run_history_cutoff

        # Clear any leftover session flags from prior tests
        api_server._fresh_start_clear()

    def tearDown(self):
        if hasattr(_db._local, "conn") and _db._local.conn:
            try:
                _db._local.conn.close()
            except Exception:
                pass
        _db._local.conn = None
        _db.DB_PATH = self._orig_db_path
        _db._db_initialized_path = self._orig_init_path
        try:
            os.unlink(self._tmp_path)
        except Exception:
            pass
        api_server._rate_limit_log.clear()
        api_server._fresh_start_clear()
        api_server._session_start_time = self._orig_session_start_time
        api_server._run_history_cutoff = self._orig_run_history_cutoff

    def _seed_fill(self):
        """Insert a fill into the temp DB to simulate a previous session."""
        from decimal import Decimal

        _db.record_fill(
            trade_id=_FAKE_TRADE_ID,
            side="buy",
            price_xch=Decimal("0.002"),
            size_xch=Decimal("0.001"),
            size_cat=Decimal("0.5"),
            cat_asset_id=_FAKE_ASSET,
        )

    def _fill_count(self):
        conn = _db.get_connection()
        return conn.execute("SELECT COUNT(*) as cnt FROM fills").fetchone()["cnt"]


# ---------------------------------------------------------------------------
# check-resume: wallet state → can_resume flag
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestCheckResume(_TempDB):
    """check-resume must report correctly based on wallet offer state."""

    def _check(self, wallet_offers, classified_buy=None, classified_sell=None):
        classified_buy = classified_buy or [
            {"trade_id": o["trade_id"]} for o in wallet_offers
        ]
        classified_sell = classified_sell or []
        with (
            patch("api_server.bot", None),
            patch("api_server._fresh_start_is_set", return_value=False),
            patch("wallet.get_all_offers", return_value=wallet_offers),
            patch(
                "wallet.classify_offers_from_list",
                return_value=(classified_buy, classified_sell, []),
            ),
        ):
            return self.client.get("/api/check-resume", environ_base=_LOOPBACK)

    def test_open_offers_returns_can_resume_true(self):
        """Wallet has open offers → can_resume=True."""
        resp = self._check([{"trade_id": "buy-1"}])
        self.assertTrue(resp.get_json().get("can_resume"))

    def test_no_offers_returns_can_resume_false(self):
        """No open offers → can_resume=False."""
        resp = self._check([])
        self.assertFalse(resp.get_json().get("can_resume"))

    def test_check_returns_200(self):
        resp = self._check([])
        self.assertEqual(resp.status_code, 200)

    def test_response_has_buy_and_sell_counts(self):
        """Response shape includes buy_count and sell_count fields."""
        resp = self._check(
            [{"trade_id": "buy-1"}, {"trade_id": "buy-2"}],
            classified_buy=[{"trade_id": "buy-1"}, {"trade_id": "buy-2"}],
            classified_sell=[],
        )
        body = resp.get_json()
        self.assertIn("buy_count", body)
        self.assertIn("sell_count", body)
        self.assertEqual(body["buy_count"], 2)

    def test_fresh_start_flag_prevents_resume_modal(self):
        """If user already pressed Start Fresh this session, skip modal."""
        api_server._fresh_start_set()
        with (
            patch("api_server.bot", None),
            patch("wallet.get_all_offers", return_value=[{"trade_id": "b1"}]),
            patch(
                "wallet.classify_offers_from_list",
                return_value=([{"trade_id": "b1"}], [], []),
            ),
        ):
            resp = self.client.get("/api/check-resume", environ_base=_LOOPBACK)
        self.assertFalse(resp.get_json().get("can_resume"))


# ---------------------------------------------------------------------------
# resume-chosen: flag cleared, DB preserved
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestResumePath(_TempDB):
    """resume-chosen must clear the flag; fills must NOT be touched."""

    def _resume_chosen(self):
        return self.client.post(
            "/api/session/resume-chosen",
            headers={"X-Bot-Local-Token": self.token},
            environ_base=_LOOPBACK,
        )

    def test_resume_chosen_returns_success(self):
        resp = self._resume_chosen()
        self.assertTrue(resp.get_json().get("success"))

    def test_resume_chosen_clears_fresh_start_flag(self):
        """After resume-chosen, fresh_start flag is gone."""
        api_server._fresh_start_set()
        self._resume_chosen()
        self.assertFalse(api_server._fresh_start_is_set())

    def test_resume_chosen_preserves_db_fills(self):
        """Fills from previous session survive resume-chosen (not wiped)."""
        self._seed_fill()
        self.assertEqual(self._fill_count(), 1)
        self._resume_chosen()
        self.assertEqual(self._fill_count(), 1)  # still there

    def test_resume_chosen_does_not_require_bot(self):
        """resume-chosen works even with bot=None."""
        with patch.object(api_server, "bot", None):
            resp = self._resume_chosen()
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# fresh-start: fills cleared from DB
# ---------------------------------------------------------------------------


@unittest.skipIf(_SKIP is not None, f"modules unavailable: {_SKIP}")
class TestFreshStartPath(_TempDB):
    """fresh-start must clear session data from DB and set the flag."""

    def _fresh_start(self):
        return self.client.post(
            "/api/session/fresh-start",
            headers={"X-Bot-Local-Token": self.token},
            environ_base=_LOOPBACK,
        )

    def test_fresh_start_returns_success(self):
        resp = self._fresh_start()
        self.assertTrue(resp.get_json().get("success"))

    def test_fresh_start_clears_fills_from_db(self):
        """_reset_fresh_run_session() deletes fills — DB fill count becomes 0."""
        self._seed_fill()
        self.assertEqual(self._fill_count(), 1)
        self._fresh_start()
        self.assertEqual(self._fill_count(), 0)

    def test_fresh_start_sets_flag(self):
        """After fresh-start, the session flag is set."""
        self._fresh_start()
        self.assertTrue(api_server._fresh_start_is_set())

    def test_check_resume_after_fresh_start_returns_false(self):
        """After fresh-start, check-resume returns can_resume=False (flag)."""
        self._fresh_start()
        with (
            patch("api_server.bot", None),
            patch("wallet.get_all_offers", return_value=[{"trade_id": "b1"}]),
            patch(
                "wallet.classify_offers_from_list",
                return_value=([{"trade_id": "b1"}], [], []),
            ),
        ):
            resp = self.client.get("/api/check-resume", environ_base=_LOOPBACK)
        self.assertFalse(resp.get_json().get("can_resume"))

    def test_response_includes_fills_cleared_count(self):
        """fresh-start response reports how many fills were cleared."""
        self._seed_fill()
        resp = self._fresh_start()
        body = resp.get_json()
        self.assertGreaterEqual(body.get("fills_cleared", 0), 1)


if __name__ == "__main__":
    unittest.main()
