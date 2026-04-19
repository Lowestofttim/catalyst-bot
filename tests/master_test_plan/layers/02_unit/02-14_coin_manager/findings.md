# Findings — Slice 02-14

Unit tests for `coin_manager.py` — coin lifecycle and preparation manager.

---

## Existing coverage (before this slice)

None for the pure utility functions; CoinManager class methods untested.

---

## New coverage added

| Module / Function | Tests | Notes |
|-------------------|-------|-------|
| `request_fast_reconcile` / `consume_fast_reconcile` | 4 | idempotent, reset, double-request |
| `_extract_coin_records` | 7 | None, non-dict, error/success=False, confirmed_records, records fallback, empty |
| `_coin_amount` | 3 | normal, missing coin key, missing amount |
| `_chia_int_to_bytes` | 4 | zero→empty, one, large roundtrip, byte count formula |
| `_coin_id_from_record` | 6 | name field, coin_id field, normalise, SHA256 compute, missing parent, empty |
| `_classify_coins` | 6 | reserve/trading/small buckets, all three, sorted reserve, empty |
| `_clamp_coin_prep_multiplier` | 6 | identity, floor, ceiling, invalid string, valid string, mid value |
| `_format_amount_xch` / `_format_amount_cat` | 4 | 1 XCH, zero, CAT 3dp, zero CAT |
| `get_tier_distribution` | 5 | zero, explicit counts, overflow, cfg buy side, total matches |
| `flip_position_tiers_to_coin_size_tiers` | 4 | sell identity, buy non-reversed, buy reversed flip, non-positional preserved |
| `get_weighted_tier_prep_counts` | 4 | multiplier 1×doubles, 2×doubles, zero slots, explicit spare override |
| `FeeCoinPool` | 7 | empty→None, refresh, reserve, available count, double-reserve blocked, refresh resets, total count |

**60 new tests** in `tests/test_plan_02_14_coin_manager_unit.py`.

---

## Test corrections

- `test_multiplier_1_equals_distribution`: Initially asserted that multiplier=1.0
  preserves the active count. Wrong — `spare_budget = round(total * 1.0)` adds 8
  spare on top of 8 active → total=16. Corrected to `test_multiplier_1_doubles_total`.

---

## No bugs found

| Count | Status |
|-------|--------|
| 0 | open bugs |
| 0 | fixed |
| 0 | blocked |
