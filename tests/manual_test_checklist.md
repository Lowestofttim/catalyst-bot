# CATalyst — Manual Test Checklist

One-page script for a tester or browser-automation session to verify every
user-facing flow against the running bot. Designed to be re-used after any
significant change — see the **"Feature-specific smoke tests"** section at the
bottom for the subset that matches a recent commit.

> **Setup preamble** (30 seconds)
> * Make sure Sage wallet is open with RPC enabled (`Settings → Advanced → Start RPC Client`)
> * Launch: `python desktop_app.py --flask` (or the desktop shortcut)
> * Open `http://127.0.0.1:5000` in a browser, or point your browser automation
>   tool at that URL
> * Click through: **Continue** → **Connect to Sage** → pick your test wallet
>   **by fingerprint** (see §0 gotcha 1) → **Skip** the Splash P2P prompt → **Use Free Tier**
>   on the Spacescan prompt (or **Save & Verify** with a Pro key)
> * You should land on the Dashboard

---

## Table of contents

0. [Test environment — recommended setup](#test-env)
1. [Dashboard tab](#dashboard)
2. [Settings tab](#settings)
3. [Liquidity Mode feature](#liquidity-mode)
4. [Smart Settings (Auto-Fill)](#smart-settings)
5. [Coin Prep flow](#coin-prep)
6. [PnL tab](#pnl)
7. [Offers tab](#offers)
8. [Logs tab](#logs)
9. [Live controls (mid-run)](#live-controls)
10. [Reserve + topup behaviour](#reserves)
11. [Shutdown and restart](#shutdown)
12. [API smoke tests (curl)](#api-smoke)
13. [Reconciliation snippets](#reconcile)
14. [Automated test suites](#auto-tests)
15. [Feature-specific smoke tests](#feature-smokes)
16. [Setup gotchas — read before every session](#gotchas)

---

<a id="test-env"></a>
## 0. Test environment — recommended setup

This is the recommended process layout for manual QA or browser automation.
Replicate this setup if you want to take over a session or reproduce a result.

### 0.1 Process layout

| Component | Where it runs | How to start |
|---|---|---|
| **Sage wallet** | User's machine, separate process | Manual — must be open with RPC on, port 9257 |
| **Flask API server** | `python desktop_app.py --flask` (no PyWebView window) | Start from a terminal when testing in a browser |
| **Browser** | Visible browser or browser automation | Open `http://127.0.0.1:5000` and use stable element IDs for automation |
| **Splash node** | Spawned by CATalyst on demand (`splash.exe` subprocess) | "Start Splash Node" button OR auto-spawn via env |

### 0.2 Browser automation notes

Any browser automation tool is fine as long as it drives the same local UI a
user sees. Prefer stable IDs for buttons and fields, capture browser console
warnings/errors, and keep a screenshot when a visual state matters.

### 0.3 Browser visibility

If your automation runs headless, capture a screenshot and console log for any
failure. For state inspection, prefer structured DOM/API reads over screenshots.

### 0.4 Standard test flow

```
1. Start `python desktop_app.py --flask`      → server up
2. Navigate to http://127.0.0.1:5000
3. Dismiss disclaimer and connect Sage
4. Select wallet by fingerprint, not display name
5. Walk through the startup gates
6. Set reserves
7. Save settings
8. Click "#cpConfirmBtn"                      → start coin prep
9. (5–10 min wait while monitoring logs)
10. Click "#startBtn"                         → start bot
11. Search logs for "cycle_complete"          → confirm ladder built
```

### 0.5 Where data lives during tests

| Path | Contents | Reset by |
|---|---|---|
| `%APPDATA%\Catalyst\bot.db` | DB: coins, offers, fills, settings | Data Reset tab → Wipe All |
| `%APPDATA%\Catalyst\.env` | User config including `SPACESCAN_API_KEY`, `CAT_WALLET_ID` | Manual edit or settings UI |
| `%APPDATA%\Catalyst\bot_superlog_*.log` | Full structured log per session | Auto-rotated at 10MB / 5 files |
| `%APPDATA%\Catalyst\user_secrets.json` | Pro Spacescan key (if set via Save & Verify) | `clear_secret()` in spacescan.py |
| `tests/.e2e_data/` | Isolated DB for e2e Playwright tests | Auto, doesn't touch user data |

The repo-root `.env` is **NOT** the active config — only the per-user
copy at `%APPDATA%\Catalyst\.env` is loaded at runtime.

### 0.6 Verification commands to run first

Always-safe one-liners. Copy-paste, no setup.

```bash
# Active offer count straight from Sage (bypasses the bot's view)
cd src/catalyst && python -c "
import wallet_sage as ws
r = ws.rpc('get_offers', {'offset':0,'limit':100,'include_completed':True}, timeout=10)
from collections import Counter
print(Counter(o.get('status') for o in (r or {}).get('offers', [])))"

# Splash daemon's own view of broadcasts/peers
curl -s --max-time 3 http://127.0.0.1:4001/metrics

# Bot's API view of coin counts
curl -s http://127.0.0.1:5000/api/coins | python -c "import json,sys; d=json.load(sys.stdin)['inventory']; print({k:v for k,v in d.items() if 'total' in k or 'reserve' in k})"

# Cancel-all progress
curl -s http://127.0.0.1:5000/api/offers/cancel_all/status | python -m json.tool

# Live cfg flags
cd src/catalyst && python -c "from config import cfg; print({k:getattr(cfg,k,None) for k in ['SPACESCAN_API_KEY','SPACESCAN_ENABLED','CAT_WALLET_ID','XCH_RESERVE','CAT_RESERVE','SPLASH_ENABLED']})"
```

---

<a id="dashboard"></a>
## 1. Dashboard tab

| # | Test | Expected |
|---|---|---|
| 1.1 | Status badge shows "◯ Stopped" in the titlebar area when bot not running | yes |
| 1.2 | Wallet balance card populates with XCH total + CAT total | ≤ 10 s after Sage connect |
| 1.3 | Coin inventory card shows `xch_total_coins`, `cat_total_coins` | non-zero after wallet sync |
| 1.4 | Trading Pair selector lists all MZ_XCH and other CAT pools from TibetSwap | refresh button works |
| 1.5 | After picking a pair, Mid Price chart starts filling | ~5 s per sample, 240-point history |
| 1.6 | Start Bot button enabled when: wallet synced + trading pair picked + settings saved + coins prepped | red disabled states with tooltips when not |
| 1.7 | **Spacescan tier** — Activity Level / On-Chain Risk show "Free tier" (dimmed) when no API key; "Disabled" when SPACESCAN_ENABLED=false; actual value when Pro key set | (shipped 2026-04-25) |
| 1.8 | **Holders** card shows "Free tier — add API key for holder count" on free tier | tooltip explains the cause |

<a id="settings"></a>
## 2. Settings tab

| # | Test | Expected |
|---|---|---|
| 2.1 | Trading Pair card shows current CAT + "Change Pair" button works | existing pair loads into both selectors |
| 2.2 | "Settings locked while bot is running" banner appears when `bot.running=true` | banner disappears after Stop |
| 2.3 | Reserves: XCH slider + manual input stay in sync | 5/10/25/50% preset buttons all work |
| 2.4 | Reserves: CAT slider respects `cat_reserve_balance` (current CAT balance) | |
| 2.5 | Base Trade Size propagates to tier sizes via `updateTierSizesFromBase` | inner = 2×, outer = 0.5×, extreme = 0.2× |
| 2.6 | Enable Tiered Sizing toggle → shows/hides tier size grid, count section, spare section | 3 sections appear/disappear together |
| 2.7 | Reverse Buy Ladder toggle swaps buy-inner ↔ buy-extreme field values | on toggle, XCH row flips orientation |
| 2.8 | Dry Run Mode checkbox + hint | when on, offers are logged but not sent |
| 2.9 | Save button writes to `.env` + shows toast | ~200 ms round trip |
| 2.10 | Export .env dumps a timestamped file | downloads as attachment |
| 2.11 | Pending-changes banner appears when any field is dirty | clears after Save |
| 2.12 | **CAT_WALLET_ID is NOT writable** via bulk-config-update (blocked) | stale values from earlier sessions can't be re-introduced |

<a id="liquidity-mode"></a>
## 3. Liquidity Mode feature *(shipped 2026-04-19, commits `1192045` + `0dbbcf3`)*

Located at the top of Settings, just under Trading Pair.

| # | Test | Expected |
|---|---|---|
| 3.1 | Three cards render: Two-Sided, Buy Only (Accumulate), Sell Only (Distribute) | icons `📈📉 / 📈 / 📉` |
| 3.2 | Currently-selected card has coloured highlight (blue / green / red) | click another → highlight moves |
| 3.3 | While bot is running, cards have `.disabled` class and inline hint appears | clicking is a no-op + toast "Stop the bot..." |
| 3.4 | After clicking **Buy Only**: Max Sell, Sell ladder sizes, CAT count row, CAT spare row, Sell prep column all hide | body class `liquidity-mode-buy-only` is set |
| 3.5 | After clicking **Sell Only**: Max Buy, Buy ladder sizes, XCH count row, XCH spare row, Buy prep column, Reverse Buy Ladder toggle all hide | body class `liquidity-mode-sell-only` |
| 3.6 | Inventory Management section hides in both single-sided modes | skew is meaningless one-sided |
| 3.7 | Sniper config (size / prep count / re-arm fields + hint) hides and is replaced by "🎯 Sniper unavailable in single-sided mode" banner | banner has grey background |
| 3.8 | Auto-Fill title switches: `Auto-Fill Settings` → `Auto-Fill — Accumulation Plan` / `— Distribution Plan` | subtitle also mode-specific |
| 3.9 | **Coin Prep Summary** Buy/Sell columns collapse to single column in single-sided mode | only active side's counts+sizes shown |
| 3.10 | Wallet-aware hint appears when wallet is ≥92% one asset | "Apply suggestion" button auto-switches mode |

<a id="smart-settings"></a>
## 4. Smart Settings (Auto-Fill)

| # | Test | Expected |
|---|---|---|
| 4.1 | Risk Profile tri-selector (Conservative / Balanced / Aggressive) picks one at a time | visually toggles |
| 4.2 | Set XCH reserve to 5 → click Smart Settings → no error toast | completes in ~3-8 s |
| 4.3 | Form populates: Max Buy, Max Sell, tier sizes (buy + sell rows), tier counts, spare counts, topup pools | all fields filled |
| 4.4 | Capital plan message shows strategy string | e.g. "standard 4-tier ladder · 24B/23S offers · 87.40 XCH trading (85%)" |
| 4.5 | **Liquidity Mode: Buy Only** + Smart Settings → `max_active_sell=0`, all `sell_*_size_xch=null`, `sniper_enabled=false`, `topup_pool_cat=0` | verify via: `fetch('/api/smart-defaults?liquidity_mode=buy_only&...').then(r=>r.json())` |
| 4.6 | **Liquidity Mode: Sell Only** + Smart Settings → `max_active_buy=0`, all `buy_*_size_xch=null`, `sniper_enabled=false`, `topup_pool_xch=0`, `buy_ladder_reversed=false` | similar |
| 4.7 | Under Reverse Buy Ladder + Two-Sided, tier sizes follow the reverse orientation (inner smaller than extreme) | (per 2026-04-19 ea0d1b5 fix) |
| 4.8 | Clicking Smart Settings twice in a row with same inputs → same output | idempotent |
| 4.9 | **Reserve % matrix** — Smart Settings adjusts trade size + slot count inversely with reserve %: 25%→1.40 XCH × 24 slots, 50%→0.73 XCH × 36 slots (verified 2026-04-25) | bigger reserve = smaller trades, more slots |

<a id="coin-prep"></a>
## 5. Coin Prep flow

| # | Test | Expected |
|---|---|---|
| 5.1 | Coin Prep Summary card shows Buy and Sell columns side-by-side under two-sided mode | totals appear in amber |
| 5.2 | "Coin prep impossible" banner appears in red when totals > wallet balance + 0.5% tolerance | scrolls into view |
| 5.3 | Clicking "Prepare Coins" button with no history (fresh run) → confirm modal → progress view | skips history-choice modal |
| 5.4 | Clicking "Prepare Coins" with ≥1 fill OR ≥1 round-trip → **history-choice modal** appears first: "Keep your trading history?" with three buttons | (shipped 2026-04-19 in `095a80d`) |
| 5.5 | History modal shows exact counts: fills, round trips, PnL XCH, net position | live counts from `/api/pnl/reset-preview` |
| 5.6 | **Keep history** → proceeds with `full_reset=false`; fills/round-trips/position survive | coin prep still rebuilds coins |
| 5.7 | **Start fresh** → proceeds with `full_reset=true`; everything wiped | match pre-2026-04-19 behaviour |
| 5.8 | **Cancel** → modal closes, nothing changes | prep does not run |
| 5.9 | Progress view: percentage bar, phase label, XCH/CAT coin counts live-update | worker emits phase transitions |
| 5.10 | If worker fails: Coin Prep Failed modal shows specific reason (e.g. "pool_exceeds_avail"), not just "❌ failed" | (shipped 2026-04-19 in `46f7844`) |
| 5.11 | After success: Done button → returns to Dashboard, Start Bot is enabled | |
| 5.12 | **Pool-coin retry warnings** ("XCH inner pool coin still intact after 45s") are non-fatal — auto-retry succeeds within 1 cycle | typically 5–10 such warnings per prep, all auto-recover |

<a id="pnl"></a>
## 6. PnL tab

| # | Test | Expected |
|---|---|---|
| 6.1 | Session/Round Trips/Total Fills hero cards populate live | updates on each fill |
| 6.2 | Volume breakdown row: Bought, Sold, Net Flow with signed XCH | + green / − red |
| 6.3 | Cumulative PnL chart shows per-fill bars | "Collecting data..." until ≥1 fill |
| 6.4 | Inventory Position panel: gauge bar, Net Position (CAT), Position Limit (XCH) | gauge centred at 50% |
| 6.5 | Position drift mini-chart | renders as line/area |
| 6.6 | **Single-sided mode banner** at top of PnL tab when mode ≠ two_sided | shows avg price + notional vs mid |
| 6.7 | **Reset Position** button: confirm → `/api/fills/purge` → clears fills | (existing legacy behaviour) |
| 6.8 | **⚠ Reset All Stats** button: confirm modal with full scope detail → `/api/pnl/reset` with `{confirm:"RESET"}` | full wipe (including runtime stats) |
| 6.9 | Current Spreads card + Sniper Stats card both update | buy/sell spread % live |

<a id="offers"></a>
## 7. Offers tab

| # | Test | Expected |
|---|---|---|
| 7.1 | Open Offers table lists each live offer | price, size, tier, Dexie link |
| 7.2 | "Dexie ✅" badge on each if `dexie_posted=true` | |
| 7.3 | Dexie link opens in external browser frame | not in-tab |
| 7.4 | Filter-by-side (Buy only / Sell only / All) works | |
| 7.5 | Cancel button on a single row → confirm → offer disappears from list within ~3 s | Sage logs show cancel |
| 7.6 | Cancel All button (when bot is stopped) | shows bulk-cancel confirm; verify via `curl -s http://127.0.0.1:5000/api/offers/cancel_all/status` |
| 7.7 | Recently Filled section below shows last ~20 fills | filled_at timestamp + Dexie link |
| 7.8 | **Cancel All progress endpoint** stays at `phase: "running"` even after backend logs `cancel_all_complete`. **Known stale state — confirm via Sage instead** (`include_completed=False` returns 0 active) | (bug discovered 2026-04-25, status payload not updated on completion) |

<a id="logs"></a>
## 8. Logs tab

| # | Test | Expected |
|---|---|---|
| 8.1 | Live log feed scrolls automatically | "Auto-scroll" toggle pauses it |
| 8.2 | Severity filter (info / success / warning / error) | chips filter in place |
| 8.3 | Search box filters by substring | case-insensitive |
| 8.4 | Export logs downloads `.log` file | timestamped filename |
| 8.5 | Coin prep subsections visually distinct | colour-coded |

<a id="live-controls"></a>
## 9. Live controls (mid-run)

While the bot is running, these should take effect without restart:

| # | Control | Expected |
|---|---|---|
| 9.1 | Base Spread slider (%) | next cycle uses new spread |
| 9.2 | Requote Threshold (bps) | next requote decision |
| 9.3 | Inventory skew intensity | only in two-sided |
| 9.4 | Max active buy / sell | clamps ladder next cycle |
| 9.5 | Dry Run toggle | flips without requiring stop |

Anything that's *capital allocation* (trade size, tier sizes, Liquidity Mode)
requires **stop first**.

<a id="reserves"></a>
## 10. Reserves and topup behaviour

| # | Test | Expected |
|---|---|---|
| 10.1 | XCH reserve > 0 → that amount is excluded from trading budget | visible in Smart Settings breakdown |
| 10.2 | Topup pool reserve = residual of (balance − tiers − reserve − headroom) | Inventory Position panel shows it |
| 10.3 | In buy_only, only XCH topup pool is tracked | CAT topup = 0 |
| 10.4 | In sell_only, only CAT topup pool is tracked | XCH topup = 0 |
| 10.5 | **Reserve floor guard** — when XCH or CAT balance ≤ reserve, bot cancels all open offers and pauses creation. **Must require successful balance read** (don't trigger on RPC failure) | (fix shipped 2026-04-25 — see `_extract_wallet_balance_or_defer`); covered by `tests/test_bot_loop_reserve_floor_guard.py` |

<a id="shutdown"></a>
## 11. Shutdown and restart

| # | Test | Expected |
|---|---|---|
| 11.1 | **Stop Bot** button stops the trading loop only — does **NOT** auto-cancel open offers | offers stay live on Dexie/Splash; bot just stops managing them |
| 11.2 | **Cancel All** button (separate from Stop): confirm modal → bulk-cancel via Sage → all offers reach `status=cancelled` on-chain | only enabled while bot is stopped; verify via Sage RPC, not the GUI counter (see 7.8) |
| 11.3 | **Shutdown App** modal explicitly states *"If you want a clean stop, cancel active offers before shutdown"* — does **NOT** auto-cancel either | clean-stop pattern: Stop → Cancel All → Shutdown |
| 11.4 | If cancel takes >60 s, `position_hard_guard_blocked` warning is rate-limited to 1/min | (shipped 2026-04-18 in `f083abe`) |
| 11.5 | Close-app sequence terminates Flask + PyWebView cleanly | both processes exit |
| 11.6 | Restart app → previous settings load; offers state is respected (resumes without duplicate-creating) | Session Recovery modal offers "Load Previous Session" |
| 11.7 | On next restart, Liquidity Mode loads from env correctly | (shipped 2026-04-19 in `0826ba2`) |
| 11.8 | **External Sage fingerprint switch** (user changes wallet inside Sage while CATalyst is running) → bot's health monitor fires `sage_fingerprint_changed_externally` warning + persistent banner within 15 s | (shipped 2026-04-25); banner clears when user switches back |

<a id="api-smoke"></a>
## 12. API smoke tests (curl / devtools console)

Drop these into a shell with the bot running:

```bash
# Core status — should always return JSON
curl -s http://127.0.0.1:5000/api/status | jq '.running, .liquidity, .stats'

# Config dump — includes LIQUIDITY_MODE
curl -s http://127.0.0.1:5000/api/config | jq '.LIQUIDITY_MODE, .ENABLE_BUY, .ENABLE_SELL, .BUY_LADDER_REVERSED'

# Smart Settings in each mode — verify zero'd fields
for m in two_sided buy_only sell_only; do
  echo "=== $m ==="
  curl -s "http://127.0.0.1:5000/api/smart-defaults?xch_reserve=5&cat_reserve=10000&risk_profile=balanced&liquidity_mode=$m" | \
    jq '{liquidity_mode, max_active_buy, max_active_sell, sniper_enabled, buy_ladder_reversed}'
done

# PnL preview — what Reset All Stats would clear
curl -s http://127.0.0.1:5000/api/pnl/reset-preview | jq .

# PnL — live stats
curl -s http://127.0.0.1:5000/api/pnl | jq '{realised_pnl_xch, total_fills, round_trips, net_position_cat}'

# Spacescan tier — verify "free" vs "pro" surfaces correctly
curl -s http://127.0.0.1:5000/api/dashboard | jq '.market_health.metrics | {spacescan_tier, spacescan_enabled, spacescan_has_data}'

# Cancel-all background-job state
curl -s http://127.0.0.1:5000/api/offers/cancel_all/status | jq .
```

<a id="reconcile"></a>
## 13. Reconciliation snippets — DB ↔ Sage ↔ chain

When you're chasing a phantom mismatch ("the GUI says X but I think it's
actually Y"), these scripts are the source of truth comparison.

### 13.1 Coin reconciliation — DB vs Sage

```bash
cd src/catalyst && python -c "
import sys, os, sqlite3
sys.path.insert(0, '.')
import wallet_sage as ws

def pull(asset_id):
    out=[]; offset=0
    while True:
        r = ws.rpc('get_coins', {'asset_id': asset_id, 'offset': offset, 'limit': 200,
                                  'sort_mode': 'amount', 'filter_mode': 'all', 'ascending': False}, timeout=15)
        c = (r or {}).get('coins', [])
        if not c: break
        out.extend(c)
        if len(c) < 200: break
        offset += 200
    return out

xch = [c for c in pull(None) if not c.get('spent_height')]
cat = [c for c in pull('b8edcc6a7cf3738a3806fdbadb1bbcfc2540ec37f6732ab3a6a4bbcd2dbec105') if not c.get('spent_height')]
print(f'Sage XCH unspent: {len(xch)} / {sum(int(c[\"amount\"]) for c in xch)} mojos')
print(f'Sage CAT unspent: {len(cat)} / {sum(int(c[\"amount\"]) for c in cat)} mojos')

db = os.path.expandvars(r'%APPDATA%\Catalyst\bot.db')
con = sqlite3.connect(db)
def db_coins(wt):
    return {r[0].lower().replace('0x',''): r[1] for r in
            con.execute(\"SELECT coin_id, amount_mojos FROM coins WHERE wallet_type=? AND status!='gone'\", (wt,)).fetchall()}
db_xch = db_coins('xch'); db_cat = db_coins('cat')
sage_xch = {c['coin_id'].lower().replace('0x',''): int(c['amount']) for c in xch}
sage_cat = {c['coin_id'].lower().replace('0x',''): int(c['amount']) for c in cat}
print(f'XCH | DB has, Sage missing: {len(set(db_xch)-set(sage_xch))} | Sage has, DB missing: {len(set(sage_xch)-set(db_xch))}')
print(f'CAT | DB has, Sage missing: {len(set(db_cat)-set(sage_cat))} | Sage has, DB missing: {len(set(sage_cat)-set(db_cat))}')"
```

A healthy bot returns `0 / 0` on both wallet types.

### 13.2 Splash daemon vs bot view

The bot's panel shows "Broadcast N" (POSTs the bot made). Splash's own
counter shows what was actually relayed to peers. They normally diverge
because Splash deduplicates against offers it already knows from other channels.

```bash
# Splash internal counter (source of truth for the wire)
curl -s --max-time 3 http://127.0.0.1:4001/metrics

# Bot's count (source of truth for what we attempted)
curl -s http://127.0.0.1:5000/api/dashboard | jq '.splash // {}'
```

### 13.3 Reserve cascade reproduction (regression check)

If you suspect the reserve-floor guard is triggering on RPC failure:

```bash
# Watch for the new debug-level "skipped" event vs the legacy error
tail -f %APPDATA%\Catalyst\bot_superlog_*.log | grep -E "reserve_floor_breached|reserve_check_skipped"
```

**Healthy:** `reserve_check_skipped` warnings during Sage outages.
**Regression:** any `reserve_floor_breached` error fires while wallet RPC was unhealthy.

<a id="auto-tests"></a>
## 14. Automated test suites

### 14.1 pytest unit suite

```bash
cd tests
python -m pytest -q --tb=line
```

Expected: ~1650 passed in ~80 s. The known-good baseline is 0 failures
after the test-pollution fix shipped 2026-04-25.

### 14.2 Playwright e2e suite (opt-in)

```bash
# One-time setup
pip install -r requirements-dev.txt
python -m playwright install chromium

# Run (against a temporary Flask server on its own data dir)
cd tests
python -m pytest e2e/ --e2e -v
# Add --headed to watch the browser drive
```

Currently covers: app boot, disclaimer dismiss, wallet-connect screen, all 7 nav
tabs present, no spurious console errors. Auto-skipped without `--e2e` so it
doesn't slow normal test runs.

Stable-selector convention (in `tests/e2e/conftest.py` docstring):
1. element ID, 2. ARIA role + name, 3. `data-view` / `data-action`, 4. `data-testid` (only when needed).

### 14.3 What to run after each commit

| Change scope | Run |
|---|---|
| Pure Python (bot_loop, offer_manager, etc.) | `pytest -q` |
| HTML / JS / CSS | `pytest e2e/ --e2e` |
| Schema migrations | `pytest test_plan_02_30_database_unit.py test_coin_manager_ssot_fallback.py` |
| Anything affecting Sage interactions | `pytest test_bot_loop_*` + manual coin reconciliation (§13.1) |

<a id="feature-smokes"></a>
## 15. Feature-specific smoke tests

Use when verifying a specific recent commit:

### Liquidity Mode (`1192045` + `0dbbcf3`)
- 3.1 – 3.10 (all Liquidity Mode UI)
- 4.5, 4.6 (smart-defaults mode branching)
- 5.1 (coin-prep summary adapts)
- 6.6 (PnL banner in single-sided)
- 10.3, 10.4 (topup pool)

### PnL reset + coin-prep history choice (`095a80d`)
- 5.3, 5.4, 5.5, 5.6, 5.7, 5.8
- 6.7, 6.8

### Reverse-buy storage revert (`ea0d1b5` + `0826ba2`)
- 2.7 (toggle swap)
- 4.7 (Smart Settings reverse-buy math)
- 11.7 (env round-trip)

### Coin prep failure surfacing (`46f7844`)
- 5.10 (specific reason in failed modal)

### Position-hard-guard rate limit (`f083abe`)
- 11.4 (once-per-60s log rate)

### Sage RPC startup hint (`5206ef6`)
- Startup flow step 4 (on wallet-not-detected path)

### Reserve-floor RPC-failure guard (2026-04-25)
- 10.5 (reserve check defers on bad balance read)
- Run `pytest test_bot_loop_reserve_floor_guard.py` (14 cases)

### Sage fingerprint drift detection (2026-04-25)
- 11.8 (external switch fires warning + banner within 15 s)
- Manually: change Sage's active key while bot is running → watch log for `sage_fingerprint_changed_externally`

### Spacescan tier display (2026-04-25)
- 1.7, 1.8 (Free tier label vs Disabled vs actual data)

### Stale CAT_WALLET_ID block (2026-04-25)
- 2.12 (bulk-config-update can no longer persist `CAT_WALLET_ID`)
- Verify `_active_cat initialized from .env` log shows resolved wallet_id, not stale value

---

<a id="gotchas"></a>
## 16. Setup gotchas — read before every session

These will silently waste 30 minutes if you don't know about them.

### G1. Similar wallet names — pick by fingerprint

The wallet selection list can contain similarly named test wallets. Selecting
by visible text is ambiguous. **Always select by fingerprint** in the wallet
card click.

```js
// Correct selector pattern
const targetFingerprint = 'YOUR_TEST_WALLET_FINGERPRINT';
Array.from(document.querySelectorAll('.fp-card'))
     .filter(el => el.offsetParent && el.innerText.includes(targetFingerprint))[0]
     ?.click();
```

### G2. Sage's offer DB persists across fingerprint switches

If you switch from wallet A → wallet B inside Sage, Sage's `get_offers` call
will still return wallet A's offers (now uncancellable from B because the
signature is invalid). The bot will report `bulk cancel: 7 succeeded, 0 failed`
but the offers won't actually go away. Verify via:

```bash
cd src/catalyst && python -c "
import wallet_sage as ws
r = ws.rpc('get_offers', {'offset':0,'limit':100,'include_completed':False}, timeout=10)
print(len([o for o in (r or {}).get('offers', []) if o.get('status')=='active']), 'active')"
```

### G3. The active `.env` is in `%APPDATA%`, NOT the repo

The repo-root `.env` is read for `dotenv` defaults during local dev runs but
gets **overridden** by `%APPDATA%\Catalyst\.env` (loaded by `user_paths.env_file()`
in `config.py:36`). When something looks misconfigured, check the user-data
copy first.

### G4. `Use Free Tier` does not clear an existing API key

Clicking "Use Free Tier" on the Spacescan gate only sets `SPACESCAN_ENABLED=true`;
it does NOT clear `SPACESCAN_API_KEY`. So if Pro fields show "Unknown" it's
because no key was ever stored, not because Free Tier nuked it. To verify:

```bash
cd src/catalyst && python -c "
from config import cfg
print('API key set?', bool((cfg.SPACESCAN_API_KEY or '').strip()))"
```

### G5. Modals can stack and absorb your clicks

Common stacking on first run: Disclaimer → Wallet pick → Change Address gate →
Splash gate → Spacescan gate → Session Recovery → Settings/Coin Prep.
A `preview_click` against a button **underneath** a stacked modal silently
no-ops. Always check `getCommandCentreDashboardState()` or count visible
modals before asserting a click landed:

```js
Array.from(document.querySelectorAll('[class*="modal"]'))
     .filter(el => el.offsetParent && el.getBoundingClientRect().width > 200)
     .length
```

If > 0, dismiss via the modal's own button OR `dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))`.

### G6. Coin prep takes 5–10 minutes even on a tiny wallet

Each split is one on-chain transaction with confirmation. `Step 4/4: Waiting
for confirmations` typically sits at 72% for 1–2 minutes — don't assume it's
hung. Check `coin_prep_ungate` event for true completion.

### G7. Long-running Sage sessions can 401

Sage's session token expires after some idle period. The bot's reserve-floor
guard now defers on this (see 10.5), but you may still see `cancel_failed`
warnings on stale offers. Restarting the wallet selection (preview MCP:
`POST /api/sage/start-with-fingerprint`) re-issues the session.

---

## Quick pass/fail script

If you only have 5 minutes, run this minimum viable set:

1. `pytest -q` → 0 failures
2. `pytest e2e/ --e2e -q` → 4/4 passed (or current count)
3. Startup flow → Dashboard renders
4. `/api/status` returns a `liquidity` block + `running` field
5. Coin reconciliation script (§13.1) returns `0 / 0` mismatches
6. `curl -s /api/dashboard | jq '.market_health.metrics | {spacescan_tier, spacescan_enabled}'` returns the new tier field

If any of those six fail, dig into the corresponding section above.
