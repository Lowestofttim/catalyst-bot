# Phase G — Settings Validation Edge Cases

**Tested:** 2026-04-20  
**Method:** API-level (`/api/config`, `/api/bot/start`, `/api/cat/select`)

## Test matrix

| # | Test | Expected | Actual | Result |
|---|------|----------|--------|--------|
| G1 | `LIQUIDITY_MODE="invalid_mode"` via `/api/config` | 400 / errors list | `success: true`, no error | ❌ Bug |
| G2 | Config update while bot running | 409 or warning | Accepted silently — `status: updated` | ❌ Bug |
| G3 | `SPREAD_BPS=-100` via `/api/config` | 400 / errors list | `success: true`, no error | ❌ Bug |
| G4 | `MAX_ACTIVE_BUY=0, MAX_ACTIVE_SELL=0` | Accept (zero-target is valid config) | Accepted | ✅ |
| G5 | `/api/bot/start` with `SPREAD_BPS=-100` | 400 with error | 400: "SPREAD_BPS is 0 or negative — bot would create bad offers" | ✅ Caught at start |
| G5b | `/api/bot/start` with `MAX_ACTIVE_BUY=0, MAX_ACTIVE_SELL=0` | Should warn or error | Started successfully — bot runs with 0 targets | ⚠ Edge case |
| G6 | `SNIPER_ENABLED=true` + `LIQUIDITY_MODE=sell_only` | Warn or reject | Accepted; smart-defaults returns `sniper_enabled: false` with message | ⚠ Inconsistency |
| G7 | `SPREAD_BPS=9999` > `MAX_SPREAD_BPS=5000` | Warn about out-of-bounds | No warning | ❌ Bug |
| G8 | `POST /api/cat/select` with non-hex `asset_id` | 400 | 400: "CAT asset_id must be exactly 64 hex characters" | ✅ |

---

## Findings

### G1 — Invalid LIQUIDITY_MODE accepted without error

**Severity:** Medium  
`POST /api/config {"LIQUIDITY_MODE": "invalid_mode"}` returns `success: true`.
The value is written to `.env`. On next bot cycle, cfg.LIQUIDITY_MODE would be
`"invalid_mode"`, causing undefined ladder behaviour.

**Expected:** 400 with `errors: ["LIQUIDITY_MODE: must be one of two_sided|buy_only|sell_only"]`

**Location:** `config.py` or `api_server.py` config update handler — no enum validation.

---

### G2 — Config can be updated while bot is running

**Severity:** Medium  
The GUI shows "Settings locked while bot is running" but the API does not enforce this.
`POST /api/config {"LIQUIDITY_MODE":"buy_only"}` while bot running returns `status: updated`.

The bot won't pick up the change until next `cfg.reload()` (happens on next cycle),
so a live mode switch mid-run is possible. This can corrupt the offer book (sell offers
remain open after switching to buy_only).

**Expected:** Either: (a) 409 with `"Bot must be stopped to change LIQUIDITY_MODE"`, or
(b) accept but only apply on next restart.

---

### G3 — Negative SPREAD_BPS accepted without error at config-save time

**Severity:** Low (caught at bot start)  
`POST /api/config {"SPREAD_BPS": -100}` succeeds. The negative spread IS caught at
`/api/bot/start` (returns 400 with clear error). However, the config file is left in
an invalid state between save and start.

**Expected:** Reject at save time with 400 error.

---

### G5b — Bot starts with `MAX_ACTIVE_BUY=0, MAX_ACTIVE_SELL=0` (no error)

**Severity:** Low  
Starting the bot with both sides at zero offers is not blocked. The bot runs, loops,
and creates nothing. No error, no warning. This is arguably valid (a "paused" state)
but could confuse users who misconfigure.

**Recommendation:** Return a warning in the start response: `"Both sides at 0 — bot will loop but create no offers"`.

---

### G6 — `SNIPER_ENABLED=true` + `LIQUIDITY_MODE=sell_only` not rejected

**Severity:** Low  
The API accepts this combination. Smart defaults correctly returns `sniper_enabled: false`
for sell_only with message "Sell-only mode: buy ladder disabled, sniper off."
But a user manually setting both via `/api/config` gets no warning. Sniper will silently
do nothing (can't fire sell probes; buy probe disabled).

**Expected:** Warning in config response or at bot start.

---

## Summary

| Severity | Count |
|----------|-------|
| Medium | 2 (G1 invalid mode, G2 live config change) |
| Low | 3 (G3 negative spread, G5b zero sides, G6 sniper+sell-only) |
| Pass | 3 (G4, G5, G8) |

All five bugs are input-validation gaps that can be fixed in the `/api/config` handler
and `bot.start` pre-flight checks without affecting trading logic.

---

## Fixes applied — commit ea991ec

**G1** — `api_config_update()` bulk path: LIQUIDITY_MODE enum validation added; invalid
values now return 400 with descriptive error.

**G2** — `api_config_update()` bulk path: LIQUIDITY_MODE change while bot running now
returns 409. Same guard added to single key-value path.

**G3** — `api_config_update()` both paths: `SPREAD_BPS <= 0` now rejected at save time
with 400 error. (Previously only caught at bot start.)

**G5b** — `api_bot_start()`: added pre-start warning when both `MAX_ACTIVE_BUY_OFFERS`
and `MAX_ACTIVE_SELL_OFFERS` are 0.

**G6** — `api_config_update()` bulk path: warning added to response when
`SNIPER_ENABLED=true` with `sell_only` or `buy_only` mode.

**G7** — `api_config_update()` bulk path: warning added when `SPREAD_BPS > MAX_SPREAD_BPS`.

All six rules also apply to the single `{"key":…,"value":…}` format path (G1/G2/G3).
Response shape now includes optional `"warnings"` field alongside `"errors"`.
