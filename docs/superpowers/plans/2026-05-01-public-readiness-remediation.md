# Public Readiness Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CATalyst safe to publish publicly, reproducible to set up/build, legally cleaner, and easier to maintain without breaking trading behavior.

**Architecture:** Fix public safety, docs, release metadata, build reproducibility, and high-confidence runtime bugs before touching larger architecture debt. Every code change must be small, tested with targeted checks, and followed by the relevant regression suite before moving to the next task.

**Tech Stack:** Python 3.12, Flask, PyWebView, SQLite WAL, vanilla HTML/CSS/JS, pytest, Ruff, Bandit, pip-audit, PyInstaller, GitHub Actions.

---

## Operating Rules

- [x] Work on a dedicated branch named `codex/public-readiness`.
- [ ] Keep one commit per task or per tightly related group of subtasks.
- [ ] Do not mix trading-logic changes with docs, GitHub metadata, or cleanup-only changes.
- [ ] Before editing code, run the smallest useful failing or baseline check for the touched behavior.
- [ ] After editing code, run the targeted check, then the release-gate subset listed in the task.
- [ ] Do not claim a fix is complete unless the command output has been read and recorded in this file.
- [ ] Do not delete local runtime data, certs, or logs with broad recursive commands. Review exact paths first.
- [ ] Do not revert unrelated user changes. If new dirty files appear, inspect them and work around them.
- [ ] If a session is interrupted or corrupted, resume from the "Recovery Checklist" before doing anything else.

## Progress Log

Update this table after each completed task.

| Task | Date | Branch | Commit | Verification run | Result | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Plan created | 2026-05-01 | current | none | `Test-Path docs/superpowers/plans/2026-05-01-public-readiness-remediation.md` | passed | Durable checklist created for future work. |
| Task 1 baseline | 2026-05-01 | codex/public-readiness | 407abcb | `python -m pytest -n 2 --dist=loadfile --tb=short --ignore=test_coin_prep.py --ignore=test_coin_prep_v2.py --ignore=test_offer_create.py` | passed | 2798 passed, 4 skipped in 183.32s. |
| Task 2 git hygiene | 2026-05-01 | codex/public-readiness | 407abcb | `python scripts/check_tracked_secrets.py`; `git ls-files --others --exclude-standard`; `gitleaks version` | partial | Secret scan passed; only plan doc was untracked and unignored before commit; Gitleaks unavailable; remaining local files intentionally left for user review. |
| Task 3 branch/version consistency | 2026-05-01 | codex/public-readiness | 0f1709c | `rg -n "\bmaster\b|\btest\b" CONTRIBUTING.md docs scripts README.md`; `python desktop_app.py --help`; `python -m ruff check . --select E9,F821` | passed | No stale public branch references found outside expected test wording and the plan itself; app reports v1.2.4; Ruff crash-class lint passed. |
| Task 4 public docs | 2026-05-01 | codex/public-readiness | 70afd95 | `rg "THIRD_PARTY_NOTICES|CHANGELOG|SECURITY|SUPPORT|Known Limitations|Tech Stack|Project Structure" README.md SECURITY.md SUPPORT.md CHANGELOG.md THIRD_PARTY_NOTICES.md`; `python -m ruff check . --select E9,F821`; `python scripts/check_tracked_secrets.py`; `git diff --check` | partial | Docs/community files added and checks passed. README screenshot/demo deferred because local screenshot folder is left for user review. |
| Task 5 build reproducibility | 2026-05-01 | codex/public-readiness | 2e3fa9c | `python -m pip install -r requirements-dev.txt`; `python -m py_compile build.py`; `python -m ruff check . --select E9,F821`; `python build.py --no-clean` | partial | Install command hit local pywebview permission issue in user site-packages; build.py compiles; Ruff passed; PyInstaller build succeeded. |
| Task 6 Splash runtime safety | 2026-05-01 | codex/public-readiness | 876fb8e | `python -m pytest tests/test_splash_runtime_paths.py tests/test_splash_receive.py tests/test_plan_04_22_splash_settings.py tests/test_bot_health_splash_daemon.py -q`; `python -m ruff check . --select E9,F821`; `python -m py_compile src\catalyst\splash_setup.py src\catalyst\splash_node.py` | passed | Splash binaries install under user data; node prefers user-data binary; downloads fail closed without SHA256 unless explicit override is set. |
| Task 7 local API route/security | 2026-05-01 | codex/public-readiness | d95a025 | `python -m pytest tests/test_security_guardrails_source.py tests/test_api_local_guard.py tests/test_plan_04_22_splash_settings.py tests/test_plan_03_15_splash_receive_path_integration.py -q`; `python -m ruff check . --select E9,F821`; `python scripts/check_tracked_secrets.py`; `python scripts/check_env_example.py`; `python -m py_compile src\catalyst\api_server.py src\catalyst\blueprints\splash.py` | passed | 77 tests passed. `/console` safely returns 404, startup external links are direct URLs, CORS reflects loopback origins only, and Splash incoming rejects non-loopback Origin plus non-JSON requests. Manual browser click-through not run in this session. |
| Task 8 tooling/CI tightening | 2026-05-01 | codex/public-readiness | 16b7da5 | `python -m ruff check .`; `python -m bandit -r src --ini .bandit -ll`; `python -m pip_audit -r requirements.txt -r requirements-dev.txt`; `Push-Location tests; python -m pytest -n 2 --dist=loadfile --tb=short --ignore=test_coin_prep.py --ignore=test_coin_prep_v2.py --ignore=test_offer_create.py --cov=..\src\catalyst --cov-report=term-missing; Pop-Location` | partial | Ruff config narrowed to the passing public gate (`E9`, `F821`); CI now uses config-based Ruff and coverage reporting. Full Ruff remains 234 findings and Ruff format would touch 249 files, so full lint/formatter enforcement is deferred. Coverage run reported 2806 passed, 4 skipped, 41% total coverage. |
| Task 9 database boundary first pass | 2026-05-01 | codex/public-readiness | 7a38439 | `python -m pytest tests/test_market_db_boundary.py -q`; `python -m pytest tests -q -k "market or database"`; `python -m pytest tests/test_market_db_boundary.py tests/test_plan_02_30_database_unit.py tests/test_plan_04_16_to_20_remaining_endpoints.py -q`; `Push-Location tests; python -m pytest -n 2 --dist=loadfile --tb=short --ignore=test_coin_prep.py --ignore=test_coin_prep_v2.py --ignore=test_offer_create.py; Pop-Location`; `python -m ruff check .`; `python -m py_compile src\catalyst\database.py src\catalyst\blueprints\market.py` | partial | Removed the direct `sqlite3.connect(DB_PATH)` from `blueprints/market.py` and moved the spare-coin query behind `database.get_smallest_free_tier_spare()`. Main suite passed with 2807 passed, 4 skipped. Wider direct `conn.execute` cleanup remains. |
| Task 10 logging/frontend safety first pass | 2026-05-01 | codex/public-readiness | 049a648 | `python -m pytest tests/test_security_guardrails_source.py::SecurityGuardrailSourceTests::test_frontend_console_calls_are_debug_gated -q`; `python -m pytest tests/test_security_guardrails_source.py tests/test_frontend_diagnostics_layout.py tests/test_api_local_guard.py -q`; JS extracted from `bot_gui.html` through `node --check`; `python -m ruff check .`; `git diff --check` | partial | Red test failed before implementation, then targeted tests passed with 32 passed. Direct frontend `console.*` calls are now gated behind `window.__CATALYST_DEBUG_LOGS`; current counts are `print_count=604`, `console_count=0`, `html_safety_count=340`. Python print cleanup and broad `innerHTML` hardening remain deferred to smaller slices. |
| Task 11 public-readiness smoke coverage | 2026-05-01 | codex/public-readiness | e4ee733 | `python -m pytest tests/test_public_readiness_smoke.py -q`; `python -m pytest tests/e2e/test_smoke.py --e2e -q`; `python -m pytest tests/test_public_readiness_smoke.py tests/test_api_local_guard.py tests/test_plan_04_09_sage_wallet_endpoints.py tests/test_plan_04_22_splash_settings.py tests/test_plan_04_05_pnl_endpoints.py tests/test_database_boost_migration.py -q`; `python -m ruff check .`; `python -m playwright install chromium`; `Push-Location tests; python -m pytest -n 2 --dist=loadfile --tb=short --ignore=test_coin_prep.py --ignore=test_coin_prep_v2.py --ignore=test_offer_create.py; Pop-Location` | passed | Added boundary smoke coverage for first-launch config seeding under isolated `CMM_DATA_DIR`, safe wallet failures, Splash setup unavailable state, token-exempt route loopback enforcement, stale open-external GET proxy behavior, destructive reset token/confirmation gates, E2E nav-view switching after simulated startup gates, and Data Reset destructive-confirmation rendering. E2E initially exposed a test assumption: startup overlay intentionally blocks nav until gates complete; test was corrected to reveal the post-gate shell. Final results: 6 public smoke tests + 6 subtests passed, E2E 12 passed, public/API subset 117 passed + 6 subtests, full non-live suite 2814 passed and 12 skipped. |
| Task 12 large-file decomposition planning | 2026-05-01 | codex/public-readiness | 01f184b | `Test-Path docs/tech_debt_public_refactor_plan.md`; `rg "First Extractable Unit|Protecting Tests|bot_gui.html|bot_loop.py|smart_defaults.py|coin_prep_worker.py|coin_manager.py" docs/tech_debt_public_refactor_plan.md`; `python -m ruff check .`; `git diff --check` | passed | Documentation-only plan for post-release refactors; no trading code changes. Ruff passed and `git diff --check` reported no whitespace errors. |
| Task 13 final release gate | 2026-05-01 | codex/public-readiness | pending | `python scripts/check_env_example.py`; `python scripts/check_tracked_secrets.py`; `python -m ruff check .`; `python -m ruff format --check .`; `python -m bandit -r src --ini .bandit -ll`; `python -m pip_audit -r requirements.txt -r requirements-dev.txt`; main non-live pytest; E2E smoke; `python build.py --no-clean`; `git status --short`; `git status --short --ignored`; `git log --oneline --decorate -10`; `git diff --check` | partial | Env example, tracked-secret scan, Ruff check, Bandit, pip-audit, main non-live pytest (2814 passed, 12 skipped), E2E smoke (12 passed), and PyInstaller build all passed. `ruff format --check .` remains a blocker: 251 files would be reformatted. Normal `git status --short` is clean; ignored local runtime/sensitive files remain for user review tomorrow. Manual GitHub settings still need owner confirmation before public visibility change. |

## Recovery Checklist

Run these commands at the start of every new session or after context corruption.

- [ ] Confirm location:

```powershell
pwd
git status --short
git branch --show-current
```

Expected: working directory is `C:\catalyst`; branch is `codex/public-readiness` once Task 1 has started.

- [ ] Read this plan before editing:

```powershell
Get-Content docs\superpowers\plans\2026-05-01-public-readiness-remediation.md -TotalCount 260
```

- [ ] Find the first unchecked task in this file.
- [ ] Inspect current changes:

```powershell
git diff --stat
git diff --check
```

Expected: no whitespace errors from `git diff --check`.

- [ ] If there are unexpected dirty files, inspect them and record the decision in the Progress Log before continuing.

---

## Task 1: Branch And Baseline Verification

**Files:**
- Modify: none
- Verify: full repository state

- [x] Create or switch to the working branch:

```powershell
git switch -c codex/public-readiness
```

If the branch already exists:

```powershell
git switch codex/public-readiness
```

- [x] Record the current dirty state:

```powershell
git status --short --ignored
```

- [x] Run baseline checks before changing code:

```powershell
python --version
python desktop_app.py --help
python scripts/check_env_example.py
python scripts/check_tracked_secrets.py
python -m ruff check . --select E9,F821
python -m bandit -r src --ini .bandit -ll
python -m pip_audit -r requirements.txt -r requirements-dev.txt
```

Expected: Python is 3.12.x; all listed checks pass.

- [x] Run the current main test suite:

```powershell
Push-Location tests
python -m pytest -n 2 --dist=loadfile --tb=short --ignore=test_coin_prep.py --ignore=test_coin_prep_v2.py --ignore=test_offer_create.py
Pop-Location
```

Expected: the known baseline is `2798 passed, 4 skipped`.

- [x] Record results in the Progress Log.

---

## Task 2: Public Safety And Git Hygiene

**Files:**
- Modify: `.gitignore`
- Verify: `git status --short --ignored`, tracked secret scan, optional Gitleaks

- [x] Add these ignore rules to `.gitignore` in the section for local/generated artifacts:

```gitignore
website-screenshots/
CODEX_REVIEW_REPORT.md
tmp_db_debug_*/
tmp_db_reconcile_*/
.coverage
htmlcov/
coverage/
test-results/
playwright-report/
*.orig
*.rej
*.bak
startup.log
```

- [x] Review local-only files before deleting or moving anything:

```powershell
git status --short --ignored
Get-ChildItem -Force | Sort-Object Name | Select-Object Name,Mode,Length
```

- [ ] Move or delete only reviewed local artifacts: `.env`, cert folders, live DBs, logs, backups, outputs, `.e2e_data`, screenshots, build/dist folders, local `splash.exe`, and agent scratch folders.

Deferred by user request on 2026-05-01: leave remaining local files for user review tomorrow. Generated caches, build outputs, logs/status files, empty throwaway DBs, and temp reconcile DB files were cleaned before implementation continued.

- [x] Re-run public safety checks:

```powershell
git status --short --ignored
python scripts/check_tracked_secrets.py
```

Expected: no untracked sensitive files are visible; tracked secret scan passes.

- [x] If Gitleaks is installed, run full-history scanning:

```powershell
gitleaks version
gitleaks detect --source . --log-opts="--all"
```

Expected: no findings. If Gitleaks is unavailable, record `Gitleaks unavailable` in the Progress Log and do not claim full-history secret scanning is complete.

Result on 2026-05-01: `gitleaks` was not installed, so full-history scanning remains not completed.

- [x] Commit:

```powershell
git add .gitignore
git commit -m "chore: tighten public git hygiene"
```

---

## Task 3: Branch, Release, And Version Consistency

**Files:**
- Modify: `CONTRIBUTING.md`
- Modify: `docs/PUBLIC_RELEASE_CHECKLIST.md`
- Modify: `scripts/apply_branch_protection.sh`
- Modify: `src/catalyst/_version.py`
- Modify: `README.md` if it describes release/version behavior

- [x] Replace public branch instructions that say `master` or `test` with `main` where they describe the current repository branch.

- [x] Update `scripts/apply_branch_protection.sh` so it protects `main` and uses current CI check names.

- [x] Resolve the version mismatch. Choose one of these two explicit options and record the decision:

Option A, source version matches latest public tag:

```python
__version__ = "1.2.4"
```

Option B, source version is intentionally overwritten by release CI:

```python
__version__ = "1.2.1"
```

If Option B is used, add a README sentence explaining: "Release builds overwrite `src/catalyst/_version.py` from the Git tag during CI."

Decision on 2026-05-01: Option A. `src/catalyst/_version.py` now reports `1.2.4`.

- [x] Verify stale branch references:

```powershell
rg "master|test" CONTRIBUTING.md docs scripts README.md
```

Expected: only intentional historical or command-output references remain.

- [x] Verify version behavior:

```powershell
python desktop_app.py --help
```

Expected: reported version matches the chosen version policy.

- [x] Commit:

```powershell
git add CONTRIBUTING.md docs/PUBLIC_RELEASE_CHECKLIST.md scripts/apply_branch_protection.sh src/catalyst/_version.py README.md
git commit -m "docs: align public branch and version metadata"
```

---

## Task 4: Public Documentation And Community Files

**Files:**
- Create: `CHANGELOG.md`
- Create: `THIRD_PARTY_NOTICES.md`
- Create: `.github/ISSUE_TEMPLATE/documentation.yml`
- Create or modify: `.github/ISSUE_TEMPLATE/security.md` or `.github/ISSUE_TEMPLATE/config.yml`
- Create: `.gitattributes`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `SUPPORT.md`

- [x] Add `CHANGELOG.md` with entries for the currently published tags. Use this structure:

```markdown
# Changelog

All notable changes to CATalyst are recorded here.

## v1.2.4

- Public release metadata and packaging fixes.

## v1.2.1

- Desktop application baseline with Flask, PyWebView, SQLite WAL, and Sage wallet integration.
```

- [x] Add `THIRD_PARTY_NOTICES.md` covering at least: Dexie, Sage, TibetSwap, Spacescan, MonkeyZoo, Google Fonts, and any bundled icons/images. For each asset, record source, owner/project, license or trademark note, and local file path.

- [x] Add a documentation issue template:

```yaml
name: Documentation issue
description: Report unclear, missing, or incorrect documentation.
title: "[Docs]: "
labels: ["documentation"]
body:
  - type: textarea
    id: location
    attributes:
      label: Documentation location
      description: Link or filename.
    validations:
      required: true
  - type: textarea
    id: problem
    attributes:
      label: What is unclear or incorrect?
    validations:
      required: true
  - type: textarea
    id: suggested-change
    attributes:
      label: Suggested change
    validations:
      required: false
```

- [x] Add or update issue-template config so security reports point to `SECURITY.md` and private vulnerability reporting, not a public issue.

- [x] Add `.gitattributes`:

```gitattributes
* text=auto
*.py text eol=lf
*.md text eol=lf
*.html text eol=lf
*.css text eol=lf
*.js text eol=lf
*.png binary
*.jpg binary
*.jpeg binary
*.ico binary
*.icns binary
*.db binary
*.sqlite binary
*.exe binary
```

- [x] Expand `README.md` with these sections near the top or in the existing setup area:

```markdown
## Tech Stack

- Python 3.12
- Flask HTTP API and Server-Sent Events
- PyWebView desktop shell
- SQLite WAL-mode local database
- Vanilla HTML/CSS/JavaScript frontend
- Sage wallet RPC integration
- Dexie, TibetSwap, Spacescan, Coinset, and Splash integrations
- PyInstaller desktop builds

## Known Limitations

CATalyst is a local desktop trading tool. It assumes a trusted local machine, a configured wallet, and network access to third-party market data services. Trading and market-making can lose funds.
```

- [x] Add setup notes for Linux WebKit/GTK, Windows WebView2, Playwright Chromium, Sage wallet, Splash, and optional external APIs.

- [ ] Add a screenshot or demo image near the top of the README after verifying the image is safe to publish and attributed.

Deferred by user request on 2026-05-01: local screenshot material remains untouched for user review tomorrow.

- [x] Verify docs links:

```powershell
rg "THIRD_PARTY_NOTICES|CHANGELOG|SECURITY|SUPPORT|Known Limitations|Tech Stack" README.md SECURITY.md SUPPORT.md CHANGELOG.md THIRD_PARTY_NOTICES.md
```

- [x] Commit:

```powershell
git add README.md SECURITY.md SUPPORT.md CHANGELOG.md THIRD_PARTY_NOTICES.md .gitattributes .github/ISSUE_TEMPLATE
git commit -m "docs: add public release documentation"
```

---

## Task 5: Build And Setup Reproducibility

**Files:**
- Modify: `requirements-dev.txt`
- Optional create: `requirements-build.txt`
- Modify: `build.py`
- Modify: `README.md`
- Modify: `.github/workflows/code-quality.yml`
- Modify release workflow files under `.github/workflows/`

- [x] Add PyInstaller explicitly to the build/dev dependency path:

```text
pyinstaller>=6,<7
```

- [x] Replace the auto-install behavior in `build.py`. The new behavior should fail with a clear message if PyInstaller is missing:

```python
def _ensure_pyinstaller():
    """Check PyInstaller is importable and fail with setup guidance if missing."""
    try:
        import PyInstaller  # noqa: F401
        import PyInstaller.__main__  # noqa: F401
        print("  PyInstaller found.")
    except Exception:
        print("  ERROR: PyInstaller is not installed.")
        print("  Install build dependencies with: python -m pip install -r requirements-dev.txt")
        raise SystemExit(1)
```

- [x] Update docs to use `python -m pip`, `python -m pytest`, and the explicit build dependency install.

- [x] Update CI to call `python -m pytest` instead of bare `pytest`.

- [x] Add a release build smoke step where practical:

```powershell
python build.py --no-clean
```

- [x] Verify:

```powershell
python -m pip install -r requirements-dev.txt
python build.py --no-clean
```

Expected: build completes or fails only for a documented platform prerequisite. Do not claim build readiness if this command is not run.

Result on 2026-05-01: `python -m pip install -r requirements-dev.txt` failed because the local user site-packages directory could not overwrite `webview\lib\Microsoft.Web.WebView2.Core.dll`. `python build.py --no-clean` succeeded with PyInstaller 6.19.0 and produced `dist\Catalyst\Catalyst.exe`.

- [x] Commit:

```powershell
git add requirements-dev.txt build.py README.md .github/workflows
git commit -m "build: make desktop build dependencies explicit"
```

---

## Task 6: Splash Runtime Safety

**Files:**
- Modify: `src/catalyst/splash_setup.py`
- Modify: `src/catalyst/splash_node.py`
- Modify: `src/catalyst/user_paths.py` only if a small helper is useful
- Add tests under `tests/` for install-path behavior

- [x] Write a failing test proving Splash install paths use user data, not `src/catalyst`.

Suggested test file: `tests/test_splash_runtime_paths.py`

```python
import os

import splash_setup
from user_paths import data_dir


def test_splash_install_path_lives_under_user_data(monkeypatch, tmp_path):
    monkeypatch.setenv("CMM_DATA_DIR", str(tmp_path))
    install_info = splash_setup.get_install_info()
    install_path = os.path.abspath(install_info["install_path"])
    assert install_path.startswith(os.path.abspath(data_dir()))
    assert "src" not in os.path.relpath(install_path, os.getcwd()).split(os.sep)
```

- [x] Run the failing test:

```powershell
python -m pytest tests/test_splash_runtime_paths.py -q
```

Expected before implementation: FAIL because install path currently points under `src/catalyst`.

Result on 2026-05-01: failed as expected. `detect_platform()` returned `C:\catalyst\src\catalyst\splash.exe` and `SplashNode.find_binary()` preferred the local source binary.

- [x] Change Splash install location to a dedicated folder under `user_paths.data_dir()`, such as:

```text
%APPDATA%\Catalyst\splash\
```

- [x] Update `splash_node.py` to find Splash in the new user-data location.

- [x] Make checksum verification fail closed unless a checksum or signature is available. If a developer override is needed, name it explicitly, such as `CATALYST_ALLOW_UNVERIFIED_SPLASH_DOWNLOAD=1`, and document it as unsafe.

- [x] Run targeted tests:

```powershell
python -m pytest tests/test_splash_runtime_paths.py -q
python -m pytest tests -q --ignore=tests/test_coin_prep.py --ignore=tests/test_coin_prep_v2.py --ignore=tests/test_offer_create.py
```

- [x] Commit:

```powershell
git add src/catalyst/splash_setup.py src/catalyst/splash_node.py tests/test_splash_runtime_paths.py
git commit -m "fix: store splash runtime files in user data"
```

---

## Task 7: Local API Route And Security Fixes

**Files:**
- Modify: `src/catalyst/api_server.py`
- Modify: `bot_gui.html`
- Add or update API/frontend tests under `tests/`

- [x] Add or update tests for `/console`. Decide the behavior first:
  - remove the route and expect 404, or
  - restore `bot_console.html` and expect 200.

Preferred public behavior: remove or disable the stale route and return 404.

- [x] Add a test for `/api/open-external` showing GET links do not break browser-only use. Preferred behavior: browser mode uses direct external URLs; desktop mode uses POST through the existing JS helper.

- [x] Fix startup links in `bot_gui.html` that currently point to `/api/open-external?url=...` as normal GET links.

- [x] Harden `/api/splash/incoming`. Add one of:
  - a random per-run local hook token,
  - a shared local secret configured in the Splash integration,
  - strict Origin plus Content-Type checks and explicit documentation of the residual local-machine risk.

- [x] Replace hardcoded CORS origin with the configured local server origin or remove it where same-origin browser calls do not need it.

- [x] Verify:

```powershell
python -m pytest tests -q --ignore=tests/test_coin_prep.py --ignore=tests/test_coin_prep_v2.py --ignore=tests/test_offer_create.py
python desktop_app.py --help
```

- [ ] Manually run browser-only mode and click startup external links:

```powershell
python desktop_app.py --flask
```

Expected: startup page loads; external links open or navigate without 405 errors; stale `/console` no longer 500s.

- [x] Commit:

```powershell
git add src/catalyst/api_server.py bot_gui.html tests
git commit -m "fix: repair local API route behavior"
```

---

## Task 8: Tooling And CI Tightening

**Files:**
- Modify: `ruff.toml`
- Optional create: `pyproject.toml`
- Modify: `.github/workflows/code-quality.yml`
- Modify affected Python files only when fixing lint findings

- [x] Decide whether full `ruff.toml` is the standard. Preferred public behavior: make full Ruff pass or narrow `ruff.toml` to the rules CI actually enforces.

Decision on 2026-05-01: narrow `ruff.toml` to the public CI gate that already passes (`E9`, `F821`). Full Ruff currently reports 234 findings and should be handled as a separate cleanup pass rather than mixed into public-readiness fixes.

- [x] Run full Ruff:

```powershell
python -m ruff check .
```

Expected before cleanup: currently reports many unused-import and Bugbear findings.

Result on 2026-05-01: 234 findings, mostly legacy unused imports and Bugbear findings. Deferred by explicit scope decision above.

- [ ] Fix or explicitly suppress intentional unused imports. For blueprint registration imports, prefer a clear registration function or a narrow `# noqa: F401` block with a comment.

Deferred on 2026-05-01: no broad lint edits made. The current public gate is narrowed to runtime-breaking checks only.

- [ ] Add formatter check to CI:

```powershell
python -m ruff format --check .
```

Deferred on 2026-05-01: `python -m ruff format --check .` would reformat 249 files. Formatter adoption should be its own mechanical PR after behavior-sensitive public-readiness fixes land.

- [x] Add coverage reporting without a threshold first:

```powershell
python -m pytest --cov=src/catalyst --cov-report=term-missing
```

- [x] Verify:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m bandit -r src --ini .bandit -ll
python -m pip_audit -r requirements.txt -r requirements-dev.txt
```

Result on 2026-05-01: `python -m ruff check .`, Bandit medium/high scan, pip-audit, and main pytest with coverage all passed. `python -m ruff format --check .` remains intentionally deferred as noted above.

- [x] Commit:

```powershell
git add ruff.toml pyproject.toml .github/workflows/code-quality.yml src tests
git commit -m "ci: enforce public quality checks"
```

---

## Task 9: Database Boundary Cleanup

**Files:**
- Modify: `src/catalyst/database.py`
- Modify first-pass callers:
  - `src/catalyst/blueprints/market.py`
  - `src/catalyst/api_server.py`
  - `src/catalyst/blueprints/offers.py`
- Add or update tests under `tests/`

- [x] Identify direct SQL outside `database.py`:

```powershell
rg -n "sqlite3\.connect|get_connection\(\).*execute|\bconn\.execute\(" src\catalyst -g "*.py"
```

- [x] Start with the direct `sqlite3.connect(DB_PATH)` use in `src/catalyst/blueprints/market.py`.

- [x] Add database helper functions for the specific queries being moved. Keep helpers narrow and named by intent, for example:

```python
def get_latest_market_snapshot():
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM market_snapshots ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
```

Use actual table and column names from the existing query when implementing.

- [x] Replace caller SQL with database helper calls.

Result on 2026-05-01: added `database.get_smallest_free_tier_spare()` and changed `/api/debug/sage-single-offer-test` to use it. Broader `conn.execute` callers in `api_server.py`, `blueprints/offers.py`, and other modules remain for later slices.

- [x] Run targeted tests for the touched blueprint:

```powershell
python -m pytest tests -q -k "market or database"
```

Result on 2026-05-01: 173 passed, 2638 deselected, 3 collection warnings from the known ignored live-integration test classes.

- [x] Run main suite:

```powershell
Push-Location tests
python -m pytest -n 2 --dist=loadfile --tb=short --ignore=test_coin_prep.py --ignore=test_coin_prep_v2.py --ignore=test_offer_create.py
Pop-Location
```

Result on 2026-05-01: 2807 passed, 4 skipped in 180.09s.

- [x] Commit:

```powershell
git add src/catalyst/database.py src/catalyst/blueprints/market.py src/catalyst/api_server.py src/catalyst/blueprints/offers.py tests
git commit -m "refactor: centralize high-risk database access"
```

---

## Task 10: Logging And Frontend Safety Cleanup

**Files:**
- Modify: Python files with production `print()` calls
- Modify: `bot_gui.html`
- Add or update tests where behavior changes

- [x] Count current logging cleanup scope:

```powershell
rg -n "print\(" src\catalyst desktop_app.py
rg -n "console\.(log|warn|error|debug)" bot_gui.html
rg -n "innerHTML|insertAdjacentHTML|onclick=" bot_gui.html
```

- [ ] In Python application code under `src/catalyst`, replace production `print()` with `slog(category, message, data=None, level="info")`.

Deferred on 2026-05-01: current scope is 604 matches across `src\catalyst` and `desktop_app.py`, including startup and integration-style output. This should be split by module instead of changed mechanically.

- [x] Keep intentional CLI/build output in `build.py` and guarded desktop startup output in `desktop_app.py` unless changing it is low-risk.

- [x] In `bot_gui.html`, gate diagnostic `console.*` calls behind a debug flag:

```javascript
const DEBUG_LOGS = Boolean(window.__CATALYST_DEBUG_LOGS);
function debugLog(...args) {
    if (DEBUG_LOGS) console.log(...args);
}
```

- [ ] Do not rewrite all `innerHTML` at once. Start with server-sourced data paths and convert to `textContent`, DOM node creation, or escaped rendering.

Deferred on 2026-05-01: current scope is 340 `innerHTML` / `insertAdjacentHTML` / inline `onclick=` matches in `bot_gui.html`; no broad rewrite was made in this pass.

- [x] Verify:

```powershell
python -m ruff check . --select E9,F821
python -m pytest tests -q --ignore=tests/test_coin_prep.py --ignore=tests/test_coin_prep_v2.py --ignore=tests/test_offer_create.py
```

Result on 2026-05-01: source-guard red test failed before implementation, then `python -m pytest tests/test_security_guardrails_source.py tests/test_frontend_diagnostics_layout.py tests/test_api_local_guard.py -q` passed with 32 passed; extracted JS from `bot_gui.html` passed `node --check`; `python -m ruff check .` passed; `git diff --check` reported only CRLF normalization warnings.

- [x] Commit:

```powershell
git add src/catalyst bot_gui.html tests
git commit -m "chore: reduce production debug output"
```

---

## Task 11: Public-Readiness Smoke Tests

**Files:**
- Add or modify tests under `tests/`
- Add or modify E2E tests under `tests/e2e/`

- [x] Add tests for first-launch config behavior with isolated `CMM_DATA_DIR`.
- [x] Add tests for missing wallet or unavailable RPC returning user-safe errors.
- [x] Add tests for Splash unavailable and Splash install path.
- [x] Add tests for `/api/open-external` behavior.
- [x] Add tests for stale `/console` behavior.
- [x] Add tests for DB recovery or migration from an older schema fixture.
- [x] Add tests for destructive endpoints requiring local token and confirmation text.
- [x] Add tests for token-exempt route protections.
- [x] Add Playwright E2E smoke tests for startup, dashboard, settings, offers, logs, and destructive dialogs.

Result on 2026-05-01: created `tests/test_public_readiness_smoke.py`; expanded `tests/e2e/test_smoke.py` with post-startup navigation checks for Dashboard, Offers, P&L, Market Intel, Settings, Logs, Data Reset, and the Data Reset destructive confirmation modal. Stale `/console`, DB migration, and some Splash install-path coverage already existed and were included in the verification subset rather than duplicated.

- [x] Run:

```powershell
Push-Location tests
python -m pytest -n 2 --dist=loadfile --tb=short --ignore=test_coin_prep.py --ignore=test_coin_prep_v2.py --ignore=test_offer_create.py
Pop-Location
python -m playwright install chromium
python -m pytest tests/e2e --e2e
```

Result on 2026-05-01: `python -m playwright install chromium` exited 0; `python -m pytest tests/e2e/test_smoke.py --e2e -q` passed with 12 passed. The full non-live suite passed with 2814 passed, 12 skipped in 180.80s.

- [x] Commit:

```powershell
git add tests
git commit -m "test: add public readiness smoke coverage"
```

---

## Task 12: Large-File Decomposition Planning

**Files:**
- Create: `docs/tech_debt_public_refactor_plan.md`
- Do not refactor trading logic in this task

- [x] Document the decomposition plan for:
  - `bot_gui.html`
  - `src/catalyst/bot_loop.py`
  - `src/catalyst/blueprints/smart_defaults.py`
  - `src/catalyst/coin_prep_worker.py`
  - `src/catalyst/coin_manager.py`

- [x] For each file, identify the first extractable unit and the test that will protect it.

- [ ] Commit:

```powershell
git add docs/tech_debt_public_refactor_plan.md
git commit -m "docs: plan post-release refactors"
```

---

## Task 13: Final Release Gate

**Files:**
- Modify only if a verification failure identifies a necessary fix

- [x] Run final source checks:

```powershell
python scripts/check_env_example.py
python scripts/check_tracked_secrets.py
python -m ruff check .
python -m ruff format --check .
python -m bandit -r src --ini .bandit -ll
python -m pip_audit -r requirements.txt -r requirements-dev.txt
```

Result on 2026-05-01: `check_env_example.py`, `check_tracked_secrets.py`, `python -m ruff check .`, Bandit medium/high scan, and pip-audit passed. `python -m ruff format --check .` failed because 251 files would be reformatted; formatter adoption remains a separate blocker.

- [x] Run final tests:

```powershell
Push-Location tests
python -m pytest -n 2 --dist=loadfile --tb=short --ignore=test_coin_prep.py --ignore=test_coin_prep_v2.py --ignore=test_offer_create.py
Pop-Location
```

Result on 2026-05-01: main non-live suite passed with 2814 passed, 12 skipped in 176.92s.

- [x] Run E2E if browser tooling is available:

```powershell
python -m playwright install chromium
python -m pytest tests/e2e --e2e
```

Result on 2026-05-01: `python -m pytest tests/e2e/test_smoke.py --e2e -q` passed with 12 passed in 22.13s.

- [x] Run build smoke:

```powershell
python build.py --no-clean
```

Result on 2026-05-01: build succeeded and produced `dist\Catalyst\Catalyst.exe`; PyInstaller warned only that hidden import `importlib_resources.trees` was not found.

- [x] Check final Git state:

```powershell
git status --short
git log --oneline --decorate -10
```

Result on 2026-05-01: normal `git status --short` was clean. `git status --short --ignored` still shows ignored local-only files including `.env`, cert folders, DBs, build outputs, `.e2e_data`, caches, screenshots, and two permission-denied temp dirs; these were intentionally left for user review tomorrow.

- [ ] Confirm manual GitHub settings before changing visibility:
  - repository description, homepage, and topics are set
  - default branch is `main`
  - branch protection enabled for `main`
  - PRs required before merge
  - CI required before merge
  - force pushes disabled
  - auto-delete merged branches enabled
  - private vulnerability reporting enabled
  - secret scanning and push protection enabled
  - Dependabot alerts and security updates enabled
  - merge strategy intentionally selected

- [x] Prepare final summary with:
  - files changed
  - checks run and exact results
  - checks not run and why
  - remaining risks

Result on 2026-05-01: final summary prepared in chat. Remaining risks are the failing formatter gate, full-history Gitleaks still unavailable/not run, manual GitHub settings not confirmed, and ignored local runtime/sensitive files still present for owner cleanup.

---

## Recommended Commit Order

- [x] `chore: tighten public git hygiene`
- [x] `docs: align public branch and version metadata`
- [x] `docs: add public release documentation`
- [x] `build: make desktop build dependencies explicit`
- [x] `fix: store splash runtime files in user data`
- [x] `fix: repair local API route behavior`
- [x] `ci: enforce public quality checks`
- [x] `refactor: centralize high-risk database access`
- [x] `chore: reduce production debug output`
- [x] `test: add public readiness smoke coverage`
- [x] `docs: plan post-release refactors`

## Completion Definition

This plan is complete only when:

- [ ] Each task checkbox is checked or explicitly marked deferred with a reason in the Progress Log.
- [ ] The final source checks pass.
- [x] The main pytest suite passes.
- [x] E2E results are recorded.
- [x] Build smoke result is recorded.
- [ ] GitHub manual settings are checked.
- [ ] No sensitive local files are visible to Git.
- [x] The final summary includes remaining risks.
