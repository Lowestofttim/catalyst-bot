"""End-to-end smoke tests for the CATalyst dashboard.

Scope: verify the app boots, the risk disclaimer renders, and the primary
navigation tabs are present. These tests are deliberately Sage-free — they
prove the static UI shell works, which is what breaks most often when the
HTML/JS is refactored.

Run with:

    cd tests
    python -m pytest e2e/test_smoke.py --e2e -v --headed   # watch in browser
    python -m pytest e2e/test_smoke.py --e2e               # headless

Anything that requires a real Sage connection should live in a separate
file (e.g. `test_full_setup.py`) marked accordingly.
"""
from __future__ import annotations

import re

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect

from .conftest import dismiss_disclaimer

pytestmark = pytest.mark.e2e


def reveal_app_shell_for_nav(page) -> None:
    """Hide startup gates so nav smoke tests can exercise the main shell.

    The real first-run flow intentionally keeps the sidebar blocked until the
    wallet/Splash/Spacescan gates complete. These tests are not validating
    those gates; they validate that the public shell views still switch once
    startup is past them.
    """
    dismiss_disclaimer(page)
    page.evaluate(
        """() => {
            for (const id of ['startupOverlay', 'splashGateOverlay', 'spacescanGateOverlay']) {
                const el = document.getElementById(id);
                if (!el) continue;
                el.classList.add('hidden');
                el.classList.remove('active');
                el.style.display = 'none';
            }
            if (typeof window.finalDismiss === 'function') {
                window.finalDismiss();
            }
        }"""
    )


def test_app_loads_with_disclaimer(app_page):
    """The dashboard should boot, render the title, and show the disclaimer."""
    assert app_page.title() == "CATalyst"
    disclaimer_btn = app_page.locator("#startupDisclaimerContinueBtn")
    disclaimer_btn.wait_for(state="visible", timeout=10_000)
    assert disclaimer_btn.is_visible()
    close_btn = app_page.locator("#startupDisclaimerCloseBtn")
    assert close_btn.is_visible()


def test_dismissing_disclaimer_reveals_wallet_gate(app_page):
    """Continuing past the disclaimer should land on a Sage startup gate."""
    assert dismiss_disclaimer(app_page) is True
    # In the Sage-free smoke environment the expected branch is "wallet not
    # open"; on a developer box with Sage already running, the same gate may
    # instead show the "Connect to Sage" button.
    connect = app_page.get_by_role("button", name=re.compile(r"Connect to Sage", re.I))
    wallet_not_open = app_page.locator("#startupSubtitle")
    try:
        connect.first.wait_for(state="visible", timeout=7_000)
    except PlaywrightTimeoutError:
        expect(wallet_not_open).to_contain_text("Sage wallet isn't running", timeout=10_000)
    assert connect.first.is_visible() or "Sage wallet isn't running" in wallet_not_open.text_content()


def test_primary_nav_tabs_present(app_page):
    """Primary nav should be in the DOM even before the user connects a wallet.

    Names are the buttons' accessible names (aria-label), which differ from
    the visible label in a couple of cases ("P&L" → "Profit and loss",
    "Market Intel" → "Market intelligence"). Test against the accessible
    name so screen-reader users and the test stay aligned.
    """
    dismiss_disclaimer(app_page)
    expected = [
        "Dashboard",
        "Offers",
        "Profit and loss",
        "Market intelligence",
        "Settings",
        "Logs",
        "Data reset",
    ]
    for label in expected:
        nav_btn = app_page.get_by_role("button", name=label, exact=True)
        assert nav_btn.count() >= 1, f"nav button '{label}' missing from DOM"


@pytest.mark.parametrize(
    ("label", "view_id"),
    [
        ("Dashboard", "v4View-dashboard"),
        ("Offers", "v4View-offers"),
        ("Profit and loss", "v4View-pnl"),
        ("Market intelligence", "v4View-intel"),
        ("Settings", "v4View-settings"),
        ("Logs", "v4View-logs"),
        ("Data reset", "v4View-data"),
    ],
)
def test_primary_nav_views_switch_without_wallet(app_page, label, view_id):
    """Core public UI views should switch once the startup gates are past."""
    reveal_app_shell_for_nav(app_page)

    app_page.get_by_role("button", name=label, exact=True).click(timeout=5_000)

    expect(app_page.locator(f"#{view_id}")).to_have_class(re.compile(r"\bactive\b"))


def test_data_reset_button_opens_destructive_confirmation(app_page):
    """Data-reset actions should show a confirmation dialog before POSTing."""
    reveal_app_shell_for_nav(app_page)
    app_page.get_by_role("button", name="Data reset", exact=True).click(timeout=5_000)

    app_page.locator("#btnResetPnl").click(timeout=5_000)

    expect(app_page.locator("#styledConfirmOverlay")).to_have_class(
        re.compile(r"\bactive\b")
    )
    expect(app_page.locator("#confirmTitle")).to_have_text("Reset P&L Counters")
    expect(app_page.locator("#confirmOkBtn")).to_have_text("Reset P&L")


def test_no_console_errors_on_initial_load(app_page):
    """Catch JS console errors that fire just from loading the dashboard."""
    errors: list[str] = []
    app_page.on(
        "console",
        lambda msg: errors.append(msg.text) if msg.type == "error" else None,
    )
    # Note: cannot use wait_until="networkidle" — the dashboard holds an
    # open SSE connection (`/api/events`) that never goes idle.
    app_page.reload(wait_until="domcontentloaded")
    # Allow a moment for deferred init scripts + the first SSE event to settle.
    app_page.wait_for_timeout(3_000)
    # SSE/network-related errors are expected when there's no real Sage —
    # filter those out so the test is meaningful.
    real_errors = [
        e for e in errors
        if "EventSource" not in e
        and "Failed to fetch" not in e
        and "NetworkError" not in e
        and "ERR_NETWORK" not in e
    ]
    assert not real_errors, f"Unexpected JS console errors: {real_errors}"
