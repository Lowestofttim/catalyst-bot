# Findings — Slice 02-12

Unit tests for `offer_lifecycle.py` — pure offer state-machine.

---

## Existing coverage (before this slice)

None.

---

## New coverage added

| Module / Function | Tests | Notes |
|-------------------|-------|-------|
| `OfferState` enum | 3 | string type, OPEN value, FILLED value |
| `_TERMINAL_STATES` | 6 | all 4 terminals present, OPEN absent, exhaustive noop |
| `apply_signal` — OPEN | 8 | all 5 signals + noop + old_state + signal propagation |
| `apply_signal` — REFRESH_DUE | 6 | all 5 signals + noop |
| `apply_signal` — CANCEL_REQUESTED | 6 | all 5 signals + noop |
| `apply_signal` — MEMPOOL_OBSERVED | 5 | all 4 signals + noop |
| `apply_fill_verification` | 4 | verified, rejected, wrong state, wrong signal |
| `coarse_status` | 9 | all 8 lifecycle states + unknown fallback |
| `is_terminal` | 7 | all 4 terminals + 3 non-terminals |
| `OfferTransition` frozen | 1 | mutation raises AttributeError/TypeError |

**55 new tests** in `tests/test_plan_02_12_offer_lifecycle_unit.py`.

---

## No bugs found

| Count | Status |
|-------|--------|
| 0 | open bugs |
| 0 | fixed |
| 0 | blocked |
