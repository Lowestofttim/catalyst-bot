# Sage Setup Fingerprint Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users configure Sage certificates and switch/persist wallet fingerprints from the app UI without editing `.env`.

**Architecture:** Keep Sage-specific persistence in `src/catalyst/blueprints/sage.py`, mirror it through `src/catalyst/app_bridge.py`, and reuse the existing startup cert panel plus wallet picker modal in `bot_gui.html`. Add tests before production edits for endpoint behavior and safety guards.

**Tech Stack:** Python Flask, PyWebView bridge, vanilla HTML/CSS/JS, pytest/unittest.

---

### Task 1: Sage Cert Suggestions And Fingerprint Persistence Endpoints

**Files:**
- Modify: `src/catalyst/blueprints/sage.py`
- Modify: `src/catalyst/sage_node.py`
- Test: `tests/test_plan_04_09_sage_wallet_endpoints.py`

- [ ] **Step 1: Write failing endpoint tests**

Add tests for:

```python
def test_cert_candidates_returns_default_wallet_crt_paths(self):
    resp = self.client.get("/api/sage/cert-candidates", environ_base=self._LOOPBACK)
    self.assertEqual(resp.status_code, 200)
    body = resp.get_json()
    self.assertTrue(body.get("success"))
    self.assertIsInstance(body.get("candidates"), list)
    self.assertTrue(any(str(path).endswith(os.path.join("ssl", "wallet.crt")) for path in body["candidates"]))
```

```python
def test_set_fingerprint_rejects_running_bot(self):
    fake_bot = MagicMock()
    fake_bot.is_running.return_value = True
    with patch.object(api_server, "bot", fake_bot):
        resp = self._post("/api/sage/fingerprint", {"fingerprint": "12345678"})
    self.assertEqual(resp.status_code, 409)
```

```python
def test_set_fingerprint_persists_and_triggers_start(self):
    fake_cfg = MagicMock()
    fake_cfg.update.return_value = True
    with patch.object(api_server, "bot", None), \
         patch.object(api_server, "cfg", fake_cfg), \
         patch("chia_node.trigger_start", return_value={"success": True}) as trigger:
        resp = self._post("/api/sage/fingerprint", {"fingerprint": "12345678"})
    self.assertEqual(resp.status_code, 200)
    self.assertTrue(resp.get_json().get("success"))
    fake_cfg.update.assert_called_once_with("SAGE_FINGERPRINT", "12345678", source="sage_wallet_settings")
    trigger.assert_called_once_with("12345678")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/test_plan_04_09_sage_wallet_endpoints.py -q`

Expected: fails because `/api/sage/cert-candidates` and `/api/sage/fingerprint` do not exist yet.

- [ ] **Step 3: Implement minimal backend**

Add a public helper in `sage_node.py` that returns candidate `wallet.crt` paths from `_candidate_sage_ssl_dirs()`.

Add routes in `blueprints/sage.py`:

- `GET /api/sage/cert-candidates`
- `POST /api/sage/fingerprint`

The fingerprint route validates a numeric value, rejects while bot is running, calls `cfg.update("SAGE_FINGERPRINT", value, source="sage_wallet_settings")`, and calls `chia_node.trigger_start(value)`.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_plan_04_09_sage_wallet_endpoints.py -q`

Expected: pass.

### Task 2: Desktop Bridge And File Picker

**Files:**
- Modify: `src/catalyst/app_bridge.py`
- Test: existing endpoint tests; bridge is a thin wrapper.

- [ ] **Step 1: Add bridge methods**

Add bridge wrappers for:

- `get_sage_cert_candidates()`
- `set_sage_fingerprint(body=None)`
- `browse_sage_cert()`

`browse_sage_cert()` uses `webview.windows[0].create_file_dialog(webview.OPEN_DIALOG, file_types=("Sage wallet certificate (*.crt)", "All files (*.*)"))`, returns the selected path, and falls back to `{success: False}` if unavailable.

- [ ] **Step 2: Map frontend API routing**

Update `bot_gui.html` bridge routing so:

- `GET sage/cert-candidates` maps to `get_sage_cert_candidates`
- `POST sage/fingerprint` maps to `set_sage_fingerprint`

### Task 3: Startup And Settings UI

**Files:**
- Modify: `bot_gui.html`

- [ ] **Step 1: Startup cert panel**

Prefill `#startupCertPath` from `/api/sage/cert-candidates` when showing cert setup. Add a Browse button in desktop mode and keep Auto-Detect/Save behavior unchanged.

- [ ] **Step 2: Wallet Session settings section**

Add a compact section near the top of Settings > Setup with:

- current fingerprint display
- Change Wallet button that opens `showWalletPickerModal()`
- Sage certificate path display/input
- Browse, Auto-Detect, and Save certificate buttons

- [ ] **Step 3: Persist wallet picker selections**

Change `walletPickerSelect()` and startup selection success handling to call `POST /api/sage/fingerprint` when a user explicitly chooses a fingerprint. Keep the existing polling and UI refresh behavior.

### Task 4: Verification And PR

**Files:**
- All modified files.

- [ ] **Step 1: Run targeted tests**

Run: `pytest tests/test_plan_04_09_sage_wallet_endpoints.py -q`

- [ ] **Step 2: Run config/security guard tests**

Run: `pytest tests/test_plan_04_02_config_endpoints.py tests/test_security_guardrails_source.py -q`

- [ ] **Step 3: Inspect git diff**

Run: `git diff --check` and `git status -sb`

- [ ] **Step 4: Commit and open PR**

Commit all intended files, push `codex/sage-setup-fingerprint-settings`, and open a draft PR into `main`.
