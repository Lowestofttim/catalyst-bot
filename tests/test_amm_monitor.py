"""Tests for AMMMonitor — live TibetSwap reserve polling and drift detection.

These tests use a mock HTTP session so no real network calls are made.
"""
import sys
import time
import types
import threading
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# --------------------------------------------------------------------------
# Minimal stubs so amm_monitor can be imported without the full app env.
# We save originals and restore after import so we don't contaminate
# sys.modules when running alongside the full test suite.
# --------------------------------------------------------------------------

_ORIGINAL_CONFIG = sys.modules.get("config")

# Install a test-only config so amm_monitor's lazy `from config import cfg`
# gets sensible defaults. We restore config in tearDown of buffer guard tests.
fake_config_mod = types.ModuleType("config")


class _FakeCfg:
    TIBET_PAIR_ID = "test-pair-id-0000"
    AMM_POLL_INTERVAL_SECS = 30
    AMM_DRIFT_REQUOTE_BPS = Decimal("40")
    ENABLE_AMM_BUFFER = True
    AMM_BUFFER_BPS = Decimal("30")
    TIBET_API_BASE = "https://api2.tibetswap.io"
    TIBET_TIMEOUT = 10
    CAT_DECIMALS = 3


fake_config_mod.cfg = _FakeCfg()
sys.modules["config"] = fake_config_mod

# Use a real (but in-memory) database so log_event calls don't fail.
# Import the real module BEFORE installing our fake config so it doesn't
# get contaminated.  We let log_event fail silently if DB is not set up.
import database as _real_database  # noqa: E402

try:
    import amm_monitor as _amm_mod  # noqa: E402
    from amm_monitor import AMMMonitor  # noqa: E402
except Exception:
    raise

# Restore config so later test files' setdefault("config", ...) can install
# their own fakes without getting our incomplete _FakeCfg.
if _ORIGINAL_CONFIG is None:
    sys.modules.pop("config", None)
else:
    sys.modules["config"] = _ORIGINAL_CONFIG


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _make_pair_response(xch_reserve: int, token_reserve: int) -> list:
    """Build the response list that TibetSwap /pairs returns.

    The list contains the target pair (matching _FakeCfg.TIBET_PAIR_ID)
    plus one extra decoy so the pair-filtering logic is exercised.
    """
    return [
        {
            "pair_id": "other-pair-decoy",
            "xch_reserve": 999,
            "token_reserve": 999,
            "liquidity": 1,
        },
        {
            "pair_id": _FakeCfg.TIBET_PAIR_ID,
            "launcher_id": _FakeCfg.TIBET_PAIR_ID,
            "xch_reserve": xch_reserve,
            "token_reserve": token_reserve,
            "liquidity": 1_000_000,
        },
    ]


def _mock_session_get(response_data: dict, status_code: int = 200):
    """Return a mock requests.Session whose .get() returns a fake response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = response_data
    mock_resp.raise_for_status = MagicMock()  # no-op unless status != 200

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    return mock_session


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

class TestAMMMonitorInit(unittest.TestCase):
    def test_starts_with_no_state(self):
        m = AMMMonitor()
        self.assertIsNone(m.get_amm_price())
        self.assertIsNone(m.get_amm_state())
        self.assertFalse(m.is_available())

    def test_accepts_price_engine(self):
        pe = MagicMock()
        m = AMMMonitor(price_engine=pe)
        self.assertIs(m._price_engine, pe)


class TestAMMMonitorFetchPair(unittest.TestCase):
    """Unit-tests for _fetch_pair() — the single HTTP call."""

    def _make_monitor(self, response_data, status_code=200):
        m = AMMMonitor()
        m._session = _mock_session_get(response_data, status_code)
        return m

    def test_parses_valid_response(self):
        # 10 XCH, 10000 tokens → price = 10/10000 = 0.001
        xch_mojos = 10 * 1_000_000_000_000      # 10 XCH
        token_mojos = 10_000 * 1_000             # 10000 tokens (3 decimals)
        m = self._make_monitor(_make_pair_response(xch_mojos, token_mojos))

        state = m._fetch_pair("test-pair-id-0000")
        self.assertIsNotNone(state)
        self.assertTrue(state["available"])
        self.assertAlmostEqual(float(state["amm_price"]), 0.001, places=8)
        self.assertAlmostEqual(float(state["xch_reserve"]), 10.0, places=4)
        self.assertAlmostEqual(float(state["token_reserve"]), 10_000.0, places=2)

    def test_returns_none_on_zero_reserves(self):
        m = self._make_monitor(_make_pair_response(0, 1000))
        state = m._fetch_pair("test-pair-id-0000")
        self.assertIsNone(state)

    def test_returns_none_on_http_error(self):
        m = AMMMonitor()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("HTTP 500")
        m._session = MagicMock()
        m._session.get.return_value = mock_resp
        state = m._fetch_pair("test-pair-id-0000")
        self.assertIsNone(state)

    def test_returns_none_on_non_dict_response(self):
        m = self._make_monitor([1, 2, 3])  # list instead of dict
        state = m._fetch_pair("test-pair-id-0000")
        self.assertIsNone(state)


class TestAMMMonitorDrift(unittest.TestCase):
    """Tests for drift detection between AMM price and last quoted prices."""

    def _monitor_with_price(self, xch_mojos, token_mojos):
        m = AMMMonitor()
        m._session = _mock_session_get(_make_pair_response(xch_mojos, token_mojos))
        state = m._fetch_pair("test-pair-id-0000")
        with m._lock:
            m._state = state
        return m

    def test_drift_none_when_no_quoted_price(self):
        m = self._monitor_with_price(10 * 10**12, 10_000 * 10**3)
        # No notify_quoted_price called → drift unknown
        self.assertIsNone(m.get_drift_bps())

    def test_drift_zero_when_prices_match(self):
        xch = 10 * 10**12
        tok = 10_000 * 10**3
        m = self._monitor_with_price(xch, tok)
        amm_price = Decimal("10") / Decimal("10000")  # 0.001
        m.notify_quoted_price(amm_price, amm_price)
        drift = m.get_drift_bps()
        self.assertIsNotNone(drift)
        self.assertAlmostEqual(float(drift), 0.0, places=4)

    def test_drift_computed_from_buy_only(self):
        xch = 10 * 10**12
        tok = 10_000 * 10**3
        m = self._monitor_with_price(xch, tok)
        amm_price = Decimal("10") / Decimal("10000")  # 0.001
        # quoted buy is 2% lower → drift ≈ 200 bps
        quoted = amm_price * Decimal("0.98")
        m.notify_quoted_price(quoted, None)
        drift = m.get_drift_bps()
        self.assertIsNotNone(drift)
        self.assertGreater(float(drift), 150)  # should be ~200 bps

    def test_drift_uses_mid_of_buy_and_sell(self):
        xch = 10 * 10**12
        tok = 10_000 * 10**3
        m = self._monitor_with_price(xch, tok)
        amm_price = Decimal("10") / Decimal("10000")  # 0.001
        # buy slightly below, sell slightly above → mid = amm → drift ≈ 0
        m.notify_quoted_price(amm_price * Decimal("0.999"),
                               amm_price * Decimal("1.001"))
        drift = m.get_drift_bps()
        self.assertIsNotNone(drift)
        self.assertLess(float(drift), 5)  # very small drift

    def test_notify_quoted_price_updates_both_sides(self):
        m = AMMMonitor()
        buy = Decimal("0.001")
        sell = Decimal("0.0011")
        m.notify_quoted_price(buy, sell)
        with m._lock:
            self.assertEqual(m._last_quoted_buy, buy)
            self.assertEqual(m._last_quoted_sell, sell)

    def test_notify_quoted_price_ignores_none(self):
        m = AMMMonitor()
        m.notify_quoted_price(Decimal("0.001"), Decimal("0.0011"))
        m.notify_quoted_price(None, None)  # should not overwrite
        with m._lock:
            self.assertIsNotNone(m._last_quoted_buy)
            self.assertIsNotNone(m._last_quoted_sell)


class TestAMMMonitorBufferGuard(unittest.TestCase):
    """Tests for check_amm_buffer() — arb zone detection.

    amm_monitor uses `from config import cfg` lazily inside methods.
    We temporarily install fake_config_mod back into sys.modules["config"]
    for each test so that the lazy import picks up our test cfg.
    """

    def setUp(self):
        # Restore the test's fake config for this test class only
        self._prior_config = sys.modules.get("config")
        sys.modules["config"] = fake_config_mod
        # Reset cfg to defaults before each test
        fake_config_mod.cfg.ENABLE_AMM_BUFFER = True
        fake_config_mod.cfg.AMM_BUFFER_BPS = Decimal("30")

    def tearDown(self):
        if self._prior_config is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = self._prior_config

    def _monitor_with_amm_price(self, amm_price_float: float):
        m = AMMMonitor()
        amm_dec = Decimal(str(amm_price_float))
        with m._lock:
            m._state = {
                "available": True,
                "amm_price": amm_dec,
                "xch_reserve": Decimal("10"),
                "token_reserve": Decimal("10000"),
                "fetched_at": time.time(),
            }
        return m

    def _patch_cfg(self, enable_buffer: bool, buffer_bps: str = "30"):
        """Set cfg attributes for this test (setUp already installs fake_config_mod)."""
        fake_config_mod.cfg.ENABLE_AMM_BUFFER = enable_buffer
        fake_config_mod.cfg.AMM_BUFFER_BPS = buffer_bps

        class _Noop:
            def __enter__(self_): return self_
            def __exit__(self_, *_): pass  # setUp/tearDown handle cleanup

        return _Noop()

    def test_buffer_disabled_always_safe(self):
        m = self._monitor_with_amm_price(0.001)
        with self._patch_cfg(enable_buffer=False):
            self.assertTrue(m.check_amm_buffer(Decimal("0.001"), "buy"))
            self.assertTrue(m.check_amm_buffer(Decimal("0.001"), "sell"))

    def test_buy_above_amm_is_unsafe(self):
        """A buy offer priced AT or above AMM would be instantly arbed."""
        m = self._monitor_with_amm_price(0.001)
        with self._patch_cfg(enable_buffer=True, buffer_bps="30"):
            # amm * (1 - 30bps) = 0.001 * 0.997 = 0.000997
            # offer at 0.001 ≥ threshold → unsafe
            self.assertFalse(m.check_amm_buffer(Decimal("0.001"), "buy"))

    def test_buy_well_below_amm_is_safe(self):
        m = self._monitor_with_amm_price(0.001)
        with self._patch_cfg(enable_buffer=True, buffer_bps="30"):
            # offer at 0.0009 is well below amm * 0.997 → safe
            self.assertTrue(m.check_amm_buffer(Decimal("0.0009"), "buy"))

    def test_sell_below_amm_is_unsafe(self):
        m = self._monitor_with_amm_price(0.001)
        with self._patch_cfg(enable_buffer=True, buffer_bps="30"):
            # amm * (1 + 30bps) = 0.001 * 1.003 = 0.001003
            # offer at 0.001 ≤ threshold → unsafe
            self.assertFalse(m.check_amm_buffer(Decimal("0.001"), "sell"))

    def test_sell_well_above_amm_is_safe(self):
        m = self._monitor_with_amm_price(0.001)
        with self._patch_cfg(enable_buffer=True, buffer_bps="30"):
            # offer at 0.0012 is well above amm * 1.003 → safe
            self.assertTrue(m.check_amm_buffer(Decimal("0.0012"), "sell"))

    def test_no_state_fails_open(self):
        """No AMM data → fail open (safe to post)."""
        m = AMMMonitor()
        with self._patch_cfg(enable_buffer=True, buffer_bps="30"):
            self.assertTrue(m.check_amm_buffer(Decimal("0.001"), "buy"))
            self.assertTrue(m.check_amm_buffer(Decimal("0.001"), "sell"))


class TestAMMMonitorGetStats(unittest.TestCase):
    def test_stats_shape(self):
        m = AMMMonitor()
        stats = m.get_stats()
        self.assertIn("available", stats)
        self.assertIn("total_polls", stats)
        self.assertIn("failed_polls", stats)
        self.assertIn("consecutive_failures", stats)
        self.assertIn("amm_price", stats)

    def test_stats_available_false_initially(self):
        m = AMMMonitor()
        self.assertFalse(m.get_stats()["available"])


class TestAMMMonitorThreadSafety(unittest.TestCase):
    """Smoke test: notify_quoted_price is safe to call from multiple threads."""

    def test_concurrent_notify(self):
        m = AMMMonitor()
        errors = []

        def _notifier(price):
            try:
                for _ in range(50):
                    m.notify_quoted_price(Decimal(str(price)), Decimal(str(price * 1.001)))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_notifier, args=(0.001 + i * 0.0001,))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread safety errors: {errors}")


if __name__ == "__main__":
    unittest.main()
