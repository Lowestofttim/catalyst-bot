# Fixes — Slice 02-05

No production code fixes needed.

---

## Test corrections

- `estimate_slippage` → correct name is `_estimate_slippage_from_reserves`, which takes
  a raw pair dict (mojos), not `get_tibet_pool_info()` output.
- EMA test used a 10% price move which triggered the fast catch-up path (5× alpha);
  corrected to a 1% move to exercise the normal alpha=0.01 branch.
