# Findings — Slice 02-05

Unit test expansion for `price_engine.py` — weighted mid, EMA, safety guards,
constant product formula.

---

## Existing coverage (before this slice)

None — no tests referenced price_engine.

---

## New coverage added

| Function | Tests | Notes |
|----------|-------|-------|
| `_decimal_sqrt` | 6 | Perfect square, irrational, zero, one, negative, large |
| `_update_reference_price` | 4 | First-set, EMA nudge, fast catch-up, monotonic convergence |
| `get_dynamic_limits` | 4 | No reference, symmetric band, band width, zero-pct disabled |
| `_apply_safety_guards` | 9 | Within band, dyn_min, dyn_max, hard_min, hard_max, step reject, step pass, no ref, breach clears |
| Pricing strategy selection | 6 | Weighted, dexie_only fallback, tibet_only, both-none, arb gap bps, no-arb |
| `_estimate_slippage_from_reserves` | 5 | Buy/sell slippage, larger→more, zero reserves, result keys |
| `get_pool_depth_ratio` | 3 | Ratio math, no pool, zero depth |

**37 new tests** in `tests/test_plan_02_05_price_engine_unit.py`.

---

## Observations

- Fast catch-up EMA (5× alpha) triggers when deviation > half_band × 0.5. A 10%
  move on a 10% band activates it. Tests must use ≤ 2.5% moves to exercise
  the normal 1% alpha path cleanly.
- `_estimate_slippage_from_reserves` takes raw pair dict (mojos), not pool-info dict.
  xch_reserve is in mojos (÷1e12 for XCH); token_reserve in token-mojos (÷10^decimals).

---

## No bugs found

| Count | Status |
|-------|--------|
| 0 | open bugs |
| 0 | fixed |
| 0 | blocked |
