# Findings — Slice 04-03

API contract tests for bot lifecycle endpoints (start/stop/shutdown).

## New coverage added

| Test class | Tests | Notes |
|------------|-------|-------|
| `TestBotStart` | 8 | 401 no-token, 500 no-bot, already-running, no-CAT-ID, zero-spread, success, blocked-by-bot, signing-block |
| `TestBotStop` | 5 | 401 no-token, 500 no-bot, 200 running, status=stopped, bot.stop() called |
| `TestShutdown` | 4 | 401 no-token, 200 response, dict body, threading.Thread called |

**17 new tests** in `tests/test_plan_04_03_bot_lifecycle_endpoints.py`.

## Fix required (test)

`get_wallet_sync_status` is lazily imported inside `api_bot_start()` — must patch
`wallet.get_wallet_sync_status`, not `api_server.get_wallet_sync_status`.

## No production bugs found
