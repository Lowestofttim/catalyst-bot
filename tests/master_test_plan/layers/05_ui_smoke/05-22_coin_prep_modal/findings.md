# Slice 05-22: Coin Prep Modal — Findings

**Tested:** 2026-04-20  
**Wallet:** Test Wallet 6 (fingerprint 2981073251)  
**Server:** Flask `--flask` mode, localhost:5000  
**Method:** Live browser via Claude-in-Chrome MCP + computer-use screenshots  
**Status:** PARTIAL — Groups 1 (confirm view) only. Groups 2–7 not yet tested.

---

## Results

| Group | Description | Result |
|-------|-------------|--------|
| 1 | Launch confirm view | ✅ |
| 2 | History-choice modal | ⏳ not tested |
| 3 | Progress view | ⏳ not tested |
| 4 | Complete view | ⏳ not tested |
| 5 | Error view | ⏳ not tested |
| 6 | Dismiss / escape | ⏳ not tested |
| 7 | Parallel-run guards | ⏳ not tested |

---

## Check-by-check

### 1. Launch confirm view
- **1.1** ✅ "Prepare Coins" button on Dashboard → modal opened (`#coinPrepConfirmOverlay` active)
- **1.2** ✅ Shows XCH side + CAT side summary (Coins / Sizes / Total columns visible)
- **1.3** ✅ "Yes, Prepare Coins" button shows headroom percent label
- **1.4** ✅ Three buttons present: Back to Settings, Skip Coin Prep, Yes Prepare Coins
- **1.5** ✅ Back button performs view-swap back to Settings (modal does not close; inner view changes)

### 2–7
Not tested in this session. Testing stopped by user request (2026-04-20) — user will drive
remaining tests manually and report issues as found.

---

## Findings

### 22-A — `showCoinPrepConfirm()` requires config object argument

**Classification:** Developer note (not a user-facing bug)  
**Severity:** Low

**Observed:** Calling `showCoinPrepConfirm()` from the browser console without arguments throws
`TypeError: Cannot read properties of undefined`. The function expects a config object with
lowercase keys.

**Workaround for manual testing:**
```javascript
fetch('/api/config')
  .then(r => r.json())
  .then(config => {
    const cfgLower = {};
    for (const [k, v] of Object.entries(config)) cfgLower[k.toLowerCase()] = v;
    showCoinPrepConfirm(cfgLower, 'recommended', 'Test run');
  });
```

---

### 22-B — Coin prep confirm view blocked by `tradingSettingsImpossible` when Smart Defaults overshoot

**Classification:** Downstream of Finding 07-A  
**Severity:** High (same root cause — see 05-07 findings)

When Smart Defaults produce a coin prep total exceeding wallet balance (166 XCH vs 128 XCH for
Test Wallet 6), the confirm view's "Yes, Prepare Coins" button is blocked before the modal even
opens, because `tradingSettingsImpossible = true` prevents Save from completing.

This is the same issue as 07-A. Fix 07-A to unblock this flow.

---

## Outstanding test coverage

Groups 2–7 need a live run with:
- A DB that has fills (`has_data: true`) for the history-choice modal path
- A successful coin prep run to verify progress → complete view
- A forced failure (high `XCH_RESERVE`) for the error view
- ESC key and outside-click tests for dismiss behaviour
- Rapid double-click test for parallel-run guards
