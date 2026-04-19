# Findings — Slice <SLICE-ID>

One entry per fault. Keep entries terse — the diff + commit has detail.

---

## Finding F1: <short title>

**Check:** <1.3> · **Severity:** <critical | high | medium | low> · **Status:** <open | fixed | blocked>

### Reproduction
```
<minimal repro — command, input, expected vs actual>
```

### Root cause (once understood)
<1-2 sentence summary. Link to file:line.>

### Resolution
- [ ] Fix committed: <hash>
- [ ] Regression test: `<path/to/test.py::test_name>`
- [ ] No regressions in `pytest -q`

---

## Finding F2: <short title>

(copy the F1 block)

---

## Closed findings tallied here

Running totals so MASTER_INDEX.md stays accurate:

| Count | Status |
|-------|--------|
| 0 | open |
| 0 | fixed |
| 0 | blocked |
