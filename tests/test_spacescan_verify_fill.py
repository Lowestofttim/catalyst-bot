import importlib
import sys
import types
import unittest


class _FakeCfg:
    SPACESCAN_ENABLED = True
    SPACESCAN_API_KEY = ""
    WALLET_ADDRESS = "xch1currentownaddress000000000000000000000000000000000000000"
    WALLET_TYPE = "sage"
    SAGE_SET_CHANGE_ADDRESS = False


class SpacescanVerifyFillTests(unittest.TestCase):
    def setUp(self):
        self.logged = []

        fake_config = types.ModuleType("config")
        fake_config.cfg = _FakeCfg()
        sys.modules["config"] = fake_config

        fake_database = types.ModuleType("database")
        fake_database.log_event = self._log_event
        sys.modules["database"] = fake_database

        fake_requests = types.ModuleType("requests")
        fake_requests.get = lambda *args, **kwargs: None
        sys.modules["requests"] = fake_requests

        sys.modules.pop("spacescan", None)
        self.spacescan = importlib.import_module("spacescan")

    def tearDown(self):
        for name in ["spacescan", "database", "config", "requests"]:
            sys.modules.pop(name, None)

    def _log_event(self, severity, event_type, message):
        self.logged.append((severity, event_type, message))

    def test_known_wallet_address_is_self_spend(self):
        historical_addr = "xch1historicalown00000000000000000000000000000000000000000"
        self.spacescan.is_coin_spent = lambda coin_id: {
            "spent": True,
            "spent_block": "123",
            "receiver_address": historical_addr,
            "amount": "1",
            "amount_mojo": "1",
            "sender_address": "",
        }
        self.spacescan._get_known_wallet_addresses = lambda: {historical_addr}

        result = self.spacescan.verify_fill("0xcoin", _FakeCfg.WALLET_ADDRESS)

        self.assertFalse(result)
        self.assertTrue(any(evt == "spacescan_self_spend" for _, evt, _ in self.logged))

    def test_unpinned_sage_change_address_is_ambiguous(self):
        self.spacescan.is_coin_spent = lambda coin_id: {
            "spent": True,
            "spent_block": "123",
            "receiver_address": "xch1unknownreceiver000000000000000000000000000000000000",
            "amount": "1",
            "amount_mojo": "1",
            "sender_address": "",
        }
        self.spacescan._get_known_wallet_addresses = lambda: {_FakeCfg.WALLET_ADDRESS}

        result = self.spacescan.verify_fill("0xcoin", _FakeCfg.WALLET_ADDRESS)

        self.assertIsNone(result)
        self.assertTrue(
            any(evt == "spacescan_fill_ambiguous" for _, evt, _ in self.logged)
        )

    def test_pinned_sage_change_address_allows_external_fill(self):
        sys.modules["config"].cfg.SAGE_SET_CHANGE_ADDRESS = True
        self.spacescan.is_coin_spent = lambda coin_id: {
            "spent": True,
            "spent_block": "123",
            "receiver_address": "xch1externalreceiver00000000000000000000000000000000000",
            "amount": "1",
            "amount_mojo": "1",
            "sender_address": "",
        }
        self.spacescan._get_known_wallet_addresses = lambda: {_FakeCfg.WALLET_ADDRESS}

        result = self.spacescan.verify_fill("0xcoin", _FakeCfg.WALLET_ADDRESS)

        self.assertTrue(result)
        self.assertTrue(
            any(evt == "spacescan_fill_confirmed" for _, evt, _ in self.logged)
        )

    def test_explicit_addresses_are_treated_as_own_wallet(self):
        explicit_addr = "xch1explicitown00000000000000000000000000000000000000000"
        self.spacescan.is_coin_spent = lambda coin_id: {
            "spent": True,
            "spent_block": "123",
            "receiver_address": explicit_addr,
            "amount": "1",
            "amount_mojo": "1",
            "sender_address": "",
        }
        self.spacescan._get_known_wallet_addresses = lambda: set()

        result = self.spacescan.verify_fill(
            "0xcoin",
            _FakeCfg.WALLET_ADDRESS,
            explicit_addresses={explicit_addr},
        )

        self.assertFalse(result)
        self.assertTrue(any(evt == "spacescan_self_spend" for _, evt, _ in self.logged))

    def test_sage_does_not_run_single_address_balance_check(self):
        sys.modules["config"].cfg.SPACESCAN_API_KEY = "pro-key"
        sys.modules["config"].cfg.WALLET_TYPE = "sage"

        self.assertFalse(self.spacescan.should_check_balance())


if __name__ == "__main__":
    unittest.main()
