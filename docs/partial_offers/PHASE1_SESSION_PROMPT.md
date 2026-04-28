# CATalyst Partial Offers — Phase 1 Session Prompt

Copy everything below this line and paste it as your first message in a new code session.

---

## Context

We are integrating CHIP-0052 partial offer support into CATalyst, an automated
market-making bot for Chia blockchain CAT tokens. Read
`docs/partial_offers/PARTIAL_OFFERS_PLAN.md` for full context on the codebase,
partial offers, and the complete integration plan.

The working directory is the project root. Runtime Python modules live under
`src/catalyst`; root-level entry points such as `desktop_app.py` and
`bot_gui.html` remain at the repo root.

## What We're Building Today — Phase 1 Only

Phase 1 is pure scaffolding: no behaviour changes, no new features that activate.
The bot must run identically to today after every file is changed.
Standard mode (`OFFER_MODE=standard`) must be completely unaffected.

Build these changes in this exact order, one file at a time:

---

### Step 1 — `config.py`

Add 5 new settings to the `_reload_inner()` method in the `Config` class.
Find the section with existing trading settings and add a new clearly-labelled
block after it:

```python
# ----- Partial Offers (CHIP-0052) -----
# OFFER_MODE: "standard" = today's fill-or-kill offers (default)
#             "partial"  = CHIP-0052 partial coins (activates when Sage supports it)
self.OFFER_MODE = _str("OFFER_MODE", "standard")

# PARTIAL_PRECISION: scale factor for the offered asset (encodes decimal places)
self.PARTIAL_PRECISION = _int("PARTIAL_PRECISION", 1000)

# PARTIAL_PRICE_PRECISION: exchange rate scale factor (encodes the price)
self.PARTIAL_PRICE_PRECISION = _int("PARTIAL_PRICE_PRECISION", 1000)

# PARTIAL_SIDES: which sides use partial offers — "buy", "sell", or "both"
self.PARTIAL_SIDES = _str("PARTIAL_SIDES", "both")

# PARTIAL_LEVELS: how many price levels to maintain as partial coins
self.PARTIAL_LEVELS = _int("PARTIAL_LEVELS", 3)
```

---

### Step 2 — `database.py`

**2a.** Add the `partial_offers` table to the schema creation block (wherever the
other `CREATE TABLE IF NOT EXISTS` statements live):

```sql
CREATE TABLE IF NOT EXISTS partial_offers (
    id                TEXT PRIMARY KEY,
    original_id       TEXT,
    side              TEXT,
    price_level       INTEGER,
    offered_asset     TEXT,
    requested_asset   TEXT,
    initial_amount    INTEGER,
    remaining_amount  INTEGER,
    price             TEXT,
    precision         INTEGER,
    price_precision   INTEGER,
    status            TEXT DEFAULT 'active',
    created_at        INTEGER,
    last_fill_at      INTEGER,
    fill_count        INTEGER DEFAULT 0,
    total_filled      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_partial_offers_status ON partial_offers(status);
CREATE INDEX IF NOT EXISTS idx_partial_offers_side   ON partial_offers(side, status);
```

**2b.** Add 4 new helper functions at the bottom of `database.py` (after the
existing offer/fill helper functions):

```python
# ---------------------------------------------------------------------------
# Partial Offers (CHIP-0052)
# ---------------------------------------------------------------------------

def add_partial_offer(partial: dict) -> None:
    """Insert a new partial offer record."""

def update_partial_offer(coin_id: str, updates: dict) -> None:
    """Update fields on an existing partial offer by coin_id."""

def get_active_partials(side: str = None) -> list:
    """Return all active partial offer records, optionally filtered by side."""

def get_partial_offer(coin_id: str) -> Optional[dict]:
    """Fetch a single partial offer by coin_id. Returns None if not found."""
```

Implement each using the same sqlite3 pattern used elsewhere in `database.py`
(thread-local connection via `get_connection()`, row_factory for dict-like rows).

---

### Step 3 — `wallet_sage.py`

Add 3 stub functions near the bottom of `wallet_sage.py`, clearly grouped under
a `# --- Partial Offers (CHIP-0052 stubs) ---` comment:

```python
def create_partial_offer(
    offered_asset_id: Optional[str],      # None = XCH, hex string = CAT
    offered_amount_mojos: int,
    requested_asset_id: Optional[str],    # None = XCH, hex string = CAT
    precision: int,
    price_precision: int,
    fee_mojos: int = 0,
) -> Optional[str]:
    """Create a partial offer coin on-chain.

    STUB — Sage does not yet support partial offers (CHIP-0052 pending ratification).
    Returns None. Replace with real RPC call when Sage adds support.
    """
    log_event("info", "partial_offer_stub",
              "create_partial_offer: Sage does not yet support CHIP-0052 partial offers")
    return None


def cancel_partial_offer(
    coin_id: str,
    fee_mojos: int = 0,
) -> bool:
    """Cancel a partial offer by spending the partial coin.

    STUB — Sage does not yet support partial offers (CHIP-0052 pending ratification).
    Returns False. Replace with real RPC call when Sage adds support.
    """
    log_event("info", "partial_offer_stub",
              f"cancel_partial_offer: Sage does not yet support CHIP-0052 partial offers (coin={coin_id})")
    return False


def get_partial_offer_state(
    coin_id: str,
) -> Optional[dict]:
    """Fetch current on-chain state of a partial offer coin.

    STUB — Returns None until Sage adds support.
    Expected return shape (for future implementation):
    {
        "coin_id": str,
        "remaining_amount": int,
        "status": "active" | "spent" | "cancelled",
        "successor_coin_id": str | None,   # set when spent by a taker
    }
    """
    return None
```

Also add passthrough imports/functions to `wallet.py` so the adapter layer
is complete:
```python
from wallet_sage import (
    create_partial_offer,
    cancel_partial_offer,
    get_partial_offer_state,
)
```
(Add these alongside the existing imports from wallet_sage in wallet.py.)

---

### Step 4 — `bot_loop.py`

Find the section of the main cycle where fills are detected and offers are created
(steps 3–6 in the docstring). Wrap that block in a mode branch:

```python
if cfg.OFFER_MODE == "partial":
    # --- Partial offers path (CHIP-0052) ---
    # Modules: partial_fill_tracker, partial_offer_manager (Phase 2+)
    # For now: stubs only, no behaviour change.
    log_event("info", "bot_loop_mode", "Running in partial offer mode (stubs active — Phase 1)")
else:
    # --- Standard offers path (existing code, completely unchanged) ---
    # [existing fill detection + offer create/requote code goes here]
    pass  # (existing code stays exactly as-is inside this else block)
```

Also add `OFFER_MODE` to the startup log line so it's visible in logs:
```python
log_event("info", "bot_loop_start", f"BotLoop starting — OFFER_MODE={cfg.OFFER_MODE}")
```

---

## What To Check After Each File

After each file change, confirm:
1. The file has no syntax errors (run `python -c "import <module>"` mentally or actually)
2. The existing tests still pass: `cd tests && pytest -x -q`
3. No imports were broken

After all 4 files are done:
- Confirm `OFFER_MODE=standard` in .env (or that `standard` is the default)
- Start the bot and confirm it runs identically to before — no new log lines
  except the startup log showing `OFFER_MODE=standard`
- Switch `OFFER_MODE=partial` in .env temporarily and confirm the bot logs the
  Phase 1 stub message and continues without crashing

## What NOT To Build In This Session

- Do not build `partial_offer_manager.py` (Phase 2)
- Do not build `partial_fill_tracker.py` or `partial_coin_monitor.py` (Phase 3)
- Do not modify `bot_gui.html` or `api_server.py` (Phase 4)
- Do not implement any real Sage RPC calls (Phase 5)

Keep Phase 1 clean and minimal. The goal is: everything compiles, tests pass,
bot runs the same, wiring is in place for Phase 2.
