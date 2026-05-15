import importlib
import sys
import types
import unittest
from unittest import mock


class WalletSageSpendableViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_modules = {
            name: sys.modules.get(name)
            for name in (
                "wallet_sage",
                "requests",
                "requests.adapters",
                "urllib3",
                "dotenv",
            )
        }

        fake_requests = types.ModuleType("requests")
        fake_requests_adapters = types.ModuleType("requests.adapters")
        fake_requests_adapters.HTTPAdapter = object
        fake_requests.adapters = fake_requests_adapters

        fake_urllib3 = types.ModuleType("urllib3")
        fake_urllib3.Retry = object
        fake_urllib3.disable_warnings = lambda *args, **kwargs: None
        fake_urllib3.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)

        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *args, **kwargs: True
        fake_dotenv.set_key = lambda *args, **kwargs: None

        sys.modules["requests"] = fake_requests
        sys.modules["requests.adapters"] = fake_requests_adapters
        sys.modules["urllib3"] = fake_urllib3
        sys.modules["dotenv"] = fake_dotenv
        sys.modules.pop("wallet_sage", None)
        cls.wallet_sage = importlib.import_module("wallet_sage")

    @classmethod
    def tearDownClass(cls):
        for name, module in cls._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    @staticmethod
    def _coin(hex_char: str, amount: int) -> dict:
        return {
            "coin_id": "0x" + (hex_char * 64),
            "parent_coin_info": "aa" * 32,
            "puzzle_hash": "bb" * 32,
            "amount": amount,
        }

    def test_get_spendable_coins_rpc_uses_selectable_only(self):
        selectable_coin = self._coin("2", 123)
        owned_only_coin = self._coin("1", 999)
        calls = []

        def fake_rpc(method, payload, timeout=15):
            self.assertEqual(method, "get_coins")
            calls.append(payload["filter_mode"])
            if payload["filter_mode"] == "selectable":
                return {"coins": [selectable_coin]}
            if payload["filter_mode"] == "owned":
                return {"coins": [owned_only_coin, selectable_coin]}
            raise AssertionError(f"Unexpected filter_mode {payload['filter_mode']}")

        with mock.patch.object(self.wallet_sage, "rpc", side_effect=fake_rpc):
            result = self.wallet_sage.get_spendable_coins_rpc(
                self.wallet_sage.WALLET_ID_XCH
            )

        self.assertEqual(calls, ["selectable"])
        self.assertTrue(result["success"])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["coin_id"], selectable_coin["coin_id"])
        self.assertEqual(result["records"][0]["coin"]["amount"], 123)

    def test_owned_fallback_helper_appends_owned_only_coins(self):
        selectable_coin = self._coin("2", 123)
        owned_only_coin = self._coin("1", 999)
        calls = []

        def fake_rpc(method, payload, timeout=15):
            self.assertEqual(method, "get_coins")
            calls.append(payload["filter_mode"])
            if payload["filter_mode"] == "owned":
                return {"coins": [owned_only_coin, selectable_coin]}
            if payload["filter_mode"] == "selectable":
                return {"coins": [selectable_coin]}
            raise AssertionError(f"Unexpected filter_mode {payload['filter_mode']}")

        if hasattr(
            self.wallet_sage.get_spendable_coins_with_owned_fallback, "_hidden_XCH"
        ):
            delattr(
                self.wallet_sage.get_spendable_coins_with_owned_fallback, "_hidden_XCH"
            )

        with mock.patch.object(self.wallet_sage, "rpc", side_effect=fake_rpc):
            with mock.patch("builtins.print"):
                result = self.wallet_sage.get_spendable_coins_with_owned_fallback(
                    self.wallet_sage.WALLET_ID_XCH
                )

        self.assertEqual(calls, ["owned", "selectable"])
        self.assertTrue(result["success"])
        self.assertEqual(
            [record["coin_id"] for record in result["records"]],
            [selectable_coin["coin_id"], owned_only_coin["coin_id"]],
        )


if __name__ == "__main__":
    unittest.main()
