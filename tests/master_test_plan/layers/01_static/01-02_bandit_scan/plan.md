# Slice 01-02: bandit security scan — secrets, injection, paths

**Layer:** 1 · **Estimated time:** 30 min · **Escalate to Opus if:** a
finding is ambiguous about whether it's a real security risk vs. a
false positive in this context.

## Goal

Run bandit over the entire Python codebase, triage findings by severity,
fix HIGH-severity real issues, and spawn-queue MEDIUM findings for later slices.

## Scope

### In-scope
- All `*.py` at repository root AND in `tests/`
- HIGH severity findings (B-codes) — must be triaged and fixed or documented
- MEDIUM severity findings — triage + spawn-queue
- Secrets/credentials exposure (B105, B106, B107, hardcoded passwords)
- Injection risks (B301-B399: pickle, yaml, subprocess shell=True, eval/exec)
- Path traversal (B108: hardcoded tmp paths)

### Out-of-scope
- `backups_pre_audit_cleanup_*`, `dist2/`, `dist/`
- LOW severity findings — log count only, no action required
- bandit config/tuning beyond what's needed to suppress documented false positives

## Checks

### 1. Baseline scan
- [ ] **1.1** Run `bandit -r . --exclude backups_pre_audit_cleanup_20260408,dist,dist2 -f json` and record HIGH/MEDIUM/LOW counts.
- [ ] **1.2** Extract HIGH-severity findings — list each with file:line and rule ID.

### 2. Triage HIGH findings
- [ ] **2.1** For each HIGH: verify it's a real risk (not a false positive).
  - Real risk: fix + regression test + fixes.md entry.
  - False positive: add `# nosec <rule>` comment with explanation, log in findings.md as "documented FP".
- [ ] **2.2** Run `pytest -q` after any fix. ZERO regressions.
- [ ] **2.3** Commit fixes with `fix(plan 01-02): <what>`.

### 3. Triage MEDIUM findings
- [ ] **3.1** List MEDIUM findings, classify each as real/FP.
- [ ] **3.2** Real risks with easy fixes: fix inline.
- [ ] **3.3** Real risks needing design changes: spawn-queue.md.
- [ ] **3.4** False positives: `# nosec` + log.

### 4. Persist suppression baseline
- [ ] **4.1** Run final `bandit -r .` — verify HIGH count is 0 or every remaining one has a `# nosec` with explanation.

## Execution notes

```bash
pip install bandit
bandit -r . --exclude backups_pre_audit_cleanup_20260408,dist,dist2 -f json -o /tmp/bandit.json
python -c "
import json
from collections import Counter
d = json.load(open('/tmp/bandit.json'))
results = d.get('results', [])
c = Counter((r['issue_severity'], r['test_id']) for r in results)
highs = [r for r in results if r['issue_severity'] == 'HIGH']
meds  = [r for r in results if r['issue_severity'] == 'MEDIUM']
print(f'HIGH: {len(highs)}, MEDIUM: {len(meds)}, LOW: {len(results)-len(highs)-len(meds)}')
print()
for r in sorted(highs, key=lambda x: x['test_id']):
    print(f\"[{r['test_id']}] {r['filename'].split('/')[-1]}:{r['line_number']} — {r['issue_text'][:80]}\")
"
```

## Success criteria

- `bandit -r .` reports 0 HIGH-severity issues (or each has `# nosec <id>`)
- `pytest -q` still green
- Every HIGH finding has an entry in findings.md
