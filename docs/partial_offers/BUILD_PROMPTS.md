# CATalyst Partial Offers Build Prompts

> Plain Markdown transcription of the phase-by-phase partial-offers build prompts. Keep this file reviewable in git; do not reintroduce binary Word copies for public planning docs.

Build Prompts — Phases 1 through 4

Copy each prompt at the start of a new code session.

Complete and test one phase before moving to the next.

Stop after Phase 4. Phase 5 (live activation) begins when Sage supports CHIP-0052.

April 2026

## How to Use This Document

Each phase has its own section with a green prompt box. The prompt box is what you copy and paste as your very first message in a new code session. Do not mix phases — one session per phase.

## The Golden Rules

1. Read the prompt fully before starting a session so you know what to expect.

2. Copy the entire prompt box — start of the green border to end.

3. After the session completes, run the bot and confirm it still works in standard mode.

4. Only move to the next phase once the current one is tested and working.

5. Stop at Phase 4. Do not attempt Phase 5 until Sage wallet publishes its partial offer RPC.

## What Each Phase Delivers

Phase

Name

What you get at the end

Phase 1

The Wiring

Config, DB table, wallet stubs, mode branch. Bot runs identically. No behaviour change.

Phase 2

The Brain

partial_offer_manager.py built and wired. Full logic runs in partial mode, stubs fire instead of real wallet calls.

Phase 3

The Eyes

partial_fill_tracker.py and partial_coin_monitor.py. Bot watches for on-chain fills when in partial mode.

Phase 4

The Dashboard

GUI panel and API endpoints. Full visibility of partial offer state. Ready for the switch.

Phase 5 ★

The Switch

Stub functions replaced with real Sage RPC calls. Testnet then live. WAIT for Sage support.

## Phase 1 of 4

The Wiring

Config • Database • Wallet Stubs • Mode Branch

### What this session builds

Adds OFFER_MODE and 4 partial settings to config.py

Adds the partial_offers table and 4 DB helper functions to database.py

Adds 3 stub functions to wallet_sage.py (and passthroughs in wallet.py)

Adds the OFFER_MODE branch to bot_loop.py

### After this session

Bot runs identically to today. No new behaviour. All wiring is in place.

Test: run the bot in standard mode, confirm nothing changed. Switch to OFFER_MODE=partial, confirm stub log message appears and bot continues without crashing.

### The Prompt

COPY THIS PROMPT → paste as your first message in a new code session

### Context

We are integrating CHIP-0052 partial offer support into CATalyst.

Read the skill at docs/partial_offers/PARTIAL_OFFERS_PLAN.md for full context.

Also read docs/partial_offers/PARTIAL_OFFERS_PLAN.md.

The working directory is the project root. Runtime Python modules live under src/catalyst; root-level entry points such as desktop_app.py and bot_gui.html remain at the repo root.

### What We Are Building — Phase 1 Only

Phase 1 is pure scaffolding. No behaviour changes. No new features that activate.

The bot must run identically after every file change.

Standard mode (OFFER_MODE=standard) must be completely unaffected.

### Build in this exact order

### Step 1 — config.py

Add these 5 settings inside _reload_inner() in the Config class.

### Place them in a clearly labelled block after the existing trading settings

# ----- Partial Offers (CHIP-0052) -----

self.OFFER_MODE = _str('OFFER_MODE', 'standard')

self.PARTIAL_PRECISION = _int('PARTIAL_PRECISION', 1000)

self.PARTIAL_PRICE_PRECISION = _int('PARTIAL_PRICE_PRECISION', 1000)

self.PARTIAL_SIDES = _str('PARTIAL_SIDES', 'both')

self.PARTIAL_LEVELS = _int('PARTIAL_LEVELS', 3)

### Step 2 — database.py

### 2a. Add the partial_offers table to the schema creation block

CREATE TABLE IF NOT EXISTS partial_offers (

id TEXT PRIMARY KEY, original_id TEXT, side TEXT,

price_level INTEGER, offered_asset TEXT, requested_asset TEXT,

initial_amount INTEGER, remaining_amount INTEGER, price TEXT,

precision INTEGER, price_precision INTEGER,

status TEXT DEFAULT 'active', created_at INTEGER,

last_fill_at INTEGER, fill_count INTEGER DEFAULT 0,

total_filled INTEGER DEFAULT 0

);

CREATE INDEX IF NOT EXISTS idx_partial_offers_status ON partial_offers(status);

CREATE INDEX IF NOT EXISTS idx_partial_offers_side ON partial_offers(side, status);

2b. Add 4 helper functions at the bottom of database.py using the same

sqlite3 pattern as the existing offer/fill helpers (thread-local connection

### via get_connection(), row_factory for dict-like rows)

def add_partial_offer(partial: dict) -> None

def update_partial_offer(coin_id: str, updates: dict) -> None

def get_active_partials(side: str = None) -> list

def get_partial_offer(coin_id: str) -> Optional[dict]

### Step 3 — wallet_sage.py

Add 3 stub functions under a # --- Partial Offers (CHIP-0052 stubs) --- comment.

Each stub logs that Sage does not yet support partial offers and returns gracefully:

def create_partial_offer(offered_asset_id, offered_amount_mojos,

### requested_asset_id, precision, price_precision, fee_mojos=0) -> Optional[str]

log_event('info', 'partial_offer_stub',

'create_partial_offer: Sage does not yet support CHIP-0052')

return None

### def cancel_partial_offer(coin_id, fee_mojos=0) -> bool

log_event('info', 'partial_offer_stub',

f'cancel_partial_offer: Sage does not yet support CHIP-0052 (coin={coin_id})')

return False

### def get_partial_offer_state(coin_id) -> Optional[dict]

return None

Then add passthroughs to wallet.py importing these three functions from wallet_sage.

### Step 4 — bot_loop.py

Find the main cycle block where fills are detected and offers are created.

### Wrap that block in a mode branch

### if cfg.OFFER_MODE == 'partial'

log_event('info', 'bot_loop_mode',

'Running in partial offer mode (stubs active - Phase 1)')

### else

# Standard path - existing code goes here, completely unchanged

pass

### Also add OFFER_MODE to the startup log

log_event('info', 'bot_loop_start',

f'BotLoop starting - OFFER_MODE={cfg.OFFER_MODE}')

### After Each File — Check

After every file: confirm no syntax errors and run: cd tests && pytest -x -q

### Final Test

1. Confirm OFFER_MODE=standard in .env — bot runs identically to before.

2. Set OFFER_MODE=partial — bot logs the Phase 1 stub message, does not crash.

3. Set back to OFFER_MODE=standard.

### Do NOT Build In This Session

Do not create partial_offer_manager.py, partial_fill_tracker.py,

or partial_coin_monitor.py. Do not touch bot_gui.html or api_server.py.

Phase 1 only. Keep it minimal.

## Phase 2 of 4

The Brain

partial_offer_manager.py

### What this session builds

Creates partial_offer_manager.py — the core logic for creating, tracking, requoting and cancelling partial coins

Wires PartialOfferManager into bot_loop.py's partial mode branch

All logic runs and DB entries are created in partial mode — wallet stubs fire instead of real calls

### After this session

Switch to OFFER_MODE=partial: the bot attempts to create/manage partial offers, DB rows appear, stubs log their messages, no actual coins are created. Switch back to standard: zero difference from before.

### The Prompt

COPY THIS PROMPT → paste as your first message in a new code session

### Context

We are integrating CHIP-0052 partial offer support into CATalyst.

Read the skill at docs/partial_offers/PARTIAL_OFFERS_PLAN.md for full context.

Also read docs/partial_offers/PARTIAL_OFFERS_PLAN.md.

Phase 1 is complete: config settings, DB table, wallet stubs, and the bot_loop

mode branch all exist. The working directory is the project root.

### What We Are Building — Phase 2 Only

Create partial_offer_manager.py and wire it into the partial mode branch

in bot_loop.py. The wallet stubs from Phase 1 mean no actual coins are created.

### Step 1 — Create partial_offer_manager.py

Model the structure after offer_manager.py. The class is PartialOfferManager.

Use the same coding standards: Decimal for money, log_event() for logging,

thread-local DB via get_connection(), threading.Lock() for shared state.

Import conversion helpers (xch_to_mojos etc.) from offer_manager.

### Implement these methods

__init__(self)

self._lock = threading.Lock()

self._active_partials = {}  # coin_id -> dict

self._last_requote_time = {'buy': 0, 'sell': 0}

create_partial_ladder(self, mid_price: Decimal, side: str) -> int

# Creates cfg.PARTIAL_LEVELS partial coins for the given side.

# Price levels: inner is closest to mid_price, outer is furthest.

# Spread per level mirrors the existing tier logic in offer_manager.

# For each level: call _create_partial_coin(), write to DB via

# add_partial_offer(), cache in self._active_partials.

# Returns count of successfully created coins (will be 0 if stubs).

check_requotes(self, current_price: Decimal) -> None

# For each active partial in DB: if price has drifted beyond

# cfg.REQUOTE_THRESHOLD_BPS, cancel it and create a fresh one.

# Respects self._last_requote_time cooldown per side.

cancel_partial_offer(self, coin_id: str) -> bool

# Calls wallet.cancel_partial_offer(coin_id).

# On success: update_partial_offer(coin_id, {'status': 'cancelled'}).

# Returns True on success, False on failure or stub.

cancel_all_partials(self, side: str = None) -> int

# Cancels all active partial coins, optionally filtered by side.

# Returns count of successfully cancelled coins.

get_active_partials(self, side: str = None) -> list

# Returns list of active partial offer dicts from DB.

_create_partial_coin(self, side, price_level, price, amount_mojos,

offered_asset, requested_asset) -> Optional[str]

# Calls wallet.create_partial_offer() with computed precision params.

# Returns coin_id string on success, None if stub returns None.

_compute_precisions(self, price: Decimal) -> tuple[int, int]

# Encodes a Decimal price into (PRECISION, PRICE_PRECISION) integers.

# Use cfg.PARTIAL_PRECISION and cfg.PARTIAL_PRICE_PRECISION as bases.

# Returns (precision, price_precision) tuple.

_price_for_level(self, mid_price: Decimal, side: str,

level: int) -> Decimal

# Computes the offer price for a given ladder level.

# Buy side: prices below mid. Sell side: prices above mid.

# Use the same tier spread logic as offer_manager.create_ladder.

### Step 2 — Wire into bot_loop.py

### In the partial mode branch added in Phase 1, replace the placeholder log with

### if cfg.OFFER_MODE == 'partial'

# Partial path

self.partial_offer_manager.check_requotes(current_price)

### if cfg.PARTIAL_SIDES in ('buy', 'both')

self.partial_offer_manager.create_partial_ladder(

current_price, 'buy')

### if cfg.PARTIAL_SIDES in ('sell', 'both')

self.partial_offer_manager.create_partial_ladder(

current_price, 'sell')

Instantiate PartialOfferManager in BotLoop.__init__() alongside the other managers:

from partial_offer_manager import PartialOfferManager

self.partial_offer_manager = PartialOfferManager()

### Step 3 — Verify

Run: cd tests && pytest -x -q

### Set OFFER_MODE=partial, start bot, confirm

- Log shows 'partial offer mode' messages each cycle

- DB partial_offers table gets rows written (status=active, remaining=initial)

- Stub log messages appear in super_log (no actual coins created)

- No crashes or exceptions

Set OFFER_MODE=standard, confirm bot is identical to before Phase 2.

### Do NOT Build In This Session

Do not create partial_fill_tracker.py or partial_coin_monitor.py (Phase 3).

Do not modify bot_gui.html or api_server.py (Phase 4).

Do not implement real Sage RPC calls (Phase 5).

## Phase 3 of 4

The Eyes

partial_fill_tracker.py • partial_coin_monitor.py

### What this session builds

Creates partial_fill_tracker.py — detects partial fills by monitoring on-chain coin state via Spacescan/coinset

Creates partial_coin_monitor.py — background thread that polls every 30s for coin state changes

Wires the monitor thread into bot_loop.py so it starts and stops with the bot

### After this session

In partial mode: if a partial coin were spent on-chain, the bot would detect it, record the fill amount, update the remaining balance in the DB, and start tracking the successor coin. Still waiting on Sage for actual coin creation — stubs still fire for creates.

### The Prompt

COPY THIS PROMPT → paste as your first message in a new code session

### Context

We are integrating CHIP-0052 partial offer support into CATalyst.

Read the skill at docs/partial_offers/PARTIAL_OFFERS_PLAN.md for full context.

Also read docs/partial_offers/PARTIAL_OFFERS_PLAN.md.

Phases 1 and 2 are complete: config, DB, stubs, bot_loop branch,

and partial_offer_manager.py all exist and work.

The working directory is the project root.

### What We Are Building — Phase 3 Only

Create two new files: partial_fill_tracker.py and partial_coin_monitor.py.

Wire the monitor thread into bot_loop.py.

### Step 1 — partial_fill_tracker.py

Class: PartialFillTracker

A partial fill is detected by checking whether a tracked partial coin has been

spent on-chain. If it has been spent by a taker (not by the maker cancelling),

a new successor coin exists with the remaining balance.

Use Spacescan as the primary source (already used in fill_tracker.py and

spacescan.py). Fall back gracefully if Spacescan is unreachable.

### Implement these methods

__init__(self)

self._known_spent = set()  # coin_ids confirmed spent this session

poll_all(self) -> list[dict]

# Get all active partial coin IDs from DB via get_active_partials().

# For each coin: call check_coin_state(coin_id).

# Returns list of fill event dicts for any fills detected.

check_coin_state(self, coin_id: str) -> Optional[dict]

# Query Spacescan for the coin's current state.

# If spent and not in self._known_spent: detect_fill(coin_id).

# If still unspent: return None.

# Handles HTTP errors gracefully (log + return None).

detect_fill(self, coin_id: str) -> Optional[dict]

# Confirms a fill: fetches the spend details to get fill amount.

# Calls find_successor(coin_id) to get the new partial coin ID.

# Calls record_fill(coin_id, fill_amount, successor_id).

# Adds coin_id to self._known_spent.

# Returns fill event dict.

find_successor(self, coin_id: str) -> Optional[str]

# Queries Spacescan for coins created by the spend of coin_id.

# Filters for coins with the same partial offer puzzle hash.

# Returns the successor coin_id, or None if fully filled / cancelled.

record_fill(self, coin_id: str, fill_amount: int,

successor_id: Optional[str]) -> None

# Writes a fill record to the fills table (same as standard fills).

### # Updates partial_offers table

#   - increment fill_count, add to total_filled

#   - set last_fill_at = now

#   - if successor_id: update id to successor_id, reset remaining_amount

#   - if no successor: set status = 'filled'

# Logs the fill event via log_event().

### Step 2 — partial_coin_monitor.py

A simple background thread that runs poll_all() every 30 seconds.

### class PartialCoinMonitor

### def __init__(self, fill_tracker: PartialFillTracker)

self._tracker = fill_tracker

self._thread = None

self._stop_event = threading.Event()

self._interval = 30  # seconds

### def start(self)

# Start background thread if not already running.

### def stop(self)

# Set stop event, join thread with 5s timeout.

### def _run(self)

# Loop: poll_all(), sleep interval, check stop event.

# Catch and log all exceptions — never let the thread die silently.

### Step 3 — Wire into bot_loop.py

### Instantiate both in BotLoop.__init__()

from partial_fill_tracker import PartialFillTracker

from partial_coin_monitor import PartialCoinMonitor

self.partial_fill_tracker = PartialFillTracker()

self.partial_coin_monitor = PartialCoinMonitor(self.partial_fill_tracker)

### Start the monitor in BotLoop.start() only when OFFER_MODE=partial

### if cfg.OFFER_MODE == 'partial'

self.partial_coin_monitor.start()

### Stop it in BotLoop.stop()

self.partial_coin_monitor.stop()

### Step 4 — Verify

Run: cd tests && pytest -x -q

### Set OFFER_MODE=partial, start the bot, confirm

- partial_coin_monitor thread starts (visible in log)

- No exceptions after 30s (first poll completes silently — no active coins yet)

- Bot continues normally

Set OFFER_MODE=standard, confirm standard mode is unchanged.

### Do NOT Build In This Session

Do not modify bot_gui.html or api_server.py (Phase 4).

Do not implement real Sage RPC calls (Phase 5).

## Phase 4 of 4

The Dashboard

GUI Panel • API Endpoints

### What this session builds

Adds /api/partial_offers and /api/partial_fills endpoints to api_server.py

Adds a Partial Offers panel to bot_gui.html — shows active coins, remaining amounts, fill history

Panel is hidden/collapsed in standard mode and visible in partial mode

After Phase 4 — you are at the switch-and-test point

Everything is built. Everything is wired. Everything is testable in partial mode with the stubs.

The only remaining step (Phase 5) is replacing the 3 stub functions with real Sage RPC calls.

That step waits until Sage publishes support for CHIP-0052.

### The Prompt

COPY THIS PROMPT → paste as your first message in a new code session

### Context

We are integrating CHIP-0052 partial offer support into CATalyst.

Read the skill at docs/partial_offers/PARTIAL_OFFERS_PLAN.md for full context.

Also read docs/partial_offers/PARTIAL_OFFERS_PLAN.md.

Phases 1, 2 and 3 are complete: config, DB, stubs, partial_offer_manager.py,

partial_fill_tracker.py, and partial_coin_monitor.py all exist and work.

The working directory is the project root.

### What We Are Building — Phase 4 Only

Add two API endpoints to api_server.py and a Partial Offers panel to bot_gui.html.

Match the existing code style in both files exactly.

### Step 1 — api_server.py

Add two new endpoints. Follow the exact same pattern as existing endpoints

### (Flask route, try/except, JSON response, log_event on error)

@app.route('/api/partial_offers')

### def get_partial_offers()

# Returns all active partial offers from DB via get_active_partials().

### # Response shape

# { 'offers': [ { 'id', 'side', 'price_level', 'price',

#     'initial_amount', 'remaining_amount', 'fill_count',

#     'total_filled', 'created_at', 'last_fill_at' } ] }

@app.route('/api/partial_fills')

### def get_partial_fills()

# Returns recent fills from the fills table where source='partial'.

### # Limit to last 50. Response shape

# { 'fills': [ { 'id', 'side', 'amount', 'price', 'timestamp' } ] }

Also add 'offer_mode': cfg.OFFER_MODE to the existing /api/status endpoint response

so the GUI knows which mode is active.

### Step 2 — bot_gui.html

Add a Partial Offers panel. Match the visual style of existing panels exactly

(same card style, same colour scheme, same font sizes).

### The panel should

- Be hidden entirely when offer_mode is 'standard' (check /api/status on load)

- Show a mode badge in the header area: 'Standard Mode' or 'Partial Mode'

### - When in partial mode, display

### Active Partial Coins table

Side | Level | Price | Initial | Remaining | Fills | Last Fill

(one row per active partial coin, colour-coded buy=green/sell=red)

### Recent Partial Fills list

Timestamp | Side | Amount filled | Price

(last 10 fills, newest first)

- Auto-refresh every 15 seconds (same as other panels)

- Show 'No active partial offers' placeholder when list is empty

Place the panel after the existing Offers panel in the layout.

No new libraries — use only what bot_gui.html already uses.

### Step 3 — Verify

Run: cd tests && pytest -x -q

### Start the bot in standard mode

- Open the GUI

- Confirm the Partial Offers panel is hidden

- Confirm the mode badge shows 'Standard Mode'

- Confirm /api/partial_offers returns { 'offers': [] }

- Confirm /api/partial_fills returns { 'fills': [] }

- Confirm /api/status includes offer_mode: 'standard'

### Switch to OFFER_MODE=partial, restart

- Confirm the Partial Offers panel is visible

- Confirm the mode badge shows 'Partial Mode'

- Confirm DB rows from Phase 2 appear in the panel

### This Is The Switch-And-Test Point

### After this session, all four build phases are complete.

The only remaining work (Phase 5) is replacing the 3 stub functions in

wallet_sage.py with real Sage RPC calls once Sage publishes support for CHIP-0052.

Do not attempt Phase 5 until that support is confirmed available.

### Do NOT Build In This Session

Do not replace any stub functions with real Sage RPC calls (Phase 5).

Do not add any other new features outside the scope above.

Phase 5 — The Switch (Not Yet)

Wait for Sage

Phase 5 is NOT included in this document because it cannot be built until Sage wallet publishes RPC support for CHIP-0052 partial offers.

When that happens, Phase 5 is straightforward: replace the 3 stub functions in wallet_sage.py with real calls to Sage's new endpoints, test on Chia testnet with small amounts, then set OFFER_MODE=partial in production .env.

A Phase 5 prompt will be written at that time, informed by Sage's actual API spec.

### Monitor the following for Sage + CHIP-0052 updates

CHIP-0052 PR: github.com/Chia-Network/chips/pull/174

Sage wallet releases: github.com/xch-dev/sage/releases

Chia Discord: #chips channel

End of Document
