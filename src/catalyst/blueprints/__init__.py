"""Flask Blueprints for CATalyst API routes.

Each submodule defines a Blueprint with its own logical group of routes.
api_server.py imports and registers them at startup, and re-exports the
route function names so external callers (app_bridge.py, tests) that do
`api_server.api_xxx(...)` keep working without change.

Blueprints access shared state (bot, events, alerts, helpers) via
`import api_server` + attribute access, since those globals are mutated
at runtime (e.g. `create_bot()` reassigns `api_server.bot`). Using
`from api_server import bot` would capture a stale `None` reference.
"""
