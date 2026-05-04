"""Slice 02-22 — wallet_chia.py + sage_node.py pure-function unit tests.

wallet_chia:  cat_to_mojos, xch_to_mojos, mojos_to_xch, mojos_to_cat,
              is_offer_time_expired, get_offer_expiry_info, _is_open_status,
              classify_offers_from_list.
sage_node:    _parse_sage_version, compare_sage_versions.
chia_node:    re-exports sage_node — verified by import spot-check only.

No network I/O or wallet RPC calls are made.
"""

import math
import os
import tempfile
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

try:
    import wallet_chia as _wc
    from wallet_chia import (
        cat_to_mojos,
        xch_to_mojos,
        mojos_to_xch,
        mojos_to_cat,
        is_offer_time_expired,
        get_offer_expiry_info,
        _is_open_status,
        classify_offers_from_list,
    )
    _SKIP_WC = None
except ModuleNotFoundError as exc:
    _SKIP_WC = str(exc)

try:
    from sage_node import (
        _detect_sage_cert_path,
        _parse_sage_version,
        compare_sage_versions,
    )
    _SKIP_SN = None
except ModuleNotFoundError as exc:
    _SKIP_SN = str(exc)

_ASSET = "b8edcc6a7cf3738a3806fdbadb1bbcfc2540ec37f6732ab3a6a4bbcd2dbec105"


# ---------------------------------------------------------------------------
# wallet_chia — cat_to_mojos
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_WC is not None, f"wallet_chia unavailable: {_SKIP_WC}")
class TestWalletChiaCatToMojos(unittest.TestCase):
    def test_standard_3_decimals(self):
        self.assertEqual(cat_to_mojos(Decimal("1.5"), 3), 1500)

    def test_truncates_not_rounds(self):
        self.assertEqual(cat_to_mojos(Decimal("1.9999"), 3), 1999)

    def test_zero_decimals(self):
        self.assertEqual(cat_to_mojos(Decimal("5"), 0), 5)

    def test_sub_unit_truncated_to_zero(self):
        self.assertEqual(cat_to_mojos(Decimal("0.0001"), 3), 0)

    def test_large_amount(self):
        self.assertEqual(cat_to_mojos(Decimal("1000000"), 3), 1_000_000_000)


# ---------------------------------------------------------------------------
# wallet_chia — xch_to_mojos
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_WC is not None, f"wallet_chia unavailable: {_SKIP_WC}")
class TestWalletChiaXchToMojos(unittest.TestCase):
    def test_one_xch(self):
        self.assertEqual(xch_to_mojos(Decimal("1")), 1_000_000_000_000)

    def test_zero(self):
        self.assertEqual(xch_to_mojos(Decimal("0")), 0)

    def test_sub_mojo_truncated(self):
        self.assertEqual(xch_to_mojos(Decimal("0.0000000000001")), 0)

    def test_truncation_not_rounding(self):
        self.assertEqual(xch_to_mojos(Decimal("0.9999999999999")), 999_999_999_999)

    def test_accepts_string_input(self):
        # wallet_chia xch_to_mojos wraps with Decimal(str(amount))
        self.assertEqual(xch_to_mojos("1"), 1_000_000_000_000)

    def test_accepts_float_via_str(self):
        self.assertEqual(xch_to_mojos(1.0), 1_000_000_000_000)


# ---------------------------------------------------------------------------
# wallet_chia — mojos_to_xch
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_WC is not None, f"wallet_chia unavailable: {_SKIP_WC}")
class TestWalletChiaMojosToXch(unittest.TestCase):
    def test_one_trillion_mojos_is_one_xch(self):
        self.assertEqual(mojos_to_xch(1_000_000_000_000), Decimal("1"))

    def test_zero(self):
        self.assertEqual(mojos_to_xch(0), Decimal("0"))

    def test_partial_xch(self):
        result = mojos_to_xch(500_000_000_000)
        self.assertEqual(result, Decimal("0.5"))

    def test_returns_decimal(self):
        self.assertIsInstance(mojos_to_xch(1000), Decimal)

    def test_round_trips_with_xch_to_mojos(self):
        original = Decimal("1.234567890123")
        mojos = xch_to_mojos(original)
        back = mojos_to_xch(mojos)
        # May lose sub-mojo precision due to floor truncation
        self.assertAlmostEqual(float(back), float(original), places=11)


# ---------------------------------------------------------------------------
# wallet_chia — mojos_to_cat
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_WC is not None, f"wallet_chia unavailable: {_SKIP_WC}")
class TestWalletChiaMojosToCat(unittest.TestCase):
    def test_1000_mojos_3_decimals(self):
        self.assertEqual(mojos_to_cat(1000, 3), Decimal("1"))

    def test_zero(self):
        self.assertEqual(mojos_to_cat(0, 3), Decimal("0"))

    def test_partial_token(self):
        self.assertEqual(mojos_to_cat(500, 3), Decimal("0.5"))

    def test_returns_decimal(self):
        self.assertIsInstance(mojos_to_cat(100, 3), Decimal)

    def test_round_trips_with_cat_to_mojos(self):
        original = Decimal("12.345")
        mojos = cat_to_mojos(original, 3)
        back = mojos_to_cat(mojos, 3)
        self.assertEqual(back, Decimal("12.345"))


# ---------------------------------------------------------------------------
# wallet_chia — is_offer_time_expired
# (wallet_chia only checks valid_times.max_time — NOT top-level max_time)
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_WC is not None, f"wallet_chia unavailable: {_SKIP_WC}")
class TestWalletChiaIsOfferTimeExpired(unittest.TestCase):
    def test_no_valid_times_returns_false(self):
        self.assertFalse(is_offer_time_expired({}))

    def test_max_time_zero_returns_false(self):
        self.assertFalse(is_offer_time_expired({"valid_times": {"max_time": 0}}))

    def test_past_max_time_returns_true(self):
        past = int(time.time()) - 3600
        self.assertTrue(is_offer_time_expired({"valid_times": {"max_time": past}}))

    def test_future_max_time_returns_false(self):
        future = int(time.time()) + 3600
        self.assertFalse(is_offer_time_expired({"valid_times": {"max_time": future}}))

    def test_top_level_max_time_ignored(self):
        # wallet_chia does NOT check top-level max_time (unlike wallet_sage)
        past = int(time.time()) - 3600
        self.assertFalse(is_offer_time_expired({"max_time": past}))


# ---------------------------------------------------------------------------
# wallet_chia — get_offer_expiry_info
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_WC is not None, f"wallet_chia unavailable: {_SKIP_WC}")
class TestWalletChiaGetOfferExpiryInfo(unittest.TestCase):
    def test_no_max_time_returns_inf(self):
        info = get_offer_expiry_info({})
        self.assertEqual(info["max_time"], 0)
        self.assertFalse(info["expired"])
        self.assertTrue(math.isinf(info["seconds_remaining"]))

    def test_future_offer_not_expired(self):
        future = int(time.time()) + 3600
        info = get_offer_expiry_info({"valid_times": {"max_time": future}})
        self.assertFalse(info["expired"])
        self.assertGreater(info["seconds_remaining"], 0)

    def test_past_offer_is_expired(self):
        past = int(time.time()) - 3600
        info = get_offer_expiry_info({"valid_times": {"max_time": past}})
        self.assertTrue(info["expired"])
        self.assertLess(info["seconds_remaining"], 0)

    def test_max_time_in_result(self):
        ts = int(time.time()) + 100
        info = get_offer_expiry_info({"valid_times": {"max_time": ts}})
        self.assertEqual(info["max_time"], ts)


# ---------------------------------------------------------------------------
# wallet_chia — _is_open_status
# (wallet_chia differs: no "ACTIVE" in OPEN_STATUSES, unknown → False w/o log)
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_WC is not None, f"wallet_chia unavailable: {_SKIP_WC}")
class TestWalletChiaIsOpenStatus(unittest.TestCase):
    def test_none_is_closed(self):
        self.assertFalse(_is_open_status(None))

    def test_int_0_is_open(self):
        self.assertTrue(_is_open_status(0))

    def test_int_1_is_open(self):
        self.assertTrue(_is_open_status(1))

    def test_int_2_pending_cancel_is_closed(self):
        self.assertFalse(_is_open_status(2))

    def test_int_3_cancelled_is_closed(self):
        self.assertFalse(_is_open_status(3))

    def test_int_4_confirmed_is_closed(self):
        self.assertFalse(_is_open_status(4))

    def test_int_5_failed_is_closed(self):
        self.assertFalse(_is_open_status(5))

    def test_string_open_statuses(self):
        for s in ("PENDING_ACCEPT", "PENDING_CONFIRM", "PENDING", "open"):
            with self.subTest(s=s):
                self.assertTrue(_is_open_status(s))

    def test_string_closed_statuses(self):
        for s in ("CANCELLED", "CANCELED", "CONFIRMED", "FAILED", "EXPIRED"):
            with self.subTest(s=s):
                self.assertFalse(_is_open_status(s))

    def test_unknown_string_is_closed(self):
        self.assertFalse(_is_open_status("SOMETHING_UNKNOWN"))

    def test_expired_offer_record_forces_closed(self):
        past = int(time.time()) - 3600
        offer = {"valid_times": {"max_time": past}}
        self.assertFalse(_is_open_status(0, offer_record=offer))

    def test_active_not_in_chia_open_set(self):
        # wallet_chia OPEN_STATUSES excludes "ACTIVE" (unlike wallet_sage)
        self.assertFalse(_is_open_status("ACTIVE"))


# ---------------------------------------------------------------------------
# wallet_chia — classify_offers_from_list
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_WC is not None, f"wallet_chia unavailable: {_SKIP_WC}")
class TestWalletChiaClassifyOffersFromList(unittest.TestCase):
    def _open_buy(self):
        return {
            "status": 0,
            "summary": {"offered": {"xch": 1000}, "requested": {_ASSET: 500}},
        }

    def _open_sell(self):
        return {
            "status": 0,
            "summary": {"offered": {_ASSET: 500}, "requested": {"xch": 1000}},
        }

    def _closed_buy(self):
        return {
            "status": 3,
            "summary": {"offered": {"xch": 1000}, "requested": {_ASSET: 500}},
        }

    def test_empty_list(self):
        buy, sell, closed = classify_offers_from_list([], _ASSET)
        self.assertEqual((buy, sell, closed), ([], [], []))

    def test_non_dict_skipped(self):
        buy, sell, closed = classify_offers_from_list(["bad", None], _ASSET)
        self.assertEqual((buy, sell, closed), ([], [], []))

    def test_open_buy(self):
        buy, sell, closed = classify_offers_from_list([self._open_buy()], _ASSET)
        self.assertEqual(len(buy), 1)
        self.assertEqual(sell, [])

    def test_open_sell(self):
        buy, sell, closed = classify_offers_from_list([self._open_sell()], _ASSET)
        self.assertEqual(buy, [])
        self.assertEqual(len(sell), 1)

    def test_closed_offer(self):
        buy, sell, closed = classify_offers_from_list([self._closed_buy()], _ASSET)
        self.assertEqual(len(closed), 1)

    def test_mixed(self):
        offers = [self._open_buy(), self._open_sell(), self._closed_buy()]
        buy, sell, closed = classify_offers_from_list(offers, _ASSET)
        self.assertEqual((len(buy), len(sell), len(closed)), (1, 1, 1))


# ---------------------------------------------------------------------------
# sage_node — _parse_sage_version
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_SN is not None, f"sage_node unavailable: {_SKIP_SN}")
class TestParseSageVersion(unittest.TestCase):
    def test_standard_semver(self):
        self.assertEqual(_parse_sage_version("1.2.3"), (1, 2, 3))

    def test_v_prefix_stripped(self):
        self.assertEqual(_parse_sage_version("v2.0.0"), (2, 0, 0))

    def test_capital_v_prefix(self):
        self.assertEqual(_parse_sage_version("V1.9.4"), (1, 9, 4))

    def test_partial_two_parts(self):
        self.assertEqual(_parse_sage_version("1.2"), (1, 2, 0))

    def test_partial_one_part(self):
        self.assertEqual(_parse_sage_version("3"), (3, 0, 0))

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_sage_version(""))

    def test_unknown_returns_none(self):
        self.assertIsNone(_parse_sage_version("unknown"))

    def test_non_numeric_returns_none(self):
        self.assertIsNone(_parse_sage_version("abc.def"))

    def test_prerelease_suffix_parsed(self):
        # "1.2.3-beta" — should parse 1.2.3 (regex stops at first non-digit)
        result = _parse_sage_version("1.2.3-beta")
        self.assertEqual(result, (1, 2, 3))


# ---------------------------------------------------------------------------
# sage_node — compare_sage_versions
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_SN is not None, f"sage_node unavailable: {_SKIP_SN}")
class TestCompareSageVersions(unittest.TestCase):
    def test_equal(self):
        self.assertEqual(compare_sage_versions("1.2.3", "1.2.3"), 0)

    def test_a_less_than_b_minor(self):
        self.assertEqual(compare_sage_versions("1.1.0", "1.2.0"), -1)

    def test_a_greater_than_b_minor(self):
        self.assertEqual(compare_sage_versions("1.3.0", "1.2.0"), 1)

    def test_major_dominates(self):
        self.assertEqual(compare_sage_versions("2.0.0", "1.9.9"), 1)

    def test_patch_comparison(self):
        self.assertEqual(compare_sage_versions("1.0.1", "1.0.2"), -1)

    def test_unparseable_a_returns_zero(self):
        self.assertEqual(compare_sage_versions("bad", "1.0.0"), 0)

    def test_unparseable_b_returns_zero(self):
        self.assertEqual(compare_sage_versions("1.0.0", ""), 0)

    def test_both_unparseable_returns_zero(self):
        self.assertEqual(compare_sage_versions("", ""), 0)


# ---------------------------------------------------------------------------
# sage_node — Sage certificate path discovery
# ---------------------------------------------------------------------------

@unittest.skipIf(_SKIP_SN is not None, f"sage_node unavailable: {_SKIP_SN}")
class TestDetectSageCertPath(unittest.TestCase):
    def _write_sage_cert_pair(self, data_dir):
        ssl_dir = os.path.join(data_dir, "ssl")
        os.makedirs(ssl_dir, exist_ok=True)
        cert_path = os.path.join(ssl_dir, "wallet.crt")
        key_path = os.path.join(ssl_dir, "wallet.key")
        with open(cert_path, "w", encoding="utf-8") as f:
            f.write("test certificate")
        with open(key_path, "w", encoding="utf-8") as f:
            f.write("test key")
        return cert_path

    def test_honors_sage_data_dir_env(self):
        with tempfile.TemporaryDirectory() as appdata, \
             tempfile.TemporaryDirectory() as localappdata, \
             tempfile.TemporaryDirectory() as sage_data_dir:
            cert_path = self._write_sage_cert_pair(sage_data_dir)
            env = {
                "APPDATA": appdata,
                "LOCALAPPDATA": localappdata,
                "SAGE_DATA_DIR": sage_data_dir,
                "SAGE_HOME": "",
                "SAGE_ALLOWED_CERT_ROOTS": "",
            }
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(_detect_sage_cert_path(), cert_path)

    def test_searches_localappdata_default_sage_dir(self):
        with tempfile.TemporaryDirectory() as appdata, \
             tempfile.TemporaryDirectory() as localappdata:
            sage_data_dir = os.path.join(localappdata, "com.rigidnetwork.sage")
            cert_path = self._write_sage_cert_pair(sage_data_dir)
            env = {
                "APPDATA": appdata,
                "LOCALAPPDATA": localappdata,
                "SAGE_DATA_DIR": "",
                "SAGE_HOME": "",
                "SAGE_ALLOWED_CERT_ROOTS": "",
            }
            with patch("platform.system", return_value="Windows"), \
                 patch.dict(os.environ, env, clear=False):
                self.assertEqual(_detect_sage_cert_path(), cert_path)


@unittest.skipIf(_SKIP_SN is not None, f"sage_node unavailable: {_SKIP_SN}")
class TestSageRpcStartupProbes(unittest.TestCase):
    def _write_sage_cert_pair(self, data_dir):
        ssl_dir = os.path.join(data_dir, "ssl")
        os.makedirs(ssl_dir, exist_ok=True)
        cert_path = os.path.join(ssl_dir, "wallet.crt")
        key_path = os.path.join(ssl_dir, "wallet.key")
        with open(cert_path, "w", encoding="utf-8") as f:
            f.write("test certificate")
        with open(key_path, "w", encoding="utf-8") as f:
            f.write("test key")
        return cert_path

    def test_running_probe_treats_listening_rpc_port_as_running(self):
        import api_server
        import sage_node

        client = api_server.app.test_client()
        token = getattr(api_server, "_LOCAL_API_TOKEN", "")

        with patch.object(sage_node, "_is_sage_rpc_available", return_value=False), \
             patch.object(sage_node, "_is_sage_rpc_port_listening", return_value=True, create=True):
            resp = client.get(
                "/api/wallet/sage-running",
                headers={"X-Bot-Local-Token": token},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("running"))

    def test_cert_env_reload_helper_refreshes_wallet_sage(self):
        import sage_node

        with tempfile.TemporaryDirectory() as sage_data_dir:
            cert_path = self._write_sage_cert_pair(sage_data_dir)
            key_path = os.path.join(os.path.dirname(cert_path), "wallet.key")
            helper = getattr(sage_node, "_set_sage_cert_env_and_reload", None)
            self.assertIsNotNone(helper, "startup must reload wallet_sage after auto-detecting certs")

            with patch.dict(os.environ, {}, clear=False), \
                 patch("wallet_sage.reload_connection_settings") as reload_settings:
                helper(cert_path, key_path)

                self.assertEqual(os.environ.get("SAGE_CERT_PATH"), os.path.realpath(cert_path))
                self.assertEqual(os.environ.get("SAGE_KEY_PATH"), os.path.realpath(key_path))
                self.assertEqual(os.environ.get("SAGE_DATA_DIR"), os.path.dirname(os.path.dirname(os.path.realpath(cert_path))))
                reload_settings.assert_called_once()


# ---------------------------------------------------------------------------
# chia_node — re-export spot-check
# ---------------------------------------------------------------------------

class TestChiaNodeReExports(unittest.TestCase):
    def test_chia_node_imports_from_sage_node(self):
        try:
            import chia_node
            self.assertTrue(hasattr(chia_node, "compare_sage_versions"))
        except ModuleNotFoundError:
            self.skipTest("chia_node unavailable")


if __name__ == "__main__":
    unittest.main()
