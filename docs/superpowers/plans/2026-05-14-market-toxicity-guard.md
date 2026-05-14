# Market Toxicity Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a live adverse-selection guard that scores toxic market flow each bot loop, widens spreads, and temporarily blocks new offers on the risky side.

**Architecture:** Add a small `market_toxicity.py` module that owns scoring and produces a serializable snapshot. `bot_loop.py` builds the context from existing price, fill, wallet, sweep, and `MarketIntel` data. `risk_manager.py` consumes the snapshot for spread multipliers, side throttling, market health, and API/dashboard state.

**Tech Stack:** Python dataclasses, `Decimal`, existing `cfg` config singleton, existing `slog`/`log_event` logging, existing Flask/SSE status path, pytest.

---

### Task 1: Scorer Core

**Files:**
- Create: `src/catalyst/market_toxicity.py`
- Test: `tests/test_market_toxicity.py`

- [ ] **Step 1: Write failing scorer tests**

Create tests for these behaviours:

```python
from decimal import Decimal

from market_toxicity import MarketToxicityGuard, ToxicityContext


def ctx(**overrides):
    base = dict(
        now=1000.0,
        loop_count=7,
        mid_price=Decimal("0.01"),
        tibet_price=Decimal("0.01"),
        dexie_price=Decimal("0.01"),
        arb_gap_bps=Decimal("0"),
        open_offers=[],
        recent_fills=[],
        market_intel={},
        orderbook_snapshot={},
        inventory_state={},
        wallet_health={},
        recent_sweep_events=[],
        liquidity_mode="two_sided",
    )
    base.update(overrides)
    return ToxicityContext(**base)


def test_one_sided_mode_not_toxic_without_bad_flow():
    guard = MarketToxicityGuard()
    snap = guard.update(ctx(liquidity_mode="buy_only"))
    assert snap.level == "normal"
    assert snap.score == 0
    assert snap.throttled_sides == []


def test_fast_same_side_fills_raise_side_score():
    guard = MarketToxicityGuard()
    snap = guard.update(ctx(recent_fills=[
        {"side": "sell", "age_secs": 12, "size_xch": "0.02"},
        {"side": "sell", "age_secs": 18, "size_xch": "0.03"},
    ]))
    assert snap.sell_score >= 55
    assert "fast_fills" in {r["key"] for r in snap.reasons}


def test_small_balance_exposure_can_throttle():
    guard = MarketToxicityGuard()
    snap = guard.update(ctx(
        open_offers=[
            {"side": "buy", "size_xch": "0.35"},
            {"side": "buy", "size_xch": "0.30"},
        ],
        inventory_state={"xch_spendable": "1.0", "cat_spendable_xch": "0.2"},
        recent_fills=[{"side": "buy", "age_secs": 15, "size_xch": "0.05"}],
    ))
    assert snap.buy_score >= 75
    assert "buy" in snap.throttled_sides
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `pytest tests/test_market_toxicity.py -q`

Expected: import failure because `market_toxicity.py` does not exist.

- [ ] **Step 3: Implement the scorer**

Create dataclasses:

```python
@dataclass
class ToxicityContext:
    now: float
    loop_count: int
    mid_price: Decimal
    tibet_price: Decimal = Decimal("0")
    dexie_price: Decimal = Decimal("0")
    arb_gap_bps: Decimal = Decimal("0")
    open_offers: list[dict] = field(default_factory=list)
    recent_fills: list[dict] = field(default_factory=list)
    market_intel: dict = field(default_factory=dict)
    orderbook_snapshot: dict = field(default_factory=dict)
    inventory_state: dict = field(default_factory=dict)
    wallet_health: dict = field(default_factory=dict)
    recent_sweep_events: list[dict] = field(default_factory=list)
    liquidity_mode: str = "two_sided"
```

Implement `MarketToxicityGuard.update()` with transparent side scores, reason dictionaries, score decay, and configurable thresholds read from `cfg`.

- [ ] **Step 4: Run scorer tests**

Run: `pytest tests/test_market_toxicity.py -q`

Expected: all scorer tests pass.

---

### Task 2: Risk Manager Integration

**Files:**
- Modify: `src/catalyst/risk_manager.py`
- Test: `tests/test_risk_manager_toxicity.py`

- [ ] **Step 1: Write failing risk-manager tests**

Test that a high toxicity snapshot widens the risky side and blocks that side:

```python
from decimal import Decimal

from market_toxicity import ToxicitySnapshot
from risk_manager import RiskManager


def test_toxicity_multiplier_widens_before_clamp(monkeypatch):
    rm = RiskManager()
    monkeypatch.setattr("risk_manager.cfg.DYNAMIC_SPREAD_ENABLED", True, raising=False)
    monkeypatch.setattr("risk_manager.cfg.BASE_SPREAD_BPS", Decimal("800"), raising=False)
    monkeypatch.setattr("risk_manager.cfg.MIN_SPREAD_BPS", Decimal("300"), raising=False)
    monkeypatch.setattr("risk_manager.cfg.MAX_SPREAD_BPS", Decimal("3000"), raising=False)
    monkeypatch.setattr("risk_manager.cfg.MIN_EDGE_BPS", Decimal("200"), raising=False)
    rm.set_market_toxicity(ToxicitySnapshot(score=82, buy_score=82, sell_score=12, level="high",
                                            buy_spread_multiplier=Decimal("1.75"),
                                            sell_spread_multiplier=Decimal("1.20"),
                                            throttled_sides=["buy"], throttle_until={"buy": 1300.0},
                                            reasons=[], suggested_action="Throttle buy"))
    assert rm.get_adjusted_spread("buy") == Decimal("0.14")
    assert rm.should_enable_side("buy", Decimal("0.01")) is False
    assert rm.should_enable_side("sell", Decimal("0.01")) is True
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `pytest tests/test_risk_manager_toxicity.py -q`

Expected: `set_market_toxicity` missing.

- [ ] **Step 3: Add risk-manager hooks**

Add:

```python
def set_market_toxicity(self, snapshot): ...
def get_market_toxicity(self) -> dict: ...
def _apply_market_toxicity(self, spread: Decimal, side: str) -> Decimal: ...
```

Call `_apply_market_toxicity()` after existing spread adjustments and before final clamps. Extend `should_enable_side()` to return `False` when the snapshot throttles that side. Add toxicity metrics and amber/red market-health conditions.

- [ ] **Step 4: Run risk tests**

Run: `pytest tests/test_risk_manager_toxicity.py -q`

Expected: pass.

---

### Task 3: Bot Loop Context Wiring

**Files:**
- Modify: `src/catalyst/bot_loop.py`
- Test: `tests/test_bot_loop_market_toxicity.py`

- [ ] **Step 1: Write failing bot-loop tests**

Create a `BotLoop` object with `object.__new__`, attach fake `market_toxicity_guard`, `risk_manager`, `market_intel`, `coin_manager`, and `offer_manager`, then call `_update_market_toxicity(...)`. Assert that:

```python
fake_guard.last_context.recent_fills[0]["side"] == "buy"
fake_risk_manager.snapshot is fake_guard.snapshot
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `pytest tests/test_bot_loop_market_toxicity.py -q`

Expected: `_update_market_toxicity` missing.

- [ ] **Step 3: Wire guard lifecycle**

Import and instantiate `MarketToxicityGuard` in `BotLoop.__init__`. Add `_update_market_toxicity()` after fill detection and before requote/create logic. Build context from:

- current `price_data`, `mid_price`, and `arb_gap`
- `buy_fills` / `sell_fills`
- current wallet `open_buys` / `open_sells`
- `market_intel.get_market_summary()`
- `market_intel.get_orderbook_snapshot()`
- coin manager inventory summary and current coin status
- sweep protection state
- `cfg.LIQUIDITY_MODE`

If context or scoring fails, log `toxicity_guard_error` and continue the loop with the previous snapshot.

- [ ] **Step 4: Run bot-loop tests**

Run: `pytest tests/test_bot_loop_market_toxicity.py -q`

Expected: pass.

---

### Task 4: Config And UI Surface

**Files:**
- Modify: `src/catalyst/config.py`
- Modify: `.env.example`
- Modify: `bot_gui.html`

- [ ] **Step 1: Add config defaults**

Add these keys near other risk/spread runtime settings:

```text
MARKET_TOXICITY_ENABLED=true
TOXICITY_WIDEN_START=30
TOXICITY_ELEVATED_START=55
TOXICITY_THROTTLE_START=75
TOXICITY_CANCEL_START=90
TOXICITY_THROTTLE_SECS=120
TOXICITY_DECAY_PER_LOOP=8
TOXICITY_MAX_SPREAD_MULTIPLIER=2.0
TOXICITY_MIN_THROTTLE_SIGNALS=2
TOXICITY_CANCEL_ENABLED=false
```

- [ ] **Step 2: Expose status**

Add toxicity snapshot to bot status via `risk_manager.get_inventory_state()` and market-health metrics. In `bot_gui.html`, add a compact Command Centre metric box:

```html
<div class="cc-metric-box">
  <div class="cc-metric-label">Toxicity</div>
  <div class="cc-metric-val" id="ccToxicity">pending</div>
</div>
```

Render using `textContent`, never `innerHTML`.

- [ ] **Step 3: Verify UI parsing**

Run: `python -m compileall src/catalyst`

Expected: no Python syntax errors. UI change is HTML/JS only and should render from existing status payload.

---

### Task 5: Verification

**Files:**
- Existing test suite only.

- [ ] **Step 1: Run focused tests**

Run:

```bash
pytest tests/test_market_toxicity.py tests/test_risk_manager_toxicity.py tests/test_bot_loop_market_toxicity.py -q
```

Expected: pass.

- [ ] **Step 2: Run related bot tests**

Run:

```bash
pytest tests/test_bot_loop_daily_reconcile.py tests/test_bot_loop_probe_anchor.py tests/test_bot_loop_recovery_mode.py -q
```

Expected: pass or report unrelated pre-existing failures.

- [ ] **Step 3: Compile**

Run: `python -m compileall src/catalyst`

Expected: all files compile.
