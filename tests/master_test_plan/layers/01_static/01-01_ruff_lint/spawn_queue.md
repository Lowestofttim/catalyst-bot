# Spawn queue — Slice <SLICE-ID>

Out-of-scope issues found during this slice. Each item becomes a future
slice / bug ticket. Don't chase them mid-slice — that's how scope dies.

Format: one item per bullet, enough context for a new session to pick up
cold.

---

## Queue

- [ ] **<SUBJECT>** — <1-line description>.
  - Discovered at: <file:line>
  - Why out-of-scope here: <e.g. "touches risk_manager which belongs to slice 02-23">
  - Severity: <critical/high/medium/low>
  - Suggested slice to handle: <e.g. 02-23 or add new slice 99-01>

- [ ] ...

---

## Once a queue item is addressed elsewhere

Move it to the **Dispatched** section below with a ref to where it went.

## Dispatched

- ~~<SUBJECT>~~ → handled in slice <XX-YY>, commit `<hash>`.
