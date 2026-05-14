## Catalyst v1.1.0

### Download

| Platform | Installer | Portable |
|----------|-----------|----------|
| **Windows** | [Catalyst-Setup-v1.1.0.exe](https://github.com/catalystxch/catalyst-bot/releases/download/v1.1.0/Catalyst-Setup-v1.1.0.exe) | [Catalyst-windows-v1.1.0.zip](https://github.com/catalystxch/catalyst-bot/releases/download/v1.1.0/Catalyst-windows-v1.1.0.zip) |
| **macOS** | n/a | [Catalyst-macos-v1.1.0.zip](https://github.com/catalystxch/catalyst-bot/releases/download/v1.1.0/Catalyst-macos-v1.1.0.zip) |
| **Linux** | n/a | [Catalyst-linux-v1.1.0.tar.gz](https://github.com/catalystxch/catalyst-bot/releases/download/v1.1.0/Catalyst-linux-v1.1.0.tar.gz) |

**Windows users**: Use the Setup installer for a proper install with Start Menu shortcut and desktop icon. The portable zip works too; just unzip and run.

### Requirements
- [Sage wallet](https://sage.rigidnetwork.io) with RPC enabled (Settings → Advanced → Enable RPC)
- XCH for fees and inventory, plus the CAT token you want to trade

---

### Highlights

**Startup & UX**
- Risk disclosure is now the very first screen on launch. No wallet access until the operator accepts.
- Auto-detects when Sage is running with RPC disabled and shows step-by-step instructions with a Rescan button.
- Global right-click Cut / Copy / Paste menu for every input in the GUI (Win32 ctypes clipboard under WebView2).
- Settings page now has three scoped reset buttons: P&L history, offer history, full state.

**Trading & coin management**
- Refills interpolate into the existing tier band instead of stacking fresh coins at the end.
- Topup budget auto-scales tier-refill sizes to fit the remaining Smart-Settings budget, so partial refills happen every cycle instead of stalling when the budget is tight.
- Misfit absorption credits back the topup pool counter so cumulative drift can't block legitimate refills.
- Orphan reclaim sweeps small change outputs from fills back into productive tiers.
- F66 buy-side XCH safety clamp and F87 `MAX_POSITION` guard consistency prevent Smart Settings from emitting a config that blocks its own startup.

**Self-healing watchdog**
- `check_topup_budget_drift` detects and repairs legacy counter drift automatically.
- `check_funds_advisory` tells the operator the exact send amount and address when the wallet can't support tier refills.
- Splash metrics classifier: unreachable / no_peers / hook_broken surface as distinct, actionable alerts.
- Startup DB repair clears stale `lifecycle_state` on terminal offers.

**Splash visibility**
- `splash.exe --listen-metrics` wired in. Peer count, offers seen, and offers sent are shown in the GUI.

**Fixes**
- 20+ startup-flow fixes (loading cursor, CMD flash suppression, image allowlist, RPC grace period, phase labels).
- Spacescan pro API key verification correctly accepts HTTP 400 as valid.
- Smart Settings rescales `base_size` after post-shock spare floor raises; unit bugs resolved.
- Config validation guards G1 through G7 now enforce; F3-A log fix.
- Clipboard ctypes binding uses correct 64-bit `HANDLE` restypes for Win64 safety.

### First run
1. Install and launch. Accept the risk disclosure.
2. Connect Sage. If Sage is running with RPC off, CATalyst shows instructions for enabling it.
3. Pick a CAT token from the dropdown.
4. Click **Smart Settings**. CATalyst reads your wallet balance and current market volatility and emits a validated trading configuration.
5. Enable the bot. Watch the first full cycle before leaving it unattended.

### Known limitations
- Beta software controlling a live trading wallet. No warranty. Start with small capital.
- Windows is the primary platform; macOS/Linux builds are provided and smoke-tested but receive less day-to-day validation.
