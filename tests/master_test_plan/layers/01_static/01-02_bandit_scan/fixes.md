# Fixes — Slice <SLICE-ID>

Landed fixes with their regression coverage. One entry per commit.

---

## Fix <commit-hash-short>: <summary>

**Addresses:** <F1, F2> · **Files touched:** `<file1.py>`, `<tests/test_X.py>`

### Change summary
<1-3 sentences. Pull from commit message body.>

### Regression coverage
- `<tests/test_X.py::test_Y>` — verifies <specific scenario>
- Before fix: FAIL · After fix: PASS

### Verified no regressions in
```
pytest -q -- <most-affected-test-files>
```
Result: <N passed, 0 failed>

### Related touches
<anything adjacent that needed editing; out-of-scope items go in spawn_queue.md instead>

---

## Lessons / gotchas

Append notes worth remembering for future slices. E.g.:

- `<module>.foo()` expects mojos, not XCH — easy to mistake the unit
- `cfg` reload doesn't re-bind `from X import cfg` — module-level imports
  must be torn down AND repopulated
