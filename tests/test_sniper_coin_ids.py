import sys
import types
import unittest
from decimal import Decimal
from unittest.mock import patch


class _FakeCfg:
    WALLET_ID_XCH = 1
    CAT_WALLET_ID = 2
    CAT_DECIMALS = 3
    CAT_ASSET_ID = "asset123"
    DRY_RUN = False
    SNIPER_EXPIRY_SECS = 1800
    SNIPER_SIZE_XCH = Decimal("0.001")
    SNIPER_PREP_COUNT = 20
    COIN_IDS_ENABLED = True
    DEXIE_AUTO_POST = True
    SPLASH_ENABLED = True
    ENABLE_BUY = True
    ENABLE_SELL = True
    DEFAULT_TRADE_XCH = Decimal("0.2")
    MIN_TRADE_XCH = Decimal("0.01")
    MAX_TRADE_XCH = Decimal("5")
    SNIPER_COOLDOWN_SECS = 0
    INVENTORY_ENABLED = False


_ORIG_MODULES = {
    name: sys.modules.get(name)
    for name in ("config", "database", "offer_manager")
    # "sniper" is intentionally NOT included here: we need sys.modules["sniper"]
    # to remain pointing at the freshly-imported fake-based module so that
    # patch("sniper.add_offer") targets the same module the Sniper class uses.
}

fake_config = types.ModuleType("config")
fake_config.cfg = _FakeCfg()
sys.modules["config"] = fake_config

fake_database = types.ModuleType("database")
fake_database.log_event = lambda *args, **kwargs: None
fake_database.add_offer = lambda *args, **kwargs: True
fake_database.lock_coin = lambda *args, **kwargs: None
sys.modules["database"] = fake_database

fake_offer_manager = types.ModuleType("offer_manager")
fake_offer_manager.xch_to_mojos = lambda x: int(
    Decimal(str(x)) * Decimal("1000000000000")
)
fake_offer_manager.cat_to_mojos = lambda x, decimals: int(
    Decimal(str(x)) * (Decimal(10) ** decimals)
)
fake_offer_manager.mojos_to_cat = lambda mojos, decimals: (
    Decimal(str(mojos)) / (Decimal(10) ** decimals)
)
sys.modules["offer_manager"] = fake_offer_manager

# Pop sniper so it re-imports with our fakes rather than the cached version
# loaded by test_api_local_guard (which uses real config/database/wallet).
sys.modules.pop("sniper", None)
from sniper import Sniper

# Restore config/database/offer_manager to originals so subsequent test files
# aren't affected.  Keep sys.modules["sniper"] as the freshly imported module
# so that patch("sniper.add_offer") patches the right object.
for _name, _mod in _ORIG_MODULES.items():
    if _mod is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _mod


class _FakeOfferManager:
    def __init__(self):
        self._offer_details_cache = {}
        self._cycle_used_coin_ids = set()
        self.calls = []

    def create_offer_with_retry(
        self,
        offer_dict,
        expiry_secs=None,
        coin_ids_enabled=False,
        preferred_tier=None,
        strict_preferred_tier=False,
    ):
        self.calls.append(
            {
                "offer_dict": offer_dict,
                "expiry_secs": expiry_secs,
                "coin_ids_enabled": coin_ids_enabled,
                "preferred_tier": preferred_tier,
                "strict_preferred_tier": strict_preferred_tier,
            }
        )
        return {
            "success": True,
            "trade_id": "trade123",
            "offer": "offer1qqqq",
            "locked_coin_id": "0xabc123",
        }


class SniperCoinIdTests(unittest.TestCase):
    def test_sniper_records_and_locks_locked_coin_id(self):
        om = _FakeOfferManager()
        sniper = Sniper(offer_manager=om, risk_manager=None, dexie_manager=None)

        with (
            patch("sniper.add_offer") as add_offer_mock,
            patch("sniper.lock_coin") as lock_coin_mock,
        ):
            result = sniper._create_snipe_offer(
                "buy", Decimal("0.00012"), Decimal("0.2")
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["coin_id"], "0xabc123")
        add_offer_mock.assert_called_once()
        self.assertEqual(add_offer_mock.call_args.kwargs["coin_id"], "0xabc123")
        lock_coin_mock.assert_called_once_with("0xabc123", "trade123")
        self.assertIn("trade123", om._offer_details_cache)
        self.assertEqual(om.calls[0]["preferred_tier"], "sniper")
        self.assertTrue(om.calls[0]["strict_preferred_tier"])

    def test_sniper_uses_dedicated_sniper_size(self):
        om = _FakeOfferManager()
        sniper = Sniper(offer_manager=om, risk_manager=None, dexie_manager=None)

        size = sniper._calculate_snipe_size(Decimal("999"))

        self.assertEqual(size, Decimal("0.001"))

    def test_single_side_sniper_posts_to_dexie_then_splash(self):
        om = _FakeOfferManager()
        post_order = []

        class _Poster:
            def __init__(self, label):
                self.label = label

            def _post_single(self, bech32, trade_id, force=False):
                post_order.append((self.label, bech32, trade_id, force))
                return {"success": True}

        sniper = Sniper(
            offer_manager=om,
            risk_manager=None,
            dexie_manager=_Poster("dexie"),
            splash_manager=_Poster("splash"),
        )

        result = sniper.try_snipe_single("buy", Decimal("0.00012"), Decimal("500"))

        self.assertEqual(len(result), 1)
        self.assertEqual([entry[0] for entry in post_order], ["dexie", "splash"])
        self.assertTrue(all(entry[3] is True for entry in post_order))


if __name__ == "__main__":
    unittest.main()
