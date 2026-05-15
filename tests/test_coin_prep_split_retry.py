import unittest
from pathlib import Path

from coin_prep_utils import (
    should_extend_pending_consumed_split_grace,
    should_wait_for_pending_fee_inputs_before_split,
    should_retry_unconsumed_split,
)


class TestCoinPrepSplitRetry(unittest.TestCase):
    def test_retries_when_source_coin_is_still_free_after_threshold(self):
        self.assertTrue(
            should_retry_unconsumed_split(
                elapsed_s=60,
                pool_coin_visible=True,
                pool_coin_selectable=True,
                outputs_selectable=False,
                retries_used=0,
            )
        )

    def test_does_not_retry_before_threshold(self):
        self.assertFalse(
            should_retry_unconsumed_split(
                elapsed_s=55,
                pool_coin_visible=True,
                pool_coin_selectable=True,
                outputs_selectable=False,
                retries_used=0,
            )
        )

    def test_does_not_retry_when_outputs_are_ready(self):
        self.assertFalse(
            should_retry_unconsumed_split(
                elapsed_s=90,
                pool_coin_visible=True,
                pool_coin_selectable=True,
                outputs_selectable=True,
                retries_used=0,
            )
        )

    def test_does_not_retry_after_budget_is_used(self):
        self.assertFalse(
            should_retry_unconsumed_split(
                elapsed_s=90,
                pool_coin_visible=True,
                pool_coin_selectable=True,
                outputs_selectable=False,
                retries_used=1,
            )
        )

    def test_retries_when_output_high_water_was_only_pending_wallet_state(self):
        self.assertTrue(
            should_retry_unconsumed_split(
                elapsed_s=120,
                pool_coin_visible=True,
                pool_coin_selectable=True,
                outputs_selectable=False,
                retries_used=0,
                owned_output_high_water=25,
                expected_count=25,
                owned_output_high_water_is_durable=False,
            )
        )

    def test_does_not_retry_when_output_high_water_was_durable(self):
        self.assertFalse(
            should_retry_unconsumed_split(
                elapsed_s=120,
                pool_coin_visible=True,
                pool_coin_selectable=True,
                outputs_selectable=False,
                retries_used=0,
                owned_output_high_water=25,
                expected_count=25,
                owned_output_high_water_is_durable=True,
            )
        )

    def test_extends_when_consumed_split_is_almost_complete_and_still_pending(self):
        self.assertTrue(
            should_extend_pending_consumed_split_grace(
                elapsed_s=120,
                current_deadline_s=120,
                pool_coin_visible=False,
                pool_coin_selectable=False,
                tx_known=True,
                tx_confirmed=False,
                owned_output_count=18,
                selectable_output_count=18,
                expected_count=19,
                extensions_used=0,
            )
        )

    def test_does_not_extend_when_too_many_outputs_are_missing(self):
        self.assertFalse(
            should_extend_pending_consumed_split_grace(
                elapsed_s=120,
                current_deadline_s=120,
                pool_coin_visible=False,
                pool_coin_selectable=False,
                tx_known=True,
                tx_confirmed=False,
                owned_output_count=23,
                selectable_output_count=23,
                expected_count=26,
                extensions_used=0,
            )
        )

    def test_does_not_extend_without_known_pending_transaction(self):
        self.assertFalse(
            should_extend_pending_consumed_split_grace(
                elapsed_s=120,
                current_deadline_s=120,
                pool_coin_visible=False,
                pool_coin_selectable=False,
                tx_known=False,
                tx_confirmed=False,
                owned_output_count=18,
                selectable_output_count=18,
                expected_count=19,
                extensions_used=0,
            )
        )

    def test_does_not_extend_after_grace_has_already_been_used(self):
        self.assertFalse(
            should_extend_pending_consumed_split_grace(
                elapsed_s=180,
                current_deadline_s=180,
                pool_coin_visible=False,
                pool_coin_selectable=False,
                tx_known=True,
                tx_confirmed=False,
                owned_output_count=18,
                selectable_output_count=18,
                expected_count=19,
                extensions_used=1,
            )
        )

    def test_worker_uses_120s_split_confirmation_timeout(self):
        source = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "catalyst"
            / "coin_prep_worker.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_poll_all_splits(pending_splits, timeout_s=120)", source)
        self.assertIn("grace_extension_s = 60", source)
        self.assertIn("should_extend_pending_consumed_split_grace(", source)

    def test_worker_aborts_instead_of_proceeding_cautiously_after_split_timeout(self):
        source = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "catalyst"
            / "coin_prep_worker.py"
        ).read_text(encoding="utf-8")
        # The abort message references whichever timeout the call site uses.
        # The function default is 120s but the caller passes 300s explicitly;
        # the message in the source should mention an abort (not "cautiously").
        self.assertTrue(
            "Split confirmation failed within 120s" in source
            or "Split confirmation failed within 300s" in source,
            "Expected 'Split confirmation failed within Xs' abort message not found",
        )
        self.assertNotIn(
            "split not confirmed after {timeout_s}s — proceeding cautiously", source
        )

    def test_worker_uses_transaction_builder_for_sage_cat_splits(self):
        source = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "catalyst"
            / "coin_prep_worker.py"
        ).read_text(encoding="utf-8")
        self.assertIn("sage_topup_split", source)
        self.assertIn("if is_cat:", source)
        self.assertIn("amount_per_coin = pool_mojos // count", source)

    def test_fee_paid_cat_splits_wait_for_fee_inputs(self):
        self.assertTrue(
            should_wait_for_pending_fee_inputs_before_split(is_cat=True, fee_mojos=1)
        )
        self.assertFalse(
            should_wait_for_pending_fee_inputs_before_split(
                is_cat=True,
                fee_mojos=1,
                has_dedicated_fee_coin=True,
            )
        )
        self.assertFalse(
            should_wait_for_pending_fee_inputs_before_split(is_cat=True, fee_mojos=0)
        )
        self.assertFalse(
            should_wait_for_pending_fee_inputs_before_split(is_cat=False, fee_mojos=1)
        )

    def test_worker_serializes_fee_paid_sage_cat_splits(self):
        source = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "catalyst"
            / "coin_prep_worker.py"
        ).read_text(encoding="utf-8")
        self.assertIn("should_wait_for_pending_fee_inputs_before_split(", source)
        self.assertIn("CAT {tier_name} fee-input-ready", source)

    def test_worker_prefunds_dedicated_cat_split_fee_inputs(self):
        source = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "catalyst"
            / "coin_prep_worker.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_prepare_cat_split_fee_coins", source)
        self.assertIn("fee_coin_id=fee_coin_id", source)
        self.assertIn("has_dedicated_fee_coin=bool(fee_coin_id)", source)

    def test_worker_distinguishes_pending_output_high_water_from_durable_split(self):
        source = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "catalyst"
            / "coin_prep_worker.py"
        ).read_text(encoding="utf-8")
        self.assertIn("pool_consumed_seen", source)
        self.assertIn("durable_high_water_complete", source)
        self.assertIn("owned_output_high_water_is_durable", source)


if __name__ == "__main__":
    unittest.main()
