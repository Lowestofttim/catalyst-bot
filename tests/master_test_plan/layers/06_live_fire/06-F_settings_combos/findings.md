# Phase F — Settings Combinations Live-Fire Results

**Tested:** 2026-04-20  
**Wallet:** Test Wallet 6 (fingerprint 2981073251)  
**CAT:** Monkeyzoo Token (MZ), asset_id b8edcc6a…

## Scenarios tested

| # | Profile | Mode | Target buy | Target sell | Result | Notes |
|---|---------|------|-----------|------------|--------|-------|
| F1 | Conservative | Two-sided | 8 | 8 | ✅ | Clean start, both sides created within 2 cycles |
| F2 | Balanced | Buy-only | 24 | 0 | ✅ | `MAX_ACTIVE_SELL=0`, zero sell offers confirmed |
| F3 | Aggressive | Two-sided | 24 | 23 | ⚠ | Buy created; **sells permanently blocked** — see Finding F3-A |
| F4 | Balanced | Sell-only | 0 | 24 | ✅ | Sells created after config fix — see Finding F4-A |

---

## Findings

### F3-A — Inherited net-short position blocks sell side in Aggressive profile

**Classification:** Expected behaviour, test artifact  
**Severity:** Low (correct risk management; only surfaces with stale history)

**Observed:** After switching from F1/F2 to Aggressive + Two-sided with 149 historical
fills present, `should_enable_side("sell")` returned False for every cycle.
`open_sells` stayed 0 despite target=23.

**Root cause:** `risk_manager.py:950` uses **signed** position:
```python
position_xch = self._net_position_cat * price
```
The 149 historical fills had net-short CAT position: `_net_position_cat × price = -29.18 XCH`.
`effective_limit × 0.9 = 28.89 XCH`. Gate condition:
```python
if side == "sell" and position_xch < -effective_limit * 0.9:
    return False  # triggered: -29.18 < -28.89
```
Sniper's `_should_snipe_side("sell")` also called `should_enable_side` → sell probe never
fired → no sell_tid → "Both probes survived" log message misleadingly fired buy-only.

**Fix:** None required — this is correct circuit-breaker behaviour protecting against
deepening an already-short position. The test used `/api/pnl/reset` to clear history
before proceeding to F4.

**Side finding:** When `sell_tid = None` (sell probe skipped), `bot_loop.py:1381-1382`:
```python
sell_required = bool(sell_tid)  # False when sell probe not placed
```
fires the "Both probes survived" confirmation with only a buy probe. The log message
text "Both probes survived" is misleading. Log it as a UX issue, not a correctness bug.

---

### F4-A — Smart defaults returns inconsistent `max_position_xch` for sell-only mode

**Classification:** Bug in `/api/smart-defaults`  
**Severity:** Medium (blocks all sell creation on first start)

**Observed:** Smart defaults for `risk_profile=balanced&liquidity_mode=sell_only` returned:
- `max_position_xch: 19.3`
- `max_active_sell: 24`
- Sell ladder total (inner 10×2.52 + mid 7×1.40 + outer 5×0.77 + extreme 2×0.35) = **39.5 XCH**

Applying these defaults caused `offer_manager.py:1432` to block ALL sells:
```
net new exposure 34.2384 XCH → projected 34.2384 XCH > hard limit 21.2300 XCH
(110% of MAX_POSITION_XCH=19.3). Allow position to unwind via the opposite side first.
```
Bot ran 9+ loops creating zero sell offers.

**Fix applied:** `MAX_POSITION_XCH` increased to 40 XCH manually for the test.
A spawned task exists to fix the smart defaults calculation to ensure
`max_position_xch ≥ (total_sell_ladder_value / 0.9)` for sell-only mode.

**Code location:** `offer_manager.py:1430–1457` — `add_long_dir` check for `side="sell"`
blocks when `net_pos_cat <= 0` and projected exposure > `max_position_xch × 1.1`.

---

## Config restored after each scenario

After all Phase F scenarios, config was restored to two-sided with MAX_POSITION_XCH=40
and fills reset to 0 via `/api/pnl/reset`.

---

## Fixes applied

**F3-A** — `bot_loop.py` commit ea991ec: "Both probes survived" log message is now
conditional. When `sell_required=False` (sell probe not placed), message reads:
"Buy probe survived (sell probe not placed — sniper single-sided or no CAT sniper coins)".

**F4-A** — `api_server.py` commit be22e3f (smart defaults): added post-calculation
guard that bumps `max_position_xch` to `ceil(total_sell_ladder_xch / 0.9 × 10) / 10`
whenever the recommended sell ladder would exceed the position limit.
(Fix was in code on disk; server restart required to pick up.)
