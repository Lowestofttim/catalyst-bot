# Master Test Plan — Index

**Single source of truth.** Update status here when a slice changes state.

Status codes:

- `[ ]` pending (available for a session to pick up)
- `[~]` in-progress (a session owns it — see note column)
- `[x]` done (commit hash in note column)
- `[!]` blocked (see findings.md for why)

Slice ID format: `NN-MM-description` where `NN` is the layer (01-05),
`MM` is the slice number within the layer.

## Session log (append as you work)

| Date | Slice | Status | Commit | Notes |
|------|-------|--------|--------|-------|
| _—_ | _—_ | _—_ | _—_ | seed / scaffold |
| 2026-04-19 | 01-01 | `[x]` | 5b16ab1 | ruff auto-fix + 5 F821/F811 bugs fixed, 10 regression tests |
| 2026-04-19 | 01-02 | `[x]` | 2968308 | 1 real B501 fixed (CA-cert TLS), 77 MEDIUM FPs documented, .bandit config |
| 2026-04-19 | 01-03 | `[x]` | 2e536f0 | 2 dead params, 18 F841 fixed, vulture_whitelist.py, 19 tests |
| 2026-04-19 | 01-04 | `[x]` | b5b3fc2 | 1 TODO (real future work) → docs/tech_debt.md TD-001 |
| 2026-04-19 | 01-05 | `[x]` | b560cc7 | 2 real bugs: missing wallet export + db missing return; 8 tests |
| 2026-04-19 | 01-06 | `[x]` | 89d0e3e | complexity audit: 129 F-grade functions; 5 TD entries; no bugs |
| 2026-04-19 | 01-07 | `[x]` | fdf9502 | circular imports: 0 module-level cycles; 241 deferred (all safe) |
| 2026-04-19 | 01-08 | `[x]` | (pending) | hardcoded paths/secrets: 0 issues; 2 minor config inconsistencies → spawn queue |

---

## Layer 1 — Static analysis (8 slices)

Find anti-patterns, dead code, and obvious bugs by scanning rather than
executing. Cheap, broad, catches the "how did that even compile" class.

| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 01-01 | ruff lint sweep — top findings + auto-fix | `[x]` | commit 5b16ab1 |
| 01-02 | bandit security scan — secrets, injection, paths | `[x]` | commit 2968308 |
| 01-03 | dead code — vulture + manual unused-function check | `[x]` | 2e536f0 |
| 01-04 | TODO/FIXME/XXX sweep — triage + file as issues or fix | `[x]` | commit b5b3fc2 |
| 01-05 | type annotation audit — mypy on public APIs of core modules | `[x]` | commit b560cc7 |
| 01-06 | complexity audit — radon CC, flag functions >10 | `[x]` | commit 89d0e3e |
| 01-07 | circular import detection — pydeps / manual | `[x]` | commit fdf9502 |
| 01-08 | hardcoded paths + secrets sweep — regex grep | `[x]` | commit ec3b099 |
| 2026-04-19 | 02-01 | `[x]` | cc2c913 | bot_loop pure helpers: 45 new tests covering gates/timers/probes |
| 2026-04-19 | 02-02 | `[x]` | a7977c2 | api_server pure helpers + endpoint shapes: 32 new tests |
| 2026-04-19 | 02-03 | `[x]` | (pending) | app_bridge: delegation-only layer, skipped — Layer 3 territory |
| 2026-04-19 | 02-04 | `[x]` | (pending) | desktop_app: launcher code, skipped — Layer 3 territory |
| 2026-04-19 | 02-05 | `[x]` | 26f9c99 | price_engine: 37 tests — EMA, guards, strategy, AMM math |
| 2026-04-19 | 02-23 | `[x]` | (pending) | risk_manager: 40 tests — CB trip/clear/hysteresis, position limits, spreads |

## Layer 2 — Unit test expansion (32 slices)

Per module, identify functions without coverage and add pytest cases.
Target ~3× current coverage. Integration-style side-effects go in Layer 3.

### Core runtime (4)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-01 | bot_loop.py — cycle orchestrator, gates, timers | `[x]` | commit cc2c913 |
| 02-02 | api_server.py (core) — status/config/bot lifecycle endpoints | `[x]` | commit a7977c2 |
| 02-03 | app_bridge.py — PyWebView API surface methods | `[x]` | delegation layer → Layer 3 |
| 02-04 | desktop_app.py — flag parsing, mode routing | `[x]` | launcher → Layer 3 |

### Market data (6)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-05 | price_engine.py — weighted mid, fallback chain | `[x]` | (pending commit) |
| 02-06 | dexie_manager.py — post, delist, queue, rate-limit | `[ ]` | |
| 02-07 | spacescan.py — activity + tier lookups | `[ ]` | |
| 02-08 | market_data_collector.py — 30-day gather pipeline | `[ ]` | |
| 02-09 | market_intel.py — regime detection, stats | `[ ]` | |
| 02-10 | coinset_client.py — mempool + block record calls | `[ ]` | |

### Offers (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-11 | offer_manager.py — create_ladder, requote, cancel | `[ ]` | |
| 02-12 | offer_lifecycle.py — state machine transitions | `[ ]` | |
| 02-13 | ladder_planner.py + ladder_watchdog.py — shape + taper checks | `[ ]` | |

### Coins (5)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-14 | coin_manager.py — inventory, counts, tier sizing | `[ ]` | |
| 02-15 | coin_prep_worker.py + coin_prep_utils.py — split logic | `[ ]` | |
| 02-16 | coin_classifier.py — classify_coin, is_misfit_coin | `[ ]` | |
| 02-17 | coin_fsm.py + coin_reservations.py + reservation_manager.py | `[ ]` | |
| 02-18 | shape_fix_orchestrator.py + sweep_coordinator.py | `[ ]` | |

### Fills (1)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-19 | fill_tracker.py + fill_classifier.py — detection + classification | `[ ]` | |

### Wallet adapters (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-20 | wallet.py — dispatch layer | `[ ]` | |
| 02-21 | wallet_sage.py — Sage RPC adapter | `[ ]` | |
| 02-22 | wallet_chia.py + chia_node.py + sage_node.py | `[ ]` | |

### Risk & safety (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-23 | risk_manager.py — circuit breaker, position, spreads | `[x]` | (pending commit) |
| 02-24 | amm_monitor.py + mempool_watcher.py — move detection | `[ ]` | |
| 02-25 | dynamic_amm_buffer.py + reaction_strategy.py | `[ ]` | |

### Strategies (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-26 | sniper.py — arb probes, single-sided gate | `[ ]` | |
| 02-27 | boost_manager.py — boost lifecycle | `[ ]` | |
| 02-28 | splash_manager.py + splash_receive.py | `[ ]` | |

### Config + storage (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-29 | config.py + config_live.py + config_validator.py | `[ ]` | |
| 02-30 | database.py — every public function | `[ ]` | |

### Utilities (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-31 | super_log.py + super_log_hooks.py — logging layer | `[ ]` | |
| 02-32 | tx_fees.py, event_taxonomy.py, notification_manager.py | `[ ]` | |

## Layer 3 — Integration tests (18 slices)

End-to-end flows with mocked externals (Sage / Dexie / TibetSwap stubs).
Confirms that modules wire together correctly. Slower than unit tests.

| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 03-01 | startup-flow — fresh app → risk → Sage → dashboard | `[ ]` | |
| 03-02 | bot start/stop cycle — state persists across | `[ ]` | |
| 03-03 | pair-switch — mid-session pair change, DB/state cleanup | `[ ]` | |
| 03-04 | coin-prep full cycle — consolidate → split → verify | `[ ]` | |
| 03-05 | coin-prep retry (soft reset, preserve fills) | `[ ]` | |
| 03-06 | coin-prep full reset (fresh-start path) | `[ ]` | |
| 03-07 | ladder creation on bot start | `[ ]` | |
| 03-08 | requote flow — price move triggers cancel+reissue | `[ ]` | |
| 03-09 | fill detection + PnL round-trip match | `[ ]` | |
| 03-10 | sniper arb cycle — both-sided probe + clean-up | `[ ]` | |
| 03-11 | circuit breaker trip + recover | `[ ]` | |
| 03-12 | cancel-all-flow — stop button → full cancel | `[ ]` | |
| 03-13 | shutdown + resume — state correct on restart | `[ ]` | |
| 03-14 | config reload (live vs stop-required split) | `[ ]` | |
| 03-15 | splash offer receive path | `[ ]` | |
| 03-16 | liquidity-mode switch cycle (two→buy→sell→two) | `[ ]` | |
| 03-17 | topup worker — reserve draws into tiers correctly | `[ ]` | |
| 03-18 | orphan coin cleanup | `[ ]` | |

## Layer 4 — API contracts (22 slices)

Every endpoint: happy path, malformed input, auth failure (if applicable),
idempotency, response-shape validation.

| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 04-01 | status endpoints — /api/status, /api/bot/state, /api/bot/price | `[ ]` | |
| 04-02 | config — GET/POST, reload, live | `[ ]` | |
| 04-03 | bot lifecycle — start, stop, shutdown | `[ ]` | |
| 04-04 | offers endpoints — list, cancel (single + batch), post-to-dexie | `[ ]` | |
| 04-05 | pnl endpoints — pnl, pnl/reset, pnl/reset-preview, fills/purge | `[ ]` | |
| 04-06 | coin-prep endpoints — trigger, status, reset, verify | `[ ]` | |
| 04-07 | session endpoints — fresh-start, resume-chosen, check-resume | `[ ]` | |
| 04-08 | diagnostics endpoints — runtime, api-stats | `[ ]` | |
| 04-09 | sage/wallet endpoints — begin-startup, detect, begin | `[ ]` | |
| 04-10 | smart-defaults endpoint — per-mode branching | `[ ]` | |
| 04-11 | trading-pair endpoints — list, select, refresh | `[ ]` | |
| 04-12 | fills endpoints — list, purge, classify | `[ ]` | |
| 04-13 | logs endpoints — list, filter, export | `[ ]` | |
| 04-14 | dashboard endpoint — aggregated payload | `[ ]` | |
| 04-15 | inventory endpoints — snapshots, current | `[ ]` | |
| 04-16 | market-intel endpoints — regime, stats | `[ ]` | |
| 04-17 | spacescan proxy endpoints | `[ ]` | |
| 04-18 | fees endpoints — status, refresh | `[ ]` | |
| 04-19 | sniper endpoints — stats, recent | `[ ]` | |
| 04-20 | risk / circuit-breaker endpoints | `[ ]` | |
| 04-21 | SSE events stream — /api/events | `[ ]` | |
| 04-22 | splash endpoints + settings export/import | `[ ]` | |

## Layer 5 — UI smoke (26 slices)

Per-view, per-modal. Verify every button does something, every form
field persists, modals open and close, keyboard navigation works.

### Dashboard (4)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-01 | dashboard startup guide card + progression | `[ ]` | |
| 05-02 | dashboard command centre panel (aggregated status) | `[ ]` | |
| 05-03 | dashboard price chart (SSE updates, axis labels) | `[ ]` | |
| 05-04 | dashboard live controls (sliders, requote, spreads) | `[ ]` | |

### Settings (8)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-05 | settings — trading pair card + change flow | `[ ]` | |
| 05-06 | settings — reserves sliders + presets | `[ ]` | |
| 05-07 | settings — liquidity mode picker end-to-end | `[ ]` | |
| 05-08 | settings — smart defaults (risk profile × 3 + apply) | `[ ]` | |
| 05-09 | settings — safety rails (dynamic band, step guard, min/max) | `[ ]` | |
| 05-10 | settings — coin prep summary + preview | `[ ]` | |
| 05-11 | settings — bot operations (sniper, fees, splash, coin prep flags) | `[ ]` | |
| 05-12 | settings — save + export .env + pending-changes banner | `[ ]` | |

### PnL (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-13 | pnl — hero cards populate + flash on change | `[ ]` | |
| 05-14 | pnl — inventory position gauge + drift chart | `[ ]` | |
| 05-15 | pnl — reset position + reset all stats flows | `[ ]` | |

### Offers (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-16 | offers — active table + per-row cancel | `[ ]` | |
| 05-17 | offers — history table + filters | `[ ]` | |

### Market Intel (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-18 | intel — market data panels | `[ ]` | |
| 05-19 | intel — smart advisor suggestions | `[ ]` | |

### Logs (1)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-20 | logs — live feed, filters, export | `[ ]` | |

### Modals + overlays (4)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-21 | startup modals — risk disclosure, Sage connect, wallet picker, splash, spacescan | `[ ]` | |
| 05-22 | coin-prep modal — confirm, progress, complete, error, history-choice | `[ ]` | |
| 05-23 | cancel-all modal + compensating cancel | `[ ]` | |
| 05-24 | reset modals — reset position, reset all stats | `[ ]` | |

### Navigation + chrome (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-25 | v4 tab switching (dashboard/offers/pnl/intel/settings/logs) | `[ ]` | |
| 05-26 | titlebar + status badge + notification badges | `[ ]` | |

## Layer 6 — Live-fire scenarios (12 slices)

**Require a human operator + secondary wallet.** Each slice uses the
`plan_live_fire.md` template. Focus: cross-tab consistency, SSE
propagation, realistic multi-module behaviour under a real fill / swap
/ competitor move. See README section "Live-fire preconditions" for
the one-off setup (second wallet + funding + timing ground rules).

### Taker fills (4)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 06-01 | taker fills bot's top BUY offer — multi-tab verification | `[ ]` | seeded |
| 06-02 | taker fills bot's top SELL offer — mirror direction | `[ ]` | |
| 06-03 | taker eats multiple offers (inner+mid burst) | `[ ]` | |
| 06-09 | taker exhausts one tier — topup worker triggers | `[ ]` | |

### TibetSwap pool moves (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 06-04 | pool price moves UP — bot requotes + (if big) sniper fires | `[ ]` | |
| 06-05 | pool price moves DOWN — mirror of 06-04 | `[ ]` | |
| 06-11 | sudden pool move triggers AMM monitor defensive cancel | `[ ]` | |

### Competitor dynamics (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 06-07 | competitor posts a tight offer on Dexie — bot's competitor-aware pricing responds | `[ ]` | |
| 06-08 | competitor removes their offer — bot returns to normal spread | `[ ]` | |

### Advanced strategies (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 06-10 | fill flips net position long↔short — inventory skew recalc visible | `[ ]` | |
| 06-12 | rapid multiple fills — circuit breaker trips, cooldown, recovers | `[ ]` | |
| 06-06 | partial fill (requires CHIP-0052 partial offer support) | `[ ]` | deferred if partials not live |

## Layer 7 — Degraded-state / disaster recovery (8 slices)

Chaos-style scenarios. Some are mockable (kill a subprocess, inject a
5xx into stubbed APIs); others need the operator to physically pull
the plug on Sage or disable the network. Each needs a restoration
protocol documented in the slice plan — some of these leave the bot
in a state that needs manual recovery.

| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 07-01 | Sage RPC disconnected mid-cycle — recovery + user-visible warning | `[ ]` | |
| 07-02 | Chia node loses sync — bot pauses non-critical writes | `[ ]` | |
| 07-03 | Dexie API returns 5xx intermittently — offer queue retries + rate-limit respected | `[ ]` | |
| 07-04 | TibetSwap API returns 5xx — price engine falls back to Dexie | `[ ]` | |
| 07-05 | coin_prep_worker crashed mid-run — orphan lock cleanup + retry | `[ ]` | |
| 07-06 | database row inconsistency (fills referencing deleted offer) — reconcile gracefully | `[ ]` | |
| 07-07 | disk space exhausted — shutdown cleanly rather than silent data loss | `[ ]` | |
| 07-08 | system clock jumps (simulate) — nothing crashes on negative uptime | `[ ]` | |

---

## Progress summary

- Total slices: **126** (Layers 1-5: 106, Layer 6: 12, Layer 7: 8)
- Pending: **126**
- In progress: **0**
- Done: **0**
- Blocked: **0**

Update these counts when statuses change.
