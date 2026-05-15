# Market Toxicity Guard Design

## Goal

Add an active adverse-selection protection layer to CATalyst. The guard should detect when the current market flow looks dangerous, then immediately make quoting safer by widening spreads and temporarily throttling the side that appears to be getting picked off.

This is not a pure Avellaneda-Stoikov implementation. CATalyst operates in the Chia Offers ecosystem, where offers are asynchronous peer-to-peer objects, public discovery happens through Dexie/Splash, AMM price discovery comes from TibetSwap, and signing/cancellation state lives in Sage. The guard should be custom to those constraints.

## Ecosystem Model

CATalyst sits between four moving parts:

- **Chia Offers:** peer-to-peer offers that can be accepted by anyone after being shared. They are not exchange orders with instant cancel/replace guarantees.
- **Dexie/Splash:** public offer discovery and orderbook/indexing. Dexie can show public offers and price references, but wallet spendability and settlement still belong to Chia/Sage.
- **TibetSwap:** an AMM pool whose reserve price can move independently from Dexie offers. AMM moves can make CATalyst's posted offers stale.
- **Sage:** the local wallet/signing layer. Sage creates offers, cancels offers, tracks coin state, and can temporarily lock coins while spends/cancels settle.

The toxic-flow path we care about is:

```text
TibetSwap/Dexie market moves
        -> CATalyst offers become too attractive
        -> takers fill one side quickly
        -> inventory moves in the wrong direction
        -> CATalyst must widen or stop adding that exposure
```

## Scope

Build **Option B** from the brainstorming session:

- Score toxicity every bot loop.
- Widen spreads immediately when toxicity rises.
- Temporarily throttle new offer creation on the risky side when toxicity is high.
- Expose the score and reasons in logs/API/dashboard state.
- Design an emergency-cancel interface, but keep automatic cancellation out of v1 default behavior because Chia cancellations have wallet/chain timing risk.

## Non-Goals

- No hidden ML model.
- No full academic optimal-control quoting engine in v1.
- No default automatic mass cancellation based only on the score.
- No dependency on live external APIs in unit tests.
- No broad refactor of `bot_loop.py` beyond the integration points needed.

## New Component

Add a focused module, likely `src/catalyst/market_toxicity.py`.

Primary class:

```python
class MarketToxicityGuard:
    def update(context: ToxicityContext) -> ToxicitySnapshot
    def get_snapshot() -> ToxicitySnapshot
    def is_side_throttled(side: str, now: float | None = None) -> bool
```

Data objects:

```python
ToxicityContext:
    now
    loop_count
    mid_price
    dexie_price
    tibet_price
    arb_gap_bps
    open_offers
    recent_fills
    recent_created_offers
    inventory_state
    wallet_health
    dexie_market_quality
    market_intel
    orderbook_snapshot
    recent_sweep_events

ToxicitySnapshot:
    score
    buy_score
    sell_score
    level
    buy_spread_multiplier
    sell_spread_multiplier
    throttled_sides
    throttle_until
    reasons
    suggested_action
```

Each reason should include a machine key, side, score contribution, and short human-readable detail. This keeps the feature explainable during beta testing.

## Signals

Start with signals CATalyst can already measure or cheaply derive.

### Fast Fills

If an offer fills shortly after it was created, score the filled side. Fast fills are not automatically bad, but repeated fast fills suggest CATalyst may be quoting too attractively.

Example:

- fill within 60 seconds: mild score
- fill within 20 seconds: stronger score
- multiple fast fills in a short window: additional score

### One-Sided Fill Streak

If recent fills are mostly buys or mostly sells, score that side. One-sided flow can be normal, but combined with inventory drift or price movement it becomes toxic.

The guard must distinguish between two cases:

- an intentional one-sided setup, such as buy-only accumulation or sell-only distribution
- an unintentional one-sided market event where takers repeatedly hit the same side because CATalyst is stale or too tight

For intentional one-sided modes, the missing opposite side is not itself toxic. The guard should still score fast fills, adverse moves, balance pressure, and sweep risk on the active side.

### Post-Fill Adverse Move

Compare the current mid/Tibet price with the price at or near fill time.

- If CATalyst buys CAT and price then drops, buy-side flow was adverse.
- If CATalyst sells CAT and price then rises, sell-side flow was adverse.

This is one of the strongest signals because it asks whether fills were followed by movement against us.

### Dexie/Tibet Dislocation

Reuse the existing arb-gap data. A widening Dexie/Tibet gap suggests one venue may be stale or under-arbed. This should increase both-side caution and may emphasize the side that is close to being picked off.

### AMM Move Against Quotes

If TibetSwap moves materially away from CATalyst's quoted mid after offers were posted, score the side that is now exposed.

### Inventory Accumulation

If fills push inventory toward the max position, increase toxicity on the side that would worsen the imbalance.

### Small-Balance Sweep Risk

Small wallets are more exposed because one public taker can consume a meaningful share of the whole working balance. The guard should explicitly score this, instead of only looking at price movement after the damage happens.

For each side, calculate:

- open exposure on that side as XCH notional
- available spendable balance and total confirmed balance from Sage/Chia
- percentage of available balance locked or exposed in live offers
- largest own offer and largest same-side offer cluster
- recent sweep events from `SweepCoordinator`
- visible public Dexie depth and whale-sized offers from `MarketIntel`

If one taker or one same-block sweep could consume a large percentage of the user's working balance, the guard should widen and may throttle that side sooner. This matters most for beta testers running small amounts, thin CAT markets, and newly prepared wallets with only a few usable coins.

Suggested initial thresholds:

```text
exposed side > 25% of spendable balance: mild
exposed side > 50% of spendable balance: elevated
recent same-block sweep on same side: elevated to high, depending on size
single offer > 35% of spendable balance: elevated
single offer > 60% of spendable balance: high
```

These thresholds should be configurable after the first implementation if live testing shows they are too conservative.

### Public Market Depth

Use the existing `MarketIntel` snapshot as a full-public-market signal:

- best competing bid and ask
- competitor spread
- overall spread including CATalyst offers
- buy and sell depth in XCH
- thin side
- whale orders
- orderbook age, refresh count, and truncation flags

This is not a complete view of every possible buyer or seller because Chia offers can be shared privately and not every taker intent appears on Dexie before it fills. The guard should treat Dexie as the best public orderbook view, then combine it with TibetSwap reserve/slippage data and CATalyst's own fill/sweep history.

If Dexie depth is stale, empty, truncated, or API-limited, that should raise data-quality caution rather than pretending the market is safe.

### Market Data Quality

Dexie crossed bid/ask, missing live bid/ask, stale price reference, or missing TibetSwap data should not alone trigger a throttle, but they should raise the background score and be shown as a reason.

## Scoring

Use a transparent bounded score, not a black box.

Suggested bands:

```text
0-29    normal     no action
30-54   mild       widen slightly
55-74   elevated   widen the risky side strongly
75-89   high       widen and throttle new offers on the risky side
90-100  extreme    log emergency state; cancellation remains gated
```

Scores should decay over time when conditions improve. A toxic burst should not punish the bot forever.

Initial scoring should require at least two independent signals before side throttling, unless the score is extreme. This avoids one noisy fill causing a full side pause.

## Actions

### Spread Widening

`RiskManager.get_adjusted_spread(side)` should apply the toxicity multiplier after existing inventory/volatility/pool-depth/competitor adjustments and before final min/max clamps.

Suggested multipliers:

```text
normal:    1.00
mild:      1.10
elevated:  1.35 on risky side, 1.10 on other side
high:      1.75 on risky side, 1.20 on other side
extreme:   2.00 on risky side, 1.35 on other side
```

The existing `MAX_SPREAD_BPS` clamp remains authoritative.

### Side Throttle

When a side reaches high toxicity:

- stop creating new offers on that side for `TOXICITY_THROTTLE_SECS`
- keep allowing the corrective side where appropriate
- do not delete historical state
- log a clear event explaining the reason

The throttle should block new offer creation, not necessarily cancel existing offers.

### Emergency Cancellation

The v1 design should expose an emergency recommendation but not enable default automatic cancellation. A later PR can turn this into targeted cancellation after live branch testing.

If implemented later, cancellation should require:

- score at or above `TOXICITY_CANCEL_START`
- at least two consecutive toxic cycles
- at least two independent reason categories
- only cancel the exposed side's unsafe offers, not necessarily every offer
- respect existing pending-cancel and wallet-sync safety checks

## Configuration

Add settings with conservative defaults:

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

The feature should be easy to disable from config while testing.

## Integration Points

### Bot Loop

At the point each loop already has price data, arb gap, fills, inventory, and open offers:

1. Build a `ToxicityContext`.
2. Call `MarketToxicityGuard.update()`.
3. Pass the snapshot to `RiskManager`.
4. Respect `is_side_throttled(side)` before creating new offers.
5. Include the snapshot in bot state/SSE updates.

### Risk Manager

RiskManager should consume the latest toxicity snapshot and apply spread multipliers. This keeps spread math in the existing spread pipeline rather than scattering pricing changes throughout the bot loop.

### API/Dashboard

Expose:

- `toxicity_score`
- `toxicity_level`
- `toxicity_reasons`
- `toxicity_buy_score`
- `toxicity_sell_score`
- `toxicity_throttled_sides`
- active spread multipliers

The dashboard should explain the state in plain English, for example:

```text
Toxicity 68: fast sell fills, AMM moved against sell quotes, inventory is leaning short.
```

## Error Handling

The guard must never crash the trading loop.

- If scoring fails, log `toxicity_guard_error` and keep the previous fresh snapshot if available.
- If no reliable data exists, return score `0` with reason `insufficient_data`.
- If config disables the feature, return score `0` and no multipliers.
- If wallet or Dexie/Tibet data is stale, score that as a data-quality reason rather than raising.

## Testing

Unit tests:

- score fast fills by side
- score one-sided fill streaks
- score adverse post-fill movement
- score inventory accumulation
- score small-balance sweep risk
- score public market depth imbalance
- do not penalize intentional one-sided mode solely for being one-sided
- score Dexie/Tibet dislocation
- decay scores when conditions calm
- apply spread multipliers by level
- require multiple signals before throttle
- respect `MARKET_TOXICITY_ENABLED=false`
- no crash on missing/partial context

Integration tests:

- `RiskManager.get_adjusted_spread()` applies toxicity multiplier and still respects clamps
- bot loop skips new offer creation for a throttled side
- corrective side remains enabled where appropriate
- API/status/dashboard payload exposes snapshot fields
- logs include human-readable reasons

Manual tests:

- replay synthetic fill bursts without wallet calls
- run local bot with small amounts and verify spread widening in logs
- confirm side throttle expires and quoting resumes
- confirm no automatic mass-cancel occurs by default

## Rollout

1. Implement the scorer and unit tests.
2. Wire spread multiplier into `RiskManager`.
3. Wire side throttle into offer creation.
4. Expose API/dashboard state.
5. Run a local simulation and live small-wallet test before merge.

## Review Notes

The feature is active from day one: widening and side throttling happen immediately when thresholds are crossed. Emergency cancellation is intentionally designed but not default-enabled because Chia offer cancellation has timing and coin-lock risks that need separate testing.
