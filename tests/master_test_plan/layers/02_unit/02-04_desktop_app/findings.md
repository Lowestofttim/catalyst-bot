# Findings — Slice 02-04

`desktop_app.py` — flag parsing, mode routing.

---

## Assessment: intentionally skipped

`desktop_app.py` is the app entry point. Its testable logic is:
- `argparse` flag handling (`--flask`, `--dev`)
- Port availability check (`check_port_free`)
- Mode routing (PyWebView vs. Flask-only vs. dev)

All of these are launcher/startup code that involves process management,
port binding, and window creation — not suitable for unit testing without
a running process. The `check_port_free` function does a socket connect to
localhost which would interfere in CI environments.

Layer 3 integration tests are the right place for startup flow validation.

**No new tests added. No bugs found.**

---

| Count | Status |
|-------|--------|
| 0 | open bugs |
| 0 | fixed |
| 0 | blocked |
