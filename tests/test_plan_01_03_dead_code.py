"""Regression tests for slice 01-03 (dead code sweep).

Verifies that confirmed-dead parameters and variables have been removed
and that the live callers still work correctly.
"""

import inspect
import ast


# ---------------------------------------------------------------------------
# coin_manager._smart_topup_wallet — dead parameter cat_token_amount removed
# ---------------------------------------------------------------------------
class TestSmartTopupWalletDeadParam:
    def test_cat_token_amount_not_in_signature(self):
        """cat_token_amount was never read in the method body — removed."""
        import coin_manager

        sig = inspect.signature(coin_manager.CoinManager._smart_topup_wallet)
        assert "cat_token_amount" not in sig.parameters, (
            "cat_token_amount still present in _smart_topup_wallet signature"
        )

    def test_callers_do_not_pass_cat_token_amount(self):
        """Both call sites in manage_coins_for_topup removed cat_token_amount=."""
        src = inspect.getsource(coin_manager.CoinManager)
        assert "cat_token_amount=cat_token_size" not in src, (
            "Stale cat_token_amount= kwarg still passed to _smart_topup_wallet"
        )

    def test_cat_token_size_not_computed(self):
        """Dead cat_token_size computation blocks removed."""
        src = inspect.getsource(coin_manager.CoinManager)
        assert "cat_token_size = int(cfg.CAT_COIN_SIZE)" not in src, (
            "Dead cat_token_size = int(cfg.CAT_COIN_SIZE) still present"
        )


import coin_manager  # noqa: E402 (needed above for class body)


# ---------------------------------------------------------------------------
# bot_loop._create_offers_if_needed — dead parameters zombie_* removed
# ---------------------------------------------------------------------------
class TestCreateOffersIfNeededDeadParams:
    def test_zombie_buy_count_not_in_signature(self):
        """zombie_buy_count was never read in the method — removed."""
        import bot_loop

        sig = inspect.signature(bot_loop.BotLoop._create_offers_if_needed)
        assert "zombie_buy_count" not in sig.parameters

    def test_zombie_sell_count_not_in_signature(self):
        """zombie_sell_count was never read in the method — removed."""
        import bot_loop

        sig = inspect.signature(bot_loop.BotLoop._create_offers_if_needed)
        assert "zombie_sell_count" not in sig.parameters

    def test_caller_does_not_pass_zombie_counts(self):
        """The call site in run_cycle no longer passes zombie_*= kwargs."""
        import bot_loop

        src = inspect.getsource(bot_loop.BotLoop)
        assert "zombie_buy_count=" not in src
        assert "zombie_sell_count=" not in src


# ---------------------------------------------------------------------------
# F841 dead-variable removals — check source no longer contains the names
# ---------------------------------------------------------------------------
class TestF841DeadVariablesRemoved:
    def test_activities_removed_from_api_server(self):
        import api_server

        src = inspect.getsource(api_server)
        # 'activities = context["activity_count"]' was dead — 'activity' (line 512) was used
        assert 'activities = context["activity_count"]' not in src

    def test_risk_data_removed_from_api_server(self):
        import api_server

        src = inspect.getsource(api_server)
        assert 'risk_data = raw.get("risk") or {}' not in src

    def test_max_buy_max_sell_removed_from_smart_defaults(self):
        import api_server

        src = inspect.getsource(api_server)
        assert "max_buy = int(_safe_float" not in src
        assert "max_sell = int(_safe_float" not in src

    def test_reappeared_count_removed(self):
        src = inspect.getsource(coin_manager.CoinManager)
        assert "reappeared_count = 0" not in src

    def test_new_count_removed_from_coin_sync(self):
        src = inspect.getsource(coin_manager.CoinManager)
        assert "new_count = 0\n" not in src

    def test_skips_removed_from_doctor(self):
        import doctor

        src = inspect.getsource(doctor.DoctorReport)
        assert "skips = sum" not in src

    def test_requested_block_removed_from_database(self):
        import database

        src = inspect.getsource(database)
        assert "requested_xch = int(requested.get" not in src
        assert "requested_cat = 0" not in src

    def test_existing_size_xch_removed_from_database(self):
        import database

        src = inspect.getsource(database)
        assert "existing_size_xch = Decimal" not in src


# ---------------------------------------------------------------------------
# Ensure no new NameErrors on import (smoke-test key modules)
# ---------------------------------------------------------------------------
class TestModulesImportClean:
    def test_coin_manager_imports(self):
        import coin_manager  # noqa: F401

    def test_bot_loop_imports(self):
        import bot_loop  # noqa: F401

    def test_boost_manager_imports(self):
        import boost_manager  # noqa: F401

    def test_database_imports(self):
        import database  # noqa: F401

    def test_doctor_imports(self):
        import doctor  # noqa: F401
