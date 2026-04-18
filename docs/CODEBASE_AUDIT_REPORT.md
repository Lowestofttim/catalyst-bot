# CATalyst — Full Codebase Documentation & Audit Map

**Repository:** `Lowestofttim/catalyst-bot` (local path `C:\chia_liquidity_bot_v2_v4_tauri`)
**Generated:** 2026-04-18
**HEAD commit at time of scan:** `5483e4b — F81: defensive cancel on mempool imminent_swap + observability`
**Scope:** Every tracked Python file, the HTML frontend, build configs, CI workflows, docs, and the test suite.

---

## How to use this document

Each file has a dedicated section with a consistent shape:

- **Purpose** — one or two sentences.
- **Key classes and functions** — the public / important symbols.
- **Imports it depends on (other project modules)** — intra-repo deps.
- **Modules/callers that import it** — who consumes it.
- **External services/APIs touched** — wallet RPC, TibetSwap, Dexie, Spacescan, Coinset, SQLite, filesystem, etc.
- **State it owns** — singletons, caches, locks, DB tables.
- **Notable behaviors / risks worth flagging for a later audit** — concrete items to review for correctness, security, or robustness.

The intent is that a reader can build a full architecture/interaction diagram from this document alone.

The report is organised in the same layers used during the scan:

1. Trading core (loop, offers, fills)
2. Coin management (UTXO, prep, FSM, reservations)
3. Wallet layer (adapter + Sage + Chia)
4. Price / market data / AMM / mempool watchers
5. Risk, health, strategy
6. Config / infrastructure / database / logging
7. UI — server, bridge, desktop window, tray, HTML
8. RPC clients, Splash, build/deploy, docs, CI, tests, tools

Size/line-count numbers are approximate (rounded; Windows line-counts can vary by CRLF/LF).

---

## 1. Trading Core

### `bot_loop.py` (~446KB, ~8985 lines)

**Purpose:** Central orchestrator of the bot. Executes one complete trading cycle every `LOOP_SECONDS`, coordinating price fetches, fill detection, offer creation/requoting, coin management, and background health monitoring.

**Key classes and functions:**
- `BotLoop` — Main orchestrator class; owns all module instances and cycle coordination.
- `BotLoop.start()` / `stop()` / `is_running()` — thread lifecycle.
- `BotLoop._run_one_cycle()` — executes one trading iteration (fetch prices → detect fills → requote → create offers → coin/ladder housekeeping).
- `map_sage_terminal_offer_status()` — maps Sage rich terminal states to the local 4-value enum (open/filled/cancelled/expired).
- `_bps_to_pct()` — format helper.

**Imports it depends on:** `config`, `database`, `price_engine`, `offer_manager`, `fill_tracker`, `dexie_manager`, `splash_manager`, `splash_node`, `coinset_client`, `coin_manager`, `risk_manager`, `sniper`, `boost_manager`, `market_intel`, `runtime_monitor`, `amm_monitor`, `splash_receive`, `wallet`, `super_log`, `mempool_watcher`.

**Modules/callers that import it:** `api_server`, `database` (indirect), `super_log_hooks`.

**External services/APIs touched:** Chia/Sage wallet RPC, Chia full-node RPC, TibetSwap (via `price_engine` / `amm_monitor`), Dexie (via `dexie_manager`), Spacescan (via optional context getter), Splash RPC, Coinset HTTP.

**State it owns:**
- All top-level module instances.
- `_running`, `_thread`, `_loop_count`, `_start_time`.
- `_bot_state` dict (GUI snapshot).
- `_ladder_grid_mid`, `_ladder_anchor_plain_mid`.
- `_probe_state` (sniper discovery sub-state machine).
- `_sweep_protection` per-side cooldown windows.
- `_chia_health`, `_watcher_data`.
- Background threads: `_health_thread`, `_watcher_thread`, `_coin_watcher_thread`, `_splash_receive_thread`.

**Notable behaviors / risks:**
- **Circular wiring post-instantiation.** `__init__` creates modules then injects cross-refs (`offer_manager.amm_monitor = self.amm_monitor`, `offer_manager._fee_pool = coin_manager.fee_pool`). Order matters; missing injection → `AttributeError` in nested calls.
- **Background thread lifecycle.** Health monitor auto-restarts bot after 5 min of unhealthy wallet (~line 350); `stop()` uses a 10s join timeout — threads may not fully quiesce before exit.
- **Probe state machine complexity.** `_probe_state` has ~5+ fields set directly in a dict; no formal state machine guards validity.
- **Mass-disappearance guard.** Fill-detection 3-strike rule (~line 108) trusts wallet RPC consistency; sustained partial RPC results can masquerade as fills.
- **Ladder anchor drift detection.** Probe-anchored mid is cached separately from plain mid; timing of `_clear_probe_side` vs next requote matters.

---

### `offer_manager.py` (~155KB, ~3211 lines)

**Purpose:** Manages the full lifecycle of market-making offers: ladder creation, price tracking, requoting when mid moves beyond threshold, expiry, cancellation; bridges `price_engine` to the wallet RPC.

**Key classes and functions:**
- `OfferManager` — core class.
- `create_ladder(mid_price, side, num_offers)` — create N offers on one side with tier-dependent spreads.
- `should_requote()` / `should_requote_graduated()` — price-move threshold checks.
- `requote_side(side, current_price, reason)` — cancel stale + create fresh.
- `cancel_offers(trade_ids, reason)` — batch cancel + mark via `_bot_cancelled_ids`.
- `detect_expiring_offers(open_offers, now_ts)` / `cleanup_expired()`.
- `is_bot_cancelled(trade_id)` — consumed by `FillTracker`.
- `retry_failed_cancels()` — retry unconfirmed cancels.
- `create_offer_with_retry()` — RPC wrapper with retry logic.
- Unit helpers: `xch_to_mojos`, `mojos_to_xch`, `cat_to_mojos`, `mojos_to_cat`.

**Imports it depends on:** `config`, `database`, `wallet`.

**Modules/callers that import it:** `bot_loop`, `sniper`, `boost_manager`, `sweep_coordinator`.

**External services/APIs touched:** Wallet RPC (`create_offer`, `cancel_offer`, `cancel_offers_batch`, `get_all_offers`, `get_exact_spendable_coins_rpc`, `get_wallet_type`, `get_owned_coins_detailed`); SQLite (`add_offer`, `update_offer_status`, `transition_offer`, `get_open_offers`, `get_offer`).

**State it owns:**
- `_bot_cancelled_ids` (set) — offers WE cancelled (fill-detector suppression).
- `_offer_details_cache` — trade_id → {coin_id, side, price, …}.
- `_last_requote_time` — per-side cooldown.
- `_pending_cancel_retries` — retry book.
- `_recently_created` — 10-min TTL cache to silence false fills during wallet sync lag.
- `_inflight_coin_ids`, `_used_coin_ids_this_cycle` — prevent double-spend across parallel ladder threads.
- `_stop_requested`.
- Injected refs: `amm_monitor`, `dexie_manager`, `_fee_pool`.

**Notable behaviors / risks:**
- **Retry book cleanup.** `_pending_cancel_retries` auto-expires only after max retries; `first_failed` timestamp is never inspected, so stale retries can linger.
- **Coin tracking fragmentation.** Three sets (`_inflight_coin_ids`, `_used_coin_ids_this_cycle`, `_recently_created`). Cross-module coordination with `sniper` / `boost_manager` can race → `MEMPOOL_CONFLICT` if coordination breaks.
- **Size jitter.** `_slot_size_variation()` adds ±0.5% to each slot; deterministic but undocumented.
- **Tier classification.** `_classify_tier()` assigns inner/mid/outer by slot position; sniper probes are implicit tier — mismatches break round-trip PnL matching.
- **Lock contention.** `_lock` protects multiple sets; no timeout/logging on acquisition.

---

### `offer_lifecycle.py` (~12KB, ~280 lines)

**Purpose:** Canonical state machine for offer tracking. Maps extended states (open / refresh_due / cancel_requested / cancelled / mempool_observed / filled / expired / phantom_rejected) to signals and enforces valid transitions. Preserves backward-compat with legacy 4-value status column.

**Key classes and functions:**
- `OfferState` (StrEnum) — 8 values.
- `OfferSignal` (StrEnum) — 10 values.
- `OfferTransition` — frozen dataclass.
- `apply_signal(state, signal)` — pure function.
- `apply_fill_verification(state, signal)` — handles FILL_VERIFIED / FILL_REJECTED from FILLED.
- `coarse_status(lifecycle_state)` — maps to legacy 4-value.
- `is_terminal(state)`.

**Imports it depends on:** None.

**Modules/callers that import it:** `database`, `fill_tracker`, `tests/test_offer_lifecycle.py`.

**External services/APIs touched:** None.

**State it owns:** None (pure).

**Notable behaviors / risks:**
- **Terminal states** {CANCELLED, FILLED, EXPIRED, PHANTOM_REJECTED} silently no-op all signals — bugs in callers fail silently.
- **MEMPOOL_OBSERVED race** — both CANCEL_SENT and TIME_EXPIRED are legal; if offer fills on-chain after cancel submission, FILL_DETECTED and CANCEL_CONFIRMED can both fire.
- **No recovery from PHANTOM_REJECTED.** If verification misclassifies, only direct DB edit unsticks it.
- **Missing REFRESH_POSTED→OPEN signal.** A failed refresh can leave an offer stuck in `refresh_due`.

---

### `fill_tracker.py` (~67KB, ~1414 lines)

**Purpose:** Detects offer fills by diffing before/after offer-ID snapshots; verifies fills on-chain via Sage → Dexie → Spacescan; records fills + matches buy↔sell round-trips for PnL. Applies mass-disappearance guard.

**Key classes and functions:**
- `FillTracker` — main class.
- `detect_fills(current_buy_ids, current_sell_ids, offer_details_cache)` → {buy_fills, sell_fills}.
- `_check_mass_disappearance()` — 3-strike RPC-blip guard.
- `_process_disappeared()` — verify on-chain + record.
- `_verify_fill_on_chain(trade_id, side)` — Sage → Dexie → Spacescan fallback chain.
- `_check_sage_offer_confirmed()`, `_check_dexie_offer_state()`.
- `_record_fill(...)` — persists fill, calls `classify_and_store_fill`, notifies `SweepCoordinator`.
- `match_round_trips()` — 4-pass matching algorithm.
- `should_protect_side(side)` — post-fill cooldown.
- `reset_baseline()` / `set_baseline()` — test hooks.

**Imports it depends on:** `config`, `database`; dynamic: `fill_classifier`, `sweep_coordinator`.

**Modules/callers that import it:** `bot_loop`.

**External services/APIs touched:** Sage RPC, Dexie HTTP, Spacescan, SQLite.

**State it owns:** `_previous_ids`, `_mass_disappearance_count`, `_mass_disappearance_first_at`, `_last_fill_time`, `_last_fill_count`, `_fill_history` (cap 50), `_last_dexie_details`.

**Notable behaviors / risks:**
- **Mass-disappearance counter never resets on good polls** — a permanently flaky wallet can "stick" the counter and treat every diff as a real fill.
- **Fallback-chain failure leaves fills in limbo** — if all three verifiers fail, fill isn't recorded and re-fires next cycle; chronic Spacescan outage → PnL never matches.
- **Dexie detail cache pop is order-sensitive** — if `_record_fill` is called out-of-order (e.g., via `sweep_coordinator`), classification downgrades to UNKNOWN.
- **4-pass matching is tier-sensitive** — changing tier sizes mid-run orphans old fills with no migration.
- **Zero-amount fills silently skipped** — no alert.

---

### `fill_classifier.py` (~12KB, ~317 lines)

**Purpose:** Classifies each detected fill into RETAIL / ARB_SWEEP_BUY / ARB_SWEEP_SELL / DEXIE_COMBINED / UNKNOWN using taker puzzle-hash matching + Dexie metadata + block-index clustering. Used by `SweepCoordinator`.

**Key classes and functions:**
- `FillType` constants.
- `FillClassification` dataclass (+ `.is_arb()`).
- `classify_fill(trade_id, fill_detail, dexie_detail)`.
- `update_fill_classification(fill_id, classification)` — fail-open persistence.
- `classify_and_store_fill(...)` — wrapper.
- `_extract_taker_puzzle_hash(detail, side)`.

**Imports it depends on:** Dynamic `config` (KNOWN_ARB_PUZZLE_HASHES), `database`.

**Modules/callers that import it:** `bot_loop` (via `splash_receive`), `fill_tracker`, `sweep_coordinator`.

**External services/APIs touched:** SQLite, config.

**State it owns:** None.

**Notable behaviors / risks:**
- **Config puzzle-hash dedupe is silent** — typos in config never get detected.
- **Dexie output_coins schema assumption** — any field-rename break silently downgrades to UNKNOWN.
- **Debug-level log on missing `spent_block_index`** can spam.
- **Fail-open** — any exception = UNKNOWN, no alert.
- **Side-aware extraction** fails silently on unexpected `side` values.

---

## 2. Coin Management

### `coin_manager.py` (~334KB, ~6738 lines)

**Purpose:** Central coin health monitor and lifecycle manager. Maintains live inventory of XCH + CAT coins classified by role (reserve/trading/small), manages the coin-prep subprocess, runs smart topup and consolidation logic, reconciles with wallet state.

**Key classes and functions:**
- `CoinManager` — main controller.
- `FeeCoinPool` — thread-safe dedicated fee-coin reservation.
- `request_fast_reconcile(reason)` / `consume_fast_reconcile()` — module-level signal.
- `_classify_coins(...)` — legacy categorisation.
- `_classify_coins_tiered(...)` — tier-aware bucketing.
- `get_tier_sizes_mojos_from_cfg()`, `get_tier_distribution()`, `get_weighted_tier_prep_counts()`.
- `update_coin_counts()`, `reconcile_with_wallet()`.
- `needs_coin_prep()`, `needs_topup()`, `start_topup()`.
- `check_runtime_health()`.
- `start_coin_prep()` — spawns `coin_prep_worker` as subprocess.
- `check_coin_prep_status()`.
- `_topup_worker(...)` — split-reserve → consolidate → backoff decision tree.

**Imports it depends on:** `config`, `database`, `tx_fees`, `wallet`, `win_subprocess`, lazy `coin_reservations`, `coin_classifier`.

**Modules/callers that import it:** `bot_loop`, `api_server`, `offer_manager`, `super_log_hooks`.

**External services/APIs touched:** Sage/Chia wallet RPC (coin queries, splits, balances); SQLite; Spacescan (confirming self-sends).

**State it owns:**
- `_xch_coins`, `_cat_coins`, `_xch_locked_coins`, `_xch_locked_amount`…
- `_xch_inventory`, `_cat_inventory`.
- `self.reservations` (ReservationRegistry), `self._fee_pool` (FeeCoinPool).
- Topup state: `_topup_running`, `_topup_thread`, `_no_coins_backoff`, `_last_topup_time`, `_topup_is_drip`.
- Prep state: `_prep_running`, `_prep_process`.
- Reserve tracking: `_reserve_ids_xch`, `_reserve_ids_cat`, `_tier_spares`.
- Globals: `_fast_reconcile_flag`, `_fast_reconcile_lock`.

**Notable behaviors / risks:**
- **DB-only snapshot during prep/topup** can go stale if the worker crashes; watchdog mitigates but has a race window.
- **Split confirmation polling** can silently give up without exponential backoff or alerting.
- **Fee-coin-pool starvation** can deadlock if locked counts don't match reality.
- **Reserve auto-promotion** on disappearance can create "reserve" + "locked-in-offer" double-state.
- **Designation write window** between classify + persist is not atomic; FSM validator logs but does not block.

---

### `coin_prep_worker.py` (~285KB, ~5800 lines)

**Purpose:** Standalone subprocess that consolidates dust, creates optimised trading pools, and splits coins into tier-sized quantities in parallel. Runs asynchronously — bot keeps trading during prep.

**Key classes and functions:**
- `CoinPrepWorker` — state machine.
- `CoinPrepStatus` dataclass.
- `PrepPhase` enum (IDLE → ANALYZING → CONSOLIDATING → CREATING_POOL → SPLITTING → VERIFYING → COMPLETE/ERROR).
- `ApiMirrorStream` — wraps stdout/stderr to mirror logs to bot's HTTP API.
- `run_full_preparation()` / `create_pools_parallel()` / `split_coins_tiered()` / `create_and_split_tier_pools_sage()`.
- `verify_coins()`, `consolidate_wallet()`.
- `_get_coins_via_rpc()`, `_poll_for_coin_count()`, `_poll_for_confirmation()`, `_get_transaction_confirmation_state()`.
- `_designate_coins_from_snapshot()`, `_designate_new_tier_coins()`.
- `update_status()`, `log()`.

**Imports it depends on:** `wallet`, `tx_fees`, `coin_prep_utils`, `database`.

**Modules/callers that import it:** Not imported as a library — spawned by `coin_manager`.

**External services/APIs touched:** Sage/Chia wallet RPC (splits, coin queries, cancel/batch-cancel, get_transaction), SQLite, HTTP POST to `localhost:5000/api/log`.

**State it owns:** Config per instance (wallet type, IDs, sizes, headroom); `created_tx_ids`, `pool_coin_ids`; status machine (`current_phase`, progress, counts); `protected_offer_ids`; API log queue.

**Notable behaviors / risks:**
- **Grace-extension spiral** — up to 2 extensions +60s each; if Sage mempool stalls, prep aborts without clear retry escalation.
- **Parallel TX race** — 5 s stagger can collapse on retry, risking mempool collision.
- **CLI split stdout parsing** for large pools has no RPC fallback.
- **Consolidation stall** → silent prep abort.
- **Unconfirmed-spend replay** with main bot — no lock; relies on Sage's conflict detection.

---

### `coin_prep_utils.py` (~3.2KB, ~87 lines)

**Purpose:** Pure decision helpers for split confirmation tolerance (retry vs extend grace).

**Key classes and functions:**
- `should_retry_unconsumed_split(...)`.
- `should_extend_pending_consumed_split_grace(...)`.

**Imports / callers:** imported by `coin_prep_worker`.

**External services:** none.

**Risks:** Hardcoded 90% completion threshold; max retries fixed at 1 with no escalation.

---

### `coin_classifier.py` (~18KB, ~400 lines)

**Purpose:** Single source of truth for coin-to-tier classification. Deterministic, bounded answers to "can this coin back an offer in tier X?".

**Key classes and functions:** `CoinFit` enum, `CoinDesignation` enum, `CoinClassification` dataclass, `classify_coin(...)`, `is_misfit_coin(...)`, `infer_designation_by_size(...)`.

**Imports / callers:** imported by `coin_manager`, `coin_prep_worker`, `database`, `ladder_planner`, `offer_manager`.

**External services:** none.

**Risks:** Decimal precision at extreme tier sizes; ambiguous misfit sorting defaults to UNDER_FLOOR; reserve/dust coins report `is_misfit=False`, easy to miss.

---

### `coin_fsm.py` (~9KB, ~259 lines)

**Purpose:** Non-blocking FSM validator for `(status, designation)` transitions on the coins table. Logs violations.

**Key classes and functions:** `CoinState` frozen dataclass, `validate_transition(old, new)`, `is_terminal(state)`, `_expand()`.

**Imports / callers:** imported by `database` at coin-write sites; self-doctest.

**External services:** none.

**Risks:** Non-blocking — false sense of safety; reanimation (gone→free) can mask double-spend; spent is strictly terminal (manual DB edit needed on misclassification).

---

### `coin_reservations.py` (~11KB, ~294 lines)

**Purpose:** In-memory, TTL-based reservation registry for short-lived coin locks. Thread-safe.

**Key classes and functions:** `Reservation` dataclass, `ReservationRegistry.reserve / release / release_by_owner / is_reserved / is_reserved_by / filter_unreserved / gc_expired / stats`, `_normalise(coin_id)`.

**Imports / callers:** lazy-imported by `coin_manager`.

**External services:** none.

**Risks:** Lazy expiration — stale entries accumulate if `gc_expired` isn't called periodically; owner-string collisions are not enforced (risk of one owner releasing another's coins); contested coins silently skipped.

---

### `reservation_manager.py` (~12KB, ~307 lines)

**Purpose:** Persistent SQLite-backed **capacity** reservation (aggregate XCH/CAT in-flight), separate from coin_reservations.

**Key classes and functions:** `ReservationResult` dataclass, `init_reservation_table()`, `get_reservation_manager()` singleton, `ReservationManager.try_acquire / release / expire_stale / expire_all / get_reserved_totals / list_active / prune_old`.

**Imports / callers:** `api_server`, `bot_loop`, `offer_manager` (lazy).

**External services:** SQLite `reservation_leases` table.

**Risks:** Lease auto-expire on startup can race on multi-instance launches; advisory only (callers may ignore totals); table bloat requires manual `prune_old` (no scheduler).

---

## 3. Wallet Layer

### `wallet.py` (~5.4KB, ~175 lines)

**Purpose:** Adapter/dispatcher choosing Sage vs Chia backend by `WALLET_TYPE` env var. Re-exports public surface so callers don't care which backend is active.

**Key symbols:** `WALLET_TYPE`, `get_wallet_type()`, and conditional re-exports of all public wallet functions (`rpc`, `get_wallet_sync_status`, `get_spendable_coins`, `create_offer`, `cancel_offer`, `get_all_offers`, `send_transaction`, `send_transaction_multi`, `split_coins_rpc`, `split_coins_bulk`, etc.).

**Imports / callers:** Imported by every module that touches the wallet: `api_server`, `bot_health`, `bot_loop`, `coinset_client`, `coin_manager`, `coin_prep_worker`, `fill_tracker`, `offer_manager`, `sage_node`, `super_log_hooks`.

**External services:** none directly — passes through.

**Risks:** Unknown `WALLET_TYPE` values silently default to Sage; Chia backend has stubbed methods (e.g. `get_owned_coins_detailed`) that Sage expects — caller code must branch on backend.

---

### `wallet_chia.py` (~46KB, ~1240 lines)

**Purpose:** Official Chia wallet RPC client (port 9256) + full-node RPC (port 8555).

**Key functions:** Connection/RPC: `rpc`, `full_node_rpc`, `set_quiet_mode`. Health: `get_wallet_sync_status`, `get_full_node_sync_status`, `get_chia_health`, `get_blockchain_state_full`, `get_peer_connections`. Coins: `get_spendable_coins`, `count_suitable_coins`, `get_spendable_coins_rpc`, `get_all_coins_for_wallet`, `split_coins_rpc`, `split_coins_bulk`, `wait_for_coin_confirmations`, `get_transaction`. Balances: `get_wallet_balance`, `get_balances_parallel`, `get_wallets`, `get_next_address`. Transactions: `send_transaction`, `send_transaction_multi`. Offers: `create_offer`, `cancel_offer`, `is_offer_time_expired`, `get_offer_expiry_info`, `cleanup_expired_offers`, `get_all_offers`, `get_offer_bech32`, `classify_offers_from_list`, `classify_open_offers_for_pair`, `cancel_offers_batch`.

**Imports / callers:** `tx_fees`; imported only via `wallet.py`.

**External services:** Chia wallet RPC + full-node RPC over mutual TLS with localhost self-signed certs.

**Risks:**
- **SSL verify disabled** (`_TLS_VERIFY = False`, documented).
- `connect=0` in retry strategy — connection-refused not retried.
- `split_coins_bulk` fallback is very slow (60 s waits between batches).
- Coin-ID computation relies on fragile field names (`name`, `coin_id`, or manual recomputation).
- Expired offers not auto-transitioned — caller must run `cleanup_expired_offers()`.
- Batch cancel is serialised with adaptive delays — large batches take >10 s.
- `rpc()` returns `None` on error; callers must distinguish "down" vs "malformed".
- No bounds-check on fallback fee in `cancel_offer`.

---

### `wallet_sage.py` (~155KB, ~3858 lines)

**Purpose:** High-performance Sage (Rust) light-wallet client (port 9257). Drop-in API compatibility with `wallet_chia` while handling Sage's different format (string amounts, asset_id-based CAT queries, direct HTTPS with `http.client`, mutual TLS client auth).

**Key features and functions:**
- **Exceptions:** `SageMempoolConflict`, `SageUnknownUnspent`, `SageAlreadyIncluding`.
- **Initialization:** `ensure_initialized()` (45 s cooldown), `_sage_rpc_port_reachable()`, `sage_initialize()`, `sage_login()` (multi-step readiness → initialize → resync → login → verify).
- **RPC/TLS:** `_sage_post()` (thread-local HTTPSConnection reuse, auto-retry on stale conn), `rpc()`, `full_node_rpc()` (stub), `set_quiet_mode()`.
- **Cert management:** `_generate_self_signed_cert()` (2048-bit RSA, 10-year, 0o600).
- **Keys/signing:** `get_sage_keys`, `get_current_key`, `_require_signing_capability()` (blocks ops on watch-only wallets).
- **Health:** `get_wallet_sync_status`, `get_chia_health` (peer-count check).
- **CAT discovery:** `_get_cat_asset_id`, `notify_cat_asset_id_changed`, `_resolve_asset_id`, `_wallet_id_to_asset_id`.
- **Coin queries (with CRITICAL BUG WORKAROUND):** `get_spendable_coins_with_owned_fallback()` — Sage's `filter_mode="selectable"` incorrectly hides coins on BOTH sides of offers; this merges owned coins back in.
- **Coin ops:** `split_coins_rpc`, `create_transaction_rpc`, `sage_topup_split`, `combine_coins`, `split_coins_bulk`, `wait_for_coin_confirmations`, `get_transaction`.
- **Balances (CRITICAL FIX):** `get_wallet_balance()` — uses `selectable_balance` for spendable, owned count for total (includes offer-locked coins).
- **Wallet discovery:** `get_wallets()` (discovers CATs via `get_cats` RPC, updates `cfg.CAT_WALLET_ID`).
- **Address/send:** `get_next_address`, `_validate_address_for_active_network` (xch1/txch1 safety), `send_transaction` (CRITICAL: `auto_submit=True` required), `send_transaction_multi`, `send_cat_multi`.
- **Offers:** `create_offer` (normalises Sage response, extracts offer_id), `cancel_offer` (V5 FIX: treats 404 as success; handles `SageAlreadyIncluding`), `get_all_offers` (CRITICAL SAFETY: client-side filter for completed offers), `_normalize_sage_summary`, `cancel_offers_batch`, `delete_offer`/`delete_offers_batch` (Sage-only local DB deletion).
- **Dashboard:** `get_blockchain_state_full`, `get_peer_connections`, `get_transactions_list`, `get_transaction_count`, `get_all_coins_for_wallet`.
- **Auto-combine:** `auto_combine_xch`, `auto_combine_cat`.
- **Utilities:** `get_sage_version`, `view_offer`.

**Imports / callers:** `tx_fees`, optional `super_log`, `config`; imported via `wallet.py`.

**External services:** Sage RPC endpoints (`initialize`, `get_keys`, `resync`, `login`, `get_sync_status`, `get_coins`, `get_cats`, `split_xch`/`split_cat`, `combine_coins`, `get_transactions`, `send_xch`/`send_cat`/`multi_send`, `make_offer`, `cancel_offer`, `get_offers`, `get_offer`, `view_offer`, `set_change_address`, `delete_offer`/`delete_offers`, `get_puzzle_hashes`); mutual TLS required.

**State it owns:** Thread-local connection cache (`_conn_local`), init state (`_init_ok`, `_init_last_attempt`, `_init_lock`), CAT mapping (`_wallet_id_to_asset_id`), `_CAT_ASSET_ID`, puzzle-hash cache (10 min TTL).

**Risks:**
- **Sage selectable bug workaround** — band-aid over buggy filter; slipped-through locked coins cause offer rejection.
- **Mutual-TLS cert auto-generated with 10-year validity** — no rotation.
- **No exponential backoff in `rpc()` / `_sage_post()`** — failed RPCs retried once with no delay.
- **SSL verification disabled.**
- **Fingerprint mismatch** detected AFTER login succeeds; error swallowed if super_log missing.
- **CAT wallet_id dynamic assignment** — multiple CATs collide on `cfg.CAT_WALLET_ID` (single int).
- **`get_all_offers` client-side drops completed** — history-dependent code loses data.
- **Sage 404 == success on cancel** — ambiguity: we cancelled vs filled/expired externally.

---

## 4. Price, Market Data, AMM / Mempool Watchers

### `price_engine.py` (~43KB, ~944 lines)

**Purpose:** Unified price discovery combining Dexie + TibetSwap with multi-strategy blending, dynamic price guards, safety rails, and volatility tracking.

**Key functions:** `PriceEngine.get_price`, `get_volatility`, `get_tibet_pool_info`, `get_tibet_quote`, `get_pool_depth_ratio`, `get_dynamic_limits`, `get_reference_price`, `_apply_safety_guards`, `_fetch_dexie_price`, `_fetch_tibet_price`, `_get_tibet_pairs` (120s cache), `get_last_price`, `get_live_amm_price`, `invalidate_tibet_cache`, `_decimal_sqrt`.

**Imports / callers:** depends on `config`, `database`; consumed by `bot_loop`, `risk_manager`, `amm_monitor`, `super_log_hooks`.

**External services:** Dexie `/v2/prices/tickers`; TibetSwap `/pairs` and `/quote`.

**State:** `_tibet_cache` (thread-safe, injected by AMMMonitor); `_reference_price` (EMA); `_last_mid_price`, `_last_dexie_price`, `_last_tibet_price`; `_last_rail_breach*`; counters; `_price_lock`; cooldown timers.

**Risks:** Dexie crossed-book fallback to stale average; EMA nudging could be slowly manipulated by sustained extreme prices; cache injection race under `_tibet_lock`; step-change guard uses last price only (no wall-clock check); hardcoded 0.993 fee multiplier is TibetSwap-specific.

---

### `dexie_manager.py` (~30KB, ~719 lines)

**Purpose:** Manages Dexie offer posting (queue, post with retries, fingerprint dedupe, trade_id→dexie_offer_id mapping) and v3 trade/pair data for volatility and pair intelligence.

**Key functions:** `DexieManager.queue_post`, `flush_queue` (parallel ≥10 batch), `_post_single`, `purge_trade_ids`, `repost_active_offers`, `get_dexie_id`, `get_dexie_link`, `get_stats`, `fetch_v3_historical_trades` (5 min cache), `compute_v3_trade_metrics`, `fetch_v3_pairs` (10 min cache), `get_v3_pair_stats`, `prune_mappings`, `_fingerprint`.

**Imports / callers:** `config`, `database`; consumed by `bot_loop`, `bot_health`, `fill_tracker`, `super_log_hooks`.

**External services:** Dexie `/v1/offers` POST/GET; `/v3/prices/historical_trades`; `/v3/prices/pairs`.

**State:** `_queue`, `_posted_fingerprints` (set capped 400), `_trade_dexie_map`, `_rate_limited_until`, counters, `_v3_trades_cache`, `_v3_pairs_cache`, module-level `_offer_detail_cache` (15s).

**Risks:** `force=True` bypasses fingerprint dedupe; 4xx treated as permanent (may drop transient 400s silently); 8-worker pool for parallel posting is uncontrolled; long v3 cache TTLs; trade_dexie_map hydration happens in `bot_loop`, not `__init__` — visibility gap at startup.

---

### `market_data_collector.py` (~72KB, ~2000+ lines)

**Purpose:** Aggregates 6 data sources (Dexie trades, Dexie ticker, TibetSwap pool, TibetSwap quote, Spacescan, internal DB) in parallel for Smart Defaults v2.

**Key functions:** `collect_all_market_data`, `analyze_market_data`, `_fetch_dexie_trades`, `_fetch_dexie_ticker`, `_fetch_tibet_pool`, `_fetch_tibet_quote`, `_fetch_spacescan_token_info`, `_fetch_internal_db_metrics`, `_compute_volatility`, `_compute_liquidity_rating`, `_compute_fill_rate_context`, `_recommend_spread`.

**Imports / callers:** `config`, `database`, `price_engine`, `dexie_manager`, `spacescan`; consumed by `api_server` (Smart Defaults endpoint).

**External services:** Dexie v2/v3, TibetSwap `/pairs` + `/quote`, CoinGecko (optional), Spacescan (1440-min cache, rate-limited).

**State:** session, analysis cache (30 min), per-source caches.

**Risks:** Falls back to stale cached data without age-bounds on API failure; no circuit breaker on cascading failures; liquidity thresholds hardcoded; volatility window mismatch across sources; optional services degrade silently.

---

### `market_intel.py` (~26KB, ~603 lines)

**Purpose:** Live competitive intelligence from Dexie orderbook: competitor spread, depth, whale detection, DBX eligibility.

**Key functions:** `MarketIntel.refresh_orderbook`, `_parse_dexie_offer`, `_analyse_orderbook`, `get_competitor_spread`, `get_spread_recommendation`, `check_dbx_eligibility`, `get_market_summary`, `get_orderbook_snapshot`, `get_stats`, `reset_session_stats`.

**Imports / callers:** `config`, `database`; consumed by `bot_loop`, `risk_manager`.

**External services:** Dexie `/v1/offers` GET (paginated).

**State:** `_orderbook`, `_competitors`, `_dbx`, `_known_dexie_ids`, threading lock.

**Risks:** Pagination caps depth visibility (page_size default 200); own-offer identification fragile when `cfg.BOT_TAG` blank; crossed-book suppresses recommendations; hardcoded DBX threshold (500 bps) and whale threshold (1 XCH).

---

### `spacescan.py` (~29KB, ~685 lines)

**Purpose:** On-chain verification "golden source" — coin spend detection, balances, fill verification via `offer_info` + child coins.

**Key functions:** `is_coin_spent`, `verify_fill`, `is_known_wallet_address`, `get_xch_balance`, `get_token_balance`, `check_balance_discrepancy`, `record_external_call`, `get_api_stats`, `should_check_balance`, `is_pro_tier`, `_spacescan_get` (tier-aware rate limiting), `_get_known_wallet_addresses` (log parsing).

**Imports / callers:** `config`, `database`; consumed by `bot_loop`, `coin_manager`, `fill_tracker`, `api_server`, `market_data_collector`.

**External services:** Spacescan Pro (`pro-api.spacescan.io`) + Free (`api.spacescan.io`).

**State:** Module-level rate limit state, daily budget tracking, wallet-address cache.

**Risks:** Free-tier 30 calls/month — a single balance check can exhaust fill-verification budget; "coin not found" detection uses undocumented shape; fill-verify priority assumes stable Spacescan schema; wallet-address extraction via regex on logs is fragile (log-format drift or injection); baseline calibration not wallet-address-aware.

---

### `amm_monitor.py` (~22KB, ~510 lines)

**Purpose:** Background TibetSwap reserve polling with drift detection + cache invalidation, so the bot requotes fresh within ~60 s on AMM drift.

**Key functions:** `AMMMonitor.start/stop`, `get_amm_price`, `get_amm_state`, `is_available`, `notify_quoted_price`, `get_drift_bps`, `get_arb_pressure`, `get_arb_pressure_label`, `check_amm_buffer`, `get_stats`, `_poll_loop`, `_do_poll`, `_fetch_pair`, `_compute_drift_vs_old_state`, `_inject_into_tibet_cache`.

**Imports / callers:** `config`, `database`, `price_engine`; consumed by `bot_loop`.

**External services:** TibetSwap `/pairs`.

**State:** Cached AMM state, last quoted buy/sell, thread, health counters, reused session.

**Risks:** Drift computed vs `last_quoted` prices (already spread), causing false alarms in wide-spread regimes; cache invalidation silent; failure tolerance low (3 consecutive); arb-pressure scales incomparable if config changes; cache injection assumes stable `pair_id`.

---

### `mempool_watcher.py` (~23KB, ~568 lines)

**Purpose:** Two background pollers (5 s each): TibetSwap reserves → `price_move` signals; Coinset mempool → `imminent_swap` + `fill_imminent` signals, firing 10–18 s before block confirmation.

**Key functions:** `MempoolWatcher.start/stop`, `get_pending_signals`, `get_current_reserves`, `set_watched_offer_coins`, `_reserve_poll_loop`, `_fetch_and_update_reserves`, `_emit_price_move_signal`, `_mempool_poll_loop`, `_check_mempool_for_pool_spend`, `compute_coin_id`, `_encode_amount`; singletons `get_or_create_watcher`, `start_watcher`, `stop_watcher`.

**Imports / callers:** `config`, `database`; consumed by `bot_loop`, `api_server`.

**External services:** TibetSwap `/pairs` every 5 s; Coinset `get_all_mempool_items`.

**State:** signal list, pool state, watched offer-coin set, debounce dicts, thread handles.

**Risks:** TTL-based fill debounce can re-fire after 300 s; no backoff on Coinset outages; exact-equality reserve-delta detection (filtered by min magnitude); custom coin-ID encoding assumes canonical form; normalization mismatch between `_watched_offer_coins` (normalized) vs mempool IDs (raw).

---

### `dynamic_amm_buffer.py` (~6.8KB, ~189 lines)

**Purpose:** Rolling-window sweep tracker that widens the AMM buffer multiplier (1.0× → 2.5×) when sweeps are frequent.

**Key functions:** `DynamicAMMBuffer.record_sweep`, `get_effective_buffer_bps`, `sweep_count_in_window`, `get_state`, `_get_multiplier`, `_prune_locked`; module-level convenience wrappers + singleton.

**Imports / callers:** `config`; consumed by `bot_loop`, `amm_monitor`.

**External services:** none.

**State:** deque of `(monotonic_time, fill_count)`; singleton instance; threading lock.

**Risks:** Coarse hardcoded multiplier table; ignores sweep magnitude (fill_count unused for multiplier); 2.5× cap insufficient on thin pairs; no "excessive sweep" operator alert.

---

## 5. Risk, Health, Strategy

### `risk_manager.py` (~54KB, ~1187 lines)

**Purpose:** Inventory-aware spread adjustment + circuit breakers + market-health grading.

**Key functions:** `RiskManager.update_inventory`, `reset_position`, `reset_session`, `record_snapshot`, `get_adjusted_spread`, `_apply_inventory_skew`, `_apply_volatility_scaling`, `_apply_pool_depth_adjustment`, `_apply_competitor_adjustment`, `check_circuit_breakers`, `_check_position_limit` (soft 1.0× / hard 1.5×), `_check_price_limits`, `_trip_circuit_breaker`, `_clear_circuit_breaker`, `circuit_breaker_active`, `get_circuit_breaker_blocked_side`, `is_full_halt`, `trip_price_rail_breach`, `should_enable_side`, `get_market_health`.

**Imports / callers:** `config`, `database`; conditionally `bot_loop`, `dexie_manager`; consumed by `bot_loop`, `super_log_hooks`, `boost_manager`, tests.

**External services:** DB snapshots; TibetSwap/Dexie via `price_engine` and `market_intel`.

**State:** net position, CB state, startup baseline, cached volatility, metrics, `_cb_lock`, `_soft_position_warned`.

**Risks:** Position-limit inheritance race at startup; hardcoded hysteresis threshold (3); soft-warn never auto-resets; CB escalation never downgrades; volatility fallback to stale cache can tighten spreads adversely.

---

### `boost_manager.py` (~58KB, ~1297 lines)

**Purpose:** Adaptive gap-closer that probes tighter spreads, detects floor, and physically cascades the main book behind the proven price.

**Key functions:** `BoostManager.activate/deactivate`, `step_tighter`, `refresh_if_needed`, `prune_active_boosts`, `_on_arbed`, `consume_inner_vulnerability_flag`, `_create_gap_closer_pair`, `_create_single_offer`, `_handoff_to_inner_tier`, `update_convergence`, `get_convergence_factor`, `should_cascade`, `cascade_main_book`, `_find_stale_offers`, `get_state`.

**Imports / callers:** `config`, `database`, `offer_manager`, optionally `risk_manager`, `dexie_manager`; consumed by `bot_loop`, `runtime_monitor`, `super_log_hooks`.

**External services:** wallet (via offer_manager), Dexie/Splash.

**State:** active flag, boost IDs, spread tracking, step/arb counts, expiry dict, convergence factor, cascade state, vulnerability flag, RLock.

**Risks:** Cascade can starve if ladder creation short-returns; arb detection uses implicit 5 s expiry window; convergence reset depends on `deactivate` kwargs; cascade off-by-one on batch boundaries; CB check asymmetry between `step_tighter` and `refresh_if_needed`.

---

### `runtime_monitor.py` (~40KB, ~915 lines)

**Purpose:** Sidecar watchdog polling DB events, wallet/DB/Dexie alignment, and superlog slow-call samples.

**Key functions:** `RuntimeMonitor.start/stop`, `reset_session`, `get_state`, `_run`, `_bootstrap_recent_events`, `_ingest_new_events`, `_handle_event`, `_ingest_superlog`, `_collect_snapshot`, `_evaluate`, `_apply_condition`, `_condition_entry`, `_summarize_status`, `_sync_alert`.

**Imports / callers:** `config`, `database`, `event_taxonomy`, optionally `super_log`; consumed by `bot_loop`.

**External services:** DB, in-process managers.

**State:** condition flags, streaks, last-logged timestamps, slow-sample deques, recent actions/findings, topup baselines, Dexie grace window, superlog offsets, snapshot.

**Risks:** Streak/condition interaction can orphan warnings; fragile superlog regex; monotonic vs wall-clock mix on re-fire timing; Dexie grace clock-skew sensitivity; topup lag requires monotonic improvement.

---

### `bot_health.py` (~22KB, ~582 lines)

**Purpose:** Legacy health-check utilities referenced by `api_server`; largely superseded by RuntimeMonitor.

**Key functions:** `run_runtime_checks` (if present), plus helpers for wallet sync meta, offer accounting, coin status, performance metrics.

**Imports / callers:** `config`, `database`, `offer_manager` (conditional); consumed by `api_server`, optionally `bot_loop`.

**Risks:** Deprecated/dual-path; no locking on cross-module reads.

---

### `reaction_strategy.py` (~1.3KB payload, ~150 lines)

**Purpose:** Graduated requote severity framework + per-cycle action budget.

**Key symbols:** `RequoteSeverity` enum, `CycleBudget` class, `can_cancel/can_create/use_cancel/use_create`, `remaining_*`, `exhausted`, `compute_offer_staleness`, `classify_drift`, `tiers_for_severity`, `filter_offers_by_tiers`.

**Imports / callers:** `config`; consumed by `bot_loop`, potentially `offer_manager`.

**Risks:** All-or-nothing budget exhaustion; divide-by-zero on ideal_price ~0; severity thresholds only overridable via code edit.

---

### `ladder_planner.py` (~6.5KB, ~317 lines)

**Purpose:** Pre-flight deterministic ladder plan (slot-by-slot coin allocation) with gap/viability detection.

**Key symbols:** `SlotStatus` enum, `SlotPlan`, `LadderPlan` (`ready_count`, `oversize_count`, `is_viable`, `summary`), `plan_ladder`, `amount_fmt`.

**Imports / callers:** `coin_classifier`, `config`; consumed by `offer_manager`, potentially `bot_loop`.

**Risks:** First-fit greedy allocation (suboptimal); oversize-acceptable coins not re-checked against wallet constraints; no re-ranking after initial classification.

---

### `ladder_watchdog.py` (~11KB, ~461 lines)

**Purpose:** Cycle-level self-audit of ladder shape + coin-accounting invariants.

**Key symbols:** `Severity` enum, `Issue`, `AuditResult`, `audit_ladder_shape`, `check_coin_invariants`, `run_periodic_audit`.

**Imports / callers:** Stateless; consumed by `bot_loop`, tests.

**Risks:** Hardcoded 5% size tolerance; median-based inversion check robust but imperfect; coin-accounting tolerance ±2 (hides small drifts); audit does not trigger action.

---

### `shape_fix_orchestrator.py` (~31KB, ~794 lines)

**Purpose:** User-triggered multi-stage recovery flow (cancel → confirm → re-check coins → rebuild) with SSE status streaming.

**Key symbols:** `Stage` / `HaltReason` enums, `FlowState` (+ `to_dict`), `ShapeFixOrchestrator.start_flow/abort_flow/is_running/current_flow/_run_flow`, stage methods, `_emit`, `_finalise`.

**Imports / callers:** `config`, `database`, optionally `super_log`; consumed by `bot_loop`, `api_server`.

**External services:** wallet via `offer_manager.cancel_offers()`, DB, SSE.

**Risks:** Confirmation polling rigid (2 s interval, 180 s timeout); abort only honoured at checkpoints; P2 rebuild halt can misfire on edge-case timing; one flow per side globally; no rollback on `NO_TIER_COINS_POSSIBLE` (cancelled offers stay cancelled).

---

### `sweep_coordinator.py` (~6KB, ~298 lines)

**Purpose:** Groups fills by `spent_block_index`, finalises after a window, upgrades UNKNOWN fills to `DEXIE_COMBINED` if 3+ share the block.

**Key symbols:** `SweepEntry`, `SweepEvent` (+ properties), `SweepCoordinator.process_fill/tick/drain_sweep_events/get_pending_summary/_expire_pending_locked/_finalise_group_locked/_upgrade_unknown_fills_locked`; module-level singleton.

**Imports / callers:** `config`, `database`, `fill_classifier`; consumed by `fill_tracker`, tests.

**Risks:** Hard-deadline window (splits across blocks lose grouping); unvalidated `SWEEP_MIN_FILLS`; DB-error silencing on classification upgrade; deque cap of 200 can FIFO-drop if consumer lags.

---

### `sniper.py` (~20KB, ~487 lines)

**Purpose:** Fast arb-closer — single probes on best bid/ask triggered by TibetSwap drift, posting to Dexie within ~3 s.

**Key functions:** `Sniper.try_snipe`, `try_snipe_single`, `_calculate_snipe_size`, `_publish_immediately`, `_should_snipe_side`, `_create_snipe_offer`, `prune_active_snipes`, `get_stats`.

**Imports / callers:** `config`, `database`, `offer_manager`, optionally `risk_manager`, `dexie_manager`, `splash_manager`; consumed by `bot_loop`, `super_log_hooks`, tests.

**External services:** wallet via `offer_manager`, Dexie/Splash, DB.

**Risks:** Per-side cap TOCTOU (read under lock but not re-checked at creation); cooldown is global but checked per-side (one side blocks the other); CB semantics slightly inconsistent vs main loop; zero/falsy `SNIPER_EXPIRY_SECS` → infinite-life offers; DB insert failure + failed compensating cancel can leave orphan on-chain offers.

---

## 6. Config / Infrastructure / Database / Logging

### `config.py` (~60KB, ~1117 lines)

**Purpose:** Centralised typed config with hot-reload.

**Key symbols:** `Config` class (`reload`, `update`, `validate`, `to_dict`, `get_spread_fraction`, `get_requote_fraction`), `get_buy_tier_size_xch`, `get_sell_tier_size_xch`, `get_tier_sizes_for_side`, `has_per_side_tier_sizes`, module singleton `cfg`.

**Imports / callers:** `user_paths`, `config_validator`, `user_secrets`; consumed by nearly every module.

**External services:** Filesystem (.env), env vars, downstream URLs (validated only).

**State:** `cfg` singleton, RLock, validation report cache, per-side tier sizes (F62).

**Risks:** Hot-reload under lock — transient inconsistency window; GUI vs legacy key precedence; type-coerce silent defaults; secrets injection via `apply_to_config`; tier-size fallback chain.

---

### `config_validator.py` (~14KB, ~314 lines)

**Purpose:** Structured config validation before trading starts.

**Key symbols:** `ConfigIssue` dataclass, `ValidationReport`, `validate_config(cfg)`, `_is_valid_url(url)`.

**Imports / callers:** None direct; consumed by `config`, `api_server`, `doctor`.

**Risks:** No auto-fix; tier-size vs max-trade check only when tiers enabled; dynamic vs hard price-limit overlap checked but all-disabled case missed; no async/URL-ping validation; bad URLs are warnings, not errors.

---

### `database.py` (~178KB, ~3461 lines)

**Purpose:** Single source of truth for all trading state via SQLite + WAL.

**Key function families:**
- **Connection:** `get_connection`, `close_connection`, `init_database`.
- **Offers:** `add_offer`, `update_offer_status`, `update_offer_coin_id`, `batch_cancel_stale_offers`, `recover_unknown_offers`, `get_open_offers`, `get_offer`, `get_offers_by_trade_ids`, `get_trade_dexie_map`, `update_offer_dexie`, `update_offer_bech32`, `update_offer_lifecycle_state`, `get_offers_for_repost`, `transition_offer`, `get_lifecycle_observability_stats`.
- **Coins:** `upsert_coin`, `batch_upsert_coins`, `lock_coin`, `free_coin`, `mark_coin_spent`, `mark_coins_gone`, `get_free_coins`, `get_locked_coins`, `get_all_coins_state`, `reconcile_coins_with_wallet`, `set_coin_designation`, `get_coins_by_designation`, `get_reserve_coins`, `designate_reserve`, `cleanup_orphaned_locked_coins`.
- **Fills:** `record_fill`, `match_round_trip`, `get_fills`, `get_unmatched_fills`, `backfill_verified_fills_from_offers`.
- **Inventory / price:** `record_inventory_snapshot`, `get_net_position`, `record_price`, `get_recent_prices`.
- **Events:** `log_event`, `get_recent_events`, `get_events_since`.
- **Utility:** `norm_coin_id`, `_now`, `_sqlite_ts`, `coin_sanity_check`, `get_coin_summary`, `get_live_tier_group_counts`.

**Tables + owners:**
- **offers** — writers: `offer_manager`, `fill_tracker`; readers: `offer_manager`, `fill_tracker`, `api_server`, `dexie_manager`, `risk_manager`.
- **fills** — writer: `fill_tracker`; readers: `fill_tracker`, `offer_manager`, `api_server`.
- **coins** — writer: `coin_manager`; readers: `coin_manager`, `offer_manager`, `coin_prep_worker`.
- **inventory** — writer: `risk_manager`; readers: `risk_manager`, `api_server`.
- **price_history** — writer: `price_engine`; readers: `price_engine`, `api_server`.
- **events** — writers: all modules; readers: `api_server` (SSE), `doctor`.
- **config_history** — writer: `config.update()`; readers: `doctor`, `api_server`.
- **bot_settings** — k/v settings (`get_setting`, `set_setting`).
- **splash_incoming_offers** — writer: `splash_receive`; readers: `sniper`, `dexie_manager`.
- **pool_snapshots** — writer: `market_data_collector`; reader: Smart Defaults.
- **market_analysis_cache** — writer: `market_data_collector`; reader: `api_server`.
- **reservation_leases** — owner: `reservation_manager`.

**Imports / callers:** `user_paths`, optional `super_log`; consumed by most modules.

**External services:** SQLite at `user_paths.database_file()` (WAL + SHM sidecars); file permissions 0o600 on first create.

**State:** Thread-local `sqlite3.Connection`, `_db_initialized_path` guard, schema-version tracking.

**Risks:**
- All queries parameterised — **no visible SQL injection surface**.
- Additive migrations via `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE`; destructive ones use temp-table swap.
- Per-thread conn; must not cross-thread.
- Coin-ID normalisation must be used consistently everywhere.
- Price history grows unbounded (no TTL/prune).
- Cascading constraints/triggers minimal — partial failure can leave `offers`/`coins` misaligned.

---

### `super_log.py` (~36KB, ~933 lines)

**Purpose:** Levelled logging with ring-buffer context for error dumps; ~95 % reduction in disk volume while preserving crash forensics.

**Key functions:** Core: `slog`, `init_super_log`, `set_file_level`, `set_terminal_level`. Cycle: `start_cycle`, `cycle_count`, `cycle_note`, `end_cycle`. Thread: `log_thread_start`, `log_thread_stop`. Error: `_dump_error_context`. SQL trace: `make_sql_trace_callback`, `trace_connection`. Archive: `_archive_log_digest`, `get_archive_summary`, `_prune_archive`. Rotation: `_rotate_if_needed`, `_cleanup_old_logs`, `periodic_maintenance`. Utility: `timed`, `intercept_log_event`, `get_log_stats`, `get_log_path`, `close_super_log`.

**Imports / callers:** Lazy imports of `config`, `database`; consumed by most modules + `super_log_hooks`.

**External services:** Filesystem (logs + `superlog_archive.jsonl`), stdout/stderr redirection.

**State:** Global file handle, path, initialized flag, `_ring_buffer` (deque 500), byte counter, error-dump rate limiter, thread-local cycle stats, file/terminal levels.

**Risks:** Ring buffer append/iterate race during error dump; no automatic secret scrubbing of `data` dict; error-dump cap of 10 per session; size-based rotation only (long idle runs may grow a single file); archive-digest regex-based parsing (fragile); `_TeeWriter` only captures `print()`/stdio-level writes.

---

### `super_log_hooks.py` (~13KB, ~319 lines)

**Purpose:** Install ~60+ method/function wrappers with 3-tier level escalation (TRACE → INFO>500 ms → WARN>2.5 s → ERROR).

**Key functions:** `_wrap_method`, `_wrap_function`, `install_all_hooks()`. Constants: `SLOW_METHOD_MS`, `SLOW_NETWORK_MS`, `SLOW_WALLET_MS`.

**Imports / callers:** `super_log`; consumed by `api_server` at startup.

**Risks:** No undo path (tests must mock before install); arg-logging can leak large objects; lock overhead on hot paths; slow-threshold inconsistency if a method is renamed and loses its custom ms.

---

### `event_taxonomy.py` (~27KB, ~545 lines)

**Purpose:** Canonical mapping of ~168 event types to 8 categories.

**Key symbols:** `EventCategory` StrEnum, `_EVENT_CATEGORY_MAP`, `categorize_event`, `get_category_events`.

**Imports / callers:** None direct; consumed by `api_server`, `doctor`.

**Risks:** No lint for orphan event types; stale map inevitable as features evolve; single-level mapping (no families); `StrEnum` fallback across Python versions.

---

### `user_paths.py` (~9.4KB, ~261 lines)

**Purpose:** Cross-platform per-user data directory layout + first-launch migration from install dir.

**Key functions:** `_install_dir`, `_default_data_dir`, `data_dir`, `install_dir`, `env_file`, `database_file`, `window_state_file`, `crash_log_file`, `worker_cancelled_ids_file`, `protected_offers_file`, `log_dir`, `backups_dir`, `migrate_legacy_files`.

**Imports / callers:** None direct; consumed by `config`, `database`, `super_log`, `user_secrets`.

**Risks:** Migration runs at import time with silent fallback on failure; `CMM_DATA_DIR` override unvalidated; read-only install falls back to install dir silently; legacy files never deleted after migration.

---

### `user_secrets.py` (~3.0KB, ~90 lines)

**Purpose:** Secrets storage (JSON) outside the repo, in the OS app-data dir.

**Key functions:** `_secrets_path`, `_load_locked`, `_save_locked`, `get_secret`, `set_secret`, `apply_to_config(cfg)`.

**Imports / callers:** None direct; consumed by `config`, `api_server`.

**External services:** Filesystem (`user_secrets.json` at platform-specific path, chmod 0o600 on Unix).

**Risks:** Plaintext JSON — on Windows, 0o600 has no effect (NTFS ACLs not set); no rotation; only SPACESCAN_API_KEY hard-wired; no validation of secret format.

---

### `notification_manager.py` (~5.2KB, ~168 lines)

**Purpose:** Native OS notifications via plyer with per-category rate limiting.

**Key symbols:** `NotificationManager` class, `DEFAULT_CATEGORIES`.

**Imports / callers:** plyer (optional); consumed by `api_server`, `offer_manager`, `risk_manager`, `coin_manager`.

**External services:** `plyer.notification.notify` → OS native toast/libnotify.

**Risks:** No delivery confirmation; rate limiting per-instance (not global); unbounded thread spawning; title-prefix concatenation relies on caller hygiene.

---

## 7. UI — Server, Bridge, Desktop Window, Tray, HTML

### `desktop_app.py` (~39KB, ~1071 lines)

**Purpose:** PyWebView entry point. Starts Flask in a background thread, creates a native window, wires system tray + notifications, handles graceful shutdown + window-state persistence.

**Key functions:** `main(argv)`, `run_desktop_mode(dev_mode)`, `run_flask_mode()`, `start_flask_server`, `check_port_free`, `wait_for_flask`, `_respawn_under_pythonw`, `_hide_windows_console`, `_apply_window_icon_win32`, `_set_windows_app_user_model_id`, `_show_fatal_error_dialog`, `_load_window_state`, `_save_window_state`, `_show_window`, `_cleanup`, `_poll_tray_status`, `_wire_notifications`, `_tray_graceful_quit`, `on_closing`.

**Imports / callers:** `api_server`, `app_bridge`, `tray_manager`, `notification_manager`, `database`, `user_paths`.

**External services:** PyWebView, pystray, plyer, urllib (tray API calls), ctypes (Win32).

**State:** Global `_state` dict (window/tray/notifier refs, confirmed_close flag); Flask app in a separate thread; window geometry file; version/constants.

**Risks:**
- Windows respawn-under-pythonw: failure silent if first respawn attempt errors.
- Custom titlebar + Alt+F4 intercept relies on JS bridge; hard-close fallback if bridge fails.
- Tray token pulled from `BOT_LOCAL_WRITE_TOKEN` env var (set at api_server import).
- Notification wiring in main thread — exceptions caught but non-fatal.
- Window-state fallback path used on read-only installs (state not persisted).
- UTF-8 reconfigure at module load may fail when stdout is captured (pytest).

---

### `app_bridge.py` (~51KB, ~1235 lines)

**Purpose:** JS ↔ Python bridge for PyWebView desktop mode. Exposes ~82 methods via `window.pywebview.api.*`; each method calls Flask handlers in-process via `test_request_context()`.

**Method groups:**
- **Bot control:** `start_bot`, `stop_bot`, `get_bot_state`, `get_status`, `get_price`, `shutdown`.
- **Config:** `get_config`, `update_config`, `live_config`, `reload_config`, `apply_config`, `validate_config`, `get_fees_status`.
- **Settings:** `get_settings_defaults`, `validate_settings`, `get_smart_defaults`.
- **Dashboard:** `get_dashboard`, `get_inventory`, `get_risk_spreads`, `get_stats`.
- **Offers:** `get_offers`, `cancel_all_offers`, `get_cancel_all_status`, `cancel_offer`, `cleanup_orphans`, `get_offers_diagnostic`.
- **Fills / PnL:** `get_fills`, `purge_fills`, `export_fills`, `get_pnl`.
- **Session:** `fresh_start`, `check_resume`.
- **Coins:** `get_coins`, `trigger_topup`, `get_coin_prep_status`, `trigger_coin_prep`, `reset_coin_prep`, `verify_coin_prep`.
- **Boost:** `get_boost_state`, `activate_boost`, `deactivate_boost`.
- **Market intel:** `get_market_intel`, `get_market_summary`, `get_market_slippage`, `get_market_orderbook`.
- **Alerts:** `get_alerts`, `dismiss_alert`.
- **Logs:** `get_logs`, `clear_logs`, `download_logs`.
- **Health / doctor:** `get_health`, `run_doctor`, `get_reservations`, `get_runtime_diagnostics`.
- **Wallet / CAT:** `get_fingerprint`, `get_cats`, `select_cat`, `refresh_cat`, `refresh_balances`, `is_sage_running`, `begin_startup`, `get_startup_status`, `get_fingerprints`, `start_with_fingerprint`, `setup_certs`, `restart_sage`.
- **Dexie / price:** `get_price_info`, `get_dexie_stats`, `repost_dexie`.
- **Splash:** `get_splash_stats`, `get_splash_node`, `check_splash_setup`, `download_splash_setup`, `get_splash_setup_progress`, `start_splash_node`, `get_splash_receive`, `set_splash_receive`.
- **Spacescan:** `get_spacescan_status`, `setup_spacescan`.
- **Console:** `get_console_status`, `toggle_console`.
- **App info / window:** `get_app_info`, `confirm_close_window`, `minimize_window`, `maximize_window`, `resize_window`, `get_window_size`, `move_window`, `get_window_pos`, `close_window`, `open_external`.

Helpers: `_safe` (decorator), `DecimalEncoder`, `_unwrap_flask_response`.

**Imports / callers:** `api_server`, `webview`, `desktop_app`.

**External services:** Flask test_request_context, PyWebView JSON serialisation, subprocess (open_external).

**State:** lazy `_api` reference; no persistent state.

**Risks:**
- **Bridge bypasses `before_request` hooks** → 82+ methods are unauthenticated from the JS runtime. Mitigated only by escapeHtml + GUI XSS discipline.
- Decimal precision preserved as string (JS-side must not convert to number).
- `_safe` masks all exceptions to generic error; full tracebacks to logs only.
- Excess-arg trimming tied to signature at decoration time.
- Window close gate depends on bot.stop() completing before bridge method returns.

---

### `api_server.py` (~573KB, ~12517 lines)

**Purpose:** Flask HTTP/SSE server providing the REST surface used by `bot_gui.html` and `app_bridge`. ~120 endpoints + SSE stream.

**Route groups (representative, not exhaustive):**
- **Static / health:** `GET /`, `GET /console`, `GET /favicon.ico`, `GET /brand/<path>`, `GET /assets/<path>`, `GET /api/health`, `GET /api/health/runtime`, `GET /api/doctor`, `GET /api/events` (SSE), `GET /api/self-test`.
- **Bot control:** `POST /api/bot/start`, `POST /api/bot/stop`, `GET /api/bot/state`, `GET /api/status`, `POST /api/shutdown`, `GET /api/bot/price`.
- **Config / settings:** `GET/POST /api/config`, `POST /api/config/live`, `POST /api/config/reload`, `POST /api/config/apply`, `GET /api/config/validate`, `GET /api/config/history`, `GET /api/config/export-env`, `GET /api/settings/defaults`, `POST /api/settings/validate`, `GET /api/smart-defaults`, `GET /api/fees/status`.
- **Offers / fills:** `GET /api/offers`, `GET /api/offers/open_count`, `POST /api/offers/cancel_all`, `GET /api/offers/cancel_all/status`, `POST /api/offers/cancel`, `POST /api/offers/cleanup_orphans`, `GET /api/offers/diagnostic`, `GET /api/fills`, `GET /api/fills/classified`, `GET /api/fills/arb-wallets`, `POST /api/fills/purge`, `GET /api/fills/export`, `GET /api/market/fill-intel`.
- **Dashboard / inventory / pnl:** `GET /api/dashboard`, `GET /api/inventory`, `GET /api/stats`, `GET /api/pnl`, `GET /api/risk/spreads`.
- **Coins / wallet:** `GET /api/coins`, `POST /api/coins/topup`, `POST /api/coins/prep`, `GET /api/fingerprint`, `POST /api/balances/refresh`, `GET /api/cats`, `POST /api/cat/select`, `POST /api/cat/refresh`, `GET /api/wallets/detect`, `POST /api/wallets/switch`.
- **Coin prep:** `GET /api/coin-prep/status`, `GET /api/coin-prep/verify`, `POST /api/coin-prep/trigger`, `POST /api/coin-prep/reset`.
- **Boost:** `GET /api/boost/state`, `POST /api/boost/activate`, `POST /api/boost/deactivate`.
- **Market intel / price:** `GET /api/market/intel`, `GET /api/market/orderbook`, `GET /api/market/slippage`, `GET /api/market/summary`, `GET /api/market/dbx`, `GET /api/price`, `GET /api/price/tibet`, `GET /api/amm/price`, `GET /api/dexie/stats`, `POST /api/dexie/repost`, `GET /api/dexie/v3-pairs`, `GET /api/coinset/stats`.
- **Alerts:** `GET /api/alerts`, `POST /api/alerts/dismiss`.
- **Splash:** `GET /api/splash/stats`, `GET /api/splash/node`, `POST /api/splash/node/start`, `GET /api/splash/node/output`, `GET /api/splash/setup/check`, `POST /api/splash/setup/download`, `GET /api/splash/setup/progress`, `GET /api/splash/setup/release`, `GET /api/splash/receive`, `POST /api/splash/receive`, `POST /api/splash/incoming` (rate-limited 200/s, token-exempt), `GET /api/splash/incoming/list`.
- **Spacescan:** `GET /api/spacescan/status`, `POST /api/spacescan/setup`.
- **Sage wallet lifecycle:** `GET /api/wallet/sage-running`, `POST /api/wallet/begin-startup`, `GET /api/sage/startup-status`, `GET /api/sage/fingerprints`, `POST /api/sage/start-with-fingerprint`, `POST /api/sage/setup-certs`, `GET /api/sage/latest-release`.
- **Session:** `POST /api/session/fresh-start`, `POST /api/session/resume-chosen`, `GET /api/check-resume`.
- **Logs / debug:** `GET /api/logs`, `POST /api/logs/clear`, `GET /api/logs/download`, `POST /api/log` (token-exempt bulk flush), `GET /api/superlog/stats`, `POST /api/superlog/level`, `GET /api/superlog/archive`, `GET /api/superlog/download`, `GET /api/crash-log`.
- **Watchdog:** `POST /api/watchdog/cancel-mismatched-offers`, `GET /api/watchdog/shape-fix-status`, `POST /api/watchdog/shape-fix-abort`.
- **Debug / dev:** `GET /api/debug/coinprep`, `GET /api/debug/pricing`, `GET /api/debug/tibet-test`, `POST /api/debug/sage-single-offer-test`.
- **Diagnostics / utility:** `GET /api/diagnostics/runtime`, `GET /api/diagnostics/api-stats`, `GET /api/reservations`, `GET /api/token_overview`, `POST /api/db/backup`, `GET /api/console/status`, `POST /api/console/toggle`, `POST /api/open-external`, `POST /api/open-data-folder`, `GET /api/check-update`.

**Imports / callers:** Every major domain module; consumed by `bot_gui.html` (fetch), `app_bridge` (test_request_context), and curl/external tooling.

**External services:** Flask, SSE, Sage RPC, Spacescan, TibetSwap, Dexie, Splash, SQLite, filesystem.

**State:** Flask app, SSE event queues, per-endpoint rate-limit dict, per-process local token, session start time, log-clear watermark, run-history cutoff, bot/module refs.

**Risks:**
- Rate limiting: 20 req/10s default; exempt routes (`/api/splash/incoming`, `/api/log`) must be loopback-protected.
- Loopback OR `X-Bot-Local-Token` — relies on `127.0.0.1` bind + local-only firewall.
- Bridge calls bypass `before_request` entirely — XSS in GUI = full bot control.
- SSE has no backpressure guards (acceptable because same-host consumer).
- `_api_error` masks detail (full in logs only).
- Live config update uses `_LIVE_REQUOTE_ONLY_KEYS` whitelist — regression risk if whitelist drifts from real "safe" keys.
- `_sanitize_config_dict` pattern-based redaction — any sensitive key not matching patterns can leak.
- `_decimal_safe` converts Decimal→float at response boundary (precision risk if clients cast to number).

---

### `bot_gui.html` (~1.6MB, ~30677 lines)

**Purpose:** Single-file HTML/CSS/JS frontend — dashboard, config editor, wallet/Splash/Spacescan startup gates, orderbook chart, PnL/intel/logs views. Works under PyWebView (bridge-routed) or plain browser (HTTP).

**Major UI sections:**
- Custom titlebar (desktop-only), sidebar with Dashboard / Offers / P&L / Intel / Settings / Logs.
- Dashboard: hero strip, market strip, token snapshot, AMM status, live controls, price-history chart, recommendations feed, command centre.
- Offers: buy/sell lists, filters, bulk cancel, history, orderbook depth chart.
- P&L: period cards, unmatched-fills warning, volume breakdown, cumulative chart, inventory gauge, circuit-breaker banner.
- Intel: competitor analysis, best bid/ask, thin-side, volume sparkline, external links.
- Settings: grouped config with validation + live preview.
- Logs: level/keyword filter, debug bundle download, clear.
- Startup gates: Sage wallet (risk disclosure, fingerprint, certs, change address, version check), Splash P2P (detect/install/start), Spacescan (API-key setup).

**Top-level JS helpers:** `apiFetch`, `apiCall`, `v4DrawLineChart`, `v4DrawOrderbookDepth`, `v4RecordPrice`, `v4RecordInventory`, `setTheme`/`getTheme`/`toggleTheme`, `formatError`, `pollBotStatus`, `toggleBotRunning`, `toggleOffersVisibility`, `cancelAllOffers`, `triggerCoinPrep`, `previewCtgOffers`, `scheduleSettingsUpdate`, `validateSettings`, `openSettingsHelpModal`, `startupBegin`, `startupSelectFingerprint`, `startupShowFingerprints`, `showWalletPickerModal`, `splashGateBegin`, `spacescanGateBegin`, `checkSplashInstalled`, `setupSplashNode`, `showShutdownModal`, `showCoinTaggerModal`, `showBoostModal`, `closeCtgModal`/`populateCtgModal`/`updateCtgCalculations`, `appendLogEntry`, `clearLogs`, `downloadLogs`, `escapeHtml`, `copyToClipboard`, `openExternalUrl`.

**External services:** Flask (127.0.0.1:5000) via fetch + SSE; PyWebView bridge; browser localStorage.

**State:** polling timers, SSE event source, theme preference, startup phase, modal visibility, cached bot-state snapshot, chart canvas refs, log buffer.

**Risks:**
- **escapeHtml discipline is the XSS line of defence.** Every `innerHTML` consumer of server data must use it. Regression = bot-control via XSS given the bridge bypass.
- `openExternalUrl` blocks same-origin + non-http(s); regressions could turn external links into CSRF to local API.
- SSE reconnect is automatic, but short gaps rely on polling fallback.
- Polling storms possible if new polling loops added without throttle.
- `bridgeMethod` param must match actual bridge method names.
- LocalStorage (theme) is non-sensitive; no PII.
- Canvas redraws clear explicitly; memory-leak surface is small.
- Startup phase FSM needs retry paths on every terminal error.

---

### `tray_manager.py` (~13KB, ~353 lines)

**Purpose:** System-tray icon via pystray + Pillow. Status-coloured icon, tooltip, context menu (Show Dashboard / Start / Stop / Exit).

**Key symbols:** `TrayManager(run/stop/set_status/update_tray_state/_apply_icon_update/_build_tooltip/_build_menu/_create_icon/_on_show/_on_quit/_on_start_bot/_on_stop_bot/_call_flask_api)`. Colour constants `COLOUR_GREEN/AMBER/RED/GREY/INDIGO`.

**Imports / callers:** pystray, Pillow, urllib; consumed by `desktop_app`.

**External services:** OS tray; Flask local API fallback.

**Risks:** Menu rebuilt on every state update — stale menu click possible; Flask fallback silently ignores errors; 3 s polling gap between state and tray; pystray menu thread-safety relies on library.

---

## 8. RPC clients, Splash, Build/Deploy, Docs, CI, Tests, Tools

### `coinset_client.py` (~31KB)

**Purpose:** Fast coin queries via Coinset API with wallet-RPC fallback; fee estimation helpers; block-record + fill-verification fallbacks.

**Key symbols:** `CoinsetClient(initialize_puzzle_hashes / get_spendable_coins / _query_coinset / verify_coin_spent_on_chain / get_block_record_by_height / get_additions_and_removals / get_coin_records_by_hint / check_health / get_stats)`.

**Imports / callers:** `config`, `database`; consumed by `coin_manager`, `fill_tracker`, `bot_loop`, `market_data_collector`.

**External services:** `api.coinset.org`; wallet RPC fallback.

**State:** puzzle-hash cache, initialization flag, hit/miss counters, rate-limit cooldown, health flag, F53 API-call counters.

**Risks:** Puzzle-hash cache only refreshed on demand — new addresses (after topup) miss until refresh; per-instance rate limit (multi-bot from same IP = shared limit); Sage path bypasses puzzle-hash cache; public API = no auth, no MITM protection beyond TLS.

---

### `cat_resolver.py` (~7.7KB)

**Purpose:** Auto-resolve CAT metadata (name, ticker, pair ID) from TibetSwap given an asset_id. Never overwrites existing `.env` values.

**Key symbols:** `resolve_cat_metadata()`, `resolve_and_apply()`, `_apply_to_cfg()`.

**Imports / callers:** `config`, `database`; consumed by `config` on startup, `api_server` (`/api/config/resolve-cat`).

**External services:** TibetSwap `/tokens`, `/pairs`.

**State:** 5-min cache.

**Risks:** CAT_DECIMALS intentionally not auto-resolved (wrong decimals corrupt all offers); soft-failures on outages; `{short_name}_XCH` ticker-ID derivation assumption.

---

### `chia_node.py` (~251 bytes)

**Purpose:** Legacy compatibility shim — `from sage_node import *`.

**Risks:** Deprecation candidate.

---

### `sage_node.py` (~95KB)

**Purpose:** Sage wallet runtime / startup layer — fingerprint discovery, login, health, coin display (with lock detection), transaction history, daemon lifecycle.

**Key functions:** `get_available_fingerprints`, `trigger_start`, `_log_in_fingerprint`, `start_preload`/`stop_preload`, `get_startup_status`, `get_node_status` (15 s cache), `get_wallet_status`, `get_all_balances`, `get_coins_for_display`, `split_coin`, `get_transaction_history`, `get_daemon_status`, `start_chia`/`stop_chia`, `start_sage`/`stop_sage`, `_detect_sage_exe_path`, `_detect_sage_cert_path`, `_launch_sage_exe`, `_is_sage_rpc_available`, `compare_sage_versions`, `get_sage_version_requirement` (min 0.12.9), `_get_sage_fingerprints`, `_build_cat_name_lookup`, `_resolve_cat_name`.

**Imports / callers:** `config`, `database`, `win_subprocess`, `wallet_sage`; consumed by `chia_node`, `api_server`, `bot_loop`, `wallet_sage`.

**External services:** Sage wallet RPC; Chia CLI (`chia keys show`, `chia stop`, `chia start`); local Sage.exe subprocess launch; Windows env.

**State:** `_selected_fingerprint`, `_startup_authorised`, `_auto_launch_sage`, caches (`_node_status_cache` 15 s, `_last_daemon_status` 10 s), `_preload_running`.

**Risks:**
- Min Sage version 0.12.9 gated at startup.
- Fingerprint selection in-memory only — crashes lose choice.
- Preload thread runs indefinitely; bot doesn't auto-restart Sage if it crashes.
- Fingerprint enumeration is pre-authorisation (visible before disclaimer).

---

### `tx_fees.py` (~11.8KB)

**Purpose:** Transaction fee estimation — manual / full-node RPC / Coinset; fee-pool helpers; settings snapshot for GUI.

**Key functions:** `xch_to_mojos`, `mojos_to_xch`, `get_transaction_fee_mode`, `get_wallet_fee_environment`, `get_manual_transaction_fee_mojos`, `get_suggested_transaction_fee`, `_full_node_rpc`, `_coinset_fee_estimate` (F77 retry), `get_effective_transaction_fee_mojos`, `get_fee_pool_count`, `get_fee_coin_size_xch`, `fee_pool_configured`, `fee_pool_enabled`, `get_fee_settings_snapshot`.

**Imports / callers:** `config`; consumed by `offer_manager`, `coin_prep_worker`, `api_server`, `wallet_sage`, `wallet_chia`.

**External services:** Full-node RPC at `localhost:8555` (mTLS); Coinset `/get_fee_estimate`; local manual fallback.

**State:** `_SUGGESTED_FEE_CACHE` (30 s), `_COINSET_FEE_CACHE` (60 s).

**Risks:** Full-node cert discovery assumes standard path layout; Coinset retry (F77) adds ≤10 s latency on 5xx; no minimum-fee floor — manual setting of 0 yields zero-fee transactions.

---

### `splash_manager.py` (~15KB)

**Purpose:** Queue + broadcast offers to the Splash P2P network (mirrors Dexie pattern).

**Key functions:** `SplashManager.queue_post`, `flush_queue` (threaded ≥10), `_post_single`, `repost_active_offers`, `check_health`, `get_stats`, `reset_session_stats`, `prune_fingerprints`, `_fingerprint`.

**Imports / callers:** `config`, `database`; consumed by `bot_loop`, `api_server`.

**External services:** Splash local HTTP on `localhost:4000`.

**State:** `_queue`, `_posted_fingerprints` (manual prune), counters, health flag.

**Risks:** No offer-string validation at queue time; whitespace-sensitive fingerprinting; unbounded fingerprint set; 8-worker concurrency on shared lock.

---

### `splash_node.py` (~23.5KB)

**Purpose:** Auto-launch + monitor the Splash P2P binary (Rust process), crash detection, status reporting, stale-process cleanup.

**Key functions:** `SplashNode.find_binary`, `start`/`stop`, `_run_loop`, `_launch_process`, `_kill_stale_process`, `_is_port_in_use`, `_read_output`, `is_running`, `check_health`, `get_status`, `get_recent_output`.

**Imports / callers:** `config`, `database`, `win_subprocess`, `splash_setup`; consumed by `api_server`, `bot_loop`.

**External services:** Splash.exe; GitHub Releases (via `splash_setup`); webhook to `/api/splash/incoming`.

**State:** `_process`, `_running`, `_restart_count` (max 5), `_pid`, `_binary_path`, `_last_start_time`, `_last_output_lines` (50).

**Risks:**
- **Aggressive stale-process kill** — any "splash"-named process on port 4000 is killed.
- **Webhook endpoint is token-exempt** — any local process can POST fake incoming offers.
- Early-startup warning suppression is fragile (30 s window).
- Windows flag choice (`DETACHED_PROCESS` vs `CREATE_NO_WINDOW`) is sensitive.

---

### `splash_receive.py` (~3.2KB)

**Purpose:** Helpers to normalise/classify incoming Splash offers for the active CAT pair.

**Key functions:** `normalize_offer_summary`, `classify_offer_for_asset`, `_asset_key`, `_normalize_side`, `_from_maker_taker`.

**Imports / callers:** Stateless; consumed by `api_server`, `bot_loop`.

**Risks:** Duck-typed JSON parsing — malformed inputs silently return empty dicts; greedy asset-key normalisation (non-XCH → CAT) could misclassify unusual labels.

---

### `splash_setup.py` (~11.7KB)

**Purpose:** Auto-download/verify Splash binary from GitHub releases; progress reporting for GUI.

**Key functions:** `detect_platform`, `check_installed`, `get_latest_release`, `download_splash`, `start_background_download`, `get_download_status`.

**Imports / callers:** `database`, `win_subprocess`; consumed by `splash_node`, `api_server`.

**External services:** GitHub Releases API + CDN.

**State:** `_download_status`, `_download_lock`.

**Risks:** Mandatory SHA256 — no fallback on verify failure; heuristic platform detection; hardcoded `INSTALL_DIR`; daemon thread can be killed on app exit with partial files left behind.

---

### `win_subprocess.py` (~1.2KB)

**Purpose:** Windows subprocess helpers for hidden-console / detached-process flags.

**Key function:** `hidden_subprocess_kwargs(detached=False, new_process_group=False)`.

**Risks:** Non-Windows returns empty dict (no `setsid`); bitwise-or on flags without validating caller-provided STARTUPINFO.

---

### `build.py` (~5.8KB)

**Purpose:** PyInstaller build orchestrator (clean → build → post-build → success).

**Key functions:** `_run`, `_ensure_pyinstaller`, `_clean`, `_build`, `_post_build`, `_print_success`, `main`.

**Risks:** Hard-wired `catalyst.spec`; HTML presence check is soft (warning only); `.env.example` optional; `--log-level WARN` can hide import warnings.

---

### `catalyst.spec` (PyInstaller spec)

Entry point `desktop_app.py`; hidden imports for `pywebview`, `pystray`, etc.; datas include `bot_gui.html`, `assets/`, `.env.example`; icon `assets/bot_icon_new.ico`; console disabled.

**Risks:** Hardcoded asset paths brittle to restructure; hidden-imports list manual.

---

### `installer.iss` (Inno Setup)

Lowest-privilege install (per-user), preserves user data on uninstall, optional desktop shortcut, post-install run, version auto-bumped by CI.

**Risks:** `AppId` GUID must stay stable across releases; code signing is a manual post-step.

---

### `doctor.py` (~23KB)

**Purpose:** Preflight readiness checks (wallet sync, CAT config, DB, Dexie/TibetSwap/Splash connectivity, Spacescan setup), with 30 s cache.

**Key symbols:** `DoctorCheck`, `DoctorReport` (+ `can_start`), `run_preflight`, `_fetch_wallet_sync_once`, `_check_db_health`, `_check_config_sanity`, `_check_cat_config`, `_check_wallet_reachable`, `_check_wallet_synced`, `_check_wallet_can_sign`, `_check_cat_wallet_mapping`, `_check_dexie_reachable`, `_check_tibet_reachable`, `_check_splash_reachable`, `_check_spacescan_setup`.

**Imports / callers:** `config`, `wallet_sage`/`wallet_chia`, `database`; consumed by `api_server` (`/api/health`), `bot_loop`.

**External services:** wallet RPC, Dexie, TibetSwap, Splash, Spacescan.

**State:** `_cached_report`, `_cache_time`.

**Risks:** Stale 30-s cache can mask fresh outages; sequential checks inflate startup latency; shared wallet-RPC call couples 3 check results; defensive try/except swallows traceable detail.

---

### `README.md` / `PARTIAL_OFFERS_PLAN.md` / `PHASE1_SESSION_PROMPT.md` / `docs/COIN_FSM_DESIGN.md`

- **README.md** — project overview, features, requirements, quick start, architecture summary, running modes, testing, disclaimer.
- **PARTIAL_OFFERS_PLAN.md** — integration plan for CHIP-0052 partial offers: feature flag (`OFFER_MODE`), DB schema, file list (`partial_offer_manager.py`, `partial_fill_tracker.py`, `partial_coin_monitor.py`), new config keys, back-compat promise.
- **PHASE1_SESSION_PROMPT.md** — copy-pasteable prompt for Phase 1 partial-offer scaffolding (5 config settings, DB table + 4 helpers, two wallet_sage stubs, bot_loop/fill_tracker/bot_gui stubs, "all tests pass" gate).
- **docs/COIN_FSM_DESIGN.md** — design of the `(status, designation)` coin FSM, rationale for deferred enforcement, phased rollout (A log-only → B blocking → C rewrite for partial offers).

---

### `.github/workflows/build-release.yml`

Trigger: version tag push. Matrix build (Windows / macOS / Linux) with PyInstaller → smoke test `/api/health` → package (.zip / Inno Setup .exe / .app / tar.gz) → release upload via `softprops/action-gh-release`.

**Risks:** No rollback on partial failure; 30 s smoke-test timeout rigid; code-signing is manual post-step.

---

### `.github/workflows/code-quality.yml`

Trigger: push to master / PR. Jobs: lint-and-syntax (AST parse + import check + `vulture --min-confidence 90`); security-scan (regex for 64-hex private keys + `.gitignore` coverage for .env/*.key/*.pem/*.db/sage_client_ssl/).

**Risks:** Regex-only secret detection misses obfuscated keys; no functional tests run in CI.

---

### `tools/fix_fills_f48.py` (~19.8KB)

One-time DB rectification for F48 (retract two false fills, backfill phantom-rejected via Coinset block-record lookup, reset position baseline). Requires Sage stopped. Safeguards: `--dry-run`, timestamped DB backup, atomic commit, hardcoded wallet puzzle hashes.

**Risks:** Wallet-specific hardcoded hashes; Coinset dependency; not idempotent.

---

### Tests directory (`tests/`)

Infrastructure: `conftest.py` (fixtures, mock wallet, test DB), `pytest.ini` (testpaths, markers, `--capture=sys` for Windows cp1252 emoji), `mock_wallet.py` (fake Sage RPC), `run_tests.py` / `run_api_tests.py` (entrypoints). Integration tests hitting live APIs are excluded in `conftest.py` `collect_ignore`.

**Catalog by area (one line each):**

- **Wallet:** `test_wallet_sage_login`, `test_wallet_sage_cancel_batch`, `test_wallet_sage_bulk_cancel_method`, `test_wallet_sage_spendable_views`, `test_wallet_sage_signing_guard`, `test_wallet_sage_startup_readiness`, `test_wallet_sync_fail_closed`.
- **Coins:** `test_coin_manager_exact_selectable`, `test_coin_manager_fee_pool`, `test_coin_manager_sage_snapshot`, `test_coin_manager_ssot_fallback`, `test_coin_manager_topup_fail_closed`, `test_coin_prep`, `test_coin_prep_confirmed_views`, `test_coin_prep_split_retry`, `test_coin_prep_v2`, `test_coin_classifier`, `test_coin_fsm`, `test_coin_reservations`, `test_hidden_coins`.
- **Offers:** `test_offer_create`, `test_offer_lifecycle`, `test_offer_manager_coin_ids`, `test_parallel_offers`.
- **Fills / verification:** `test_fill_tracker_verification`, `test_fill_pnl_matching`, `test_spacescan_verify_fill`, `test_spacescan`, `test_sniper_coin_ids`, `test_database_verified_fills`, `test_needs_topup_thresholds`.
- **Market / ladder:** `test_market_data_collector_spacescan`, `test_market_intel_orderbook`, `test_pool_rebuild_respects_tier_target`, `test_ladder_planner`, `test_ladder_watchdog`.
- **Bot / monitoring:** `test_bot_loop_probe_anchor`, `test_bot_loop_recovery_mode`, `test_bot_loop_sage_status_mapping`, `test_bot_health_orphan_locks`, `test_bot_health_pending_cancels`, `test_runtime_monitor`, `test_session_management`, `test_amm_monitor`.
- **API / config:** `test_all_apis`, `test_api_data_sources`, `test_api_local_guard`, `test_config_validator`, `test_security_guardrails_source`.
- **DB / reconciliation / misc:** `test_database_reconcile_cat_tiers`, `test_fill_classifier`, `test_event_taxonomy`, `test_dynamic_amm_buffer`, `test_tier_group_counts`, `test_tier_sizes_mojos_reverse_buy`, `test_topup_budget_empty_tier_bypass`, `test_topup_empty_first`, `test_reverse_buy_tier_size`, `test_sage_startup_version_gate`, `test_splash_receive`, `test_sweep_coordinator`, `test_tx_fees`, `test_doctor`, `test_risk_manager_snapshot`.

---

## Top audit themes to carry forward

The following recurring concerns surfaced across the scan and are the best candidates for follow-up reviews:

1. **Wallet-layer safety gaps.** Sage adapter has critical workarounds (selectable filter bug, 404-as-success on cancel), disabled SSL verification, no exponential backoff, and cert generation with 10-year validity and no rotation.
2. **Fill-detection robustness.** Mass-disappearance counter never resets on good polls; triple-fallback verification can leave fills in permanent limbo on Spacescan outages; round-trip matching is tier/size-coupled with no migration on config change.
3. **Coin-state ordering.** `coin_manager` ↔ `coin_prep_worker` ↔ `offer_manager` share state via DB + module-level globals with several race windows (reserve promotion, fee-pool starvation, parallel TX stagger collapse).
4. **Bridge bypasses auth.** `app_bridge` invokes Flask handlers via `test_request_context`, skipping `before_request`. Everything behind the bridge relies on `escapeHtml` discipline in `bot_gui.html`; an XSS there is catastrophic.
5. **Token-exempt endpoints.** `/api/splash/incoming` and `/api/log` accept requests without token — any local process can post fake Splash offers or spam logs.
6. **Secrets at rest.** `user_secrets.json` is plaintext with chmod 0o600 on Unix; Windows has no equivalent protection. No encryption at rest, no rotation, only SPACESCAN_API_KEY wired through.
7. **Logging discipline.** `slog(data=…)` is not automatically scrubbed; ring-buffer dump can race with TRACE appends; size-only rotation can keep a single file huge on long idle runs.
8. **Config defaulting.** Type coercion silently defaults on parse errors; validator warns but does not fail for bad URLs; tier-size sanity check gated on `TIER_ENABLED`.
9. **Database integrity.** Queries are parameterised (no visible SQL-injection surface), but cross-table invariants (offers ↔ coins ↔ fills) are not enforced by triggers/constraints; `price_history` grows unbounded.
10. **External dependencies.** TibetSwap schema assumptions (`/pairs` shape, 0.993 fee constant), Dexie field names, Spacescan free-tier 30-call budget — any upstream drift is silent until a runtime failure.

---

*End of report.*
