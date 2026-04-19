# Findings — Slice 02-29

Unit test expansion for `config.py` + `config_validator.py` — pure functions.

---

## Existing coverage (before this slice)

None — no tests referenced config.py or config_validator.py directly.

---

## New coverage added

| Function / class | Tests | Notes |
|-----------------|-------|-------|
| `_strip_quotes` | 7 | Double/single, empty, single-char, mismatched, whitespace |
| `_bool` | 8 | All truthy strings, falsy, missing → default, quoted "true" |
| `Config.get_spread_fraction` | 2 | BPS → fraction |
| `Config.get_requote_fraction` | 1 | BPS → fraction |
| `Config.is_two_sided` | 3 | Both enabled, buy_only, sell disabled |
| `Config.is_single_sided` | 3 | buy_only, sell_only, two_sided |
| `Config.active_side` | 4 | buy_only, sell_only, two_sided, unknown → both |
| `Config.to_dict` | 4 | Excludes sensitive keys, includes others, Decimal→str, private attrs |
| `get_buy_tier_size_xch` | 3 | Invalid tier, modern field, legacy fallback |
| `get_sell_tier_size_xch` | 3 | Modern field, legacy fallback, invalid tier |
| `get_tier_sizes_for_side` | 2 | Returns all tiers dict, unknown side → sell path |
| `has_per_side_tier_sizes` | 2 | True when any BUY field set, False when all zero |
| `ValidationReport.is_valid` / `to_dict` | 4 | Empty, with error, counts, keys |
| `_is_valid_url` | 5 | http, https, ftp, empty, no scheme |
| `validate_config` | 9 | Valid baseline + 8 error conditions |

**60 new tests** in `tests/test_plan_02_29_config_unit.py`.

---

## No bugs found

| Count | Status |
|-------|--------|
| 0 | open bugs |
| 0 | fixed |
| 0 | blocked |
