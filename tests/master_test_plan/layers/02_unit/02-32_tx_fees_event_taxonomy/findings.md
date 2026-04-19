# Findings — Slice 02-32

Unit tests for `tx_fees.py`, `event_taxonomy.py`, `notification_manager.py`.

---

## Existing coverage (before this slice)

None — no tests referenced these modules.

---

## New coverage added

| Module / Function | Tests | Notes |
|-------------------|-------|-------|
| `tx_fees._decimal_or_zero` | 6 | String, int, None, empty, invalid, Decimal instance |
| `tx_fees.xch_to_mojos` | 6 | 1 XCH, 0.5 XCH, zero, negative, None, fractional rounds up |
| `tx_fees.mojos_to_xch` | 5 | 1T mojos, zero, negative, None, 0.5T |
| `tx_fees.get_fee_pool_count/size/configured` | 8 | Pool count clamp, coin size, fee_pool_configured logic |
| `tx_fees.get_transaction_fee_mode` | 3 | manual, auto, invalid → auto |
| `event_taxonomy.EventCategory` | 2 | All 8 categories present, is_str |
| `event_taxonomy.categorize_event` | 5 | Known events, unknown → SYSTEM, empty → SYSTEM |
| `event_taxonomy.get_category_map` | 3 | Returns dict, copy, has entries |
| `NotificationManager.notify` | 4 | First call, cooldown, disabled, category disabled |
| `NotificationManager.set/get categories` | 4 | Enable/disable, copy isolation, independent rate limits |

**46 new tests** in `tests/test_plan_02_32_tx_fees_event_taxonomy_unit.py`.

---

## No bugs found

| Count | Status |
|-------|--------|
| 0 | open bugs |
| 0 | fixed |
| 0 | blocked |
