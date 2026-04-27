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

### From the installer (recommended)

1. Download `Catalyst-Setup-v*.exe` from the [latest release](https://github.com/Lowestofttim/catalyst-bot/releases/latest).
2. Run it. The installer places CATalyst in Program Files and adds a desktop shortcut.
3. Launch CATalyst. On first run it will prompt for your Sage wallet connection and walk you through Smart Settings.

### From source

```bash
git clone https://github.com/Lowestofttim/catalyst-bot.git
cd catalyst-bot
pip install -r requirements.txt
cp .env.example .env
# Edit .env to fill in SAGE_CERT_PATH and SAGE_KEY_PATH
python desktop_app.py
```

---

## Configuration

All settings live in `.env`, but you rarely edit it by hand. The required fields are just the wallet paths:

| Setting | What it does |
|---------|-------------|
| `SAGE_RPC_URL` | Sage wallet RPC endpoint (default `https://127.0.0.1:9257`) |
| `SAGE_CERT_PATH` / `SAGE_KEY_PATH` | Path to Sage's mTLS client cert and key |
| `CAT_ASSET_ID` | The CAT you want to trade (filled in automatically when you pick a token in the GUI) |

Every other trading parameter (spread, offer count, tier sizes, reserves, topup budgets) is configured via **Smart Settings** in the GUI. Smart Settings reads your wallet balance and current market volatility and emits a validated configuration in one click. You can override any individual field afterwards.

> **Security:** `.env` contains wallet cert paths. Never commit it. The `.gitignore` excludes it by default.

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
| Browser | `python desktop_app.py --flask` | Server-only, open in any browser at `http://localhost:5000`. |
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
