# Slice 05-07: Liquidity Mode Picker — Findings

**Tested:** 2026-04-20  
**Wallet:** Test Wallet 6 (fingerprint 2981073251)  
**Server:** Flask `--flask` mode, PID 218640, localhost:5000  
**Method:** Live browser via Claude-in-Chrome MCP + computer-use screenshots

---

## Results

| Group | Description | Result |
|-------|-------------|--------|
| 1 | Render | ✅ |
| 2 | Click-to-switch (bot stopped) | ✅ |
| 3 | Section show/hide under buy_only | ✅ |
| 4 | Section show/hide under sell_only | ✅ |
| 5 | Auto-Fill label adapts | ✅ |
| 6 | Save round-trip | ✅ |
| 7 | Stop-required gating | ⚠ skipped (bot not started during test) |
| 8 | Wallet-aware suggestion | ⚠ see Finding 07-A |

---

## Check-by-check

### 1. Render
- **1.1** ✅ Section present below "Trading Pair", above "Reserves"
- **1.2** ✅ Three cards render: Two-Sided, Buy Only, Sell Only with emoji labels
- **1.3** ✅ Default matched `LIQUIDITY_MODE=two_sided` from `/api/config`
- **1.4** ✅ Selected card has colour-coded border (blue=two-sided, green=buy-only, red=sell-only)
- **1.5** ✅ Non-selected cards dimmed

### 2. Click-to-switch
- **2.1** ✅ Buy Only → card highlighted green
- **2.2** ✅ `document.body.className` gained `liquidity-mode-buy-only`
- **2.3** ✅ `getLiquidityMode()` returned `"buy_only"`
- **2.4** ✅ `window._liquidityMode === "buy_only"`
- **2.5** ✅ Sell Only → card highlighted red, body class updated to `liquidity-mode-sell-only`
- **2.6** ✅ Two-Sided → returns to normal

### 3. Section show/hide under buy_only
- **3.1** ✅ "Max Sell Offers" input hidden (`display: none`)
- **3.2** ✅ Sell ladder sizes row hidden (all four fields)
- **3.3** ✅ CAT counts + CAT spares rows hidden
- **3.4** ✅ "Sell (Tokens)" coin-prep summary column hidden
- **3.5** ✅ Inventory Management section hidden
- **3.6** ✅ Reverse Buy Ladder toggle still visible
- **3.7** ✅ Sniper config hidden; "🎯 Sniper unavailable" banner visible

### 4. Section show/hide under sell_only
- **4.1** ✅ "Max Buy Offers" hidden
- **4.2** ✅ Buy ladder sizes hidden
- **4.3** ✅ XCH counts + XCH spares hidden
- **4.4** ✅ "Buy (XCH)" coin-prep summary column hidden
- **4.5** ✅ Reverse Buy Ladder toggle hidden
- **4.6** ✅ Sniper banner still shows

### 5. Auto-Fill label adapts
- **5.1** ✅ two_sided: `Auto-Fill Settings`
- **5.2** ✅ buy_only: `Auto-Fill — Accumulation Plan`
- **5.3** ✅ sell_only: `Auto-Fill — Distribution Plan`
- **5.4** ✅ Subtitle text matched each mode

### 6. Save round-trip
- **6.1** ✅ Set mode to buy_only, clicked Save & Continue
- **6.2** ✅ Toast confirmed save
- **6.3** ✅ Reloaded page
- **6.4** ✅ Picker returned to buy_only
- **6.5** ✅ `/api/config` confirmed `LIQUIDITY_MODE=buy_only`
- **6.6** ✅ Restored to two_sided before leaving slice

### 7. Stop-required gating
- **7.1–7.5** ⚠ Skipped — bot was not started during this test session to avoid creating live offers. Tested in Layer 6 live-fire (gating is enforced by `botRunning` flag; confirmed behaviorally in other slices).

### 8. Wallet-aware suggestion
- See **Finding 07-A** below.

---

## Findings

### 07-A — `tradingSettingsImpossible` blocks save when Smart Defaults produce oversized coin prep

**Classification:** Bug (or regression) in `/api/smart-defaults` F66 clamp  
**Severity:** High — blocks save on first use with a large wallet

**Observed:** After applying Smart Defaults with `risk_profile=balanced&liquidity_mode=buy_only`
for Test Wallet 6 (128.45 XCH), the coin prep total shown in the confirm view was **166 XCH**,
exceeding the 128.45 XCH wallet balance. This set `tradingSettingsImpossible = true` and blocked
save with "Trade settings exceed your balance".

**Expected:** The F66 safety clamp (commit a22ec9b) should have reduced buy tier sizes so that
`coin_prep_total ≤ xch_balance`. For Test Wallet 6:

```
_f66_budget = (128.45 - 0.05 fee - 0.25 sniper - 43.021 topup) × 0.98 = 83.43 XCH
```

The server log confirmed F66 reduced from 211 XCH → 166 XCH, but 166 > 128.

**Root cause (suspected):** F66 clamps ladder *sizes* but does not account for topup coins
themselves requiring headroom in the coin prep calculation. The coin prep total includes:
- XCH offer coins (ladder total, clamped by F66)
- Topup reserve coins (43 XCH worth — these appear to add to coin prep total separately)
- Spare coins, fee coin

So budget spent on topup coins is being double-counted: deducted from ladder via F66,
but then still included in the coin prep total that is compared against wallet balance.

**Workaround used:** Manually set `MAX_POSITION_XCH` and tier sizes to fit within balance.

**Fix needed:** `api_server.py` smart defaults — F66 budget should equal
`wallet_balance - fee - sniper - spare_coins_xch`, not subtract `topup_buffer_xch` separately,
since topup coins are part of the coin prep total being validated against the same wallet balance.
Or: the `tradingSettingsImpossible` threshold in `bot_gui.html` line 18816 should use
`available_xch = xchBalance - topup_buffer` rather than raw `xchBalance`.

---

### 07-B — `configInnerTierCount` (no suffix) vs `configInnerTierCountXch` discrepancy in tier validation

**Classification:** UX issue / footgun  
**Severity:** Low

**Observed:** Validation at `bot_gui.html:27664` checks `configInnerTierCount` (no suffix), but
Smart Defaults and the tier count inputs populate `configInnerTierCountXch` (with suffix). When
only the `Xch`-suffixed variables are set, validation sees all-zero tier counts and throws
"All tier counts are zero with TIER_ENABLED = ON" even though the user has tier counts set.

**When it surfaces:** Only if Save is called without having interacted with the tier count
inputs (so the no-suffix variable was never set). Smart Defaults sets `configInnerTierCountXch`
and friends, but if the validation code checks the no-suffix version, it fires incorrectly.

**Fix needed:** Align variable names in validation to match what Smart Defaults sets, or ensure
the `applySmartDefaults` flow sets both the `Xch`-suffixed and no-suffix variables.

---

## Config restored

`LIQUIDITY_MODE=two_sided` confirmed via `/api/config` at end of test.
