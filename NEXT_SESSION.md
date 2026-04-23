# Next Session Bootstrap — Live Bot Endurance Testing

**Main goal:** watch the running bot react to real and induced events and keep it running smoothly, unattended, for as long as possible. Codex is your partner; use it with discipline.

---

## 1. Paste this as your first message

```
You are continuing a live CATalyst market-maker hardening session. Read these
before doing anything else:
  - CLAUDE.md (project conventions)
  - memory\MEMORY.md (session history)
  - NEXT_SESSION.md (this file — workflow + testing protocol)

Current live state to verify FIRST:
  - Live .env:   %APPDATA%\ChiaMarketMaker\.env  (NOT repo .env)
  - Live DB:     %APPDATA%\ChiaMarketMaker\bot.db
  - CAT pair:    MZ (asset b8edcc6a7cf3738a3806fdbadb1bbcfc2540ec37f6732ab3a6a4bbcd2dbec105, wallet 1002)
  - Bot token:   scrape from GET http://127.0.0.1:5000/  →  __BOT_LOCAL_TOKEN
  - Sage wallet is running; I can send SEP-wallet test fills on request.

Your task: keep the bot alive and healthy. Before any change, diagnose config +
live state. Use Codex for counterfactuals and narrow patches. Do not refactor.
```

---

## 2. Codex partnership rules (from hard-won experience)

1. **One finding per turn.** Not "audit category X" — "Codex, is line 273 of offer_manager.py racy given the lock at 261? Answer yes/no + one-line reason."
2. **Fresh chat per category.** When Codex drifts or stream-disconnects, do NOT try to recover the same chat — start fresh with a 5-line context recap.
3. **Push back on hallucinations within one turn.** If Codex cites "Task Manager shows 47% CPU" — it has no computer-use. Call it out immediately: "You cannot see my screen. Re-answer without that."
4. **Paste the exact lines.** Never "look at bot_loop.py around the retry" — always `bot_loop.py:842-871` with the actual code block.
5. **Kill bloat.** If a Codex reply is >20 lines of prose, you asked too broad a question. Re-ask tighter.
6. **Use Codex for counterfactuals, not implementation.** "What would happen if fills arrive during cancel-all?" is gold. "Write me a fix" usually isn't.
7. **Keep a running gotchas note.** Append to `memory\project_gotchas_<date>.md` every time something surprises you: config-drift signatures, DB-shape quirks, log-message patterns. Saves hours next time.

---

## 3. Environment gotchas (don't rediscover these)

| Gotcha | What to do |
|---|---|
| `%APPDATA%\ChiaMarketMaker\.env` overrides repo `.env` via `config.py:54 load_dotenv(override=True)` | ALWAYS check live .env first. Repo .env is decoration. |
| `textinputhost.exe` (Windows IME) blocks automation input | `tasklist | grep -i textinput` → `taskkill //PID <pid> //F`. Windows respawns clean. |
| `/api/*` writes need `__BOT_LOCAL_TOKEN` | GET `/`, regex the HTML for the token, use in `X-Bot-Local-Token` header. |
| DB `trade_id` is bare hex, no `0x` prefix | Query both forms when joining against Dexie/Sage. |
| Dexie status codes | 3 = cancelled, 4 = completed. PENDING_CANCEL is INT 2, still fillable. |
| Zombie offers (cancelled in DB, live in Sage) | Already detected on startup. Excluded from caps. Don't "clean them up" — they burn themselves out. |
| Codex compacts & dies on long sessions | Restart chat with scope recap every ~30 turns. |

---

## 4. Live-testing protocol (the main job)

**Phase A: passive endurance (always running)**

Start `Monitor` on the bot log and leave it streaming. Each event, ask:
- Is `loop=` still advancing? (frozen loop = ALERT, investigate immediately)
- `err=` increasing? (one-off is fine; sustained = ALERT)
- Are fills moving to `filled` lifecycle_state within 2 minutes of Dexie status 4?
- Are zombies trending down over days, or holding steady?

**Phase B: induced stress (once per session, when Phase A is green)**

Run these one at a time, 15 minutes apart, with Monitor still active:

1. **Cancel storm** — `POST /api/bot/cancel-all` with token. Watch for stuck `pending_cancel` rows, DB lock waits, coin-manager UTXO churn.
2. **Restart mid-cycle** — kill the desktop app while `is_trading=True`. Restart. Verify: pagination picks up pre-existing offers, zombies re-detected, no duplicate `lifecycle_state` transitions.
3. **Fill during cancel-all** — start cancel-all, immediately ask user to send a SEP fill on an already-live offer (one that hasn't hit the cancel queue yet). Verify: fill wins, cancel skips that offer, no orphan coin.
4. **Price-oracle flake** — temporarily block `dexie.space` DNS (Windows hosts file). Verify: risk_manager degrades gracefully (TibetSwap fallback), circuit-breakers engage, bot doesn't crash.
5. **Sage RPC stall** — pause Sage process for 30s (`pssuspend`). Verify: bot_health flags it, loop doesn't wedge, resume → recovery within one cycle.
6. **Coin exhaustion** — set `MIN_COIN_COUNT_FLOOR` very high via Settings; watch coin_prep_worker kick in without deadlocking.
7. **Deposit advisory** — have user send XCH from external wallet. Verify: modal fires, allocation choices execute correctly, counters reconcile.

**Phase C: golden-gate verification**

After every fill (real or induced):
- Spacescan on-chain confirm → DB `lifecycle_state='filled'` → wallet balance delta matches offer amount.
- If any of the three disagree: STOP, investigate, do not let the bot continue trading until reconciled.

---

## 5. Known open items (tackle in order if idle)

1. **CAT-selector defensive guard** — `api_server.py:10790` and `:10852` silently write placeholder asset_ids to live .env. Add: reject write if asset_id not in `wallet.list_cats()` response.
2. **Price-rail circuit breakers off** — `HARD_MIN_PRICE_XCH` / `HARD_MAX_PRICE_XCH` unset. Compute sane defaults from current oracle price ± 30%, write to live .env.
3. **Fill investigation** — during the last session, CAT total went up ~104k MZ. Only c546 was reconciled. Verify via Spacescan whether there were additional unrecorded fills; if so, audit fill_tracker's Dexie-polling path.

---

## 6. What NOT to do

- Don't refactor. Don't add abstractions. Don't write new tests unless verifying a specific fix.
- Don't cancel zombies. They expire on their own.
- Don't trust the repo .env. Only the live .env matters.
- Don't use `print()` or stdlib `logging`. Always `slog(category, message)`.
- Don't raise exceptions into JS. AppBridge returns `{success: bool, ...}`.
- Don't commit without the auto-checkpoint hook — it tags sessions correctly.
- Don't ask the user permission for routine fixes — apply and verify (user feedback: "just fix it and carry on").
- Don't claim a fix works without live-bot verification — type-check and unit tests prove code correctness, not behaviour.

---

## 7. End-of-session ritual

1. Confirm Monitor shows bot still running, loop advancing, err=0 (or explain non-zero).
2. Append to `memory\project_overnight_monitor_<date>.md` with: loops run, fills processed, induced-stress results, any new gotchas.
3. If leaving bot running overnight: state the exact loop number and fill count so the next session has a baseline.
4. Update `MEMORY.md` index with any new memory files.
