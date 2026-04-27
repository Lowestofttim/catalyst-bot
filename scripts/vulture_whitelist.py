# vulture whitelist — names that are intentionally "unused" from Python's
# perspective but are called from JavaScript, the Flask framework, or
# pytest infrastructure.
#
# Usage: vulture . vulture_whitelist.py --min-confidence 80

# --- src-layout bootstrap (auto-inserted) ---
import os as _os
import sys as _sys
_sys.path.insert(
    0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "src", "catalyst")
)
# --- end bootstrap ---

from vulture.utils import whitelist_item

# ── PyWebView API methods ────────────────────────────────────────────────────
# All public methods of AppBridge are called via window.pywebview.api.*
# from bot_gui.html.  Vulture sees them as unused Python functions.
import app_bridge  # noqa: F401

for _name in dir(app_bridge.AppBridge):
    if not _name.startswith("_"):
        whitelist_item(app_bridge.AppBridge, _name)

# ── Flask route functions ────────────────────────────────────────────────────
# Every function decorated with @app.route is called by Flask's router, not
# directly from Python code.  Import the module so vulture can resolve names.
import api_server  # noqa: F401

# ── reaction_strategy.CycleBudget ───────────────────────────────────────────
# Defined in reaction_strategy.py but not yet wired to a caller.
# Tracked in spawn_queue 01-03 for removal decision.
from reaction_strategy import CycleBudget  # noqa: F401
whitelist_item(CycleBudget, "cancels_used")
whitelist_item(CycleBudget, "creates_used")
whitelist_item(CycleBudget, "requotes_used")
whitelist_item(CycleBudget, "max_cancels")
whitelist_item(CycleBudget, "max_creates")
whitelist_item(CycleBudget, "max_requotes")
