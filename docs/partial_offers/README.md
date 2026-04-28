# Partial Offers (CHIP-0052) design docs

This folder bundles the design, reference, and planning material for the CHIP-0052 partial-offers integration. **Nothing here is live code.** The implementation has not yet landed. Use `PARTIAL_OFFERS_PLAN.md` as the canonical build plan.

| File | Purpose |
|------|---------|
| `PARTIAL_OFFERS_PLAN.md` | Complete integration plan, phased build order |
| `BUILD_PROMPTS.md` | Build-session prompts per phase |
| `PARTIAL_OFFERS_REFERENCE.md` | CHIP-0052 reference material |
| `PHASE1_SESSION_PROMPT.md` | Phase-1 kickoff prompt (standalone session starter) |

When the implementation lands (`partial_offer_manager.py`, `partial_fill_tracker.py`, `partial_coin_monitor.py`, `OFFER_MODE` feature flag, DB schema), runtime Python modules should live under `src/catalyst`. These docs stay here as the design record.
