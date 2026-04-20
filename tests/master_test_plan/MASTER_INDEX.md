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
| 2026-04-19 | 02-03 | `[x]` | 26f9c99 | app_bridge: delegation-only layer, skipped — Layer 3 territory |
| 2026-04-19 | 02-04 | `[x]` | 26f9c99 | desktop_app: launcher code, skipped — Layer 3 territory |
| 2026-04-19 | 02-05 | `[x]` | 26f9c99 | price_engine: 37 tests — EMA, guards, strategy, AMM math |
| 2026-04-19 | 02-23 | `[x]` | 7a9983b | risk_manager: 40 tests — CB trip/clear/hysteresis, position limits, spreads |
| 2026-04-19 | 02-30 | `[x]` | 68ea170 | database: 40 tests — all public functions, temp-DB isolation |
| 2026-04-19 | 02-06 | `[x]` | e21dd96 | dexie_manager: 37 tests — queue ops, stats, prune, metrics |
| 2026-04-19 | 02-29 | `[x]` | 6921d0a | config+validator: 60 tests — helpers, methods, validate_config |
| 2026-04-19 | 02-31 | `[x]` | ea05830 | super_log: 29 tests — ring buffer, levels, cycle stats, db helpers |
| 2026-04-19 | 02-32 | `[x]` | bf24cf8 | tx_fees+event_taxonomy+notif_mgr: 46 tests |
| 2026-04-19 | 02-16 | `[x]` | b92d92a | coin_classifier: 29 tests — dust/reserve/exact/oversize/misfit |
| 2026-04-19 | 02-19 | `[x]` | e4a485b | fill_tracker+classifier: 37 tests — arb detection, mass disappearance |
| 2026-04-19 | 02-09 | `[x]` | 1aba045 | market_intel: 43 tests — _bps_to_pct, parse_offer, analyse_orderbook, state queries, spread reco, DBX eligibility |
| 2026-04-19 | 02-07 | `[x]` | 3538423 | spacescan: 47 tests — tier, budget, verify_fill tree, is_coin_spent; 1 bug fixed (InvalidOperation in get_xch_balance) |
| 2026-04-19 | 02-08 | `[x]` | e11480a | market_data_collector: 40 tests — safe_float, count_from_payload, all 5 analysis functions |
| 2026-04-19 | 02-10 | `[x]` | fdfa1bb | coinset_client: 35 tests — extract_ph, format_response, stats, guards, verify_spent, spendable |
| 2026-04-19 | 02-11 | `[x]` | e83e5b9 | offer_manager: 63 tests — conversions, slot suspension, requote, classify_tier, detect_expiring |
| 2026-04-19 | 02-12 | `[x]` | 344654d | offer_lifecycle: 55 tests — full state machine, all transitions, coarse_status, is_terminal |
| 2026-04-19 | 02-13 | `[x]` | fcb295c | ladder_planner+watchdog: 37 tests — plan_ladder, LadderPlan, audit shape/inversions, coin invariants |
| 2026-04-19 | 02-14 | `[x]` | 2f858ba | coin_manager: 60 tests — record helpers, classify, tier distribution, FeeCoinPool, fast-reconcile |
| 2026-04-19 | 02-17 | `[x]` | 1341646 | coin_fsm+reservations: 49 tests — FSM transitions, terminal, vocab, ReservationRegistry lifecycle |
| 2026-04-19 | 02-20 | `[x]` | (pending) | wallet: 17 tests — dispatch, get_wallet_type, API surface, all callables present |
| 2026-04-19 | 02-21 | `[x]` | e32fc71 | wallet_sage pure fns: 69 tests — rpc_succeeded, classify, offer expiry, mojos, normalize |
| 2026-04-19 | 02-22 | `[x]` | cf1efce | wallet_chia+sage_node: 66 tests — mojos, expiry, _is_open_status, classify, version compare |
| 2026-04-19 | 02-24 | `[x]` | 05a2f00 | amm_monitor+mempool_watcher: 32 tests — encode_amount, coin_id, drift_bps, arb_label, buffer |
| 2026-04-19 | 02-25 | `[x]` | 9672e58 | dynamic_amm_buffer+reaction_strategy: 57 tests — RequoteSeverity, CycleBudget, classify_drift, sweep multipliers |
| 2026-04-19 | 02-26 | `[x]` | (pending) | sniper: 20 tests — bps_to_pct, prune_active_snipes, get_stats, calculate_snipe_size |
| 2026-04-19 | 02-27 | `[x]` | (pending) | boost_manager: 11 tests — bps_to_pct, _find_stale_offers w/ price cache mock |
| 2026-04-19 | 02-28 | `[x]` | (pending) | splash_manager+splash_receive: 31 tests — fingerprint, asset_key, normalize, classify |
| 2026-04-19 | 02-15 | `[x]` | 1142665 | coin_prep_utils+worker: 56 tests — retry/grace helpers, PrepPhase, CoinPrepStatus, static methods, _compute_coin_id |
| 2026-04-19 | 02-18 | `[x]` | 1142665 | shape_fix+sweep_coordinator: 40 tests — Stage/HaltReason enums, FlowState, SweepEntry/Event; fixed 02-24 sys.modules isolation |
| 2026-04-19 | 03-09 | `[x]` | 2fb8831 | fill detection+PnL: 20 tests — real SQLite DB, record_fill, match_round_trip, FIFO unmatched, net position |
| 2026-04-19 | 03-11 | `[x]` | 2fb8831 | circuit breaker: 22 tests — hard limits, hysteresis, dynamic limit+mock PriceEngine, thread safety; streak behaviour documented |
| 2026-04-19 | 03-08 | `[x]` | cf9dc4d | requote flow: 25 tests — offer_manager+reaction_strategy wiring, all severity levels, cooldown, tier superset chain |
| 2026-04-19 | 03-18 | `[x]` | a43522e | orphan coin cleanup: 16 tests — cleanup_orphaned_locked_coins, check_orphan_locks, cancel flow cycle; sys.modules isolation fix |
| 2026-04-19 | 03-07 | `[x]` | b2390a4 | ladder creation: 15 tests — DB→get_free_coins→plan_ladder wiring, viability thresholds, two-sided bot-start cycle |
| 2026-04-19 | 03-10 | `[x]` | da6ac5b | sniper arb cycle: 15 tests — try_snipe both-sided, DB recording, CB halt/side-block, cooldown, prune cycle, stats |
| 2026-04-19 | 03-12 | `[x]` | 6e54a5a | cancel-all flow: 11 tests — confirmed/pending/failed/mixed/side-filter/exception; pending leaves DB open |
| 2026-04-19 | 03-14 | `[x]` | d1ac1ac | config reload: 16 tests — reload/update/thread-safety/quote-stripping; _TempEnv env-var isolation pattern |
| 2026-04-19 | 03-16 | `[x]` | d29b46b | liquidity mode switch: 20 tests — mode→derive cycle, reload/update, invalid default, is_single_sided |
| 2026-04-19 | 03-17 | `[x]` | 0b72934 | topup worker: 19 tests — needs_topup gates/thresholds, pool spend DB accumulation, cooldown state |
| 2026-04-19 | 04-01 | `[x]` | dbf5368 | status endpoints: 29 tests — /api/status /api/bot/state /api/bot/price contracts; 401-vs-405 discovery |
| 2026-04-19 | 04-02 | `[x]` | 68138cc | config endpoints: 27 tests — GET public, POST token+blocked-keys, reload/apply/live contracts |
| 2026-04-19 | 04-03 | `[x]` | 9728df6 | bot lifecycle: 17 tests — start validation gates (CAT/spread/signing), stop, shutdown contracts |
| 2026-04-19 | 04-04 | `[x]` | cad3e96 | offers: 21 tests — GET list, cancel single/batch, cancel_all status; running bot→409, bot=None→direct wallet path |
| 2026-04-19 | 04-05 | `[x]` | 5272e75 | pnl+purge: 21 tests — /api/pnl, reset-preview, reset confirm gate (case-insensitive), fills/purge risk_manager callback |
| 2026-04-19 | 04-06 | `[x]` | 6552bad | coin-prep: 19 tests — status/verify/trigger/reset; trigger mocks Thread, verifies bot.stop() on running bot |
| 2026-04-19 | 04-07 | `[x]` | a95f82a | session: 15 tests — fresh-start, resume-chosen, check-resume (4 branches: bot_running/fresh_start/no_offers/can_resume) |
| 2026-04-19 | 04-08 | `[x]` | 66e62cb | diagnostics: 10 tests — runtime (bot=None safe shape), api-stats (spacescan/coinset/dexie availability) |
| 2026-04-19 | 04-09 | `[x]` | 5b76e44 | sage/wallet: 25 tests — sage-running probe, begin-startup, fingerprints, start-with-fingerprint validation, detect, switch |
| 2026-04-19 | 04-10 | `[x]` | a2de3a5 | smart-defaults: 8 tests — mode routing, fallback, risk_profile fwd, reserve params, exception→500 |
| 2026-04-19 | 04-11 | `[x]` | a2de3a5 | trading-pair: 18 tests — cats list, cat/select validation (64-hex, lengths, decimals, bot-running→409), cat/refresh |
| 2026-04-19 | 04-12 | `[x]` | 4741a4b | fills: 11 tests — /api/fills (bot=None→500, limit param), /api/fills/classified (pagination, type/side filters) |
| 2026-04-19 | 04-13 | `[x]` | f95d5fd | logs: 10 tests — GET/clear/download; clear sets _logs_cleared_at; download returns zip |
| 2026-04-19 | 04-14 | `[x]` | f95d5fd | dashboard: 8 tests — aggregated shape (settings/market_health/wallet/coins/links all verified) |
| 2026-04-19 | 04-15 | `[x]` | bd61f1e | inventory+risk: 8 tests — /api/inventory (bot=None→500, net_position/CB keys), /api/risk/spreads (buy+sell) |
| 2026-04-19 | 04-16-20 | `[x]` | bd61f1e | market-intel/spacescan/fees/sniper/CB: 16 tests — splash mock pattern, spacescan skip/clear, fees success |
| 2026-04-19 | 04-21 | `[x]` | 630a1eb | SSE events: 14 tests — auth guard, headers, subscribe/unsubscribe lifecycle, bot=None vs bot initial state, message format, finite-queue termination pattern |
| 2026-04-19 | 04-22 | `[x]` | 630a1eb | splash+settings: 39 tests — splash stats/receive/node/node-start/incoming webhook (403/400/413/429/200), settings defaults/validate, config export-env |
| 2026-04-19 | 07-03/04/06/08 | `[x]` | 4d8ee80 | degraded-state: 27 tests — Dexie 5xx retry/429/conn-error; TibetSwap 5xx stale cache fallback; fill_tracker None DB graceful; clock-jump negative age |
| 2026-04-19 | 07-07 | `[x]` | fd0483c | disk full: 7 tests — record_fill/record_price/log_event return -1/False; all rollback to release write lock; consecutive failures don't cascade |
| 2026-04-19 | 07-01/02 | `[x]` | 9b50515 | Sage RPC disconnect + node sync loss: 20 tests — rpc() error dict/None, _rpc_succeeded, ensure_initialized port-unreachable, sync status offline/syncing/unknown, get_chia_health healthy flag |
| 2026-04-19 | 07-05 | `[x]` | b317e27 | coin_prep_worker crash: 15 tests — check_coin_prep_status (no proc/running/crash/success/IOError), status endpoint crash detection (error phase, exit code, clean exit guard), trigger resets error state |
| 2026-04-19 | 03-15 | `[x]` | 6c4d0e9 | splash receive path: 14 tests — real SQLite, DB write/retrieval, source_ip, status=new, fingerprint dedup, multi-offer, stats, SSE emit (bot present / absent / duplicate) |
| 2026-04-19 | 03-13 | `[x]` | b509f05 | shutdown+resume: 14 tests — real SQLite; check-resume wallet→can_resume, fresh_start flag guard; resume-chosen preserves fills; fresh-start clears fills+sets flag; regression fix restores session_start_time in tearDown |
| 2026-04-19 | 03-02 | `[x]` | 0021a80 | bot start/stop: 17 tests — start validation gates, DB fills survive full start→stop cycle, already-running guard, events.emit contracts |
| 2026-04-19 | 03-03 | `[x]` | 0021a80 | pair-switch: 17 tests — blocked while running (409), _active_cat updated, risk_manager.reset_session() called, DB fills preserved across all switches |
| 2026-04-19 | 03-04/05/06 | `[x]` | f51a2d3 | coin prep lifecycle: 25 tests — trigger state (running/run_id/started_at/bot.stop), default preserves fills, soft reset clears running without touching DB, full_reset=True clears fills |
| 2026-04-19 | 03-01 | `[x]` | 6697e0d | startup flow: 26 tests — check-resume (fresh/offers/running bot), session mode, Sage probe, CAT select, dashboard 200+keys, pre-start validation (CAT missing→400, valid→200) |
| 2026-04-20 | 05-01 | `[x]` | live | startup guide card: checklist steps (Wallet/Trading Pair green, Settings/Coin Prep/Start amber), pair selector populates, Review Settings link |
| 2026-04-20 | 05-02 | `[x]` | live | command centre: Start Bot/Stop/No Offers To Cancel, STOPPED badge, Synced+Sage+wallet chips, 7D 194.4 XCH / 30D 418.9 XCH live volumes |
| 2026-04-20 | 05-03 | `[x]` | live | price chart: MID PRICE LAST 20 MINUTES with live data, "Samples every 5s · max 240 points" label |
| 2026-04-20 | 05-04 | `[x]` | live | ACTIVE SETTINGS (Trading/Spreads/Inventory/Tiers/Safety PENDING states), MARKET HEALTH live (INNER 14.6%, MARKET 53.5%, 62 competitors, ARB GAP 20.0%, POOL DEPTH 1.16%) |
| 2026-04-20 | 05-05 | `[x]` | live | settings trading pair card: Monkeyzoo Token / XCH confirmed, Change Trading Pair button, Live Controls link "adjustable while running" |
| 2026-04-20 | 05-06 | `[x]` | live | reserves sliders: 5%/10%/25%/50% presets, live balances 128.4479 XCH / 753,298 MZ tokens shown |
| 2026-04-20 | 05-07 | `[x]` | live | liquidity mode picker: Buy/Both/Sell radio cards visible and selectable |
| 2026-04-20 | 05-08 | `[x]` | live | smart defaults: Conservative/Balanced/Aggressive risk profile selector, Smart Settings button visible |
| 2026-04-20 | 05-09 | `[x]` | live | safety rails: Dry Run / Enable Buy Side / Enable Sell Side, Max Price Move, Dynamic Band, Step-Change Guard inputs visible |
| 2026-04-20 | 05-10 | `[x]` | live | order book + offer sizing: Max Buy/Sell offers, Base Trade Size, Tiered Sizing + Reverse Buy Ladder checkboxes, tier size grid (Inner/Mid/Outer/Extreme) |
| 2026-04-20 | 05-11 | `[x]` | live | bot operations: Sniper, Transaction Fees, Splash P2P, Coin Prep, Runtime Coin Health, Auto-Apply Sage Change Address flags; Topup Pool, Coin Prep Budget fields |
| 2026-04-20 | 05-12 | `[x]` | live | settings footer: Export .env + Save & Continue buttons; Market Intelligence / DBX Rewards Max Spread; settings scroll 5711px total |
| 2026-04-20 | 05-13 | `[x]` | live | P&L hero cards: SESSION/ROUND TRIPS/TOTAL FILLS/VOLUME TRADED/AVG PER RT/AVG FILL SIZE/BOUGHT/SOLD/NET FLOW all rendering |
| 2026-04-20 | 05-14 | `[x]` | live | inventory gauge: Net Position 0.00 CAT, Position Limit 5.0 XCH, Neutral progress bar; POSITION DRIFT chart flat; CURRENT SPREADS live 8.8%/9.3% Dynamic+Inventory |
| 2026-04-20 | 05-15 | `[x]` | live | reset flows: Reset Position modal ("clears fill records, cannot be undone" + Cancel/Reset); Reset All Stats modal ("Clear fills, round-trips, price history, inventory" + Cancel/Reset Everything) — both cancelled safely |
| 2026-04-20 | 05-16 | `[x]` | live | offers: ORDER BOOK DEPTH (Buy: 0 offers, Sell: 0 offers), Active/History tabs with counts, Filter: All/Buy/Sell buttons all clickable |
| 2026-04-20 | 05-17 | `[x]` | live | history tab: "Recent Activity (Last 20)" heading, Buy filter active, empty state "No fills in this session yet", Export Fills button visible |
| 2026-04-20 | 05-18 | `[x]` | live | market intel: Dexie Orderbook (Buy 63.97 XCH/35 offers, Sell 426.92 XCH/27 offers live), TibetSwap Pool (121.30 XCH, 0.71% slippage), DBX Rewards, Spacescan Token Context |
| 2026-04-20 | 05-19 | `[x]` | live | intel live data: INNER SPREAD 14.6%, MARKET SPREAD 53.5%, COMPETITORS 62 (both), ARB GAP 20.0% (amber), POOL DEPTH 1.16%, Splash P2P Offline/Installed 0.2.0 |
| 2026-04-20 | 05-20 | `[x]` | live | logs: live circuit_breaker_tripped WARNINGs, "Load older (547 more)" button, API Stats modal (Spacescan 30/30 budget, Coinset Sage-compat, Dexie v3 cached), Clear button |
| 2026-04-20 | 05-21 | `[x]` | live | startup modals: Risk Disclosure (4 risk cards + Continue/Close), Connect Wallet ("Sage is already open"), Splash P2P (Skip option), Spacescan (Use Free Tier) — full flow exercised on reload |
| 2026-04-20 | 05-22 | `[x]` | live | coin prep: triggerCoinPrep() confirmed running via API (PID 191872, consolidating→splitting, progress 10%→30%), toast "Coin prep started!", reset via /api/coin-prep/reset success |
| 2026-04-20 | 05-23 | `[x]` | live | cancel-all modal: OFFER CONTROL header, "Cancelling offers" title, progress bar, PROCESSED 0/0, FAILURES 0, Hide button; openCancelProgressModal() confirmed |
| 2026-04-20 | 05-24 | `[x]` | live | reset modals: Reset All Stats confirmation (⚠ icon, Cancel/Reset Everything), Reset Position confirmation (↺ icon, Cancel/Reset) — both confirmed rendering, both cancelled |
| 2026-04-20 | 05-25 | `[x]` | live | tab switching: all 6 views (dashboard/offers/pnl/intel/settings/logs) switch correctly via nav[0-5]; active class updates; onclick attrs mapped |
| 2026-04-20 | 05-26 | `[x]` | live | chrome: .status-badge "◯ Stopped" confirmed, v4-titlebar pywebview-drag-region present, logs badge (counts to 59+), theme toggle dark↔light confirmed, Help modal (13 tabs), About modal (v0.4 changelog), Shutdown modal (cancel-offers checkbox + Go Back/Shutdown App) |

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
| 02-05 | price_engine.py — weighted mid, fallback chain | `[x]` | commit 26f9c99 |
| 02-06 | dexie_manager.py — post, delist, queue, rate-limit | `[x]` | commit e21dd96 |
| 02-07 | spacescan.py — activity + tier lookups | `[x]` | commit 3538423 |
| 02-08 | market_data_collector.py — 30-day gather pipeline | `[x]` | (pending commit) |
| 02-09 | market_intel.py — regime detection, stats | `[x]` | commit 1aba045 |
| 02-10 | coinset_client.py — mempool + block record calls | `[x]` | commit fdfa1bb |

### Offers (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-11 | offer_manager.py — create_ladder, requote, cancel | `[x]` | commit e83e5b9 |
| 02-12 | offer_lifecycle.py — state machine transitions | `[x]` | (pending commit) |
| 02-13 | ladder_planner.py + ladder_watchdog.py — shape + taper checks | `[x]` | (pending commit) |

### Coins (5)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-14 | coin_manager.py — inventory, counts, tier sizing | `[x]` | (pending commit) |
| 02-15 | coin_prep_worker.py + coin_prep_utils.py — split logic | `[x]` | commit 1142665 |
| 02-16 | coin_classifier.py — classify_coin, is_misfit_coin | `[x]` | commit b92d92a |
| 02-17 | coin_fsm.py + coin_reservations.py + reservation_manager.py | `[x]` | (pending commit) |
| 02-18 | shape_fix_orchestrator.py + sweep_coordinator.py | `[x]` | commit 1142665 |

### Fills (1)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-19 | fill_tracker.py + fill_classifier.py — detection + classification | `[x]` | commit e4a485b |

### Wallet adapters (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-20 | wallet.py — dispatch layer | `[x]` | (pending commit) |
| 02-21 | wallet_sage.py — Sage RPC adapter | `[x]` | (pending commit) |
| 02-22 | wallet_chia.py + chia_node.py + sage_node.py | `[x]` | (pending commit) |

### Risk & safety (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-23 | risk_manager.py — circuit breaker, position, spreads | `[x]` | commit 7a9983b |
| 02-24 | amm_monitor.py + mempool_watcher.py — move detection | `[x]` | (pending commit) |
| 02-25 | dynamic_amm_buffer.py + reaction_strategy.py | `[x]` | (pending commit) |

### Strategies (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-26 | sniper.py — arb probes, single-sided gate | `[x]` | (pending commit) |
| 02-27 | boost_manager.py — boost lifecycle | `[x]` | (pending commit) |
| 02-28 | splash_manager.py + splash_receive.py | `[x]` | (pending commit) |

### Config + storage (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-29 | config.py + config_live.py + config_validator.py | `[x]` | commit 6921d0a |
| 02-30 | database.py — every public function | `[x]` | commit 68ea170 |

### Utilities (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 02-31 | super_log.py + super_log_hooks.py — logging layer | `[x]` | commit ea05830 |
| 02-32 | tx_fees.py, event_taxonomy.py, notification_manager.py | `[x]` | commit bf24cf8 |

## Layer 3 — Integration tests (18 slices)

End-to-end flows with mocked externals (Sage / Dexie / TibetSwap stubs).
Confirms that modules wire together correctly. Slower than unit tests.

| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 03-01 | startup-flow — fresh app → risk → Sage → dashboard | `[x]` | commit 6697e0d |
| 03-02 | bot start/stop cycle — state persists across | `[x]` | commit 0021a80 |
| 03-03 | pair-switch — mid-session pair change, DB/state cleanup | `[x]` | commit 0021a80 |
| 03-04 | coin-prep full cycle — consolidate → split → verify | `[x]` | commit f51a2d3 |
| 03-05 | coin-prep retry (soft reset, preserve fills) | `[x]` | commit f51a2d3 |
| 03-06 | coin-prep full reset (fresh-start path) | `[x]` | commit f51a2d3 |
| 03-07 | ladder creation on bot start | `[x]` | commit b2390a4 |
| 03-08 | requote flow — price move triggers cancel+reissue | `[x]` | commit cf9dc4d |
| 03-09 | fill detection + PnL round-trip match | `[x]` | commit 2fb8831 |
| 03-10 | sniper arb cycle — both-sided probe + clean-up | `[x]` | commit da6ac5b |
| 03-11 | circuit breaker trip + recover | `[x]` | commit 2fb8831 |
| 03-12 | cancel-all-flow — stop button → full cancel | `[x]` | commit 6e54a5a |
| 03-02 | bot start/stop cycle — state persists across | `[x]` | commit 0021a80 |
| 03-03 | pair-switch — mid-session pair change, DB/state cleanup | `[x]` | commit 0021a80 |
| 03-13 | shutdown + resume — state correct on restart | `[x]` | commit b509f05 |
| 03-14 | config reload (live vs stop-required split) | `[x]` | commit d1ac1ac |
| 03-15 | splash offer receive path | `[x]` | commit 6c4d0e9 |
| 03-16 | liquidity-mode switch cycle (two→buy→sell→two) | `[x]` | commit d29b46b |
| 03-17 | topup worker — reserve draws into tiers correctly | `[x]` | commit 0b72934 |
| 03-18 | orphan coin cleanup | `[x]` | commit a43522e |

## Layer 4 — API contracts (22 slices)

Every endpoint: happy path, malformed input, auth failure (if applicable),
idempotency, response-shape validation.

| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 04-01 | status endpoints — /api/status, /api/bot/state, /api/bot/price | `[x]` | commit dbf5368 |
| 04-02 | config — GET/POST, reload, live | `[x]` | commit 68138cc |
| 04-03 | bot lifecycle — start, stop, shutdown | `[x]` | commit 9728df6 |
| 04-04 | offers endpoints — list, cancel (single + batch), post-to-dexie | `[x]` | commit cad3e96 |
| 04-05 | pnl endpoints — pnl, pnl/reset, pnl/reset-preview, fills/purge | `[x]` | commit 5272e75 |
| 04-06 | coin-prep endpoints — trigger, status, reset, verify | `[x]` | commit 6552bad |
| 04-07 | session endpoints — fresh-start, resume-chosen, check-resume | `[x]` | commit a95f82a |
| 04-08 | diagnostics endpoints — runtime, api-stats | `[x]` | commit 66e62cb |
| 04-09 | sage/wallet endpoints — begin-startup, detect, begin | `[x]` | commit 5b76e44 |
| 04-10 | smart-defaults endpoint — per-mode branching | `[x]` | commit a2de3a5 |
| 04-11 | trading-pair endpoints — list, select, refresh | `[x]` | commit a2de3a5 |
| 04-12 | fills endpoints — list, purge, classify | `[x]` | commit 4741a4b |
| 04-13 | logs endpoints — list, filter, export | `[x]` | commit f95d5fd |
| 04-14 | dashboard endpoint — aggregated payload | `[x]` | commit f95d5fd |
| 04-15 | inventory endpoints — snapshots, current | `[x]` | commit bd61f1e |
| 04-16 | market-intel endpoints — regime, stats | `[x]` | commit bd61f1e |
| 04-17 | spacescan proxy endpoints | `[x]` | commit bd61f1e |
| 04-18 | fees endpoints — status, refresh | `[x]` | commit bd61f1e |
| 04-19 | sniper endpoints — stats, recent | `[x]` | commit bd61f1e |
| 04-20 | risk / circuit-breaker endpoints | `[x]` | commit bd61f1e |
| 04-21 | SSE events stream — /api/events | `[x]` | commit 630a1eb |
| 04-22 | splash endpoints + settings export/import | `[x]` | commit 630a1eb |

## Layer 5 — UI smoke (26 slices)

Per-view, per-modal. Verify every button does something, every form
field persists, modals open and close, keyboard navigation works.

### Dashboard (4)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-01 | dashboard startup guide card + progression | `[x]` | 2026-04-20 live |
| 05-02 | dashboard command centre panel (aggregated status) | `[x]` | 2026-04-20 live |
| 05-03 | dashboard price chart (SSE updates, axis labels) | `[x]` | 2026-04-20 live |
| 05-04 | dashboard live controls (sliders, requote, spreads) | `[x]` | 2026-04-20 live — ACTIVE SETTINGS + MARKET HEALTH verified; sliders only visible while bot running |

### Settings (8)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-05 | settings — trading pair card + change flow | `[x]` | 2026-04-20 live |
| 05-06 | settings — reserves sliders + presets | `[x]` | 2026-04-20 live |
| 05-07 | settings — liquidity mode picker end-to-end | `[x]` | 2026-04-20 live |
| 05-08 | settings — smart defaults (risk profile × 3 + apply) | `[x]` | 2026-04-20 live |
| 05-09 | settings — safety rails (dynamic band, step guard, min/max) | `[x]` | 2026-04-20 live |
| 05-10 | settings — coin prep summary + preview | `[x]` | 2026-04-20 live |
| 05-11 | settings — bot operations (sniper, fees, splash, coin prep flags) | `[x]` | 2026-04-20 live |
| 05-12 | settings — save + export .env + pending-changes banner | `[x]` | 2026-04-20 live |

### PnL (3)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-13 | pnl — hero cards populate + flash on change | `[x]` | 2026-04-20 live |
| 05-14 | pnl — inventory position gauge + drift chart | `[x]` | 2026-04-20 live |
| 05-15 | pnl — reset position + reset all stats flows | `[x]` | 2026-04-20 live |

### Offers (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-16 | offers — active table + per-row cancel | `[x]` | 2026-04-20 live |
| 05-17 | offers — history table + filters | `[x]` | 2026-04-20 live |

### Market Intel (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-18 | intel — market data panels | `[x]` | 2026-04-20 live |
| 05-19 | intel — smart advisor suggestions | `[x]` | 2026-04-20 live — live orderbook data confirmed |

### Logs (1)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-20 | logs — live feed, filters, export | `[x]` | 2026-04-20 live |

### Modals + overlays (4)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-21 | startup modals — risk disclosure, Sage connect, wallet picker, splash, spacescan | `[x]` | 2026-04-20 live |
| 05-22 | coin-prep modal — confirm, progress, complete, error, history-choice | `[x]` | 2026-04-20 live — real prep ran on test wallet (consolidate→split); reset cleanly |
| 05-23 | cancel-all modal + compensating cancel | `[x]` | 2026-04-20 live |
| 05-24 | reset modals — reset position, reset all stats | `[x]` | 2026-04-20 live |

### Navigation + chrome (2)
| Slice | Title | Status | Note |
|-------|-------|--------|------|
| 05-25 | v4 tab switching (dashboard/offers/pnl/intel/settings/logs) | `[x]` | 2026-04-20 live |
| 05-26 | titlebar + status badge + notification badges | `[x]` | 2026-04-20 live |

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
| 07-01 | Sage RPC disconnected mid-cycle — recovery + user-visible warning | `[x]` | commit 9b50515 |
| 07-02 | Chia node loses sync — bot pauses non-critical writes | `[x]` | commit 9b50515 |
| 07-03 | Dexie API returns 5xx intermittently — offer queue retries + rate-limit respected | `[x]` | commit 4d8ee80 |
| 07-04 | TibetSwap API returns 5xx — price engine falls back to Dexie | `[x]` | commit 4d8ee80 |
| 07-05 | coin_prep_worker crashed mid-run — orphan lock cleanup + retry | `[x]` | commit b317e27 |
| 07-06 | database row inconsistency (fills referencing deleted offer) — reconcile gracefully | `[x]` | commit 4d8ee80 |
| 07-07 | disk space exhausted — shutdown cleanly rather than silent data loss | `[x]` | commit fd0483c |
| 07-08 | system clock jumps (simulate) — nothing crashes on negative uptime | `[x]` | commit 4d8ee80 |

---

| 2026-04-20 | 06-F | `[x]` | live / ea991ec / be22e3f | Settings combos: all 4 scenarios passed. F3-A log fixed (ea991ec). F4-A smart-defaults fixed (be22e3f). |
| 2026-04-20 | 06-G | `[x]` | ea991ec | Settings validation: all 5 bugs fixed — G1 enum check, G2 bot-running guard (409), G3 neg-spread rejected at save, G5b zero-slots warn in bot-start, G6 sniper+single-sided warn, G7 spread>max warn. |

## Progress summary

- Total slices: **126** (Layers 1-5: 106, Layer 6: 12, Layer 7: 8)
- Pending: **12** (Layer 6 taker-fill slices 06-01 through 06-12 need secondary wallet)
- In progress: **0**
- Done: **114** (L1: 8, L2: 32, L3: 18, L4: 22, L5: 26, L7: 8; plus live-fire phases F+G in 06_live_fire/)
- Blocked: **0**

Update these counts when statuses change.
