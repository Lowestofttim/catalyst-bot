# CATalyst

**Automated liquidity for Dexie, running from your own Chia wallet.**

Providing liquidity on [Dexie](https://dexie.space), Chia's main DEX, means
constantly adjusting offers as the market moves and replacing the ones that
fill. CATalyst handles that work for you. Choose the CAT you want to trade, set
your capital budget, and it maintains a live bid/ask ladder around the market
price, requotes as conditions change, and refills completed offers.

CATalyst runs locally as a desktop application and connects to your
[Sage wallet](https://sagewallet.net/): your keys, your coins, your trades.

**Status:** Beta, actively used in production. No warranty. Use at your own risk.

### [Download the latest release](https://github.com/Lowestofttim/catalyst-bot/releases/latest)

---

## What It Does

Market making means posting both buy and sell offers around the current market
price, then earning the spread as orders fill. Doing this well on Chia is hard:

- Offers are native blockchain assets, not database rows, so every quote move
  costs a transaction.
- Wallet coins must be pre-split into the right denominations before offers can
  be created.
- Fills can appear through Dexie, mempool, wallet, and on-chain signals at
  different times.
- Competitors move the book constantly, and arbitrageurs sweep gaps quickly.

CATalyst handles that operational load. It prepares wallet coins, builds the
ladder, verifies fills, and keeps the book live through reconnects, API outages,
and market shocks.

---

## Features

### Trading

- **Tiered ladder.** Inner, mid, outer, and extreme bands with configurable size
  and count per tier, per side.
- **Dynamic spreads.** Adapts to realised volatility, inventory skew, and
  competitor depth.
- **Smart Settings.** One-click capital planning based on wallet balance and
  market conditions.
- **Sniper probes.** Detects arbitrage gaps between Dexie and TibetSwap AMM and
  fires targeted orders to capture them.
- **Gap-close cascades.** Closes large market moves in staged steps instead of a
  single shock requote.
- **Mempool watch.** Spots TibetSwap swaps before they confirm on chain and
  preempts price moves.

### Execution and Safety

- **Multi-source fill verification.** Spacescan, Sage, and Dexie fallback chain.
  An offer is not recorded as filled until at least one authoritative source
  confirms.
- **Circuit breakers.** Hard price bands, step-change guards, sweep detection,
  and per-cycle cancel/create caps.
- **Dynamic price limits.** Tracks a live reference price and rejects quotes
  outside a configurable band.
- **Risk disclosure.** On first run, the operator must accept an on-screen
  disclosure before the bot can be enabled.

### Coin Management

- **Automatic UTXO splitting.** A background worker keeps the wallet supplied
  with the right size coins for each tier.
- **Proactive drip topup.** Refills each tier at 75% utilisation rather than
  waiting for exhaustion.
- **Orphan reclaim.** Sweeps small change outputs from fills back into
  productive tiers.
- **Budget autoscale.** Performs partial refills when the capital budget is
  tight, instead of stalling the whole ladder.

### Operations

- **Native desktop app.** System tray, notifications, and background operation.
- **Splash P2P.** Broadcasts offers directly to other Splash nodes for
  private-mempool distribution.
- **Self-healing watchdog.** Detects stuck state, stale lifecycle flags, and
  budget drift; repairs them without restarts.
- **Data management.** Separate resets for P&L history, offer history, or full
  state, directly from the GUI.
- **Update checker.** Polls GitHub for new releases.

---

## Requirements

- Windows 10/11 (64-bit), macOS, or Linux.
- [Sage wallet](https://sagewallet.net/) installed with RPC enabled
  (Settings -> Advanced -> Enable RPC).
- XCH for fees and inventory, plus the CAT token you want to trade.
- Python 3.10+ if running from source. Packaged releases have no external Python
  requirement.

Release packages are published on the
[Releases page](https://github.com/Lowestofttim/catalyst-bot/releases).

---

## Quick Start

CATalyst is local-first software. Each operator runs their own copy on the same
computer as Sage wallet. GitHub hosts the source code and release downloads; it
does not provide a hosted trading service.

For wallet safety, the browser/API interface is loopback-only by default.
`127.0.0.1` means "this computer", so open the dashboard from the same machine
where CATalyst is running.

### From the Installer (Recommended)

1. Download `Catalyst-Setup-v*.exe` from the
   [latest release](https://github.com/Lowestofttim/catalyst-bot/releases/latest).
2. Run the installer. It places CATalyst in Program Files and adds a desktop
   shortcut.
3. Launch CATalyst on the same computer as Sage wallet. On first run it checks
   the Sage connection, asks you to choose a wallet fingerprint, and guides you
   through Smart Settings.

### From Source on Windows

Use this path if you want the current source code or plan to develop the app.
Run these commands on the same PC as Sage wallet:

```powershell
git clone https://github.com/Lowestofttim/catalyst-bot.git
cd catalyst-bot
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python desktop_app.py --flask
```

Then open `http://127.0.0.1:5000/` in a browser on that same PC. To use the
native desktop window instead, run:

```powershell
python desktop_app.py
```

### From Source on macOS or Linux

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

### First Launch

CATalyst creates a per-user `.env` automatically in the app data directory.
Normal users should not need to copy or edit `.env` by hand.

Startup checks Sage, waits for RPC if needed, and asks you to choose a wallet
fingerprint in the GUI. CAT selection and Smart Settings then save the trading
configuration as you set up the app.

### Changing the Browser Port

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

### Access Denied or Loopback-Only Messages

CATalyst only accepts local browser/API requests by default. If you see an
access warning, check that:

- CATalyst is running on the computer opening the browser.
- You are using `http://127.0.0.1:5000/`, not another PC's IP address, a
  browser-preview URL, or a forwarded port.
- Sage wallet RPC is enabled locally in Sage Settings -> Advanced.
- If Sage certificate auto-detection fails, use the in-app setup prompt or edit
  the local `.env` as a fallback.

Direct API clients also need CATalyst's per-run local write token. The web page
handles this automatically; custom scripts must supply the token themselves.

---

## Configuration

CATalyst creates a per-user `.env` on first launch and updates it through the
GUI. Most operators never need to edit it directly:

| Setting | What it does |
|---------|--------------|
| `SAGE_RPC_URL` | Sage wallet RPC endpoint. Default: `https://127.0.0.1:9257`. |
| `SAGE_CERT_PATH` / `SAGE_KEY_PATH` | Optional fallback paths if Sage certificate auto-detection fails. |
| `CAT_ASSET_ID` | The CAT to trade. Written when you pick a token in the GUI. |

Every other trading parameter, including spread, offer count, tier sizes,
reserves, and topup budgets, is configured through **Smart Settings** in the GUI.
Smart Settings reads your wallet balance and current market conditions, then
produces a validated configuration in one click. You can override individual
fields afterwards.

> **Security:** `.env` can contain local wallet paths. Never commit it. The
> `.gitignore` excludes it by default.

---

## How It Works

```mermaid
flowchart TD
    PE[Price Engine] -->|reference price| RM[Risk Manager]
    RM -->|quote targets| OM[Offer Manager]
    OM -->|offer files| SW[Sage Wallet]
    OM -->|posts offers| DX[Dexie]
    SW --> CP[Coin Prep]
    DX --> FT[Fill Tracker]
    SW --> FT
    FT --> OM
```

The trading loop runs every 45 to 90 seconds:

1. Fetch the latest mid price from TibetSwap and Dexie.
2. Check each side of the book for new fills and verify them on-chain.
3. Decide whether the book needs to be requoted because of price drift,
   inventory skew, or tier exhaustion.
4. Cancel stale offers, create replacements, and post them to Dexie and Splash.
5. Top up UTXOs when a tier is running low.

Between cycles, the coin prep subprocess runs asynchronously and the mempool
watcher polls for TibetSwap swaps that may move the market.

---

## Architecture

| Module | Role |
|--------|------|
| `desktop_app.py` | Entry point. Boots Flask, PyWebView window, and system tray. |
| `api_server.py` | HTTP API and Server-Sent Events for the GUI. |
| `bot_loop.py` | Main trading loop orchestrator. |
| `bot_gui.html` | Single-file dashboard UI. |
| `offer_manager.py` | Offer creation, cancellation, and rolling requote. |
| `fill_tracker.py` | Fill detection and multi-source verification. |
| `price_engine.py` | Price oracle using TibetSwap and Dexie. |
| `risk_manager.py` | Circuit breakers, position limits, and spread calculation. |
| `coin_manager.py` | UTXO tracking, tier classification, and topup. |
| `coin_prep_worker.py` | Async coin splitting subprocess. |
| `wallet_sage.py` | Sage wallet RPC adapter. |
| `dexie_manager.py` | Dexie API integration. |
| `spacescan.py` | On-chain verification via Spacescan. |
| `sniper.py` | Arbitrage gap probing. |
| `splash_manager.py` | Splash P2P node integration. |
| `smart_defaults.py` | Capital-aware config generator. |
| `bot_health.py` | Self-healing watchdog. |
| `database.py` | SQLite state layer using WAL mode. |
| `config.py` | Typed `.env` loader with hot reload. |

---

## Running Modes

| Mode | Command | Use case |
|------|---------|----------|
| Desktop | `python desktop_app.py` | Native window and system tray. |
| Browser | `python desktop_app.py --flask` | Server-only mode for `http://127.0.0.1:5000/`. |
| Dev | `python desktop_app.py --dev` | Desktop window and browser access together. |

---

## Data Location

CATalyst stores its SQLite database, logs, and runtime state in the OS standard
app-data directory:

- **Windows:** `%APPDATA%\Catalyst\`
- **macOS:** `~/Library/Application Support/Catalyst/`
- **Linux:** `~/.local/share/Catalyst/`

Override with the `CMM_DATA_DIR` environment variable.

---

## Building from Source

```bash
python build.py              # full clean build, produces dist/Catalyst/
python build.py --no-clean   # skip cleaning for faster iteration
```

The local build output stays on the machine that ran `python build.py`. To share
builds with users, publish a GitHub Release or push a `v*` tag. The release
workflow builds Windows, macOS, and Linux packages, plus a Windows installer, and
uploads them to a new GitHub Release.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q --ignore=tests/e2e --disable-warnings
python -m ruff check . --select E9,F821
python -m bandit -r src --ini .bandit -ll
python scripts/check_tracked_secrets.py
```

Live API integration scripts are excluded from normal test collection.

---

## Disclaimer

This is beta software that controls a live trading wallet. **There is no
warranty.** You can lose funds if the bot misbehaves or if you misconfigure it.
The authors accept no liability for financial losses. Start with small capital
and monitor the bot while you learn its behaviour.

---

## License

[MIT License](LICENSE). Copyright (c) 2026.
