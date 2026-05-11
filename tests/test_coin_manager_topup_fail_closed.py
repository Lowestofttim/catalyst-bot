import unittest
from unittest.mock import patch
import sys
import types
from decimal import Decimal
from contextlib import ExitStack

# Track which stub modules we installed so we can clean them up after the
# test class runs.  Other test files (test_amm_monitor.py) import the real
# requests module, so leaving a stub in sys.modules would break them.
_INSTALLED_STUBS: list = []

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    dotenv_stub.set_key = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub
    _INSTALLED_STUBS.append("dotenv")

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")

    class _DummyResponse:
        status_code = 200

        def json(self):
            return {"status": "success"}

        def raise_for_status(self):
            return None

    class _StubSession:
        """Minimal Session stub — supports headers.update() so amm_monitor.__init__ works."""
        def __init__(self):
            self.headers = {}

        def get(self, *args, **kwargs):
            return _DummyResponse()

        def mount(self, *args, **kwargs):
            pass

    requests_stub.get = lambda *args, **kwargs: _DummyResponse()
    requests_stub.Session = _StubSession
    requests_stub.exceptions = types.SimpleNamespace(
        Timeout=Exception,
        ConnectionError=Exception,
    )
    requests_adapters_stub = types.ModuleType("requests.adapters")
    requests_adapters_stub.HTTPAdapter = object
    requests_stub.adapters = requests_adapters_stub
    sys.modules["requests"] = requests_stub
    sys.modules["requests.adapters"] = requests_adapters_stub
    _INSTALLED_STUBS.extend(["requests", "requests.adapters"])

if "urllib3" not in sys.modules:
    urllib3_stub = types.ModuleType("urllib3")
    urllib3_stub.Retry = object
    urllib3_stub.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
    urllib3_stub.disable_warnings = lambda *args, **kwargs: None
    sys.modules["urllib3"] = urllib3_stub
    _INSTALLED_STUBS.append("urllib3")

import coin_manager


def _record(coin_id: str, amount: int) -> dict:
    return {
        "coin_id": coin_id,
        "coin": {
            "amount": amount,
        },
    }


_CACHED_MODS = ("amm_monitor", "coin_manager", "wallet_sage",
                "wallet_chia", "wallet", "price_engine", "tx_fees",
                "win_subprocess", "config", "database")


class CoinManagerTopupFailClosedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Save originals of modules we will replace/evict so tearDownClass
        # can restore them and later test files see the real modules, not
        # stubs we installed at module-load time.
        cls._saved_modules = {
            name: sys.modules.get(name)
            for name in list(_INSTALLED_STUBS) + list(_CACHED_MODS)
        }

    @classmethod
    def tearDownClass(cls):
        # Remove stub modules installed at module-load time so they don't
        # pollute sys.modules for test files that run later.
        for name in _INSTALLED_STUBS:
            sys.modules.pop(name, None)
        # Evict modules that may have been cached against stub requests/
        # urllib3 at import time so later test files re-import fresh.
        for name in _CACHED_MODS:
            sys.modules.pop(name, None)
        # Restore any originals we saved so later files that imported
        # these modules before we loaded keep the same instance.
        for name, saved in cls._saved_modules.items():
            if saved is not None:
                sys.modules[name] = saved

    def _make_manager(self):
        with patch.object(coin_manager.CoinManager, "_resolve_fingerprint", return_value="123456789"):
            return coin_manager.CoinManager()

    def test_missing_fingerprint_is_startup_info_not_error(self):
        manager = coin_manager.CoinManager.__new__(coin_manager.CoinManager)
        fake_wallet = types.SimpleNamespace(get_current_key=lambda: None)

        with patch.dict(sys.modules, {"wallet": fake_wallet}), \
                patch.dict(coin_manager.os.environ, {"WALLET_TYPE": "sage"}), \
                patch.object(coin_manager.cfg, "WALLET_FINGERPRINT", "", create=True), \
                patch.object(coin_manager, "log_event") as log_event:
            result = manager._resolve_fingerprint()

        self.assertEqual(result, "")
        fingerprint_events = [
            call for call in log_event.call_args_list
            if call.args[1] == "coin_mgr_no_fingerprint"
        ]
        self.assertEqual(len(fingerprint_events), 1)
        self.assertEqual(fingerprint_events[0].args[0], "info")

    def test_extract_sage_transaction_ids_handles_nested_fields(self):
        tx_ids = coin_manager.CoinManager._extract_sage_transaction_ids({
            "transaction_id": "abc123",
            "transaction": {
                "transaction_ids": ["def456", "0xabc123"],
            },
        })
        self.assertEqual(tx_ids, ["0xabc123", "0xdef456"])

    def test_unadvised_unknown_deposit_is_not_a_topup_source(self):
        coin_id = "0x65119c25b5bc049c2496a5791349c552269ff51483ada6bcb2bc68ae51ed08be"
        records = [_record(coin_id, 193_886_291)]

        safe, blocked = coin_manager._filter_unallocated_deposit_sources(
            records,
            wallet_type="cat",
            db_designations={coin_id: "unknown"},
            advised_coin_ids=set(),
            threshold_mojos=60_320_000,
        )

        self.assertEqual(safe, [])
        self.assertEqual(blocked, 1)

    def test_advised_unknown_deposit_can_be_used_as_topup_source(self):
        coin_id = "0x65119c25b5bc049c2496a5791349c552269ff51483ada6bcb2bc68ae51ed08be"
        records = [_record(coin_id, 193_886_291)]

        safe, blocked = coin_manager._filter_unallocated_deposit_sources(
            records,
            wallet_type="cat",
            db_designations={coin_id: "unknown"},
            advised_coin_ids={coin_id},
            threshold_mojos=60_320_000,
        )

        self.assertEqual(safe, records)
        self.assertEqual(blocked, 0)

    def test_owned_coin_map_falls_back_to_selectable_lower_bound(self):
        manager = self._make_manager()

        with patch.object(coin_manager, "get_owned_coins", side_effect=RuntimeError("owned unavailable")), \
             patch.object(coin_manager, "_get_free_coins_rpc", return_value={
                 "records": [
                     _record("0xcoin1", 100),
                     _record("0xcoin2", 200),
                 ]
             }), \
             patch.object(coin_manager, "log_event"):
            owned_map = manager._get_owned_coin_amount_map(1, "topup_test")

        self.assertEqual(owned_map, {
            "0xcoin1": 100,
            "0xcoin2": 200,
        })

    def test_two_step_split_aborts_when_address_lookup_fails(self):
        manager = self._make_manager()

        with patch.object(coin_manager, "get_next_address", return_value=None), \
             patch.object(coin_manager, "log_event"):
            with self.assertRaises(coin_manager._TopupWalletDegraded):
                manager._two_step_split(
                    name="XCH-mid",
                    wallet_id=1,
                    source_coin_id="0xsource",
                    pool_amount_mojos=400,
                    num_to_create=4,
                    trading_size_mojos=100,
                    is_cat=False,
                )

    def test_two_step_split_continues_when_spacescan_confirms_split_spend(self):
        manager = self._make_manager()

        # sage_topup_split is imported locally inside _sage_one_step_split via
        # `from wallet_sage import sage_topup_split`.  Patch it at the source
        # module so the local `from … import` picks up the mock at call time.
        # Scenario: sage_topup_split returns None (e.g. RPC hiccup), but
        # spacescan confirms the split landed on-chain, so the method recovers
        # and waits until the output coins become selectable.
        with patch("wallet_sage.sage_topup_split", return_value=None), \
             patch.object(coin_manager, "get_next_address", return_value={"success": True, "address": "xch1testaddress"}), \
             patch.object(coin_manager, "get_wallet_type", return_value="sage"), \
             patch.object(manager, "_spacescan_self_send_confirmed", return_value=True), \
             patch.object(manager, "_get_owned_coin_amount_map", side_effect=[
                 {"0xsource": 600},             # pre-snapshot
                 {"0xsource": 600},             # iteration 1 — no new coins yet
                 {"0xchange": 200, "0xa": 100, "0xb": 100, "0xc": 100, "0xd": 100},  # iteration 2 — coins ready
             ]), \
             patch.object(manager, "_get_strict_selectable_coin_id_set", side_effect=[
                 set(),                                      # iteration 1
                 {"0xa", "0xb", "0xc", "0xd"},              # iteration 2
             ]), \
             patch.object(manager, "_get_transaction_confirmation_state", side_effect=[
                 {"known": False, "confirmed": False, "confirmed_count": 0, "total": 1, "height": 0},
                 {"known": False, "confirmed": False, "confirmed_count": 0, "total": 1, "height": 0},
             ]), \
             patch.object(coin_manager, "log_event"), \
             patch.object(coin_manager.time, "sleep", return_value=None):
            result = manager._two_step_split(
                name="XCH-mid",
                wallet_id=1,
                source_coin_id="0xsource",
                pool_amount_mojos=400,
                num_to_create=4,
                trading_size_mojos=100,
                is_cat=False,
            )

        self.assertTrue(result)

    def test_two_step_split_accepts_tx_confirmed_owned_outputs_before_selectable(self):
        manager = self._make_manager()

        # Scenario: sage_topup_split returns a tx result; Sage confirms the tx
        # but the outputs are owned (in owned_map) before they become selectable.
        # The method should accept tx-confirmed + owned outputs as sufficient.
        with patch("wallet_sage.sage_topup_split", return_value={"transaction_id": "def456"}), \
             patch.object(coin_manager, "get_next_address", return_value={"success": True, "address": "xch1testaddress"}), \
             patch.object(coin_manager, "get_wallet_type", return_value="sage"), \
             patch.object(manager, "_get_owned_coin_amount_map", side_effect=[
                 {"0xsource": 600},             # pre-snapshot
                 {"0xsource": 600},             # iteration 1 — tx pending, no new coins
                 {"0xchange": 200, "0xa": 100, "0xb": 100, "0xc": 100, "0xd": 100},  # iteration 2 — coins owned (tx confirmed)
             ]), \
             patch.object(manager, "_get_strict_selectable_coin_id_set", side_effect=[
                 set(),    # iteration 1 — nothing selectable yet
                 set(),    # iteration 2 — coins owned but not yet selectable (key assertion)
             ]), \
             patch.object(manager, "_get_transaction_confirmation_state", side_effect=[
                 {"known": False, "confirmed": False, "confirmed_count": 0, "total": 1, "height": 0},
                 {"known": True, "confirmed": True, "confirmed_count": 1, "total": 1, "height": 123},
             ]), \
             patch.object(coin_manager, "log_event"), \
             patch.object(coin_manager.time, "sleep", return_value=None):
            result = manager._two_step_split(
                name="XCH-mid",
                wallet_id=1,
                source_coin_id="0xsource",
                pool_amount_mojos=400,
                num_to_create=4,
                trading_size_mojos=100,
                is_cat=False,
            )

        self.assertTrue(result)

    def test_sage_topup_split_passes_fee_coin_to_create_transaction(self):
        import wallet_sage

        with patch.object(wallet_sage, "_require_signing_capability", return_value=True), \
             patch.object(wallet_sage, "_get_cat_asset_id", return_value="0xasset"), \
             patch.object(wallet_sage, "create_transaction_rpc", return_value={"success": True}) as create_tx:
            result = wallet_sage.sage_topup_split(
                source_coin_id="0xsource",
                num_coins=1,
                trading_size_mojos=100,
                own_address="xch1testaddress",
                fee_mojos=13,
                is_cat=True,
                fee_coin_id="0xfee",
            )

        self.assertEqual(result, {"success": True})
        create_tx.assert_called_once()
        self.assertEqual(
            create_tx.call_args.kwargs["selected_coin_ids"],
            ["0xsource", "0xfee"],
        )

    def test_create_transaction_rpc_signs_and_submits_coin_spends_without_txid(self):
        import wallet_sage

        with patch.object(wallet_sage, "_require_signing_capability", return_value=True), \
                patch.object(wallet_sage, "rpc", return_value={
                    "summary": {},
                    "coin_spends": [{"coin": "spend"}],
                }) as rpc_mock, \
                patch.object(wallet_sage, "_sage_post", side_effect=[
                    {"spend_bundle": {"aggregated_signature": "0xsig"}},
                    {"success": True, "transaction_id": "0xtx"},
                ]) as sage_post:
            result = wallet_sage.create_transaction_rpc(
                selected_coin_ids=["0xsource"],
                actions=[{"type": "send", "amount": "1"}],
                auto_submit=True,
            )

        rpc_mock.assert_called_once()
        self.assertTrue(result["success"])
        self.assertTrue(result["submitted"])
        self.assertEqual(result["transaction_id"], "0xtx")
        self.assertEqual(sage_post.call_args_list[0].args[0], "sign_coin_spends")
        self.assertEqual(sage_post.call_args_list[1].args[0], "submit_transaction")

    def test_combine_coins_signs_and_submits_coin_spends_without_txid(self):
        import wallet_sage

        with patch.object(wallet_sage, "_require_signing_capability", return_value=True), \
                patch.object(wallet_sage, "rpc", return_value={
                    "summary": {},
                    "coin_spends": [{"coin": "a"}, {"coin": "b"}],
                }) as rpc_mock, \
                patch.object(wallet_sage, "_sage_post", side_effect=[
                    {"spend_bundle": {"aggregated_signature": "0xsig"}},
                    {"success": True, "transaction_id": "0xcombine"},
                ]) as sage_post:
            result = wallet_sage.combine_coins(
                coin_ids=["0xa", "0xb"],
                fee_mojos=1,
            )

        rpc_mock.assert_called_once()
        self.assertTrue(result["success"])
        self.assertTrue(result["submitted"])
        self.assertEqual(result["transaction_id"], "0xcombine")
        self.assertEqual(sage_post.call_args_list[0].args[0], "sign_coin_spends")
        self.assertEqual(sage_post.call_args_list[1].args[0], "submit_transaction")

    def test_create_transaction_rpc_rejects_submit_without_txid_or_pending(self):
        import wallet_sage

        with patch.object(wallet_sage, "_require_signing_capability", return_value=True), \
                patch.object(wallet_sage, "rpc", return_value={
                    "summary": {},
                    "coin_spends": [{"coin": "spend"}],
                }), \
                patch.object(wallet_sage, "_sage_post", side_effect=[
                    {"spend_bundle": {"aggregated_signature": "0xsig"}},
                    {"success": True, "status": "success"},
                ]), \
                patch.object(wallet_sage, "get_pending_transactions", return_value=[]):
            result = wallet_sage.create_transaction_rpc(
                selected_coin_ids=["0xsource"],
                actions=[{"type": "send", "amount": "1"}],
                auto_submit=True,
            )

        self.assertFalse(result["success"])
        self.assertIn("no transaction id", result["error"])

    def test_cat_one_step_topup_reserves_fee_pool_coin(self):
        manager = self._make_manager()
        manager.fee_pool.refresh([_record("0xfee", 1000)])

        with patch("wallet_sage.sage_topup_split", return_value={"transaction_id": "fee123"}) as split_mock, \
             patch.object(coin_manager, "get_next_address", return_value={"success": True, "address": "xch1testaddress"}), \
             patch.object(manager, "_fee_pool_enabled", return_value=True), \
             patch.object(manager, "_tx_fee_mojos", return_value=13), \
             patch.object(manager, "_get_owned_coin_amount_map", side_effect=[
                 {"0xsource": 600},
                 {"0xsource": 600, "0xa": 100},
             ]), \
             patch.object(manager, "_get_strict_selectable_coin_id_set", return_value={"0xa"}), \
             patch.object(manager, "_get_transaction_confirmation_state", return_value={
                 "known": False,
                 "confirmed": False,
                 "confirmed_count": 0,
                 "total": 1,
                 "height": 0,
             }), \
             patch.object(coin_manager, "log_event"), \
             patch.object(coin_manager.time, "sleep", return_value=None):
            result = manager._sage_one_step_split(
                name="CAT-inner",
                wallet_id=2,
                source_coin_id="0xsource",
                num_to_create=1,
                trading_size_mojos=100,
                is_cat=True,
            )

        self.assertTrue(result)
        self.assertEqual(split_mock.call_args.kwargs["fee_coin_id"], "0xfee")
        self.assertEqual(manager.fee_pool.available_count, 0)

    def test_one_step_split_debounces_duplicate_pending_source(self):
        manager = self._make_manager()

        with patch("wallet_sage.sage_topup_split",
                   return_value={"transaction_id": "0xtx"}) as split_mock, \
             patch.object(coin_manager, "get_next_address",
                          return_value={"success": True, "address": "xch1test"}), \
             patch.object(manager, "_tx_fee_mojos", return_value=0), \
             patch.object(manager, "_fee_pool_enabled", return_value=False), \
             patch.object(manager, "_get_owned_coin_amount_map", side_effect=[
                 {},
                 {"0xnew": 100},
                 {},
                 {"0xnew2": 50, "0xnew3": 50},
             ]), \
             patch.object(manager, "_get_strict_selectable_coin_id_set", side_effect=[
                 {"0xnew"},
                 {"0xnew2", "0xnew3"},
             ]), \
             patch.object(manager, "_get_transaction_confirmation_state",
                          return_value={"confirmed": False, "height": None}), \
             patch.object(manager, "_stamp_topup_output_designations"), \
             patch.object(coin_manager, "log_event"), \
             patch.object(coin_manager.time, "sleep", return_value=None):
            first = manager._sage_one_step_split(
                name="CAT-inner",
                wallet_id=2,
                source_coin_id="0xsource",
                num_to_create=1,
                trading_size_mojos=100,
                is_cat=True,
            )
            second = manager._sage_one_step_split(
                name="CAT-inner",
                wallet_id=2,
                source_coin_id="0xsource",
                num_to_create=2,
                trading_size_mojos=50,
                is_cat=True,
            )

        self.assertTrue(first)
        self.assertEqual(second, "pending")
        split_mock.assert_called_once()

    def test_one_step_split_without_txid_and_no_pending_does_not_debounce_source(self):
        manager = self._make_manager()

        times = iter([1000, 1001, 1002, 1123, 1124])

        with patch("wallet_sage.sage_topup_split",
                   return_value={"success": True}) as split_mock, \
             patch("wallet_sage.get_pending_transactions", return_value=[]), \
             patch.object(coin_manager, "get_next_address",
                          return_value={"success": True, "address": "xch1test"}), \
             patch.object(manager, "_tx_fee_mojos", return_value=0), \
             patch.object(manager, "_fee_pool_enabled", return_value=False), \
             patch.object(manager, "_get_owned_coin_amount_map", side_effect=[
                 {"0xsource": 600},
                 {"0xsource": 600},
                 {"0xsource": 600},
                 {"0xsource": 600},
             ]), \
             patch.object(manager, "_get_strict_selectable_coin_id_set",
                          return_value={"0xsource"}), \
             patch.object(manager, "_get_transaction_confirmation_state",
                          return_value={"confirmed": False, "height": None}), \
             patch.object(coin_manager, "log_event") as log_event, \
             patch.object(coin_manager.time, "time", side_effect=lambda: next(times)), \
             patch.object(coin_manager.time, "sleep", return_value=None):
            first = manager._sage_one_step_split(
                name="CAT-inner",
                wallet_id=2,
                source_coin_id="0xsource",
                num_to_create=1,
                trading_size_mojos=100,
                is_cat=True,
            )
            second = manager._sage_one_step_split(
                name="CAT-inner",
                wallet_id=2,
                source_coin_id="0xsource",
                num_to_create=1,
                trading_size_mojos=100,
                is_cat=True,
            )

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(split_mock.call_count, 2)
        event_types = [call.args[1] for call in log_event.call_args_list]
        self.assertIn("topup_cat-inner_osstep_not_submitted", event_types)
        self.assertNotIn("topup_cat-inner_osstep_debounce", event_types)
        not_submitted_levels = [
            call.args[0] for call in log_event.call_args_list
            if call.args[1] == "topup_cat-inner_osstep_not_submitted"
        ]
        self.assertTrue(not_submitted_levels)
        self.assertTrue(all(level == "info" for level in not_submitted_levels))

    def test_one_step_split_without_txid_clears_debounce_after_grace(self):
        manager = self._make_manager()

        clock = {"now": 1000}

        def fake_time():
            clock["now"] += 20
            return clock["now"]

        with patch("wallet_sage.sage_topup_split",
                   return_value={"success": True}) as split_mock, \
             patch("wallet_sage.get_pending_transactions", return_value=[]), \
             patch.object(coin_manager, "get_next_address",
                          return_value={"success": True, "address": "xch1test"}), \
             patch.object(manager, "_tx_fee_mojos", return_value=0), \
             patch.object(manager, "_fee_pool_enabled", return_value=False), \
             patch.object(manager, "_get_owned_coin_amount_map", side_effect=[
                 {"0xsource": 600},  # first pre-snapshot
                 {"0xsource": 600},  # first grace poll
                 {"0xsource": 600},  # second pre-snapshot
             ]), \
             patch.object(manager, "_get_strict_selectable_coin_id_set", side_effect=[
                 set(),          # source briefly hidden just after submit
                 {"0xsource"},   # source returned selectable during grace
                 {"0xsource"},   # second attempt should not be debounced
             ]), \
             patch.object(manager, "_get_transaction_confirmation_state",
                          return_value={"confirmed": False, "height": None}), \
             patch.object(coin_manager, "log_event") as log_event, \
             patch.object(coin_manager.time, "time", side_effect=fake_time), \
             patch.object(coin_manager.time, "sleep", return_value=None):
            first = manager._sage_one_step_split(
                name="CAT-inner",
                wallet_id=2,
                source_coin_id="0xsource",
                num_to_create=1,
                trading_size_mojos=100,
                is_cat=True,
            )
            second = manager._sage_one_step_split(
                name="CAT-inner",
                wallet_id=2,
                source_coin_id="0xsource",
                num_to_create=1,
                trading_size_mojos=100,
                is_cat=True,
            )

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(split_mock.call_count, 2)
        event_types = [call.args[1] for call in log_event.call_args_list]
        self.assertIn("topup_cat-inner_osstep_not_submitted", event_types)
        self.assertNotIn("topup_cat-inner_osstep_debounce", event_types)

    def test_sage_one_step_split_stamps_sniper_outputs(self):
        manager = self._make_manager()

        with patch("wallet_sage.sage_topup_split", return_value={"transaction_id": "sniper123"}), \
             patch("database.upsert_coin") as upsert_coin, \
             patch("database.set_coin_designation") as set_designation, \
             patch.object(coin_manager, "get_next_address", return_value={"success": True, "address": "xch1testaddress"}), \
             patch.object(manager, "_get_owned_coin_amount_map", side_effect=[
                 {"0xsource": 600},
                 {"0xsource": 600, "0xa": 100, "0xb": 100},
             ]), \
             patch.object(manager, "_get_strict_selectable_coin_id_set", return_value={"0xa", "0xb"}), \
             patch.object(manager, "_get_transaction_confirmation_state", return_value={
                 "known": False,
                 "confirmed": False,
                 "confirmed_count": 0,
                 "total": 1,
                 "height": 0,
             }), \
             patch.object(coin_manager, "log_event"), \
             patch.object(coin_manager.time, "sleep", return_value=None):
            result = manager._sage_one_step_split(
                name="XCH-sniper",
                wallet_id=1,
                source_coin_id="0xsource",
                num_to_create=2,
                trading_size_mojos=100,
                is_cat=False,
            )

        self.assertTrue(result)
        self.assertEqual(upsert_coin.call_count, 2)
        for call in upsert_coin.call_args_list:
            self.assertEqual(call.kwargs["wallet_type"], "xch")
            self.assertEqual(call.kwargs["designation"], "tier_spare")
            self.assertEqual(call.kwargs["assigned_tier"], "sniper")
        self.assertEqual(
            {call.args for call in set_designation.call_args_list},
            {("0xa", "tier_spare", "sniper"), ("0xb", "tier_spare", "sniper")},
        )

    def test_topup_prioritizes_missing_offers_over_xch_misfit_absorption(self):
        manager = self._make_manager()
        manager._topup_is_drip = True

        tier_counts = {"inner": 7, "mid": 7, "outer": 6, "extreme": 4}
        prepped_counts = {"inner": 17, "mid": 12, "outer": 9, "extreme": 6}
        tier_sizes = {
            "inner": 26_000_000,
            "mid": 13_000_000,
            "outer": 6_500_000,
            "extreme": 3_250_000,
        }

        def _coins(n, prefix):
            return [_record(f"0x{prefix}{i}", 1_000_000) for i in range(n)]

        xch_inv = {
            "reserve": [_record("0xxchreserve", 10_000_000_000_000)],
            "small": [_record("0xxchmisfit", 500_000_000_000)],
            "inner": _coins(20, "xi"),
            "mid": _coins(20, "xm"),
            "outer": _coins(20, "xo"),
            "extreme": _coins(20, "xe"),
            "sniper": [],
            "fees": _coins(50, "xf"),
        }
        cat_inv = {
            "reserve": [_record("0xcatreserve", 500_000_000)],
            "small": [],
            "inner": [],
            "mid": _coins(14, "cm"),
            "outer": _coins(4, "co"),
            "extreme": _coins(1, "ce"),
            "sniper": [],
            "fees": [],
        }

        def classify(_records, wallet_type, _tier_sizes):
            return xch_inv if wallet_type == "xch" else cat_inv

        def open_offers(side=None, cat_asset_id=None, **_kwargs):
            del cat_asset_id
            if side == "buy":
                return [
                    {"side": "buy", "tier": tier}
                    for tier, count in tier_counts.items()
                    for _ in range(count)
                ]
            if side == "sell":
                return [
                    {"side": "sell", "tier": tier}
                    for tier, count in {"mid": 7, "outer": 6, "extreme": 4}.items()
                    for _ in range(count)
                ]
            return []

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.get_wallet_sync_status = lambda: {"synced": True, "reachable": True}

        absorb_calls = []

        def absorb(name, *_args, **_kwargs):
            absorb_calls.append(name)
            return name == "XCH"

        original_reversed = getattr(coin_manager.cfg, "BUY_LADDER_REVERSED", False)
        self.addCleanup(setattr, coin_manager.cfg, "BUY_LADDER_REVERSED", original_reversed)
        coin_manager.cfg.BUY_LADDER_REVERSED = False

        with patch.dict(sys.modules, {"wallet": fake_wallet}), \
                patch.object(coin_manager.cfg, "TIER_ENABLED", True), \
                patch.object(coin_manager.cfg, "ENABLE_BUY", True), \
                patch.object(coin_manager.cfg, "ENABLE_SELL", True), \
                patch.object(coin_manager.cfg, "MAX_ACTIVE_BUY_OFFERS", 24), \
                patch.object(coin_manager.cfg, "MAX_ACTIVE_SELL_OFFERS", 24), \
                patch.object(coin_manager.cfg, "WALLET_ID_XCH", 1), \
                patch.object(coin_manager.cfg, "CAT_WALLET_ID", 2), \
                patch.object(coin_manager.cfg, "CAT_ASSET_ID", "a" * 64), \
                patch.object(coin_manager.cfg, "CAT_DECIMALS", 3), \
                patch.object(coin_manager.cfg, "COIN_PREP_MULTIPLIER", Decimal("1")), \
                patch.object(coin_manager, "get_tier_distribution", return_value=tier_counts), \
                patch.object(coin_manager, "get_weighted_tier_prep_counts", return_value=prepped_counts), \
                patch.object(coin_manager, "_get_free_coins_rpc", return_value={
                    "confirmed_records": [_record("0xwalletcoin", 1)]
                }), \
                patch.object(manager, "_classify_coins_by_designation", side_effect=classify), \
                patch.object(manager, "_get_tier_sizes_mojos", return_value=tier_sizes), \
                patch.object(manager, "_configured_tier_sizes_xch", return_value={
                    "inner": Decimal("1"),
                    "mid": Decimal("0.5"),
                    "outer": Decimal("0.25"),
                    "extreme": Decimal("0.125"),
                }), \
                patch.object(manager, "get_trading_pace", return_value="normal"), \
                patch.object(manager, "_absorb_misfits_to_reserve", side_effect=absorb), \
                patch.object(manager, "_smart_topup_wallet", return_value=True) as smart_topup, \
                patch("database.get_open_offers", side_effect=open_offers):
            manager._topup_worker(active_buy=24, active_sell=17)

        self.assertNotIn("XCH", absorb_calls)
        smart_topup.assert_called_once()
        self.assertEqual(smart_topup.call_args.args[0], "CAT-inner")

    def test_topup_uses_sell_cap_for_sell_offer_deficit_scan(self):
        manager = self._make_manager()
        manager._topup_is_drip = True

        def _coins(n, prefix):
            return [_record(f"0x{prefix}{i}", 1_000_000) for i in range(n)]

        xch_inv = {
            "reserve": [_record("0xxchreserve", 10_000_000_000_000)],
            "small": [_record("0xxchmisfit", 500_000_000_000)],
            "inner": _coins(100, "xi"),
            "mid": _coins(100, "xm"),
            "outer": _coins(100, "xo"),
            "extreme": _coins(100, "xe"),
            "sniper": [],
            "fees": _coins(50, "xf"),
        }
        cat_inv = {
            "reserve": [_record("0xcatreserve", 500_000_000)],
            "small": [],
            "inner": _coins(100, "ci"),
            "mid": _coins(100, "cm"),
            "outer": _coins(100, "co"),
            "extreme": _coins(100, "ce"),
            "sniper": [],
            "fees": [],
        }

        def classify(_records, wallet_type, _tier_sizes):
            return xch_inv if wallet_type == "xch" else cat_inv

        def open_offers(side=None, cat_asset_id=None, **_kwargs):
            del cat_asset_id
            counts = {
                "buy": {"inner": 14, "mid": 13, "outer": 11, "extreme": 7},
                "sell": {"inner": 14, "mid": 13, "outer": 11, "extreme": 6},
            }.get(side, {})
            return [
                {"side": side, "tier": tier}
                for tier, count in counts.items()
                for _ in range(count)
            ]

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.get_wallet_sync_status = lambda: {"synced": True, "reachable": True}
        prepared_counts = {"inner": 100, "mid": 100, "outer": 100, "extreme": 100}
        tier_sizes = {
            "inner": 26_000_000,
            "mid": 13_000_000,
            "outer": 6_500_000,
            "extreme": 3_250_000,
        }
        absorb_calls = []

        def absorb(name, *_args, **_kwargs):
            absorb_calls.append(name)
            return name == "XCH"

        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"wallet": fake_wallet}))
            for name, value in {
                "TIER_ENABLED": True,
                "ENABLE_BUY": True,
                "ENABLE_SELL": True,
                "BUY_LADDER_REVERSED": True,
                "MAX_ACTIVE_BUY_OFFERS": 45,
                "MAX_ACTIVE_SELL_OFFERS": 44,
                "BUY_INNER_TIER_COUNT": 14,
                "BUY_MID_TIER_COUNT": 13,
                "BUY_OUTER_TIER_COUNT": 11,
                "BUY_EXTREME_TIER_COUNT": 7,
                "SELL_INNER_TIER_COUNT": 14,
                "SELL_MID_TIER_COUNT": 13,
                "SELL_OUTER_TIER_COUNT": 11,
                "SELL_EXTREME_TIER_COUNT": 7,
                "WALLET_ID_XCH": 1,
                "CAT_WALLET_ID": 2,
                "CAT_ASSET_ID": "a" * 64,
                "CAT_DECIMALS": 3,
                "COIN_PREP_MULTIPLIER": Decimal("1"),
            }.items():
                stack.enter_context(patch.object(coin_manager.cfg, name, value))
            stack.enter_context(patch.object(
                coin_manager,
                "get_weighted_tier_prep_counts",
                return_value=prepared_counts,
            ))
            stack.enter_context(patch.object(coin_manager, "_get_free_coins_rpc", return_value={
                "confirmed_records": [_record("0xwalletcoin", 1)]
            }))
            stack.enter_context(patch.object(
                manager,
                "_classify_coins_by_designation",
                side_effect=classify,
            ))
            stack.enter_context(patch.object(
                manager,
                "_get_tier_sizes_mojos",
                return_value=tier_sizes,
            ))
            stack.enter_context(patch.object(manager, "get_trading_pace", return_value="normal"))
            stack.enter_context(patch.object(
                manager,
                "_absorb_misfits_to_reserve",
                side_effect=absorb,
            ))
            stack.enter_context(patch.object(manager, "_smart_topup_wallet", return_value=True))
            log_event = stack.enter_context(patch.object(coin_manager, "log_event"))
            stack.enter_context(patch("database.get_open_offers", side_effect=open_offers))
            manager._topup_worker(active_buy=45, active_sell=44)

        self.assertIn("XCH", absorb_calls)
        self.assertNotIn(
            "topup_missing_offers_prioritized",
            [call.args[1] for call in log_event.call_args_list],
        )

    def test_topup_prioritizes_floor_spares_over_xch_misfit_absorption(self):
        manager = self._make_manager()
        manager._topup_is_drip = True

        tier_counts = {"inner": 7, "mid": 7, "outer": 6, "extreme": 4}
        prepped_counts = {"inner": 17, "mid": 12, "outer": 9, "extreme": 6}
        tier_sizes = {
            "inner": 26_000_000,
            "mid": 13_000_000,
            "outer": 6_500_000,
            "extreme": 3_250_000,
        }

        def _coins(n, prefix):
            return [_record(f"0x{prefix}{i}", 1_000_000) for i in range(n)]

        xch_inv = {
            "reserve": [_record("0xxchreserve", 10_000_000_000_000)],
            "small": [_record("0xxchmisfit", 500_000_000_000)],
            "inner": _coins(20, "xi"),
            "mid": _coins(20, "xm"),
            "outer": _coins(20, "xo"),
            "extreme": _coins(20, "xe"),
            "sniper": [],
            "fees": _coins(50, "xf"),
        }
        cat_inv = {
            "reserve": [_record("0xcatreserve", 500_000_000)],
            "small": [],
            "inner": [],
            "mid": _coins(14, "cm"),
            "outer": _coins(4, "co"),
            "extreme": _coins(1, "ce"),
            "sniper": [],
            "fees": [],
        }

        def classify(_records, wallet_type, _tier_sizes):
            return xch_inv if wallet_type == "xch" else cat_inv

        def open_offers(side=None, cat_asset_id=None, **_kwargs):
            del cat_asset_id
            if side in {"buy", "sell"}:
                return [
                    {"side": side, "tier": tier}
                    for tier, count in tier_counts.items()
                    for _ in range(count)
                ]
            return []

        fake_wallet = types.ModuleType("wallet")
        fake_wallet.get_wallet_sync_status = lambda: {"synced": True, "reachable": True}

        absorb_calls = []

        def absorb(name, *_args, **_kwargs):
            absorb_calls.append(name)
            return name == "XCH"

        original_reversed = getattr(coin_manager.cfg, "BUY_LADDER_REVERSED", False)
        self.addCleanup(setattr, coin_manager.cfg, "BUY_LADDER_REVERSED", original_reversed)
        coin_manager.cfg.BUY_LADDER_REVERSED = False

        with patch.dict(sys.modules, {"wallet": fake_wallet}), \
                patch.object(coin_manager.cfg, "TIER_ENABLED", True), \
                patch.object(coin_manager.cfg, "ENABLE_BUY", True), \
                patch.object(coin_manager.cfg, "ENABLE_SELL", True), \
                patch.object(coin_manager.cfg, "MAX_ACTIVE_BUY_OFFERS", 24), \
                patch.object(coin_manager.cfg, "MAX_ACTIVE_SELL_OFFERS", 24), \
                patch.object(coin_manager.cfg, "WALLET_ID_XCH", 1), \
                patch.object(coin_manager.cfg, "CAT_WALLET_ID", 2), \
                patch.object(coin_manager.cfg, "CAT_ASSET_ID", "a" * 64), \
                patch.object(coin_manager.cfg, "CAT_DECIMALS", 3), \
                patch.object(coin_manager.cfg, "COIN_PREP_MULTIPLIER", Decimal("1")), \
                patch.object(coin_manager, "get_tier_distribution", return_value=tier_counts), \
                patch.object(coin_manager, "get_weighted_tier_prep_counts", return_value=prepped_counts), \
                patch.object(coin_manager, "_get_free_coins_rpc", return_value={
                    "confirmed_records": [_record("0xwalletcoin", 1)]
                }), \
                patch.object(manager, "_classify_coins_by_designation", side_effect=classify), \
                patch.object(manager, "_get_tier_sizes_mojos", return_value=tier_sizes), \
                patch.object(manager, "_configured_tier_sizes_xch", return_value={
                    "inner": Decimal("1"),
                    "mid": Decimal("0.5"),
                    "outer": Decimal("0.25"),
                    "extreme": Decimal("0.125"),
                }), \
                patch.object(manager, "get_trading_pace", return_value="normal"), \
                patch.object(manager, "_absorb_misfits_to_reserve", side_effect=absorb), \
                patch.object(manager, "_smart_topup_wallet", return_value=True) as smart_topup, \
                patch("database.get_open_offers", side_effect=open_offers):
            manager._topup_worker(active_buy=24, active_sell=24)

        self.assertNotIn("XCH", absorb_calls)
        smart_topup.assert_called_once()
        self.assertEqual(smart_topup.call_args.args[0], "CAT-inner")

    def test_absorb_misfits_debounces_duplicate_pending_inputs(self):
        manager = self._make_manager()
        reserve = _record("0xreserve", 1_000_000_000)
        small = _record("0xsmall", 200_000_000)
        inventory = {
            "reserve": [reserve],
            "small": [small],
            "inner": [],
            "mid": [],
            "outer": [],
            "extreme": [],
        }
        tier_sizes = {
            "inner": 500_000_000,
            "mid": 250_000_000,
            "outer": 125_000_000,
            "extreme": 60_000_000,
        }
        free_result = {"confirmed_records": [reserve, small]}

        with patch.object(coin_manager, "get_wallet_type", return_value="sage"), \
                patch.object(coin_manager, "_get_free_coins_rpc", return_value=free_result), \
                patch.object(manager, "_tx_fee_mojos", return_value=1), \
                patch.object(manager, "_filter_out_protected_coin_ids", side_effect=lambda ids: ids), \
                patch.object(manager, "_record_topup_pool_refund"), \
                patch("database.set_setting", return_value=True), \
                patch("wallet_sage.combine_coins", return_value={
                    "success": True,
                    "transaction_id": "0xabsorbtx",
                    "coin_spends": [{}, {}],
                }) as combine:
            first = manager._absorb_misfits_to_reserve(
                "XCH",
                1,
                inventory,
                tier_sizes,
                is_cat=False,
            )
            second = manager._absorb_misfits_to_reserve(
                "XCH",
                1,
                inventory,
                tier_sizes,
                is_cat=False,
            )

        self.assertTrue(first)
        self.assertEqual(second, "pending")
        combine.assert_called_once()

    def test_absorb_misfits_without_txid_and_no_pending_does_not_debounce_inputs(self):
        manager = self._make_manager()
        reserve = _record("0xreserve", 1_000_000_000)
        small = _record("0xsmall", 200_000_000)
        inventory = {
            "reserve": [reserve],
            "small": [small],
            "inner": [],
            "mid": [],
            "outer": [],
            "extreme": [],
        }
        tier_sizes = {
            "inner": 500_000_000,
            "mid": 250_000_000,
            "outer": 125_000_000,
            "extreme": 60_000_000,
        }
        free_result = {"confirmed_records": [reserve, small]}

        with patch.object(coin_manager, "get_wallet_type", return_value="sage"), \
                patch.object(coin_manager, "_get_free_coins_rpc", return_value=free_result), \
                patch.object(manager, "_tx_fee_mojos", return_value=1), \
                patch.object(manager, "_filter_out_protected_coin_ids", side_effect=lambda ids: ids), \
                patch.object(manager, "_get_strict_selectable_coin_id_set",
                             return_value={"0xreserve", "0xsmall"}), \
                patch.object(manager, "_record_topup_pool_refund") as refund, \
                patch("wallet_sage.get_pending_transactions", return_value=[]), \
                patch("wallet_sage.combine_coins", return_value={
                    "summary": {},
                    "coin_spends": [{}, {}],
                }) as combine, \
                patch.object(coin_manager, "log_event") as log_event:
            first = manager._absorb_misfits_to_reserve(
                "XCH",
                1,
                inventory,
                tier_sizes,
                is_cat=False,
            )
            second = manager._absorb_misfits_to_reserve(
                "XCH",
                1,
                inventory,
                tier_sizes,
                is_cat=False,
            )

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(combine.call_count, 2)
        refund.assert_not_called()
        event_types = [call.args[1] for call in log_event.call_args_list]
        self.assertIn("topup_xch_absorb_not_submitted", event_types)
        self.assertNotIn("topup_xch_absorb_debounce", event_types)
        not_submitted_levels = [
            call.args[0] for call in log_event.call_args_list
            if call.args[1] == "topup_xch_absorb_not_submitted"
        ]
        self.assertTrue(not_submitted_levels)
        self.assertTrue(all(level == "info" for level in not_submitted_levels))

    def test_pool_rebuild_waits_quietly_when_consolidation_is_pending(self):
        manager = self._make_manager()
        reserve = _record("0xreserve", 150)
        keep_mid = _record("0xmidkeep", 400)
        excess_a = _record("0xmida", 400)
        excess_b = _record("0xmidb", 400)
        inventory = {
            "reserve": [reserve],
            "small": [],
            "inner": [],
            "mid": [excess_a, excess_b, keep_mid],
            "outer": [],
            "extreme": [],
        }
        fresh_result = {"confirmed_records": [reserve, excess_a, excess_b, keep_mid]}

        class _Conn:
            def execute(self, *args, **kwargs):
                return self

            def fetchall(self):
                return []

        with patch.object(coin_manager, "_get_free_coins_rpc", return_value=fresh_result), \
                patch.object(manager, "_configured_tier_sizes_xch", return_value={}), \
                patch.object(coin_manager, "get_weighted_tier_prep_counts",
                             return_value={"mid": 1}), \
                patch("database.get_connection", return_value=_Conn()), \
                patch.object(manager, "_consolidate_coins",
                             return_value="pending") as consolidate, \
                patch.object(manager, "_record_topup_pool_refund") as refund, \
                patch.object(coin_manager, "log_event") as log_event:
            result = manager._smart_topup_wallet(
                "CAT-inner",
                2,
                inventory,
                trading_size_mojos=450,
                needed=2,
                is_cat=True,
                tier_is_empty=True,
            )

        self.assertFalse(result)
        consolidate.assert_called_once()
        refund.assert_not_called()
        event_types = [call.args[1] for call in log_event.call_args_list]
        self.assertIn("topup_cat-inner_pool_rebuild_wait", event_types)
        self.assertNotIn("topup_cat-inner_pool_rebuild_ok", event_types)
        self.assertNotIn("topup_cat-inner_pool_rebuild_fail", event_types)

    def test_pool_rebuild_not_submitted_is_info_retry_not_warning_fail(self):
        manager = self._make_manager()
        reserve = _record("0xreserve", 150)
        keep_mid = _record("0xmidkeep", 400)
        excess_a = _record("0xmida", 400)
        excess_b = _record("0xmidb", 400)
        inventory = {
            "reserve": [reserve],
            "small": [],
            "inner": [],
            "mid": [excess_a, excess_b, keep_mid],
            "outer": [],
            "extreme": [],
        }
        fresh_result = {"confirmed_records": [reserve, excess_a, excess_b, keep_mid]}

        class _Conn:
            def execute(self, *args, **kwargs):
                return self

            def fetchall(self):
                return []

        def not_submitted_consolidate(*args, **kwargs):
            manager._last_consolidate_not_submitted = True
            return False

        with patch.object(coin_manager, "_get_free_coins_rpc", return_value=fresh_result), \
                patch.object(manager, "_configured_tier_sizes_xch", return_value={}), \
                patch.object(coin_manager, "get_weighted_tier_prep_counts",
                             return_value={"mid": 1}), \
                patch("database.get_connection", return_value=_Conn()), \
                patch.object(manager, "_consolidate_coins",
                             side_effect=not_submitted_consolidate) as consolidate, \
                patch.object(manager, "_record_topup_pool_refund") as refund, \
                patch.object(coin_manager, "log_event") as log_event:
            result = manager._smart_topup_wallet(
                "CAT-inner",
                2,
                inventory,
                trading_size_mojos=450,
                needed=2,
                is_cat=True,
                tier_is_empty=True,
            )

        self.assertFalse(result)
        consolidate.assert_called_once()
        refund.assert_not_called()
        events = [(call.args[0], call.args[1]) for call in log_event.call_args_list]
        self.assertIn(("info", "topup_cat-inner_pool_rebuild_not_submitted"), events)
        self.assertNotIn(("warning", "topup_cat-inner_pool_rebuild_fail"), events)

    def test_consolidate_coins_debounces_duplicate_pending_inputs(self):
        manager = self._make_manager()
        source_ids = ["0xa", "0xb", "0xc"]

        with patch.object(coin_manager, "get_wallet_type", return_value="sage"), \
                patch.object(manager, "_tx_fee_mojos", return_value=1), \
                patch.object(manager, "_filter_out_protected_coin_ids",
                             side_effect=lambda ids: ids), \
                patch("wallet_sage.combine_coins", return_value={
                    "success": True,
                    "transaction_id": "0xcombinetx",
                    "coin_spends": [{}, {}, {}],
                }) as combine:
            first = manager._consolidate_coins(
                "CAT-inner",
                2,
                119_442_000,
                True,
                source_coin_ids=source_ids,
            )
            second = manager._consolidate_coins(
                "CAT-inner",
                2,
                119_442_000,
                True,
                source_coin_ids=source_ids,
            )

        self.assertTrue(first)
        self.assertEqual(second, "pending")
        combine.assert_called_once()

    def test_consolidate_coins_without_txid_and_no_pending_does_not_debounce_inputs(self):
        manager = self._make_manager()
        source_ids = ["0xa", "0xb", "0xc"]

        with patch.object(coin_manager, "get_wallet_type", return_value="sage"), \
                patch.object(manager, "_tx_fee_mojos", return_value=1), \
                patch.object(manager, "_filter_out_protected_coin_ids",
                             side_effect=lambda ids: ids), \
                patch.object(manager, "_get_strict_selectable_coin_id_set",
                             return_value={"0xa", "0xb", "0xc"}), \
                patch("wallet_sage.get_pending_transactions", return_value=[]), \
                patch("wallet_sage.combine_coins", return_value={
                    "summary": {},
                    "coin_spends": [{}, {}, {}],
                }) as combine, \
                patch.object(coin_manager, "log_event") as log_event:
            first = manager._consolidate_coins(
                "CAT-inner",
                2,
                119_442_000,
                True,
                source_coin_ids=source_ids,
            )
            second = manager._consolidate_coins(
                "CAT-inner",
                2,
                119_442_000,
                True,
                source_coin_ids=source_ids,
            )

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(combine.call_count, 2)
        event_types = [call.args[1] for call in log_event.call_args_list]
        self.assertIn("consolidate_cat-inner_combine_not_submitted", event_types)
        self.assertNotIn("consolidate_cat-inner_debounce", event_types)
        not_submitted_levels = [
            call.args[0] for call in log_event.call_args_list
            if call.args[1] == "consolidate_cat-inner_combine_not_submitted"
        ]
        self.assertTrue(not_submitted_levels)
        self.assertTrue(all(level == "info" for level in not_submitted_levels))

    def test_consolidate_coins_without_txid_keeps_hidden_inputs_pending(self):
        manager = self._make_manager()
        source_ids = ["0xa", "0xb", "0xc"]

        with patch.object(coin_manager, "get_wallet_type", return_value="sage"), \
                patch.object(manager, "_tx_fee_mojos", return_value=1), \
                patch.object(manager, "_filter_out_protected_coin_ids",
                             side_effect=lambda ids: ids), \
                patch.object(manager, "_get_strict_selectable_coin_id_set",
                             return_value=set()), \
                patch("wallet_sage.get_pending_transactions", return_value=[]), \
                patch("wallet_sage.combine_coins", return_value={
                    "summary": {},
                    "coin_spends": [{}, {}, {}],
                }) as combine, \
                patch.object(coin_manager.cfg, "TOPUP_COMBINE_NO_TXID_GRACE_SECS",
                             0, create=True), \
                patch.object(coin_manager, "log_event") as log_event:
            result = manager._consolidate_coins(
                "CAT-inner",
                2,
                119_442_000,
                True,
                source_coin_ids=source_ids,
            )

        self.assertEqual(result, "pending")
        combine.assert_called_once()
        event_types = [call.args[1] for call in log_event.call_args_list]
        self.assertIn("consolidate_cat-inner_combine_unverified", event_types)
        self.assertNotIn("consolidate_cat-inner_combine_ok", event_types)

    def test_pool_rebuild_combines_undersized_reserve_with_excess_spares(self):
        manager = self._make_manager()
        reserve = _record("0xreserve", 150)
        keep_mid = _record("0xmidkeep", 400)
        excess_a = _record("0xmida", 400)
        excess_b = _record("0xmidb", 400)
        inventory = {
            "reserve": [reserve],
            "small": [],
            "inner": [],
            "mid": [excess_a, excess_b, keep_mid],
            "outer": [],
            "extreme": [],
        }
        fresh_result = {"confirmed_records": [reserve, excess_a, excess_b, keep_mid]}

        class _Conn:
            def execute(self, *args, **kwargs):
                return self

            def fetchall(self):
                return []

        with patch.object(coin_manager, "_get_free_coins_rpc", return_value=fresh_result), \
                patch.object(manager, "_configured_tier_sizes_xch", return_value={}), \
                patch.object(coin_manager, "get_weighted_tier_prep_counts",
                             return_value={"mid": 1}), \
                patch("database.get_connection", return_value=_Conn()), \
                patch.object(manager, "_consolidate_coins", return_value=True) as consolidate:
            result = manager._smart_topup_wallet(
                "CAT-inner",
                2,
                inventory,
                trading_size_mojos=450,
                needed=2,
                is_cat=True,
                tier_is_empty=True,
            )

        self.assertEqual(result, "rebuild")
        consolidate.assert_called_once()
        call = consolidate.call_args
        self.assertEqual(call.args[:4], ("CAT-inner", 2, 950, True))
        self.assertEqual(
            set(call.kwargs["source_coin_ids"]),
            {"0xreserve", "0xmida", "0xmidb"},
        )

    def test_floor_priority_pool_rebuild_borrows_from_farther_spares(self):
        manager = self._make_manager()
        reserve = _record("0xreserve", 150)
        keep_mid = _record("0xmidkeep", 400)
        borrow_a = _record("0xmida", 400)
        borrow_b = _record("0xmidb", 400)
        inventory = {
            "reserve": [reserve],
            "small": [],
            "inner": [],
            "mid": [borrow_a, borrow_b, keep_mid],
            "outer": [],
            "extreme": [],
        }
        fresh_result = {"confirmed_records": [reserve, borrow_a, borrow_b, keep_mid]}

        class _Conn:
            def execute(self, *args, **kwargs):
                return self

            def fetchall(self):
                return []

        with patch.object(coin_manager, "_get_free_coins_rpc", return_value=fresh_result), \
                patch.object(manager, "_configured_tier_sizes_xch", return_value={}), \
                patch.object(coin_manager, "get_weighted_tier_prep_counts",
                             return_value={"mid": 3}), \
                patch("database.get_connection", return_value=_Conn()), \
                patch.object(manager, "_consolidate_coins", return_value=True) as consolidate:
            result = manager._smart_topup_wallet(
                "CAT-inner",
                2,
                inventory,
                trading_size_mojos=450,
                needed=2,
                is_cat=True,
                tier_is_empty=True,
                soft_budget_bypass_reason="floor-nearest sell slot",
            )

        self.assertEqual(result, "rebuild")
        consolidate.assert_called_once()
        call = consolidate.call_args
        self.assertEqual(call.args[:4], ("CAT-inner", 2, 950, True))
        self.assertEqual(
            set(call.kwargs["source_coin_ids"]),
            {"0xreserve", "0xmida", "0xmidb"},
        )


if __name__ == "__main__":
    unittest.main()
