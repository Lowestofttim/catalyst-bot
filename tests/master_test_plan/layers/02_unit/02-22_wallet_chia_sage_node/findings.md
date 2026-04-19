# Findings — Slice 02-22

Unit tests for `wallet_chia.py` and `sage_node.py` pure functions.

---

## New coverage added

| Module / Function | Tests | Notes |
|-------------------|-------|-------|
| `wallet_chia.cat_to_mojos` | 5 | standard, truncation, 0 decimals, sub-unit, large amount |
| `wallet_chia.xch_to_mojos` | 6 | 1 XCH, zero, sub-mojo, truncation, string input, float input |
| `wallet_chia.mojos_to_xch` | 5 | 1 XCH, zero, partial, returns Decimal, round-trip |
| `wallet_chia.mojos_to_cat` | 5 | 1000 mojos, zero, partial, returns Decimal, round-trip |
| `wallet_chia.is_offer_time_expired` | 5 | no valid_times, zero, past, future; confirms top-level max_time ignored |
| `wallet_chia.get_offer_expiry_info` | 4 | inf, future, past, max_time in result |
| `wallet_chia._is_open_status` | 12 | None, int 0-5, string open/closed, unknown, expired record, ACTIVE not in chia set |
| `wallet_chia.classify_offers_from_list` | 6 | empty, non-dict, buy, sell, closed, mixed |
| `sage_node._parse_sage_version` | 9 | semver, v/V prefix, partial, empty, unknown, non-numeric, prerelease |
| `sage_node.compare_sage_versions` | 8 | equal, less, greater, major dominates, patch, unparseable cases |
| `chia_node` re-export spot-check | 1 | confirms compare_sage_versions exported |

**66 new tests** in `tests/test_plan_02_22_wallet_chia_sage_node_unit.py`.

---

## Key difference documented

`wallet_chia.is_offer_time_expired` checks ONLY `valid_times.max_time` — it does NOT
check a top-level `max_time` key (unlike `wallet_sage.is_offer_time_expired`).
Test `test_top_level_max_time_ignored` pins this behavioural difference.

---

## No bugs found
