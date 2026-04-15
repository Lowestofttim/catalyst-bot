# Monitor Session — Launch Prompt

**How to use**: Open a fresh `claude` terminal, then paste the exact text between the `---` markers below as your first message. Do not edit it.

---

I am starting a fresh monitoring session for the CATalyst bot.

**Mandatory onboarding — do this before anything else**, in order:

1. Read `C:\chia_liquidity_bot_v2_v4_tauri\CLAUDE.md` (project conventions)
2. Read `C:\chia_liquidity_bot_v2_v4_tauri\.claude\monitor\MEMORY.md` (cross-session knowledge — if missing, copy from `MEMORY.md.template`)
3. Read `C:\chia_liquidity_bot_v2_v4_tauri\.claude\monitor\MONITOR_PLAYBOOK.md` (your full operating manual)

Then execute the onboarding sequence from **Part 2** of the playbook:

- Step 2: Module tour — use the Explore agent for each module listed in Part 2 Step 2
- Step 3: Verify API access to bot, Dexie, Spacescan, and Sage (run all 4 checks in Part 2 Step 3)
- Step 4: Initialize `.claude/monitor/monitor.log` with an `onboarding_start` event
- Step 5: Schedule Tier 1 (every 2 min), Tier 2 (every 15 min), and Tier 3 (hourly) via `mcp__scheduled-tasks__create_scheduled_task`. Each task's prompt should reference this playbook and respect the `monitor.lock` file to avoid overlapping sweeps.
- Step 6: Run a Tier 1 sweep immediately to establish baseline
- Step 7: Post to chat: `✅ Monitor session active. Onboarding complete. Tier 1/2/3 scheduled.`

**Authority & safety** (per playbook Part 1):
- Full autonomy on fixes — do NOT ask permission for routine patterns (Part 5)
- Only escalate: novel issues (5.14) and critical triggers (Part 8)
- Every fix uses the 10-step protocol in Part 6 (observe → 2nd confirm → diagnose → dry-run → snapshot → apply → verify → log)
- Code fixes: auto-commit + push. Runtime state changes: log only, never commit.

**Model & session** (per playbook Part 9.5):
- Start on Sonnet. Recommend Opus only for novel issues or complex multi-module diagnoses.
- Self-assess context usage after every Tier 3 sweep. Ask user for `/compact` at 50-75% or handoff at >85%.
- If you cannot self-switch models, post a recommendation with justification; the user will run `/model`.

**Source of truth** (per playbook Part 3):
- Dexie v1 API (`status=0` = active, `offered`/`requested` params)
- Spacescan for analytics only (rate-limited, cache 15 min)
- Sage RPC via `wallet_sage` module (never direct HTTP)
- Bot DB status column uses `'open'` not `'active'`

Begin onboarding now.

---

*After pasting, the new session will read all three files, tour the modules, verify access, schedule the tiered sweeps, and start monitoring. No further input from you required unless it flags a novel issue or critical trigger.*

---

## Quick-access commands for YOU (not the new session)

If you ever need to:

- **Check if monitor is running**: look for an open `claude` terminal, or check for recent events in `.claude/monitor/monitor.log`
- **Stop the monitor**: close the `claude` terminal. The scheduled tasks will still fire but will no-op if the session isn't running.
- **Force a fresh start**: `cd C:\chia_liquidity_bot_v2_v4_tauri\.claude\monitor; Remove-Item monitor.lock -ErrorAction SilentlyContinue; Remove-Item MEMORY.md -ErrorAction SilentlyContinue` (this resets to template). Then paste the prompt above into a new session.
- **Switch the monitor's model mid-session**: in that session's terminal, run `/model opus` or `/model sonnet`.
- **Ask the monitor to compact**: in that terminal, run `/compact`.
