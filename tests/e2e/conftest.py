"""Pytest fixtures for end-to-end browser tests.

These tests are opt-in: they're skipped unless `--e2e` is passed on the
command line. This keeps the regular `pytest` invocation fast (the e2e
fixtures spawn a real Flask server and a real browser).

Run them with:

    cd tests
    python -m pytest e2e/ --e2e -v

Prerequisites (one-time):

    pip install -r requirements-dev.txt
    python -m playwright install chromium

Stable selector convention
--------------------------
Prefer locators in this order, falling back only when the previous form
isn't usable for the element you need:

    1. Element id (e.g. `#startupReviewSettingsBtn`) — most stable, never
       changes.
    2. `aria-label` via `page.get_by_role("button", name=...)` — stable
       for accessibility-labelled controls and self-documenting.
    3. `data-view` / `data-action` attributes the app already uses for
       its own routing.
    4. Add a `data-testid` attribute to the source HTML *only when* none
       of the above work for an element the test must exercise. Avoid
       speculative `data-testid` additions — they rot.

Visible text changes more often than IDs or accessibility labels, so
avoid `page.get_by_text(...)` for anything load-bearing.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Avoid clashing with the developer's local CATalyst install on port 5000.
_E2E_PORT = int(os.environ.get("CATALYST_E2E_PORT", "5099"))
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / ".e2e_data"


def pytest_addoption(parser):
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="Run end-to-end browser tests (requires Playwright + chromium).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--e2e"):
        return
    skip = pytest.mark.skip(reason="e2e tests are opt-in; pass --e2e to enable")
    for item in items:
        if "e2e" in item.keywords or "e2e" in str(item.fspath).lower():
            item.add_marker(skip)


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.25)
    return False


@pytest.fixture(scope="session")
def flask_server():
    """Spawn the CATalyst Flask server in a subprocess for the test session.

    Uses a dedicated data dir (`.e2e_data/`) so the test bot has its own DB
    and doesn't pollute the developer's real wallet state. The server runs in
    `--flask` mode (no PyWebView window) on a non-default port.
    """
    _DATA_DIR.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["CMM_DATA_DIR"] = str(_DATA_DIR)
    env["CATALYST_FLASK_PORT"] = str(_E2E_PORT)
    # Explicitly avoid pulling the user's real wallet — tests should run
    # against an unconfigured app and exercise the disclaimer/connect flow.
    env.pop("SAGE_FINGERPRINT", None)

    cmd = [sys.executable, "desktop_app.py", "--flask"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        if not _wait_for_port(_E2E_PORT, timeout=30):
            proc.terminate()
            pytest.skip(f"Flask server failed to start on port {_E2E_PORT} within 30s")
        else:
            base_url = f"http://127.0.0.1:{_E2E_PORT}"
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def app_page(flask_server, page):
    """A Playwright page already navigated to the CATalyst dashboard.

    The disclaimer is NOT auto-dismissed — tests that need to skip past it
    should call `dismiss_disclaimer(page)` explicitly so they document the
    flow.
    """
    page.goto(flask_server, wait_until="domcontentloaded")
    return page


def dismiss_disclaimer(page) -> bool:
    """Click 'Continue to wallet connection' if the risk disclaimer is up."""
    btn = page.locator("#startupDisclaimerContinueBtn")
    if btn.count() == 0:
        return False
    if not btn.is_visible():
        return False
    btn.click()
    return True
