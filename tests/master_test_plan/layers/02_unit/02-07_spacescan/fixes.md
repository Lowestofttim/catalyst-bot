# Fixes — Slice 02-07

## Production code fixes

### `spacescan.py` — `get_xch_balance` does not catch `InvalidOperation`

**Root cause:** `Decimal("not-a-number")` raises `decimal.InvalidOperation` which
inherits from `ArithmeticError`, NOT from `ValueError`. The existing `except (KeyError,
ValueError)` clause does not catch it.

**Fix:**
1. Added `InvalidOperation` to the import: `from decimal import Decimal, InvalidOperation`
2. Extended the catch clause: `except (KeyError, ValueError, InvalidOperation) as e:`

**Regression test:** `TestBalanceFunctions::test_get_xch_balance_parse_error_returns_none`

---

## Test corrections

- `test_free_balance_at_limit_false`: Initial version did not pre-set `_today_date`,
  causing `_check_daily_budget` to reset `_calls_today = 0` before checking the limit.
  Fixed by adding `_ss_mod._today_date = datetime.date.today().isoformat()` before
  setting `_calls_today`.
