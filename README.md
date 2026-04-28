# CATalyst

**Provide liquidity on Dexie without babysitting your offers.**

Providing liquidity on [Dexie](https://dexie.space) (Chia's main DEX) means manually adjusting your offers every time the price moves and re-posting the ones that get filled. It is a full-time job if you do it by hand. CATalyst does it for you: set your target liquidity and capital budget, and it maintains a live bid/ask ladder around the market price, requotes when the market moves, and refills filled offers. It runs as a native desktop application on your own machine and connects to your [Sage wallet](https://sage.rigidnetwork.io). Your keys, your coins, your trades.

**Status:** Beta, actively used in production. No warranty. Use at your own risk.

### [⬇ Download for Windows](https://github.com/Lowestofttim/catalyst-bot/releases/latest)

---

## What it does

Market making means posting both a buy offer and a sell offer around the current market price, then profiting from the spread when both fill. Doing this well on Chia is hard:

- Offers are native blockchain assets, not database rows, so every quote move costs a transaction.
- Wallet coins must be pre-split into the right denominations before offers can be created.
- Fills arrive through multiple paths (Dexie API, mempool, on-chain), each with its own latency and propagation delay, so they can temporarily disagree.
- Competitors move the order book constantly and arbitrageurs sweep gaps.

CATalyst handles all of that. You tell it the CAT you want to trade and your capital budget; it produces and maintains a professional order book that stays live through wallet reconnects, API outages, and market shocks.

---

## Features

### Trading
- **Tiered ladder.** Inner / mid / outer / extreme bands with configurable size and count per tier, per side.
- **Dynamic spreads.** Adjusts based on realised volatility, inventory skew, and competitor depth.
- **Smart Settings.** One-click capital planning. Reads your wallet balance and market volatility, emits a validated trading configuration.
- **Sniper probes.** Detects arbitrage gaps between Dexie and TibetSwap AMM and fires targeted orders to capture them.
- **Gap-close cascades.** When the market moves through several tiers, CATalyst closes the gap in staged steps instead of a single shock requote.
- **Mempool watch.** Spots TibetSwap swaps before they confirm on chain and preempts price moves.

### Execution & safety
- **Multi-source fill verification.** Spacescan + Sage + Dexie fallback chain. An offer isn't recorded as filled until at least one authoritative source confirms.
- **Circuit breakers.** Hard price bands, step-change guards, sweep detection, and per-cycle cancel/create caps.
- **Dynamic price limits.** Tracks a live reference price and rejects quotes that stray beyond a configurable band.
- **Risk disclosure.** On first run, the operator must accept an on-screen disclosure before the bot can be enabled.

### Coin management
- **Automatic UTXO splitting.** A background worker keeps the wallet supplied with the right size coins for each tier.
- **Proactive drip topup.** Refills each tier at 75% utilisation rather than waiting for exhaustion.
- **Orphan reclaim.** Sweeps small change outputs from fills back into productive tiers.
- **Budget autoscale.** Partial refills when the capital budget is tight, rather than stalling.

### Operations
- **Native desktop app.** System tray, notifications, runs in background. Survives terminal closes.
- **Splash P2P.** Broadcasts offers directly to other Splash nodes for private-mempool distribution.
- **Self-healing watchdog.** Detects stuck state, stale lifecycle flags, and budget drift; repairs them without restarts.
- **Data management.** Separate resets for P&L history, offer history, or full state, directly from the GUI.
- **Update checker.** Polls GitHub for new releases.

---

## Requirements

- Cross-platform: Windows 10/11 (64-bit), macOS, and Linux. Prebuilt binaries for all three ship with every release; see the [Releases page](https://github.com/Lowestofttim/catalyst-bot/releases).
- [Sage wallet](https://sage.rigidnetwork.io) installed with RPC enabled (Settings → Advanced → Enable RPC).
- XCH for fees and inventory, plus the CAT token you want to trade.
- Python 3.10+ only if running from source. The packaged release has no external runtime requirements.

---

## Quick start

CATalyst is not a hosted web app. Every operator runs their own local copy on
their own computer, connected to their own Sage wallet. GitHub provides the
source code and release downloads; it does not provide a shared CATalyst server.

`127.0.0.1` always means "this computer". Nobody should connect to the
maintainer's machine, and CATalyst deliberately blocks non-local browser/API
access for wallet safety.

### From the installer (recommended)

1. Download `Catalyst-Setup-v*.exe` from the [latest release](https://github.com/Lowestofttim/catalyst-bot/releases/latest).
2. Run it. The installer places CATalyst in Program Files and adds a desktop shortcut.
3. Launch CATalyst on the same computer as Sage wallet. On first run it will prompt for your Sage wallet connection and walk you through Smart Settings.

### From source on Windows

Use this path if you want to run the current source code or develop the app.
Run these commands on the same PC that has Sage wallet installed:

```powershell
git clone https://github.com/Lowestofttim/catalyst-bot.git
cd catalyst-bot
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python desktop_app.py --flask
```

Then open `http://127.0.0.1:5000/` in a browser on that same PC. For the native
desktop window instead, run `python desktop_app.py`.

### From source on macOS/Linux

```bash
git clone https://github.com/Lowestofttim/catalyst-bot.git
cd catalyst-bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python desktop_app.py --flask
```

Then open `http://127.0.0.1:5000/` in a browser on that same machine.

On first launch CATalyst creates a per-user `.env` automatically in the app data
directory. Normal users should not need to copy or edit `.env` by hand. The
startup flow looks for Sage, waits for RPC if needed, and asks the user to pick
their wallet fingerprint in the GUI. After that, CAT selection and Smart Settings
write the trading settings as the user configures the app.

If port `5000` is already in use, set `CATALYST_FLASK_PORT` before starting.
Windows PowerShell:

```powershell
$env:CATALYST_FLASK_PORT = "5010"
python desktop_app.py --flask
```

macOS/Linux:

```bash
CATALYST_FLASK_PORT=5010 python desktop_app.py --flask
```

Then open `http://127.0.0.1:5010/` instead.

### If you see "Access denied" or "Loopback only"

That usually means the browser request is not reaching CATalyst as a local
request from the same machine. Check that:

- CATalyst is running locally on the computer opening the browser.
- You are using `http://127.0.0.1:5000/`, not another PC's IP address, a
  Codespaces/browser-preview URL, or a port-forwarded URL.
- Sage wallet RPC is enabled locally in Sage Settings -> Advanced.
- If Sage certificate auto-detection fails, use the in-app setup prompt or edit
  that user's local `.env` as a fallback.

Direct API clients also need CATalyst's per-run local write token. The normal
web page handles this automatically; hand-written scripts must supply the token.

---

## Configuration

CATalyst creates a per-user `.env` on first launch and updates it through the
GUI. You rarely edit it by hand:

| Setting | What it does |
|---------|-------------|
| `SAGE_RPC_URL` | Sage wallet RPC endpoint (default `https://127.0.0.1:9257`) |
| `SAGE_CERT_PATH` / `SAGE_KEY_PATH` | Optional fallback paths if Sage cert auto-detection fails |
| `CAT_ASSET_ID` | The CAT you want to trade, written when you pick a token in the GUI |

Every other trading parameter (spread, offer count, tier sizes, reserves, topup budgets) is configured via **Smart Settings** in the GUI. Smart Settings reads your wallet balance and current market volatility and emits a validated configuration in one click. You can override any individual field afterwards.

> **Security:** `.env` can contain local wallet paths. Never commit it. The `.gitignore` excludes it by default.

---

## How it works

```
         ┌─────────────────┐
         │  Price Engine   │  ← TibetSwap AMM + Dexie book, weighted
         └────────┬────────┘
                  │ reference price
         ┌────────▼────────┐
         │  Risk Manager   │  ← spread, skew, circuit breakers
         └────────┬────────┘
                  │ quote targets
         ┌────────▼────────┐
         │ Offer Manager   │  ← create / cancel / requote
         └────────┬────────┘
                  │ offer files
          ┌───────▼────────┐    ┌────────────────┐
          │ Sage Wallet    │───▶│     Dexie      │
          └───────┬────────┘    └────────────────┘
                  │                      │
                  ▼                      ▼
          ┌────────────────┐     ┌────────────────┐
          │  Coin Prep     │     │ Fill Tracker   │
          │  (UTXO split)  │     │ (verification) │
          └────────────────┘     └────────────────┘
```

The trading loop runs every 45 to 90 seconds:

1. Fetch the latest mid price from TibetSwap and Dexie.
2. Check for new fills against each side of the book; verify on-chain.
3. Decide whether the book needs to be requoted (price drift > threshold, inventory skewed, tier exhausted).
4. Cancel stale offers, create new ones, post to Dexie + Splash.
5. Top up UTXOs if any tier is running low.

Between cycles, the coin prep subprocess runs asynchronously and the mempool watcher polls for TibetSwap swaps that will move the market.

---

## Architecture

| Module | Role |
|--------|------|
| `desktop_app.py` | Entry point. Boots Flask, PyWebView window, system tray |
| `api_server.py` | HTTP API + Server-Sent Events for the GUI |
| `bot_loop.py` | Main trading loop orchestrator |
| `bot_gui.html` | Single-file dashboard UI |
| `offer_manager.py` | Offer creation, cancellation, rolling requote |
| `fill_tracker.py` | Fill detection + multi-source verification |
| `price_engine.py` | Price oracle (TibetSwap + Dexie weighted) |
| `risk_manager.py` | Circuit breakers, position limits, spread calc |
| `coin_manager.py` | UTXO tracking, tier classification, topup |
| `coin_prep_worker.py` | Async coin splitting subprocess |
| `wallet_sage.py` | Sage wallet RPC adapter |
| `dexie_manager.py` | Dexie API integration |
| `spacescan.py` | On-chain verification via Spacescan |
| `sniper.py` | Arbitrage gap probing |
| `splash_manager.py` | Splash P2P node integration |
| `smart_defaults.py` | Capital-aware config generator |
| `bot_health.py` | Self-healing watchdog |
| `database.py` | SQLite state layer (WAL mode) |
| `config.py` | Typed `.env` loader with hot reload |

---

## Running modes

| Mode | Command | Use case |
|------|---------|----------|
| Desktop | `python desktop_app.py` | Default. Native window + system tray. |
| Browser | `python desktop_app.py --flask` | Server-only, open in any browser on the same machine at `http://127.0.0.1:5000/`. |
| Dev | `python desktop_app.py --dev` | Desktop window AND browser access simultaneously. |

---

## Data location

CATalyst stores its SQLite database, logs, and runtime state in the OS standard app-data directory:

- **Windows:** `%APPDATA%\Catalyst\`
- **macOS:** `~/Library/Application Support/Catalyst/`
- **Linux:** `~/.local/share/Catalyst/`

Override with the `CMM_DATA_DIR` environment variable.

---

## Building from source

```bash
python build.py              # full clean build, produces dist/Catalyst/
python build.py --no-clean   # skip cleaning for faster iteration
```

The local build output stays on the machine that ran `python build.py`. To make
builds available to other users, publish a GitHub Release or push a `v*` tag so
the GitHub Actions release workflow can upload downloadable packages.

Tag a commit as `v*` to trigger the GitHub Actions build-release pipeline, which produces Windows/macOS/Linux packages plus a Windows installer and uploads them all to a new GitHub Release.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q --ignore=tests/e2e --disable-warnings
python -m ruff check . --select E9,F821
python -m bandit -r src --ini .bandit -ll
python scripts/check_tracked_secrets.py
```

Integration tests that hit live APIs are excluded via `conftest.py` by default.

---

## Disclaimer

This is beta software that controls a live trading wallet. **There is no warranty.** You can lose funds if the bot misbehaves or if you misconfigure it. The authors accept no liability for financial losses. Always start with small capital and monitor the bot while you learn its behaviour.

---

## License

[MIT License](LICENSE). Copyright (c) 2026.
