# Findings — Slice 03-16

Integration tests for the liquidity-mode switch cycle: `LIQUIDITY_MODE` drives
`ENABLE_BUY`/`ENABLE_SELL`/`active_side()`/`is_single_sided()` across the full
`two_sided → buy_only → sell_only → two_sided` cycle via real `.env` I/O.

## New coverage added

| Test class | Tests | Notes |
|------------|-------|-------|
| `TestLiquidityModeInitialLoad` | 7 | Initial load for each mode + invalid mode default |
| `TestLiquidityModeSingleSided` | 3 | `is_single_sided()` for all three modes |
| `TestLiquidityModeSwitchCycle` | 7 | `reload()` and `update()` switch tests + full 3-step cycle |
| `TestLiquidityModeFieldConsistency` | 3 | `active_side()` / `LIQUIDITY_MODE` attribute consistency |

**20 new tests** in `tests/test_plan_03_16_liquidity_mode_switch_integration.py`.

## No bugs found

All 20 tests passed on first run.

## Key finding

`LIQUIDITY_MODE` always overrides `ENABLE_BUY`/`ENABLE_SELL` for `buy_only`/`sell_only`.
For `two_sided` mode, the raw `ENABLE_BUY`/`ENABLE_SELL` values from the env are used.
Invalid `LIQUIDITY_MODE` values silently default to `"two_sided"`.
