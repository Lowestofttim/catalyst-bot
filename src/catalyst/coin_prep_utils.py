#!/usr/bin/env python3
"""Stateless decision helpers for coin_prep_worker split retry logic

A tiny collection of pure functions that the coin prep worker consults when a
split transaction appears stuck. The helpers decide whether a pending split
should be retried (because the source coin is still selectable and no outputs
have materialised) or whether the grace window should simply be extended (the
transaction is likely still propagating). Kept in a separate module so the
retry policy can be unit-tested in isolation.

Key responsibilities:
    - should_retry_unconsumed_split: detect a silently-missed split
    - should_extend_pending_consumed_split_grace: detect in-flight progress
    - Remain completely stateless and side-effect free
"""


def should_retry_unconsumed_split(
    *,
    elapsed_s: int,
    pool_coin_visible: bool,
    pool_coin_selectable: bool,
    outputs_selectable: bool,
    retries_used: int,
    retry_after_s: int = 60,
    max_retries: int = 1,
) -> bool:
    """Return True when a split looks silently missed and is worth retrying once.

    The key signal is: after a reasonable wait, the original pool coin is still
    present *and* still strictly selectable, while the expected split outputs are
    not yet all present/selectable. That means Sage has not actually consumed the
    source coin, so waiting longer is unlikely to help.
    """
    if retries_used >= max_retries:
        return False
    if elapsed_s < retry_after_s:
        return False
    if outputs_selectable:
        return False
    return pool_coin_visible and pool_coin_selectable


def should_extend_pending_consumed_split_grace(
    *,
    elapsed_s: int,
    current_deadline_s: int,
    pool_coin_visible: bool,
    pool_coin_selectable: bool,
    tx_known: bool,
    tx_confirmed: bool,
    owned_output_count: int,
    selectable_output_count: int,
    expected_count: int,
    extensions_used: int,
    extension_missing_limit: int = 2,
    min_completion_ratio: float = 0.90,
) -> bool:
    """Return True when a consumed split is nearly complete and worth a short grace extension.

    This is intentionally narrow. We only extend when:
      - the current timeout has actually been reached
      - the source coin is no longer intact/selectable
      - Sage still knows about the transaction but has not confirmed it yet
      - almost all exact outputs are already present (and usually selectable)

    That catches the recent CAT edge case where the split appears materially in
    flight, but Sage is still surfacing the final one or two outputs.
    """
    if extensions_used >= 1:
        return False
    if elapsed_s < current_deadline_s:
        return False
    if expected_count <= 0:
        return False
    if tx_confirmed or not tx_known:
        return False
    if pool_coin_visible and pool_coin_selectable:
        return False

    owned_output_count = max(0, int(owned_output_count or 0))
    selectable_output_count = max(0, int(selectable_output_count or 0))
    expected_count = int(expected_count or 0)

    missing_owned = max(0, expected_count - owned_output_count)
    if missing_owned > extension_missing_limit:
        return False

    # All outputs are OWNED — the transaction has been accepted into the wallet.
    # If none are selectable yet, the whole batch is still pending in mempool
    # (common for CAT transactions on slow blocks). Extend anyway; we just need
    # the block to arrive.
    all_owned = owned_output_count >= expected_count
    missing_selectable = max(0, expected_count - selectable_output_count)
    if not all_owned and missing_selectable > extension_missing_limit:
        return False

    completion_ratio = owned_output_count / float(expected_count)
    return completion_ratio >= float(min_completion_ratio)

