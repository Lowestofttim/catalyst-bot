# Public Refactor Plan

This plan captures post-release decomposition work for the largest files in
CATalyst. It is intentionally design-only. The public-readiness pass should
not refactor trading behavior and documentation in the same commit.

## Goals

- Reduce the risk of editing large, mixed-responsibility files.
- Preserve current behavior with small, test-backed extraction slices.
- Keep wallet, offer, and database boundaries explicit.
- Make frontend safety work practical by moving pure utilities into testable
  modules.

## Guardrails

- Do not change trading logic while moving code. The first commit for every
  extraction should be behavior-preserving.
- Keep one extraction target per branch or PR.
- Add or identify a focused regression test before each extraction.
- Run the full non-live pytest gate after touching trading, coin, wallet, or DB
  behavior.
- Keep `bot_gui.html` extraction compatible with PyInstaller and Flask asset
  serving before moving more code.

## Current Size Snapshot

| File | Current lines | Main responsibility mix |
| --- | ---: | --- |
| `bot_gui.html` | 32626 | HTML, CSS, JS runtime, API client, startup gates, charts, settings, logs, modals |
| `src/catalyst/bot_loop.py` | 11355 | Trading loop orchestration, wallet sync policy, safety gates, runtime state, event emission |
| `src/catalyst/coin_manager.py` | 7954 | Coin IDs, tier classification, fee pool, topup, prep coordination, wallet reconciliation |
| `src/catalyst/coin_prep_worker.py` | 5972 | CLI worker, status model, wallet RPC calls, split/consolidate flow, API mirroring |
| `src/catalyst/blueprints/smart_defaults.py` | 3260 | HTTP route, external price context, DBX defaults, capital solver, warning copy |

## Suggested Order

1. Extract pure coin tier/count helpers from `coin_manager.py`.
2. Extract frontend utilities from `bot_gui.html`.
3. Extract coin-prep status/progress model from `coin_prep_worker.py`.
4. Extract wallet-offer status policy from `bot_loop.py`.
5. Extract Smart Defaults pricing/context helpers from `smart_defaults.py`.

This order starts with the highest test coverage and lowest runtime coupling,
then moves toward orchestration-heavy files.

## `bot_gui.html`

### Target Shape

- Keep `bot_gui.html` as the Flask-served shell while extracting JavaScript and
  CSS by domain:
  - `assets/js/catalyst_runtime.js`: debug logging, URL building, HTML escaping,
    API fetch wrapper, local-token handling.
  - `assets/js/catalyst_views.js`: `v4SwitchView`, nav state, charts, tab
    refresh hooks.
  - `assets/js/catalyst_startup.js`: risk disclosure, Sage/Splash/Spacescan
    gates.
  - `assets/js/catalyst_settings.js`: settings form, validation, presets,
    Smart Settings frontend.
  - `assets/css/catalyst.css`: non-critical CSS after the JS extraction path is
    proven.

### First Extractable Unit

Move the pure runtime utilities into a single script loaded before the inline
application script:

- `debugConsole`, `debugLog`, `debugWarn`, `debugError`, `debugDebug`
- `escapeHtml`, `escapeAttr`
- `sanitizeExternalHref`
- `buildLocalApiUrl`

Expose them under a small namespace such as `window.CatalystRuntime` and leave
temporary backwards-compatible globals in place for the existing inline code.

### Protecting Tests

- Existing: `tests/test_security_guardrails_source.py`
- Existing: `tests/test_api_local_guard.py`
- Existing: `tests/e2e/test_smoke.py --e2e`
- Add before extraction: a Node-based unit test that loads the extracted
  runtime script and asserts escaping, URL, and debug-gating behavior.

### Verification Command

```powershell
python -m pytest tests/test_security_guardrails_source.py tests/test_api_local_guard.py -q
python -m pytest tests/e2e/test_smoke.py --e2e -q
```

## `src/catalyst/bot_loop.py`

### Target Shape

Split orchestration from policies:

- `bot_loop.py`: lifecycle, thread ownership, cycle sequencing.
- `offer_status_policy.py`: Sage/Chia terminal status mapping and wallet-open
  classification decisions.
- `bot_safety_gates.py`: reserve-floor guard, degraded-wallet gate, price-shock
  gate, startup probe decisions.
- `runtime_state.py`: public state snapshot serialization.

### First Extractable Unit

Move `map_sage_terminal_offer_status()` and closely related status constants to
`offer_status_policy.py`. Keep imports in `bot_loop.py` as wrappers for one
release if needed so existing tests and callers keep working.

### Protecting Tests

- Existing: `tests/test_bot_loop_sage_status_mapping.py`
- Existing: `tests/test_bot_loop_probe_anchor.py`
- Existing: `tests/test_bot_loop_reserve_floor_guard.py`
- Existing: `tests/test_wallet_sync_fail_closed.py`

### Verification Command

```powershell
python -m pytest tests/test_bot_loop_sage_status_mapping.py tests/test_bot_loop_probe_anchor.py tests/test_bot_loop_reserve_floor_guard.py tests/test_wallet_sync_fail_closed.py -q
```

## `src/catalyst/blueprints/smart_defaults.py`

### Target Shape

Separate HTTP handling from calculation:

- `blueprints/smart_defaults.py`: request parsing, response shaping, Flask
  errors.
- `smart_defaults_pricing.py`: Dexie/Tibet/DBX price and context gathering.
- `smart_defaults_engine.py`: pure calculation from typed inputs to config
  recommendations.
- `smart_defaults_messages.py`: user-facing warning/advice strings.

### First Extractable Unit

Move the pure pricing helpers into `smart_defaults_pricing.py`:

- `_smart_trade_vwap`
- `_resolve_smart_mid_price`
- `_smart_tibet_shock_trigger_pct`

Keep the public route unchanged and import these helpers back into the
blueprint.

### Protecting Tests

- Existing: `tests/test_plan_04_10_smart_defaults_endpoint.py`
- Existing: `tests/test_api_local_guard.py::TestApiLocalGuard::test_smart_defaults_orderbook_uses_dexie_v1_params`
- Add before deeper engine extraction: fixture-based tests for the full
  recommendation payload from a frozen wallet/market input.

### Verification Command

```powershell
python -m pytest tests/test_plan_04_10_smart_defaults_endpoint.py tests/test_api_local_guard.py -q
```

## `src/catalyst/coin_prep_worker.py`

### Target Shape

Split the worker into durable process concerns:

- `coin_prep_worker.py`: CLI entry point and top-level orchestration.
- `coin_prep_status.py`: `PrepPhase`, `CoinPrepStatus`, progress snapshots.
- `coin_prep_wallet_ops.py`: wallet RPC calls for consolidation and splitting.
- `coin_prep_api_mirror.py`: local API logging/progress mirror.

### First Extractable Unit

Move `PrepPhase`, `CoinPrepStatus`, and status JSON serialization helpers to
`coin_prep_status.py`. Leave a compatibility import in `coin_prep_worker.py`
until callers/tests are moved.

### Protecting Tests

- Existing: `tests/test_plan_02_15_coin_prep_utils_worker_unit.py`
- Existing: `tests/test_plan_07_05_coin_prep_crash.py`
- Existing: `tests/test_coin_prep_worker_cancel.py`
- Existing: `tests/test_plan_03_04_05_06_coin_prep_lifecycle_integration.py`

### Verification Command

```powershell
python -m pytest tests/test_plan_02_15_coin_prep_utils_worker_unit.py tests/test_plan_07_05_coin_prep_crash.py tests/test_coin_prep_worker_cancel.py tests/test_plan_03_04_05_06_coin_prep_lifecycle_integration.py -q
```

## `src/catalyst/coin_manager.py`

### Target Shape

Separate pure coin math from wallet/database side effects:

- `coin_manager.py`: high-level manager class and orchestration.
- `coin_ids.py`: coin ID derivation and record extraction.
- `coin_tiers.py`: tier size mapping, reverse-buy bucket mapping, distribution
  counts.
- `coin_prep_counts.py`: prepared/spare count recommendations.
- `fee_coin_pool.py`: fee-pool bookkeeping.
- `topup_planner.py`: topup budget and empty-tier selection policy.

### First Extractable Unit

Move tier and count helpers into `coin_tiers.py`:

- `get_tier_sizes_mojos_from_cfg`
- `flip_position_tiers_to_coin_size_tiers`
- `coin_size_tier_for_slot_position`
- `get_tier_distribution`
- `get_weighted_tier_prep_counts`
- `get_recommended_tier_spare_counts`

Leave wrapper imports in `coin_manager.py` during the first release cycle.

### Protecting Tests

- Existing: `tests/test_tier_group_counts.py`
- Existing: `tests/test_tier_sizes_mojos_reverse_buy.py`
- Existing: `tests/test_reverse_buy_tier_size.py`
- Existing: `tests/test_topup_budget_autoscale.py`
- Existing: `tests/test_topup_budget_empty_tier_bypass.py`

### Verification Command

```powershell
python -m pytest tests/test_tier_group_counts.py tests/test_tier_sizes_mojos_reverse_buy.py tests/test_reverse_buy_tier_size.py tests/test_topup_budget_autoscale.py tests/test_topup_budget_empty_tier_bypass.py -q
```

## Release-Safe Refactor Template

For each extraction:

1. Add or identify the protecting tests.
2. Copy the target functions to the new module.
3. Import from the old file without changing behavior.
4. Run targeted tests.
5. Run `python -m ruff check .`.
6. Run the full non-live pytest gate if trading, wallet, DB, or startup code is
   touched.
7. Only after that, remove compatibility wrappers in a later cleanup pass.

## Full Verification Gate

```powershell
python -m ruff check .
Push-Location tests
python -m pytest -n 2 --dist=loadfile --tb=short --ignore=test_coin_prep.py --ignore=test_coin_prep_v2.py --ignore=test_offer_create.py
Pop-Location
python -m pytest tests/e2e/test_smoke.py --e2e -q
```
