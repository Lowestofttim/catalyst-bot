# Fixes — Slice 02-23

No production code fixes needed.

---

## Test corrections

- Position-limit tests initially failed because `_check_position_limit` on first call
  records `_startup_position_xch = position_xch` (inherited position baseline), then
  raises the effective limit to `baseline × 1.1`. Setting `_startup_position_xch = 0`
  before calling disables the inheritance logic and exercises the clean path.
