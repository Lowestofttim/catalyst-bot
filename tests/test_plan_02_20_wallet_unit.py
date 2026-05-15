"""Slice 02-20 — wallet.py unit tests.

The wallet module is a thin dispatch layer that re-exports a unified API from
either wallet_sage or wallet_chia based on WALLET_TYPE. Tests verify the
dispatch decision, that get_wallet_type() returns a known backend, that all
expected public names are exported, and that dispatched callables are callable.
No wallet RPC calls are made — functions are only referenced, not invoked.
"""

import unittest

try:
    import wallet as _w
    from wallet import get_wallet_type

    _SKIP = None
except ModuleNotFoundError as exc:
    _SKIP = str(exc)

# Names that MUST be present regardless of backend
_REQUIRED_NAMES = [
    "WALLET_ID_XCH",
    "get_spendable_coins",
    "create_offer",
    "cancel_offer",
    "get_all_offers",
    "cancel_offers_batch",
    "get_wallet_balance",
    "get_next_address",
    "send_transaction",
    "split_coins_rpc",
    "get_wallet_type",
    "get_exact_spendable_coins_rpc",
    "get_owned_coins",
    "get_owned_coins_detailed",
    "classify_offers_from_list",
    "cat_to_mojos",
]


@unittest.skipIf(_SKIP is not None, f"wallet unavailable: {_SKIP}")
class TestWalletDispatch(unittest.TestCase):
    def test_get_wallet_type_returns_string(self):
        self.assertIsInstance(get_wallet_type(), str)

    def test_get_wallet_type_known_backend(self):
        self.assertIn(get_wallet_type(), ("sage", "chia"))

    def test_wallet_type_constant_matches_getter(self):
        self.assertEqual(_w.WALLET_TYPE, get_wallet_type())

    def test_wallet_type_is_lowercase(self):
        self.assertEqual(_w.WALLET_TYPE, _w.WALLET_TYPE.lower())

    def test_wallet_id_xch_is_int(self):
        self.assertIsInstance(_w.WALLET_ID_XCH, int)

    def test_wallet_id_xch_positive(self):
        self.assertGreater(_w.WALLET_ID_XCH, 0)


@unittest.skipIf(_SKIP is not None, f"wallet unavailable: {_SKIP}")
class TestWalletExports(unittest.TestCase):
    def test_all_required_names_exported(self):
        missing = [name for name in _REQUIRED_NAMES if not hasattr(_w, name)]
        self.assertEqual(missing, [], f"Missing exports: {missing}")

    def test_create_offer_callable(self):
        self.assertTrue(callable(_w.create_offer))

    def test_cancel_offer_callable(self):
        self.assertTrue(callable(_w.cancel_offer))

    def test_get_spendable_coins_callable(self):
        self.assertTrue(callable(_w.get_spendable_coins))

    def test_split_coins_rpc_callable(self):
        self.assertTrue(callable(_w.split_coins_rpc))

    def test_get_wallet_balance_callable(self):
        self.assertTrue(callable(_w.get_wallet_balance))

    def test_get_exact_spendable_coins_rpc_callable(self):
        self.assertTrue(callable(_w.get_exact_spendable_coins_rpc))

    def test_classify_offers_from_list_callable(self):
        self.assertTrue(callable(_w.classify_offers_from_list))

    def test_cat_to_mojos_callable(self):
        self.assertTrue(callable(_w.cat_to_mojos))

    def test_get_owned_coins_detailed_callable(self):
        # Sage: real function; Chia: compatibility stub that returns None
        self.assertTrue(callable(_w.get_owned_coins_detailed))

    def test_sage_backend_has_delete_offer(self):
        # Sage-specific: sage_delete_offer only present on sage backend
        if get_wallet_type() == "sage":
            self.assertTrue(hasattr(_w, "sage_delete_offer"))
            self.assertTrue(callable(_w.sage_delete_offer))


if __name__ == "__main__":
    unittest.main()
