# Findings — Slice 02-23

Unit test expansion for `risk_manager.py` — circuit breakers, position limits, spreads.

---

## Existing coverage (before this slice)

None — no tests referenced risk_manager.

---

## New coverage added

| Function | Tests | Notes |
|----------|-------|-------|
| `_trip_circuit_breaker` | 4 | Activates, sets reason, idempotent, blocked_side |
| `_clear_circuit_breaker` | 2 | Deactivates, safe when not active |
| `circuit_breaker_active` | 1 | Initial state |
| `is_full_halt` | 2 | Price CB = full halt, position CB = partial |
| `get_circuit_breaker_blocked_side` | 1 | Returns correct side |
| `should_enable_side` | 8 | No CB, full halt, buy/sell CB, soft inventory limits |
| `_check_position_limit` | 7 | No position, below soft, soft-hard range, above hard, side routing, zero limit |
| `_check_price_limits` | 5 | Zero limits, zero price, hard min, hard max, within limits |
| `check_circuit_breakers` (hysteresis) | 3 | 1 cycle not enough, 3 cycles clears, new trip resets streak |
| `_get_base_spread` | 2 | Dynamic mode, static mode |
| `_apply_inventory_skew` | 5 | Neutral, long widens buy, long tightens sell, short tightens buy, min-edge floor |

**40 new tests** in `tests/test_plan_02_23_risk_manager_unit.py`.

---

## Key discovery: startup baseline logic

`_check_position_limit` records the first position it sees as `_startup_position_xch`.
If that baseline exceeds `MAX_POSITION_XCH`, the effective limit is raised to
`baseline × 1.1` to avoid tripping the CB on historical fills inherited from prior
sessions. Tests must set `_startup_position_xch = Decimal("0")` to exercise the
normal (no-inherited-position) path cleanly.

---

## No bugs found

| Count | Status |
|-------|--------|
| 0 | open bugs |
| 0 | fixed |
| 0 | blocked |
