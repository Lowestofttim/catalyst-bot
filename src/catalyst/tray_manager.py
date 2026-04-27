"""System-tray icon and context menu for the CATalyst desktop app

Wraps `pystray` plus `Pillow` to present a coloured status icon in the OS
tray alongside the PyWebView window. `TrayManager` renders a dynamically
tinted icon (green / amber / red / grey / indigo), maintains a tooltip that
reflects bot state and active pair, and drives a short context menu
(Show Dashboard / Start / Stop / Exit). A polling thread owned by
`desktop_app` refreshes the icon colour and tooltip on a timer.

Key responsibilities:
    - Build and run the pystray icon on a dedicated thread
    - Render state-coloured icons with Pillow at runtime
    - Route menu actions to `desktop_app` callbacks when wired, or fall back
      to loopback Flask API calls against `127.0.0.1:5000`
    - Degrade gracefully when `pystray` or `Pillow` are not installed

The module imports `pystray` and `PIL` behind try/except so the rest of the
app still starts in headless or reduced-dependency environments.
"""

import threading

# Attempt imports — graceful fail if not installed
try:
    import pystray
    from pystray import MenuItem, Menu
    from PIL import Image, ImageDraw
    PYSTRAY_AVAILABLE = True
except ImportError:
    PYSTRAY_AVAILABLE = False

# HTTP client for calling Flask API actions from tray
try:
    import urllib.request
    import urllib.error
    _HTTP_AVAILABLE = True
except ImportError:
    _HTTP_AVAILABLE = False


# ---------------------------------------------------------------------------
# Icon colour constants matching DESIGN_SPEC.md
# ---------------------------------------------------------------------------
COLOUR_GREEN = (16, 185, 129)     # Running, healthy
COLOUR_AMBER = (245, 158, 11)     # Warning, degraded
COLOUR_RED = (239, 68, 68)        # Error, critical
COLOUR_GREY = (107, 114, 128)     # Stopped, unknown
COLOUR_INDIGO = (99, 102, 241)    # Brand accent (used for default icon)

FLASK_BASE = "http://127.0.0.1:5000"


class TrayManager:
    """
    Manages the system tray icon and menu.

    Usage:
        tray = TrayManager(app_name="CATalyst")
        tray.on_show_dashboard = lambda: show_window()
        tray.on_quit = lambda: quit_app()

        # Run in a thread (blocking)
        thread = threading.Thread(target=tray.run, daemon=True)
        thread.start()

        # Update state from bot loop
        tray.set_status("running", cat_name="MZ")   # green icon, tooltip includes "MZ"
        tray.set_status("warning")                   # amber icon
        tray.set_status("error")                     # red icon
        tray.set_status("stopped")                   # grey icon

        # Or use the higher-level updater (Phase 3):
        tray.update_tray_state("running", cat_name="MZ")
    """

    def __init__(self, app_name: str = "CATalyst", app_version: str = "4.0.0"):
        if not PYSTRAY_AVAILABLE:
            raise ImportError("pystray and/or Pillow not installed")

        self.app_name = app_name
        self.app_version = app_version
        self.is_running = False
        self._icon = None
        self._status = "stopped"
        self._tooltip_extra = ""
        self._cat_name = ""      # Active trading pair name (e.g. "MZ")

        # Callbacks — set these from desktop_app.py
        self.on_show_dashboard = None
        self.on_quit = None
        self.on_pause = None
        self.on_resume = None
        self.on_start_bot = None   # Phase 3: Start Bot from tray
        self.on_stop_bot = None    # Phase 3: Stop Bot from tray

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run(self):
        """Start the tray icon. Blocks until stop() is called."""
        self._icon = pystray.Icon(
            name="chia_market_maker",
            icon=self._create_icon(COLOUR_INDIGO),
            title=self._build_tooltip(),
            menu=self._build_menu()
        )
        self.is_running = True
        self._icon.run()  # Blocks

    def stop(self):
        """Stop and remove the tray icon."""
        self.is_running = False
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

    def set_status(self, status: str, extra: str = ""):
        """
        Update the tray icon colour and tooltip.

        status: "running", "warning", "error", "stopped"
        extra: additional tooltip text (e.g. "50 offers, +0.3 XCH today")
        """
        self._status = status
        self._tooltip_extra = extra
        self._apply_icon_update()

    def update_tray_state(self, status: str, cat_name: str = ""):
        """
        Phase 3 updater — rebuilds icon colour, tooltip, and menu in one call.

        status:   "running", "warning", "paused", "error", "stopped"
        cat_name: Active trading pair ticker (e.g. "MZ"). Shown in tooltip when running.

        Tooltip format:
            Running:  "Market Maker — RUNNING (MZ)"
            Stopped:  "Market Maker — Stopped"
            Error:    "Market Maker — Error"
            Warning:  "Market Maker — Warning"
            Paused:   "Market Maker — Paused"
        """
        self._status = status
        self._cat_name = cat_name or ""
        self._tooltip_extra = ""   # update_tray_state owns tooltip; clear legacy extra
        self._apply_icon_update()

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _apply_icon_update(self):
        """Apply current _status / _cat_name / _tooltip_extra to the live icon."""
        colour_map = {
            "running": COLOUR_GREEN,
            "warning": COLOUR_AMBER,
            "paused":  COLOUR_AMBER,
            "error":   COLOUR_RED,
            "stopped": COLOUR_GREY,
        }
        colour = colour_map.get(self._status, COLOUR_GREY)

        if self._icon:
            try:
                self._icon.icon = self._create_icon(colour)
                self._icon.title = self._build_tooltip()
                # Rebuild menu so dynamic items (Start/Stop/Pause) reflect new state
                self._icon.menu = self._build_menu()
            except Exception:
                pass

    def _build_tooltip(self) -> str:
        """Build tooltip string from current state."""
        status = self._status

        if status == "running":
            label = "RUNNING"
            if self._cat_name:
                label += f" ({self._cat_name})"
        elif status == "paused":
            label = "Paused"
            if self._cat_name:
                label += f" ({self._cat_name})"
        elif status == "warning":
            label = "Warning"
        elif status == "error":
            label = "Error"
        else:
            label = "Stopped"

        tooltip = f"{self.app_name} — {label}"

        # Legacy _tooltip_extra support (set by set_status calls)
        if self._tooltip_extra:
            tooltip += f"\n{self._tooltip_extra}"

        return tooltip

    def _build_menu(self):
        """
        Build the right-click context menu.

        Dynamic items based on bot state:
          - Stopped/Error:   [Start Bot]
          - Running/Warning: [Stop Bot]
        Always present: [Show Dashboard] --- [Exit]

        NOTE: Pause/Resume are intentionally omitted — the bot does not
        support a pause state.  The user should Stop the bot if they want
        to suspend trading.
        """
        status_text = self._status.capitalize()
        if self._cat_name and self._status in ("running", "paused"):
            status_text += f" · {self._cat_name}"
        if self._tooltip_extra:
            status_text += f" · {self._tooltip_extra}"

        items = [
            MenuItem(
                f"{self.app_name}",
                action=self._on_show,
                default=True,   # Double-click action
                enabled=False   # Just a label
            ),
            MenuItem(f"Status: {status_text}", action=None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("📊 Show Dashboard", self._on_show),
        ]

        # Dynamic bot-control items — Start when idle, Stop when active.
        if self._status in ("running", "warning", "paused"):
            items.append(MenuItem("⏹ Stop Bot", self._on_stop_bot))
        else:
            # stopped / error / unknown
            items.append(MenuItem("▶ Start Bot", self._on_start_bot))

        items.extend([
            Menu.SEPARATOR,
            MenuItem("❌ Exit", self._on_quit),
        ])

        return Menu(*items)

    # -----------------------------------------------------------------------
    # Action handlers
    # -----------------------------------------------------------------------

    def _on_show(self, _icon=None, _item=None):
        """Show dashboard callback."""
        if self.on_show_dashboard:
            self.on_show_dashboard()

    def _on_quit(self, _icon=None, _item=None):
        """Quit app callback."""
        if self.on_quit:
            self.on_quit()
        else:
            self.stop()

    def _on_pause(self, _icon=None, _item=None):
        """Pause bot callback."""
        if self.on_pause:
            self.on_pause()

    def _on_resume(self, _icon=None, _item=None):
        """Resume bot callback."""
        if self.on_resume:
            self.on_resume()

    def _on_start_bot(self, _icon=None, _item=None):
        """
        Start bot from tray.

        Priority: use the on_start_bot callback if wired (desktop_app.py sets it).
        Fallback: call Flask API directly in a background thread so the tray
        action returns immediately without blocking pystray's event loop.
        """
        if self.on_start_bot:
            threading.Thread(target=self.on_start_bot, daemon=True).start()
            return
        # Fallback — call Flask API
        if _HTTP_AVAILABLE:
            threading.Thread(
                target=self._call_flask_api,
                args=("/api/bot/start", "POST"),
                daemon=True
            ).start()
        # Show the window so user can see the bot starting
        if self.on_show_dashboard:
            self.on_show_dashboard()

    def _on_stop_bot(self, _icon=None, _item=None):
        """
        Stop bot from tray.

        Priority: use the on_stop_bot callback if wired.
        Fallback: call Flask API directly.
        """
        if self.on_stop_bot:
            threading.Thread(target=self.on_stop_bot, daemon=True).start()
            return
        # Fallback — call Flask API
        if _HTTP_AVAILABLE:
            threading.Thread(
                target=self._call_flask_api,
                args=("/api/bot/stop", "POST"),
                daemon=True
            ).start()

    def _call_flask_api(self, path: str, method: str = "POST"):
        """
        Call a local Flask API endpoint.  Runs in a background thread.
        Failures are silently ignored — the tray is a convenience, not critical path.
        """
        url = f"{FLASK_BASE}{path}"
        try:
            req = urllib.request.Request(
                url,
                data=b"{}",
                method=method,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                _ = resp.read()   # Consume response
        except Exception:
            pass   # Non-critical — silently ignore network/server errors

    def _create_icon(self, colour: tuple, size: int = 64) -> 'Image.Image':
        """
        Create a simple tray icon — a filled circle on transparent background.
        The circle colour indicates bot status.
        """
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Outer circle (slightly darker for depth)
        outer_colour = tuple(max(0, c - 30) for c in colour) + (255,)
        draw.ellipse([2, 2, size - 3, size - 3], fill=outer_colour)

        # Inner circle (main colour)
        inset = 6
        draw.ellipse(
            [inset, inset, size - inset - 1, size - inset - 1],
            fill=colour + (255,)
        )

        # Bright highlight dot (top-left) for depth effect
        hl_size = size // 5
        hl_offset = size // 4
        highlight = tuple(min(255, c + 80) for c in colour) + (120,)
        draw.ellipse(
            [hl_offset, hl_offset, hl_offset + hl_size, hl_offset + hl_size],
            fill=highlight
        )

        return img

