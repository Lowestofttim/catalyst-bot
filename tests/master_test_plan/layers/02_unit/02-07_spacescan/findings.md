# Findings — Slice 02-07

Unit tests for `spacescan.py` — on-chain verification client.

---

## Existing coverage (before this slice)

None — no tests referenced this module directly.

---

## New coverage added

| Module / Function | Tests | Notes |
|-------------------|-------|-------|
| `is_pro_tier`, `_get_call_interval` | 4 | True/False + interval values |
| `_get_base_url`, `_get_headers` | 4 | pro vs free URL/key |
| `should_check_balance` | 2 | pro=True, free=False |
| `_check_daily_budget` | 5 | pro always True, fill_verify always True, balance within/at limit, date reset |
| `get_api_stats` | 3 | tier, required keys, values |
| `record_external_call` | 2 | session + daily counter increments |
| `is_known_wallet_address` | 5 | empty/None, match, no match, explicit override |
| `is_coin_spent` | 6 | disabled, API fail, empty coin, spent, unspent, 0x prefix |
| `verify_fill` decision tree | 8 | API error, unspent, offer_info 4, offer_info 3, status 4 wins, child coin external, all ours, receiver ours |
| `get_xch_balance` | 3 | success, API fail, parse error |
| `get_token_balance` | 4 | with id, not found, first, empty list |

**47 new tests** in `tests/test_plan_02_07_spacescan_unit.py`.

---

## Bugs found and fixed

### BUG: `get_xch_balance` does not catch `decimal.InvalidOperation`

**File:** `spacescan.py:476`  
**Symptom:** `Decimal("not-a-number")` raises `decimal.InvalidOperation` which is NOT a `ValueError` — the exception propagates uncaught, crashing the caller.  
**Fix:** Added `InvalidOperation` to the `except` clause and to the module import:
```python
from decimal import Decimal, InvalidOperation
...
except (KeyError, ValueError, InvalidOperation) as e:
```

---

## Test design notes

- Module-level state (`_calls_this_session`, `_calls_today`, `_today_date`,
  `_known_wallet_addresses_cache`, etc.) is saved and restored in setUp/tearDown.
- `_check_daily_budget` has a date-comparison guard that resets `_calls_today` to 0
  when the stored date doesn't match today. Tests that need a non-zero call count
  must pre-set `_today_date = datetime.date.today().isoformat()` to prevent the reset.
- `patch.object(_ss_mod, ...)` used throughout to avoid `sys.modules` aliasing issues.
