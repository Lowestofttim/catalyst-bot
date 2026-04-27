"""Levelled structured logger with ring-buffered crash context

The bot's single logging facade. INFO and above are written to the active
log file; TRACE and DEBUG stay in an in-memory ring buffer and only get
flushed to disk when an ERROR fires, so crashes arrive with full context
while normal runs stay quiet. `slog(category, message, data=None,
level="info")` is the one entry point every other module should use — never
`print()` and never stdlib `logging`.

Key responsibilities:
    - Emit structured log records with level, category, message, and data
    - Maintain a ring buffer of recent TRACE/DEBUG lines for error dumps
    - Track per-cycle counters (`start_cycle`, `cycle_count`, `end_cycle`)
    - Manage thread lifecycle markers, log rotation, and archive digesting

The file singleton is created on the first `init_super_log(log_dir, ...)`
call; later imports reuse it. Levels map: trace < debug < info < warn < error.
"""

import os
import sys
import time
import glob
import json
import threading
import sqlite3
import collections
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Log levels (lower number = more verbose)
# ---------------------------------------------------------------------------
LEVELS = {"trace": 0, "debug": 1, "info": 2, "warn": 3, "error": 4}
LEVEL_TAGS = {"trace": "TRACE", "debug": "DEBUG", "INFO": "INFO",
              "warn": " WARN", "error": "ERROR"}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MAX_LOG_FILES = 5           # Keep only this many recent log files
MAX_LOG_SIZE_MB = 10        # Rotate when file exceeds this (was 50, now 10)
_MAX_LOG_BYTES = MAX_LOG_SIZE_MB * 1024 * 1024

# Ring buffer for error context — keeps last N verbose lines in memory
RING_BUFFER_SIZE = 500      # Lines of context to dump on error
# How many error context dumps per session (prevent runaway dumps)
MAX_ERROR_DUMPS = 10

# Archive: compact digest of each rotated log (errors, stats, timeline)
# Stored as JSONL (one JSON line per rotated log) — stays tiny forever
ARCHIVE_FILENAME = "superlog_archive.jsonl"
MAX_ARCHIVE_ENTRIES = 500   # ~1-2 years of daily rotations
MAX_ARCHIVE_BYTES = 1024 * 1024  # 1MB hard cap

# File output level — only this level and above gets written to disk
# Can be changed at runtime via set_file_level()
_file_level = LEVELS["info"]

# Terminal output level — what shows in the console
_terminal_level = LEVELS["info"]

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_log_file = None
_log_lock = threading.Lock()
_log_path = ""
_log_dir = ""
_start_time = time.time()
_initialized = False
_bytes_written = 0

# Ring buffer for verbose context (collections.deque is thread-safe for append/iter)
_ring_buffer = collections.deque(maxlen=RING_BUFFER_SIZE)
_error_dump_count = 0
# Category/event types already dumped this session — each distinct error type
# only gets one context dump so repeated known failures (e.g. no_unique_coin_preselected
# during coin-prep shortage) don't burn through the entire MAX_ERROR_DUMPS budget.
_error_dump_seen_categories: set = set()

# Track DB connections per thread
_connection_count = 0
_connection_lock = threading.Lock()

# Cycle summary accumulator
_cycle_stats = threading.local()


# ---------------------------------------------------------------------------
# Log file setup
# ---------------------------------------------------------------------------

def init_super_log(log_dir: str = None, file_level: str = "info",
                   terminal_level: str = "info"):
    """Initialize super logging — call once at startup.

    Args:
        log_dir: Directory for log files (default: script directory)
        file_level: Minimum level written to file ("trace"/"debug"/"info"/"warn"/"error")
        terminal_level: Minimum level printed to terminal
    """
    global _log_file, _log_path, _log_dir, _initialized, _start_time
    global _bytes_written, _file_level, _terminal_level

    if _initialized:
        return _log_path

    _start_time = time.time()
    _file_level = LEVELS.get(file_level.lower(), LEVELS["info"])
    _terminal_level = LEVELS.get(terminal_level.lower(), LEVELS["info"])

    if log_dir is None:
        # Default to the per-user data directory so the log files are
        # writable regardless of install location.
        try:
            from user_paths import log_dir as _user_log_dir
            log_dir = _user_log_dir()
        except Exception:
            log_dir = os.path.dirname(os.path.abspath(__file__))
    _log_dir = log_dir

    # Clean up old log files
    _cleanup_old_logs(log_dir)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_path = os.path.join(log_dir, f"bot_superlog_{timestamp}.log")
    _bytes_written = 0

    _log_file = open(_log_path, "w", encoding="utf-8", buffering=1)

    # Wrap stdout/stderr to tee to file
    sys.stdout = _TeeWriter(sys.__stdout__, _log_lock)
    sys.stderr = _TeeWriter(sys.__stderr__, _log_lock)

    _initialized = True

    slog("SUPER_LOG", f"Logging to {_log_path}")
    slog("SUPER_LOG", f"Python {sys.version}")
    slog("SUPER_LOG", f"PID: {os.getpid()}, Threads: {threading.active_count()}")
    slog("SUPER_LOG", f"File level: {file_level.upper()}, Terminal level: {terminal_level.upper()}")
    slog("SUPER_LOG", f"Rotation: max {MAX_LOG_SIZE_MB}MB/file, keep {MAX_LOG_FILES} files")
    slog("SUPER_LOG", f"Error context buffer: {RING_BUFFER_SIZE} lines")

    return _log_path


def set_file_level(level: str):
    """Change the file output level at runtime.

    Useful for temporarily enabling verbose logging for debugging.
    Call set_file_level("trace") to log everything, then
    set_file_level("info") to go back to quiet mode.
    """
    global _file_level
    _file_level = LEVELS.get(level.lower(), LEVELS["info"])
    slog("SUPER_LOG", f"File level changed to {level.upper()}")


def set_terminal_level(level: str):
    """Change the terminal output level at runtime."""
    global _terminal_level
    _terminal_level = LEVELS.get(level.lower(), LEVELS["info"])


def _cleanup_old_logs(log_dir: str):
    """Delete old superlog files, keeping only the most recent MAX_LOG_FILES."""
    try:
        pattern = os.path.join(log_dir, "bot_superlog_*.log")
        log_files = sorted(glob.glob(pattern))
        if len(log_files) > MAX_LOG_FILES:
            to_delete = log_files[:len(log_files) - MAX_LOG_FILES]
            for f in to_delete:
                try:
                    os.remove(f)
                    if sys.__stdout__ is not None:
                        sys.__stdout__.write(f"[SUPER_LOG] Cleaned up old log: {os.path.basename(f)}\n")
                except OSError:
                    pass
    except Exception:
        pass


def _rotate_if_needed():
    """Check file size and rotate to a new file if over the limit.

    Before deleting old logs, extracts a compact archive digest so
    long-term error history is preserved even after files are deleted.
    """
    global _log_file, _log_path, _bytes_written

    if _bytes_written < _MAX_LOG_BYTES:
        return

    try:
        old_path = _log_path
        _log_file.flush()
        old_file = _log_file

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _log_path = os.path.join(_log_dir, f"bot_superlog_{timestamp}.log")
        _log_file = open(_log_path, "w", encoding="utf-8", buffering=1)
        _bytes_written = 0

        old_file.close()

        # Archive the old log before cleanup might delete it
        _archive_log_digest(old_path)

        slog("SUPER_LOG", f"Rotated log: {os.path.basename(old_path)} -> {os.path.basename(_log_path)}")
        _cleanup_old_logs(_log_dir)
    except Exception as e:
        if sys.__stderr__ is not None:
            sys.__stderr__.write(f"[SUPER_LOG] Rotation error: {e}\n")


# ---------------------------------------------------------------------------
# Archive digest — compact summary of each rotated log file
# ---------------------------------------------------------------------------

def _archive_log_digest(log_path: str):
    """Extract a compact digest from a rotated log file and append to archive.

    Scans the log for errors, warnings, and key metrics. Stores one small
    JSON line per rotated log. This means you keep months/years of error
    history in under 1MB, even after the full log files are deleted.
    """
    try:
        if not os.path.exists(log_path):
            return

        errors = []
        warnings = []
        cycle_count = 0
        fill_count = 0
        first_ts = ""
        last_ts = ""
        file_size = os.path.getsize(log_path)

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Extract timestamp from structured log lines
                # Format: [   1.234s] [HH:MM:SS.mmm] ...
                if line.startswith("[") and "] [" in line:
                    try:
                        ts_start = line.index("] [") + 3
                        ts_end = line.index("]", ts_start)
                        ts = line[ts_start:ts_end]
                        if not first_ts:
                            first_ts = ts
                        last_ts = ts
                    except (ValueError, IndexError):
                        pass

                # Count errors (keep first 20 for the digest)
                if "ERROR" in line:
                    if len(errors) < 20:
                        # Extract just the message, trim to 200 chars
                        msg = line.strip()
                        if len(msg) > 200:
                            msg = msg[:200] + "..."
                        errors.append(msg)

                # Count warnings (keep first 10)
                if " WARN" in line and len(warnings) < 10:
                    msg = line.strip()
                    if len(msg) > 200:
                        msg = msg[:200] + "..."
                    warnings.append(msg)

                # Count cycles and fills
                if "[CYCLE" in line:
                    cycle_count += 1
                if "fills=" in line:
                    try:
                        # Extract fill count from cycle summary
                        idx = line.index("fills=")
                        num_str = ""
                        for c in line[idx + 6:idx + 10]:
                            if c.isdigit():
                                num_str += c
                            else:
                                break
                        if num_str:
                            fill_count += int(num_str)
                    except (ValueError, IndexError):
                        pass

        digest = {
            "file": os.path.basename(log_path),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "file_size_mb": round(file_size / (1024 * 1024), 2),
            "cycles": cycle_count,
            "total_fills": fill_count,
            "error_count": len(errors),
            "warn_count": len(warnings),
            "errors": errors,
            "warnings": warnings[:5],  # Keep fewer warnings in archive
        }

        # Append to archive file
        archive_path = os.path.join(_log_dir, ARCHIVE_FILENAME)
        with open(archive_path, "a", encoding="utf-8") as af:
            af.write(json.dumps(digest) + "\n")

        # Prune archive if too large
        _prune_archive(archive_path)

    except Exception as e:
        if sys.__stderr__ is not None:
            sys.__stderr__.write(f"[SUPER_LOG] Archive digest error: {e}\n")


def _prune_archive(archive_path: str):
    """Keep archive file under size/entry limits.

    Removes oldest entries when the file exceeds MAX_ARCHIVE_ENTRIES
    or MAX_ARCHIVE_BYTES. This ensures the archive stays tiny forever.
    """
    try:
        if not os.path.exists(archive_path):
            return

        file_size = os.path.getsize(archive_path)
        if file_size <= MAX_ARCHIVE_BYTES:
            return

        # Read all entries, keep the newest ones
        with open(archive_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if len(lines) <= MAX_ARCHIVE_ENTRIES and file_size <= MAX_ARCHIVE_BYTES:
            return

        # Keep the most recent entries
        keep = lines[-MAX_ARCHIVE_ENTRIES:] if len(lines) > MAX_ARCHIVE_ENTRIES else lines

        # If still over size limit, keep fewer
        while len(keep) > 10:
            total = sum(len(line) for line in keep)
            if total <= MAX_ARCHIVE_BYTES:
                break
            keep = keep[len(keep) // 4:]  # Drop oldest quarter

        with open(archive_path, "w", encoding="utf-8") as f:
            f.writelines(keep)

    except Exception as e:
        if sys.__stderr__ is not None:
            sys.__stderr__.write(f"[SUPER_LOG] Archive prune error: {e}\n")


def get_archive_summary(last_n: int = 10) -> list:
    """Read the last N archive digests for the GUI/API.

    Returns a list of dicts, most recent first. Useful for showing
    error history across past sessions without keeping full logs.
    """
    try:
        archive_path = os.path.join(_log_dir, ARCHIVE_FILENAME)
        if not os.path.exists(archive_path):
            return []

        with open(archive_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        entries = []
        for line in lines[-last_n:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        entries.reverse()  # Most recent first
        return entries
    except Exception:
        return []


def periodic_maintenance():
    """Run periodic log maintenance — call from bot loop every ~50 cycles.

    - Cleans up old log files that might have been orphaned
    - Prunes archive if needed
    - Returns stats dict for monitoring
    """
    try:
        if _log_dir:
            _cleanup_old_logs(_log_dir)
            archive_path = os.path.join(_log_dir, ARCHIVE_FILENAME)
            if os.path.exists(archive_path):
                _prune_archive(archive_path)
        return get_log_stats()
    except Exception:
        return {}


class _TeeWriter:
    """Writes to both the original stream and the log file.

    Only passes through lines that meet the terminal threshold.
    All print() output (from Flask, libraries, etc.) goes through at INFO level.
    """

    def __init__(self, original, lock):
        self._original = original
        self._lock = lock

    def _write_terminal(self, text):
        try:
            self._original.write(text)
            return
        except UnicodeEncodeError:
            encoding = getattr(self._original, "encoding", None) or "utf-8"
            try:
                safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
            except Exception:
                safe_text = text.encode("ascii", errors="replace").decode("ascii")
            try:
                self._original.write(safe_text)
            except Exception:
                pass
        except Exception:
            pass

    def write(self, text):
        global _bytes_written
        if text:
            # Always write to terminal (external prints aren't level-filtered)
            self._write_terminal(text)
            # Always write to file (external prints are INFO-equivalent)
            with self._lock:
                try:
                    if _log_file and not _log_file.closed:
                        _log_file.write(text)
                        _bytes_written += len(text)
                except Exception:
                    pass

    def flush(self):
        self._original.flush()
        with self._lock:
            try:
                if _log_file and not _log_file.closed:
                    _log_file.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._original, name)


# ---------------------------------------------------------------------------
# Main logging function
# ---------------------------------------------------------------------------

def slog(category: str, message: str, data: dict = None, level: str = "info"):
    """Log a structured event with level-based filtering.

    Args:
        category: Short tag like "DB", "LOOP", "STARTUP", "DEXIE"
        message: Human-readable description
        data: Optional dict with structured data
        level: "trace", "debug", "info", "warn", "error"
    """
    global _error_dump_count, _bytes_written

    lvl = LEVELS.get(level.lower(), LEVELS["info"])
    now = datetime.now(timezone.utc)
    elapsed = time.time() - _start_time
    thread = threading.current_thread().name

    # Sanitize inputs to prevent log injection (newlines, control chars)
    category = category.replace("\n", " ").replace("\r", "")[:12] if category else ""
    message = message.replace("\n", " ").replace("\r", "") if message else ""

    # Format the line
    ts = now.strftime("%H:%M:%S.%f")[:-3]
    lvl_tag = level.upper()[:5].rjust(5)
    prefix = f"[{elapsed:8.3f}s] [{ts}] [{thread:16s}] [{lvl_tag}] [{category:12s}]"

    if data:
        data_str = " | ".join(f"{k}={v}" for k, v in data.items())
        line = f"{prefix} {message} :: {data_str}"
    else:
        line = f"{prefix} {message}"

    # ---- Ring buffer: ALL levels go here (for error context) ----
    _ring_buffer.append(line)

    # ---- Terminal output: filtered by _terminal_level ----
    # sys.__stdout__ can be None when running without a console (e.g. pythonw,
    # detached subprocess, or certain frozen-app launchers on Windows).
    if lvl >= _terminal_level and sys.__stdout__ is not None:
        try:
            sys.__stdout__.write(line + "\n")
            sys.__stdout__.flush()
        except Exception:
            pass

    # ---- File output: filtered by _file_level ----
    if lvl >= _file_level and _initialized:
        with _log_lock:
            try:
                if _log_file and not _log_file.closed:
                    _log_file.write(line + "\n")
                    _bytes_written += len(line) + 1
            except Exception:
                pass

    # ---- ERROR: dump ring buffer context to file ----
    # Each distinct error category gets at most one context dump per session.
    # This prevents a single repeated failure (e.g. no_unique_coin_preselected
    # during a coin-prep shortage) from burning through all MAX_ERROR_DUMPS slots
    # and leaving no budget for genuinely unexpected errors later in the session.
    if (lvl >= LEVELS["error"] and _initialized
            and _error_dump_count < MAX_ERROR_DUMPS
            and category not in _error_dump_seen_categories):
        _error_dump_seen_categories.add(category)
        _error_dump_count += 1
        _dump_error_context(category, message)

    # Check rotation
    if _initialized and _bytes_written >= _MAX_LOG_BYTES:
        with _log_lock:
            _rotate_if_needed()


def _dump_error_context(error_category: str, error_message: str):
    """Dump the ring buffer to the log file for post-mortem analysis.

    This gives you full TRACE/DEBUG context around the error without
    logging verbose stuff all the time.
    """
    with _log_lock:
        try:
            if not _log_file or _log_file.closed:
                return
            global _bytes_written
            separator = "=" * 80
            header = (f"\n{separator}\n"
                      f"ERROR CONTEXT DUMP #{_error_dump_count} — "
                      f"{error_category}: {error_message}\n"
                      f"Last {len(_ring_buffer)} verbose log lines before this error:\n"
                      f"{separator}\n")
            _log_file.write(header)
            _bytes_written += len(header)

            for buffered_line in _ring_buffer:
                out = buffered_line + "\n"
                _log_file.write(out)
                _bytes_written += len(out)

            footer = f"{separator}\nEND ERROR CONTEXT DUMP\n{separator}\n\n"
            _log_file.write(footer)
            _bytes_written += len(footer)
            _log_file.flush()
        except Exception as e:
            if sys.__stderr__ is not None:
                sys.__stderr__.write(f"[SUPER_LOG] Error dumping context: {e}\n")


# ---------------------------------------------------------------------------
# SQL Tracing — only logs slow queries + errors; rest goes to ring buffer
# ---------------------------------------------------------------------------

# Threshold: queries taking longer than this (ms) get logged at WARN level.
# With 5+ threads hitting SQLite concurrently (bot loop, GUI polling, splash,
# coin watcher, price watcher), waits of 1-3 seconds are normal WAL contention.
# Only flag genuinely stuck queries (5+ seconds).
SLOW_QUERY_MS = 5000

def make_sql_trace_callback(conn_id: str):
    """Create a SQL trace callback that's much quieter than v1.

    - Normal queries → TRACE level (ring buffer only)
    - Gaps between traced statements are recorded as debug-only context
    - Bulk coin operations → suppressed entirely, counted
    """
    _bulk_state = {"suppressed": 0, "last_table": "", "last_flush": time.time()}
    _last_query_time = {"t": time.time()}

    def _flush_bulk_summary():
        if _bulk_state["suppressed"] > 0:
            slog("SQL", f"[bulk] {_bulk_state['suppressed']} ops on '{_bulk_state['last_table']}' (suppressed)",
                 {"conn": conn_id}, level="debug")
            _bulk_state["suppressed"] = 0
            _bulk_state["last_table"] = ""

    def _trace(statement):
        now = time.time()
        stmt_upper = statement.strip().upper()

        # Skip noisy pragmas entirely
        if stmt_upper.startswith("PRAGMA"):
            return

        # Suppress bulk coin sync operations
        if "INTO COINS" in stmt_upper or (
            stmt_upper.startswith("SELECT") and "FROM COINS WHERE COIN_ID" in stmt_upper
        ):
            _bulk_state["suppressed"] += 1
            _bulk_state["last_table"] = "coins"
            # Auto-flush summary every 30 seconds so we don't lose count
            if now - _bulk_state["last_flush"] > 30:
                _flush_bulk_summary()
                _bulk_state["last_flush"] = now
            return

        # Flush bulk summary when switching to non-bulk
        if _bulk_state["suppressed"] > 0:
            if stmt_upper in ("BEGIN", "COMMIT"):
                _bulk_state["suppressed"] += 1
                return
            _flush_bulk_summary()

        # Measure the gap between trace callbacks. This is NOT query execution
        # time; SQLite's trace callback fires after statements are prepared and
        # tells us nothing reliable about how long they took to run.
        gap_ms = (now - _last_query_time["t"]) * 1000
        _last_query_time["t"] = now

        # Truncate long statements
        display = statement.strip().replace("\n", " ")
        if len(display) > 200:
            display = display[:200] + "..."

        # Keep long gaps as debug context only. They often just mean the
        # connection was idle between queries, not that the SQL itself was slow.
        if gap_ms > SLOW_QUERY_MS:
            slog("SQL_GAP", display, {"conn": conn_id, "gap_ms": f"{gap_ms:.0f}"},
                 level="debug")
        else:
            # Normal query → TRACE only (ring buffer, not file)
            slog("SQL", display, {"conn": conn_id}, level="trace")

    _trace.flush_bulk = _flush_bulk_summary
    return _trace


def trace_connection(conn: sqlite3.Connection, label: str = None):
    """Add SQL tracing to an existing connection."""
    global _connection_count
    with _connection_lock:
        _connection_count += 1
        conn_id = label or f"conn-{_connection_count}"

    conn.set_trace_callback(make_sql_trace_callback(conn_id))
    thread = threading.current_thread().name
    slog("DB_CONN", f"Connection opened: {conn_id}", {"thread": thread}, level="debug")
    return conn_id


# ---------------------------------------------------------------------------
# Function timing decorator
# ---------------------------------------------------------------------------

def timed(category: str = "TIMING", slow_ms: float = 500):
    """Decorator to log function timing.

    - Normal execution → DEBUG level
    - Slow execution (>slow_ms) → INFO level
    - Very slow execution (>max(slow_ms * 10, 5000ms)) → WARN level
    - Errors → ERROR level (triggers context dump)
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            fname = func.__qualname__
            slog(category, f">>> {fname}", level="trace")
            start = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed_ms = (time.time() - start) * 1000
                if elapsed_ms > max(slow_ms * 10, 5000):
                    lvl = "warn"
                elif elapsed_ms > slow_ms:
                    lvl = "info"
                else:
                    lvl = "debug"
                slog(category, f"<<< {fname}", {"time_ms": f"{elapsed_ms:.1f}"}, level=lvl)
                return result
            except Exception as e:
                elapsed_ms = (time.time() - start) * 1000
                slog(category, f"!!! {fname} ERROR: {e}",
                     {"time_ms": f"{elapsed_ms:.1f}"}, level="error")
                raise
        wrapper.__name__ = func.__name__
        wrapper.__qualname__ = func.__qualname__
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Cycle summary — replaces 50+ per-cycle lines with one compact line
# ---------------------------------------------------------------------------

def start_cycle(cycle_num: int):
    """Call at start of each bot loop cycle."""
    _cycle_stats.__dict__.clear()
    _cycle_stats.cycle_num = cycle_num
    _cycle_stats.start_time = time.time()
    _cycle_stats.fills = 0
    _cycle_stats.offers_created = 0
    _cycle_stats.offers_cancelled = 0
    _cycle_stats.snipes = 0
    _cycle_stats.requotes = 0
    _cycle_stats.errors = 0
    _cycle_stats.notes = []


def cycle_count(key: str, value: int = 1):
    """Increment a cycle counter."""
    current = getattr(_cycle_stats, key, 0)
    setattr(_cycle_stats, key, current + value)


def cycle_note(note: str):
    """Add a short note to the cycle summary."""
    notes = getattr(_cycle_stats, 'notes', [])
    notes.append(note)
    _cycle_stats.notes = notes


def end_cycle(mid_price: float = 0, spread_bps: float = 0,
              inventory: float = 0, open_offers: int = 0):
    """Call at end of each bot loop cycle — logs one compact summary line."""
    elapsed_ms = (time.time() - getattr(_cycle_stats, 'start_time', time.time())) * 1000
    cycle_num = getattr(_cycle_stats, 'cycle_num', '?')
    fills = getattr(_cycle_stats, 'fills', 0)
    created = getattr(_cycle_stats, 'offers_created', 0)
    cancelled = getattr(_cycle_stats, 'offers_cancelled', 0)
    snipes = getattr(_cycle_stats, 'snipes', 0)
    requotes = getattr(_cycle_stats, 'requotes', 0)
    errors = getattr(_cycle_stats, 'errors', 0)
    notes = getattr(_cycle_stats, 'notes', [])

    data = {
        "cycle": cycle_num,
        "time_ms": f"{elapsed_ms:.0f}",
        "mid": f"{mid_price:.10f}" if mid_price else "0",
        "spread_bps": f"{spread_bps:.0f}",
        "inv": f"{inventory:.4f}",
        "offers": open_offers,
        "fills": fills,
        "created": created,
        "cancelled": cancelled,
    }

    if snipes:
        data["snipes"] = snipes
    if requotes:
        data["requotes"] = requotes
    if errors:
        data["errors"] = errors

    notes_str = f" [{'; '.join(notes)}]" if notes else ""

    # Cycle summary always goes to INFO
    slog("CYCLE", f"Cycle #{cycle_num} complete{notes_str}", data, level="info")


# ---------------------------------------------------------------------------
# Thread tracker
# ---------------------------------------------------------------------------

def log_thread_start(name: str = None):
    """Call at the start of a background thread to log it."""
    t = threading.current_thread()
    thread_name = name or t.name
    slog("THREAD", f"Thread started: {thread_name}",
         {"total_threads": threading.active_count()}, level="info")


def log_thread_stop(name: str = None):
    """Call at the end of a background thread to log it."""
    t = threading.current_thread()
    thread_name = name or t.name
    slog("THREAD", f"Thread stopped: {thread_name}", level="info")


# ---------------------------------------------------------------------------
# Convenience: log_event interceptor
# ---------------------------------------------------------------------------

_original_log_event = None

def intercept_log_event():
    """Monkey-patch database.log_event() to also write to super_log."""
    global _original_log_event
    try:
        import database
        current_log_event = database.log_event
        original_log_event = current_log_event

        # Repeated imports of api_server in tests can call this more than once
        # in the same Python process. Always unwrap to the real DB writer so
        # wrappers do not call wrappers and recurse forever.
        seen = set()
        while getattr(original_log_event, "_super_log_interceptor", False):
            marker_id = id(original_log_event)
            if marker_id in seen:
                break
            seen.add(marker_id)
            original_log_event = getattr(
                original_log_event,
                "_super_log_original",
                original_log_event,
            )
        _original_log_event = original_log_event

        # Map database severity to super_log levels
        _severity_map = {
            "debug": "debug",
            "info": "info",
            "warning": "warn",
            "error": "error",
            "critical": "error",
        }

        def _patched_log_event(severity, event_type, message, data=None):
            lvl = _severity_map.get(severity.lower(), "info")
            slog("EVENT", f"[{severity.upper():7s}] [{event_type}] {message}",
                 data if data else None, level=lvl)
            return original_log_event(severity, event_type, message, data)

        _patched_log_event._super_log_interceptor = True
        _patched_log_event._super_log_original = original_log_event

        database.log_event = _patched_log_event

        # Also patch modules that imported log_event directly (from database import log_event)
        # These hold stale references and bypass the database.log_event monkey-patch.
        import sys
        _direct_import_modules = [
            "coin_manager", "bot_loop", "offer_manager", "fill_tracker",
            "risk_manager", "price_engine", "market_intel", "sniper",
            "boost_manager", "splash_manager", "dexie_manager",
            "coin_prep_worker", "wallet_sage", "wallet_chia",
        ]
        for _mod_name in _direct_import_modules:
            _mod = sys.modules.get(_mod_name)
            if _mod and hasattr(_mod, "log_event"):
                try:
                    _mod.log_event = _patched_log_event
                except Exception:
                    pass

        slog("SUPER_LOG", "Intercepted database.log_event()")
    except Exception as e:
        slog("SUPER_LOG", f"Failed to intercept log_event: {e}", level="warn")


# ---------------------------------------------------------------------------
# DB operation wrappers
# ---------------------------------------------------------------------------

def log_db_write(operation: str, detail: str = ""):
    """Log a database write operation."""
    thread = threading.current_thread().name
    slog("DB_WRITE", f"{operation}",
         {"thread": thread, "detail": detail} if detail else {"thread": thread},
         level="debug")


def log_db_lock(operation: str, wait_ms: float = 0):
    """Log a database lock event."""
    thread = threading.current_thread().name
    lvl = "warn" if wait_ms > 100 else "debug"
    slog("DB_LOCK", f"{operation}",
         {"thread": thread, "wait_ms": f"{wait_ms:.1f}"} if wait_ms else {"thread": thread},
         level=lvl)


# ---------------------------------------------------------------------------
# Diagnostic: get logging stats
# ---------------------------------------------------------------------------

def get_log_stats() -> dict:
    """Return current logging statistics for the GUI/API."""
    # Count total log files on disk
    total_log_bytes = 0
    log_file_count = 0
    try:
        pattern = os.path.join(_log_dir, "bot_superlog_*.log")
        for f in glob.glob(pattern):
            total_log_bytes += os.path.getsize(f)
            log_file_count += 1
    except Exception:
        pass

    # Archive info
    archive_entries = 0
    archive_bytes = 0
    try:
        archive_path = os.path.join(_log_dir, ARCHIVE_FILENAME)
        if os.path.exists(archive_path):
            archive_bytes = os.path.getsize(archive_path)
            with open(archive_path, "r") as f:
                archive_entries = sum(1 for _ in f)
    except Exception:
        pass

    return {
        "log_path": _log_path,
        "bytes_written": _bytes_written,
        "mb_written": round(_bytes_written / (1024 * 1024), 2),
        "total_log_mb": round(total_log_bytes / (1024 * 1024), 2),
        "log_file_count": log_file_count,
        "ring_buffer_size": len(_ring_buffer),
        "ring_buffer_capacity": RING_BUFFER_SIZE,
        "error_dumps": _error_dump_count,
        "file_level": [k for k, v in LEVELS.items() if v == _file_level][0],
        "terminal_level": [k for k, v in LEVELS.items() if v == _terminal_level][0],
        "max_log_mb": MAX_LOG_SIZE_MB,
        "archive_entries": archive_entries,
        "archive_kb": round(archive_bytes / 1024, 1),
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def close_super_log():
    """Close the log file cleanly."""
    global _log_file, _initialized
    if _log_file:
        slog("SUPER_LOG", "Closing super log")
        try:
            if sys.__stdout__ is not None:
                sys.stdout = sys.__stdout__
            if sys.__stderr__ is not None:
                sys.stderr = sys.__stderr__
            _log_file.flush()
            _log_file.close()
        except Exception as e:
            if sys.__stderr__ is not None:
                sys.__stderr__.write(f"[SUPER_LOG] Error closing log: {e}\n")
    _initialized = False


def get_log_path() -> str:
    """Return the path to the current log file."""
    return _log_path

