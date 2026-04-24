"""
Pytest configuration — test collection, isolation, and encoding settings.

Fixes:
- Excludes standalone diagnostic scripts that crash pytest collection
- Sets UTF-8 encoding for stdout/stderr capture on Windows (prevents
  UnicodeDecodeError from emoji output in print() statements when pytest
  tries to decode its capture buffer with cp1252)
"""

import sys
import os
import io

# ---------------------------------------------------------------------------
# Src-layout bootstrap: add src/catalyst/ to sys.path so tests can use
# flat imports (`from database import X`) against the reorganised source
# tree.  This runs at conftest load time, before any collection happens.
# ---------------------------------------------------------------------------
_SRC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "catalyst")
)
if os.path.isdir(_SRC_DIR) and _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# ---------------------------------------------------------------------------
# Exclude standalone integration scripts from collection.
# These files contain module-level code (sys.exit, live API calls) that
# crashes pytest's importer. They're meant to be run directly, not via pytest.
# ---------------------------------------------------------------------------
collect_ignore = [
    "test_parallel_offers.py",
    "test_spacescan.py",
    "test_api_data_sources.py",
    "test_all_apis.py",
]

# ---------------------------------------------------------------------------
# Force UTF-8 for pytest's stdout/stderr capture on Windows.
#
# On Windows, the default console encoding is cp1252 (or the OEM code page).
# Our bot code contains emoji (✅, 🎯, 💰, ×) and Unicode math symbols in
# print() statements. When these are captured by pytest, the bytes land in
# the capture buffer. When pytest later tries to decode the buffer as UTF-8
# (for display in its output), an isolated 0x97 continuation byte (part of
# the UTF-8 × sequence \xc3\x97, split across two capture reads) causes
# UnicodeDecodeError inside contextlib._GeneratorContextManager.__exit__,
# appearing as hundreds of spurious "ERROR at setup/teardown" lines.
#
# The os.environ approach doesn't help because Python has already determined
# sys.stdout's encoding at process start. We must reconfigure the actual
# stream objects AND set the env var for any child processes.
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"

    # Reconfigure stdout and stderr to use UTF-8, replacing any bytes that
    # can't be encoded with the Unicode replacement character rather than
    # raising. This covers the case where pytest has NOT yet replaced
    # sys.stdout with its own capture object.
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        elif _stream is not None and hasattr(_stream, "buffer"):
            try:
                _wrapped = io.TextIOWrapper(
                    _stream.buffer, encoding="utf-8", errors="replace", line_buffering=True
                )
                setattr(sys, _stream_name, _wrapped)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# sys.modules isolation between test files.
#
# Several test files install stub `sys.modules` entries for `database`,
# `wallet`, `wallet_sage`, `coin_manager`, etc. to isolate the module
# under test from the real bot dependencies. A few had buggy tearDowns
# that popped those entries instead of restoring the originals — leaving
# later files to re-import fresh copies of those modules which then broke
# `patch(...)` calls that assume sys.modules still holds the original.
#
# This autouse fixture snapshots `sys.modules` before each test module is
# loaded and restores the snapshot after, so file-level leaks can't reach
# the next file even if individual teardowns are sloppy.
# ---------------------------------------------------------------------------
import pytest


# Snapshot these at first conftest load — these are the real modules the
# bot ships, and the ones tests most commonly stub.
_ISOLATION_GUARDED = (
    "database", "wallet", "wallet_sage", "wallet_chia",
    "coin_manager", "coin_prep_worker", "bot_health", "bot_loop",
    "fill_tracker", "offer_manager", "price_engine",
    "dexie_manager", "spacescan", "amm_monitor", "tx_fees",
    "config",
)


@pytest.fixture(autouse=True, scope="module")
def _restore_isolation_guarded_modules():
    """Restore stubbed bot modules between test files.

    If a test file replaces `sys.modules["database"]` with a stub and
    forgets to restore it, this fixture catches the damage at the end
    of the module so the next file starts clean.
    """
    saved = {name: sys.modules.get(name) for name in _ISOLATION_GUARDED}
    yield
    for name, original in saved.items():
        current = sys.modules.get(name)
        if original is None:
            # Wasn't loaded before this file; drop any stub installed.
            sys.modules.pop(name, None)
        elif current is not original:
            sys.modules[name] = original
