"""Desktop entry point that wraps the Flask server in a PyWebView window

Top-level launcher for CATalyst. Starts the Flask API server (`api_server.py`)
in a background thread, opens a native frameless PyWebView window that loads
the dashboard from loopback, wires the system tray and native notifications,
and coordinates graceful shutdown with window-state persistence. On Windows
it transparently respawns under `pythonw.exe` so the console window is hidden
during normal use.

Key responsibilities:
    - Launch and supervise the embedded Flask server thread
    - Create the PyWebView window and inject the AppBridge as `js_api`
    - Own the tray icon, native notifications, and window-state persistence
    - Dispatch `main(argv)` to `run_desktop_mode(dev_mode)` or `run_flask_mode()`

The `--flask` flag skips the desktop window and serves the GUI to a browser
at localhost:5000; `--dev` runs both simultaneously for debugging.
"""

import sys
import os
import io
import signal
import threading
import time
import argparse
import subprocess

# ---------------------------------------------------------------------------
# Fix Windows cp1252 terminal encoding so emoji in log messages don't crash.
# Forces UTF-8 on stdout/stderr (including sys.__stdout__/__stderr__ used by
# super_log's slog() function).
# ---------------------------------------------------------------------------
_under_pythonw = (
    sys.platform == "win32"
    and os.path.basename(sys.executable or "").lower() == "pythonw.exe"
)

if sys.platform == "win32":
    # stdout and __stdout__ share a buffer, so detach old wrapper first.
    # Under pythonw.exe both are None — redirect to a startup log so that
    # print() calls don't crash and startup errors are captured on disk.
    _pythonw_log = None  # opened lazily below
    for _pair in [("stdout", "__stdout__"), ("stderr", "__stderr__")]:
        _st = getattr(sys, _pair[0], None)
        if _st is not None and hasattr(_st, "buffer"):
            _buf = _st.detach()  # disconnect old wrapper without closing buffer
            _wrapped = io.TextIOWrapper(
                _buf, encoding="utf-8", errors="replace",
                line_buffering=True,
            )
            setattr(sys, _pair[0], _wrapped)
            setattr(sys, _pair[1], _wrapped)
        elif _st is None and _under_pythonw:
            # pythonw.exe — open a startup log file so print() works
            if _pythonw_log is None:
                try:
                    _log_dir = os.path.join(
                        os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Catalyst",
                    )
                    os.makedirs(_log_dir, exist_ok=True)
                    _pythonw_log = open(
                        os.path.join(_log_dir, "startup.log"),
                        "w", encoding="utf-8", errors="replace",
                    )
                except Exception:
                    _pythonw_log = open(os.devnull, "w", encoding="utf-8")
            setattr(sys, _pair[0], _pythonw_log)
            setattr(sys, _pair[1], _pythonw_log)

# ---------------------------------------------------------------------------
# Early path setup — make application modules importable.
#
# The application code lives under `src/catalyst/`. We put that directory
# on sys.path so flat imports like `from api_server import ...` resolve
# without every module having to know it lives inside a `src/` tree. The
# repo root stays on sys.path too for any stragglers that look next to
# desktop_app.py (e.g. bundled assets).
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(APP_DIR, "src", "catalyst")
os.chdir(APP_DIR)
for _p in (_SRC_DIR, APP_DIR):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


def _bundle_path(relative: str) -> str:
    """Return the absolute path to a bundled resource file.

    In a PyInstaller onedir bundle, data files are extracted alongside the
    executable in sys._MEIPASS.  In normal (dev) mode, files live next to
    this script.  Using this helper ensures path resolution is correct in
    both environments.

    Usage:
        path = _bundle_path('bot_gui.html')
        path = _bundle_path('splash.exe')
    """
    base = getattr(sys, '_MEIPASS', APP_DIR)
    return os.path.join(base, relative)


# ---------------------------------------------------------------------------
# Version & constants
# ---------------------------------------------------------------------------
from _version import __version__ as APP_VERSION

APP_NAME = "CATalyst"
FLASK_HOST = "127.0.0.1"
try:
    FLASK_PORT = int(os.environ.get("CATALYST_FLASK_PORT", "5000"))
except (TypeError, ValueError):
    FLASK_PORT = 5000
WINDOW_WIDTH = 1600
WINDOW_HEIGHT = 1000
WINDOW_MIN_WIDTH = 1000
WINDOW_MIN_HEIGHT = 700
# True when the app is running without a visible console (pythonw.exe or
# after _hide_windows_console() is called).  The crash handler uses this to
# decide whether to show a native dialog instead of printing to the terminal.
_CONSOLE_HIDDEN = _under_pythonw
_RESPAWN_ENV = "BOT_GUI_RESPAWNED_UNDER_PYTHONW"
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000


def _kill_on_close_job_limit_flags() -> int:
    """Return the Windows Job Object flags used by the desktop parent."""
    return JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK

# Window geometry persistence — lives in the user data directory so
# the setting survives installs to read-only locations like Program Files.
try:
    from user_paths import window_state_file as _window_state_file
    _WINDOW_STATE_FILE = _window_state_file()
except Exception:
    _WINDOW_STATE_FILE = os.path.join(APP_DIR, ".window_state.json")


def _load_window_state() -> dict:
    """Return the last saved window size/position, or {} if none."""
    try:
        import json as _json
        if not os.path.exists(_WINDOW_STATE_FILE):
            return {}
        with open(_WINDOW_STATE_FILE, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
        if not isinstance(data, dict):
            return {}
        # Minimal validation — ignore obviously bad values
        width = int(data.get("width", 0) or 0)
        height = int(data.get("height", 0) or 0)
        if width < WINDOW_MIN_WIDTH or height < WINDOW_MIN_HEIGHT:
            return {}
        if width > 8000 or height > 8000:
            return {}
        return {
            "width": width,
            "height": height,
            "x": int(data.get("x", 0) or 0),
            "y": int(data.get("y", 0) or 0),
            "maximized": bool(data.get("maximized", False)),
        }
    except Exception as e:
        print(f"[WINDOW] Could not load window state: {e}", flush=True)
        return {}


def _save_window_state(window) -> None:
    """Persist the current window size/position to disk."""
    if window is None:
        return
    try:
        import json as _json
        state = {
            "width": int(getattr(window, "width", 0) or 0),
            "height": int(getattr(window, "height", 0) or 0),
            "x": int(getattr(window, "x", 0) or 0),
            "y": int(getattr(window, "y", 0) or 0),
        }
        # Skip obviously invalid snapshots (e.g. minimized window reports 0/0)
        if state["width"] < WINDOW_MIN_WIDTH or state["height"] < WINDOW_MIN_HEIGHT:
            return
        with open(_WINDOW_STATE_FILE, "w", encoding="utf-8") as fh:
            _json.dump(state, fh)
    except Exception as e:
        print(f"[WINDOW] Could not save window state: {e}", flush=True)


def _apply_window_icon_win32(ico_path: str) -> None:
    """Set the CATalyst .ico as the Win32 window icon (taskbar + Alt+Tab).

    PyWebView does not expose a window icon API, and in dev mode
    (python desktop_app.py) the OS defaults to Python's snake icon.
    This function fixes that by sending WM_SETICON directly to the
    HWND via ctypes — works in both dev mode and the built .exe.

    Runs in a background thread because the window may not exist yet
    when the bot is starting.  Polls for up to 15 seconds.
    """
    if sys.platform != "win32":
        return
    if not os.path.isfile(ico_path):
        return

    import ctypes
    import ctypes.wintypes

    WM_SETICON   = 0x0080
    ICON_SMALL   = 0        # 16 × 16 — title bar
    ICON_BIG     = 1        # 32 × 32 — taskbar / Alt+Tab
    IMAGE_ICON   = 1
    LR_LOADFROMFILE   = 0x0010
    LR_DEFAULTSIZE    = 0x0040

    pid = os.getpid()

    # --- Helper: enumerate visible top-level windows owned by our PID ---
    def _find_hwnd() -> int:
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _cb(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            pid_buf = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
            if pid_buf.value == pid:
                found.append(hwnd)
            return True

        ctypes.windll.user32.EnumWindows(_cb, 0)
        # Prefer a window whose title matches APP_NAME
        for h in found:
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(h, buf, 256)
            if APP_NAME.lower() in buf.value.lower():
                return h
        return found[0] if found else 0

    deadline = time.time() + 15.0
    hwnd = 0
    while not hwnd and time.time() < deadline:
        time.sleep(0.25)
        hwnd = _find_hwnd()

    if not hwnd:
        print("  [ICON] Warning: window HWND not found — taskbar icon unchanged.", flush=True)
        return

    small_ico = ctypes.windll.user32.LoadImageW(
        None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE
    )
    big_ico = ctypes.windll.user32.LoadImageW(
        None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE
    )
    # Fallback: let Windows pick the best size from the .ico file
    if not small_ico:
        small_ico = ctypes.windll.user32.LoadImageW(
            None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
    if not big_ico:
        big_ico = small_ico

    if small_ico:
        ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small_ico)
    if big_ico:
        ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big_ico)

    print(f"  [ICON] Taskbar icon set from {os.path.basename(ico_path)}", flush=True)


def _set_windows_app_user_model_id() -> None:
    """Set the Windows App User Model ID (AUMID) so the taskbar groups all
    CATalyst windows together under the same identity regardless of whether the
    app is launched from the built .exe, pythonw, or python.

    Without an explicit AUMID, Windows falls back to the executable path, which
    means python.exe / pythonw.exe / Catalyst.exe each get their own
    taskbar bucket.  Setting a stable AUMID unifies them and also makes
    jump-lists and Start-Menu pinning work correctly.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "com.monkeyzoo.catalyst"
        )
    except Exception:
        pass


def _hide_windows_console() -> bool:
    """Hide the parent console window in normal desktop mode.

    This keeps the app GUI-only for day-to-day use on Windows while still
    allowing explicit console mode in dev or Flask-only runs.
    """
    global _CONSOLE_HIDDEN
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
            _CONSOLE_HIDDEN = True
            return True
    except Exception:
        pass
    return False


def _show_fatal_error_dialog(message: str):
    """Show a native fatal error dialog when no console is visible."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)
            return
        except Exception:
            pass
    print(message)


def _get_pythonw_executable() -> str:
    """Find a matching pythonw.exe for the current interpreter."""
    if sys.platform != "win32":
        return ""
    exe = os.path.abspath(sys.executable or "")
    base = os.path.basename(exe).lower()
    if base == "pythonw.exe":
        return exe
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(candidate):
        return candidate
    return ""


def _respawn_under_pythonw() -> bool:
    """Restart the app under pythonw so no console is created at all.

    Hiding a console after launch still leaves a Windows taskbar entry when the
    process itself was started by python.exe. Relaunching under pythonw avoids
    creating that console in the first place.
    """
    if sys.platform != "win32":
        return False
    if os.environ.get(_RESPAWN_ENV) == "1":
        return False
    if os.path.basename(sys.executable or "").lower() == "pythonw.exe":
        return False

    pythonw = _get_pythonw_executable()
    if not pythonw:
        return False

    env = os.environ.copy()
    env[_RESPAWN_ENV] = "1"
    creationflags = 0
    for flag_name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= getattr(subprocess, flag_name, 0)

    cmd = [pythonw, os.path.abspath(__file__), *sys.argv[1:]]
    try:
        subprocess.Popen(
            cmd,
            cwd=APP_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return True
    except Exception:
        return False


def check_port_free(port: int) -> bool:
    """Check if localhost port is available. Returns True if free."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1)
        sock.connect(("127.0.0.1", port))
        sock.close()
        return False  # Port is in use
    except (ConnectionRefusedError, OSError, socket.timeout):
        return True  # Port is free
    finally:
        try:
            sock.close()
        except Exception:
            pass


# Module-level handles so the OS resources (singleton lock, kill-on-close
# Job Object) survive the lifetime of the process. If these are GC'd the
# lock is released, the job is destroyed early, and the protection breaks.
_instance_lock_handle = None
_kill_on_close_job = None


def _instance_lock_path() -> str:
    """Path to the cross-process singleton lock file."""
    try:
        from user_paths import data_dir
        return os.path.join(data_dir(), ".instance.lock")
    except Exception:
        return os.path.join(APP_DIR, ".instance.lock")


def _acquire_instance_lock() -> bool:
    """Acquire an exclusive cross-process singleton lock.

    The previous duplicate-prevention logic relied on `check_port_free()`
    after Flask startup. That left a 1-2 second race window during Python
    startup where a second launch could pass the port check before
    instance #1 had bound 5000. Both instances would then run their own
    Flask, AppBridge, and coin-prep worker — the workers would race to
    split the same wallet coins, and Sage would reject the loser with
    MEMPOOL_CONFLICT.

    OS-level file locks close that window: the lock is granted atomically
    by the kernel and is released automatically when the process dies
    (even on SIGKILL / Task Manager / power loss), so there is no stale-
    lock-file problem to clean up.

    Returns True when the lock is held by us. Returns False when another
    instance already holds it — caller should redirect the user to the
    running instance and exit.
    """
    global _instance_lock_handle
    lock_path = _instance_lock_path()
    try:
        fh = open(lock_path, "a+", encoding="utf-8")
    except Exception as e:
        print(f"[INSTANCE-LOCK] Could not open {lock_path}: {e} — skipping lock", flush=True)
        return True  # Fail open: don't block startup on a filesystem hiccup
    try:
        if sys.platform == "win32":
            import msvcrt
            # msvcrt.locking() locks bytes starting at the *current* file
            # position. "a+" leaves the position at end-of-file, so without
            # this seek a second launcher (which sees the file populated by
            # instance #1) would lock a different byte and the call would
            # succeed; both processes pass the singleton check, both run
            # coin-prep, both race the wallet -> MEMPOOL_CONFLICT cascade.
            # Pin the lock to byte 0 so every process contends on the same
            # region.
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            # flock locks the open file description, not a byte range, so
            # file position doesn't matter on POSIX.
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        try:
            fh.close()
        except Exception:
            pass
        return False
    try:
        # Identify this process in the lock file. We must seek past the
        # locked byte (Windows: byte 0) before truncating, otherwise the
        # subsequent write/truncate could clip the locked region.
        fh.seek(1) if sys.platform == "win32" else fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} started={int(time.time())}\n")
        fh.flush()
    except Exception:
        pass
    _instance_lock_handle = fh
    return True


def _open_existing_instance_in_browser() -> None:
    """Bring the already-running instance forward when our own start was rejected."""
    _app_url = f"http://{FLASK_HOST}:{FLASK_PORT}/"
    print(f"\n  {APP_NAME} is already running — opening {_app_url}", flush=True)
    try:
        import webbrowser
        webbrowser.open(_app_url)
    except Exception:
        pass


def _attach_to_kill_on_close_job() -> bool:
    """Assign this process to a Windows Job Object that kills every child
    when the parent dies — including SIGKILL / Task Manager / power loss.

    Without this, coin-prep workers spawned via subprocess.Popen survive
    a forced parent-kill on Windows (no SIGHUP is sent), and continue
    submitting split TXs to Sage from the dead. That orphan-worker class
    of bug is what produced the MEMPOOL_CONFLICT cascade earlier today.

    Children of this process inherit job assignment automatically on
    Windows 8+ via nested-job support. We hold the job handle for the
    lifetime of this process; when the kernel releases it on exit, the
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE flag terminates everything still
    running in the job. The job also allows explicit child breakaway so
    independently owned external apps can survive CATalyst shutdown.

    Returns True on success. False on platforms / Windows versions where
    nested jobs aren't supported — non-fatal, startup continues without
    the protection (the singleton lock still bounds duplicate parents).
    """
    global _kill_on_close_job
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    JobObjectExtendedLimitInformation = 9

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE

        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return False

        limits = _EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = _kill_on_close_job_limit_flags()
        if not kernel32.SetInformationJobObject(
            h_job, JobObjectExtendedLimitInformation,
            ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            return False

        if not kernel32.AssignProcessToJobObject(h_job, kernel32.GetCurrentProcess()):
            # Most common failure: the parent (e.g. Explorer) already put us in
            # a job that doesn't allow nesting. Non-fatal — singleton lock
            # alone still prevents duplicate parents.
            return False

        _kill_on_close_job = h_job
        return True
    except Exception as e:
        print(f"[JOB] Could not attach to kill-on-close job: {e}", flush=True)
        return False


def wait_for_flask(timeout: float = 15.0) -> bool:
    """Wait for Flask to start accepting connections. Returns True if ready."""
    import socket
    start = time.time()
    while time.time() - start < timeout:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(1)
            sock.connect((FLASK_HOST, FLASK_PORT))
            sock.close()
            return True
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.3)
        finally:
            try:
                sock.close()
            except Exception:
                pass
    return False


def start_flask_server():
    """
    Start the Flask API server in the current thread.
    This runs in a daemon thread so it dies when the main process exits.
    """
    # Import api_server - this triggers all the module imports and init
    import api_server

    # Initialise database
    from database import init_database
    init_database()

    # Create bot instance
    api_server.create_bot()

    # Wallet startup is triggered later by the GUI via POST /api/wallet/begin-startup
    # after the user accepts the disclaimer and chooses how to connect to Sage.
    # Do NOT call sage_node.start_preload() here — it would auto-launch Sage
    # before the user reaches the "Connect to Sage" screen.

    # Record session start time — dashboard/logs only show events from THIS session
    from datetime import datetime, timezone
    api_server._session_start_time = datetime.now(timezone.utc).isoformat()

    # Restore log clear-point from database
    try:
        from database import get_setting
        saved = get_setting("logs_cleared_at")
        if saved:
            api_server._logs_cleared_at = saved
    except Exception:
        pass

    # Restore fresh-run cutoff so PnL/history stay scoped to the current run
    try:
        restored_cutoff = api_server._restore_run_history_cutoff_from_events()
        if restored_cutoff:
            print(f"  [Fresh Run] Restored history cutoff: {restored_cutoff}")
    except Exception:
        pass

    # Run Flask (this blocks until shutdown)
    from database import log_event
    log_event("info", "server_started", f"Desktop app v{APP_VERSION} starting Flask on port {FLASK_PORT}")

    api_server.app.run(
        host=FLASK_HOST,
        port=FLASK_PORT,
        debug=False,
        threaded=True,
        use_reloader=False  # Important: don't use reloader in desktop mode
    )


def run_desktop_mode(dev_mode: bool = False):
    """Main desktop app flow."""
    try:
        import webview
    except ImportError:
        print("\n  ERROR: pywebview is not installed.")
        print("  Install it with: pip install pywebview --break-system-packages")
        print("  Running in Flask-only mode instead.\n")
        run_flask_mode()
        return

    print(f"\n  {APP_NAME} v{APP_VERSION}")
    print(f"  {'=' * 40}")

    # Check port — if our app is already running, open it in the browser
    # instead of showing an error and quitting.
    if not check_port_free(FLASK_PORT):
        _app_url = f"http://{FLASK_HOST}:{FLASK_PORT}/"
        print(f"\n  {APP_NAME} is already running — opening {_app_url}")
        try:
            import webbrowser
            webbrowser.open(_app_url)
        except Exception:
            pass
        if _CONSOLE_HIDDEN or _under_pythonw:
            # Only show dialog if browser open fails silently (edge case)
            pass
        sys.exit(0)

    # Start Flask in background thread
    print(f"  Starting Flask server on port {FLASK_PORT}...")
    flask_thread = threading.Thread(target=start_flask_server, daemon=True, name="FlaskServer")
    flask_thread.start()

    # Wait for Flask to be ready
    print("  Waiting for Flask to accept connections...")
    if not wait_for_flask(timeout=20.0):
        print("\n  ERROR: Flask didn't start within 20 seconds.")
        print("  Check the console output above for errors.")
        sys.exit(1)

    print("  Flask is ready.")

    # Start system tray in background
    tray_thread = None
    try:
        from tray_manager import TrayManager
        tray = TrayManager(app_name=APP_NAME, app_version=APP_VERSION)
        tray_thread = threading.Thread(target=tray.run, daemon=True, name="SystemTray")
        tray_thread.start()
        print("  System tray icon active.")
    except ImportError:
        print("  System tray disabled (pystray not installed).")
        tray = None
    except Exception as e:
        print(f"  System tray failed: {e}")
        tray = None

    # Start notification manager
    try:
        from notification_manager import NotificationManager
        notifier = NotificationManager(app_name=APP_NAME)
        print("  Notifications enabled.")
    except ImportError:
        print("  Notifications disabled (plyer not installed).")
        notifier = None
    except Exception as e:
        print(f"  Notifications failed: {e}")
        notifier = None

    # Wire up tray callbacks
    if tray:
        tray.on_show_dashboard = lambda: _show_window(webview)
        # Tray "Exit" now routes through the SAME graceful shutdown as the
        # in-window X button: show the window, trigger the shutdown modal,
        # and let the user confirm (or cancel) offer cancellation. This
        # removes the previous inconsistency where tray exit silently
        # bypassed the shutdown confirmation.
        tray.on_quit = lambda: _tray_graceful_quit(webview, tray)

        # Phase 3: Start / Stop from tray — call Flask API and show window
        # Read the auth token from the environment (set by api_server at import time).
        _tray_token = os.environ.get("BOT_LOCAL_WRITE_TOKEN", "")

        def _tray_start_bot():
            """Start bot from tray: call Flask, then bring window to front."""
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"http://{FLASK_HOST}:{FLASK_PORT}/api/bot/start",
                    data=b"{}",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Bot-Local-Token": _tray_token,
                    }
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    _ = resp.read()
            except Exception as e:
                print(f"[TRAY] Start bot failed: {e}", flush=True)
            _show_window(webview)

        def _tray_stop_bot():
            """Stop bot from tray: call Flask."""
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"http://{FLASK_HOST}:{FLASK_PORT}/api/bot/stop",
                    data=b"{}",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-Bot-Local-Token": _tray_token,
                    }
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    _ = resp.read()
            except Exception as e:
                print(f"[TRAY] Stop bot failed: {e}", flush=True)

        tray.on_start_bot = _tray_start_bot
        tray.on_stop_bot = _tray_stop_bot

    # Wire up notifier to bot events (if both available)
    if notifier:
        _wire_notifications(notifier)

    # Phase 3: Start tray status poller — polls /api/status every 10s
    # and calls tray.update_tray_state() so the icon/tooltip/menu stay current.
    if tray:
        tray_poll_thread = threading.Thread(
            target=_poll_tray_status,
            args=(tray,),
            daemon=True,
            name="TrayStatusPoller"
        )
        tray_poll_thread.start()

    print("\n  Launching desktop window...")
    if dev_mode:
        print(f"  Dev mode: also accessible at http://{FLASK_HOST}:{FLASK_PORT}/")

    # Create JS bridge for window.pywebview.api calls
    try:
        from app_bridge import AppBridge
        bridge = AppBridge()
        print("  JS bridge ready (AppBridge).")
    except Exception as e:
        print(f"  Warning: JS bridge failed to load: {e}")
        bridge = None

    # Restore last-saved window geometry if we have one
    _saved_state = _load_window_state()
    _win_width  = _saved_state.get("width",  WINDOW_WIDTH)
    _win_height = _saved_state.get("height", WINDOW_HEIGHT)
    _win_x      = _saved_state.get("x")
    _win_y      = _saved_state.get("y")

    # Show a local splash page first (logo + "created by MonkeyZoo") so the
    # window doesn't flash black while the WebView2 backend boots and Flask's
    # first HTML render lands. The splash auto-redirects to the Flask URL
    # after a brief delay (see splash.html).
    _splash_path = _bundle_path("splash.html")
    if os.path.exists(_splash_path):
        _initial_url = "file:///" + _splash_path.replace("\\", "/")
    else:
        _initial_url = f"http://{FLASK_HOST}:{FLASK_PORT}/"

    _create_window_kwargs = dict(
        title=APP_NAME,
        url=_initial_url,
        js_api=bridge,
        width=_win_width,
        height=_win_height,
        min_size=(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
        resizable=True,
        frameless=True,
        shadow=True,
        easy_drag=False,
        text_select=False,
        background_color="#0B0E14",
    )
    # Only pass x/y if we actually have a saved position — PyWebView will
    # centre on primary display otherwise, which is the correct default.
    if _win_x is not None and _win_y is not None:
        _create_window_kwargs["x"] = _win_x
        _create_window_kwargs["y"] = _win_y

    window = webview.create_window(**_create_window_kwargs)

    # Apply CATalyst icon to the taskbar / Alt+Tab button via Win32 WM_SETICON.
    # PyWebView has no icon API — in dev mode the OS shows Python's snake icon
    # by default.  We patch it in a background thread (the window HWND isn't
    # available until after webview.start() launches the event loop).
    if sys.platform == "win32":
        _ico = _bundle_path(os.path.join("assets", "bot_icon_new.ico"))
        if not os.path.isfile(_ico):
            _ico = _bundle_path("bot_icon_new.ico")  # root fallback
        _icon_thread = threading.Thread(
            target=_apply_window_icon_win32,
            args=(_ico,),
            daemon=True,
            name="WindowIconSetter",
        )
        _icon_thread.start()

    # Close button = graceful shutdown (stop bot, kill Flask, exit).
    # We also snapshot the window geometry here so the next launch
    # restores the same size/position.
    #
    # Alt+F4 protection: if the bot is running and the user has not yet
    # acknowledged the shutdown modal, we cancel the close and bounce
    # through the in-GUI modal first. Only when the modal has finished
    # its graceful sequence (and sets _state["confirmed_close"]=True) do
    # we let the window actually close.
    _state["confirmed_close"] = False

    def on_closing():
        # If the user already confirmed via the modal, let it through.
        if _state.get("confirmed_close"):
            try:
                _save_window_state(window)
            except Exception:
                pass
            _cleanup()
            if tray:
                try:
                    tray.stop()
                except Exception:
                    pass
            return True

        # Check whether the bot is actually running. If not, we can
        # close immediately — there's nothing to gracefully shut down.
        bot_running = False
        try:
            import api_server as _api
            if _api.bot and getattr(_api.bot, "_running", False):
                bot_running = True
        except Exception:
            pass

        if not bot_running:
            # Safe path — save state and close.
            try:
                _save_window_state(window)
            except Exception:
                pass
            _cleanup()
            if tray:
                try:
                    tray.stop()
                except Exception:
                    pass
            return True

        # Bot is running — route through the in-GUI shutdown modal.
        try:
            window.show()
            window.restore()
            window.evaluate_js("window.showShutdownModal && window.showShutdownModal();")
            print("\n  Alt+F4 intercepted — showing shutdown confirmation.", flush=True)
        except Exception as e:
            print(f"  [CLOSE] Could not show shutdown modal: {e}", flush=True)
            # Fall back to hard close so the user isn't trapped
            try:
                _save_window_state(window)
            except Exception:
                pass
            _cleanup()
            return True
        # Cancel this close event — the modal will set confirmed_close
        # and re-invoke the close when it finishes the graceful sequence.
        return False

    window.events.closing += on_closing
    # Expose the confirm flag to the JS side via the bridge.
    # The shutdown modal's "Shutdown App" button sets this before
    # re-triggering close, so a second on_closing() call is honoured.
    if bridge is not None:
        try:
            bridge._set_confirmed_close = lambda: _state.update({"confirmed_close": True})
        except Exception:
            pass

    # Store window reference for tray callbacks
    _state["window"] = window
    _state["tray"] = tray
    _state["notifier"] = notifier

    # Start PyWebView event loop (blocks until all windows closed)
    webview.start(
        debug=dev_mode,
        gui=_detect_gui_backend(),
        http_server=False,  # We run our own Flask server
    )

    # If we get here, all windows are closed
    print("\n  Desktop window closed.")
    print("  Stopping bot...", flush=True)
    _cleanup()
    print("  Shutdown complete. Goodbye!", flush=True)
    time.sleep(0.5)  # Brief pause so user can see the shutdown messages
    os._exit(0)  # Force exit - daemon threads (Flask, tray) won't block


def run_flask_mode():
    """Fallback: run as plain Flask server (like v3)."""
    print(f"\n  {APP_NAME} v{APP_VERSION} - Flask Mode")
    print(f"  {'=' * 40}")
    print(f"  Open http://{FLASK_HOST}:{FLASK_PORT}/ in your browser")
    print("  Press Ctrl+C to stop\n")

    if not check_port_free(FLASK_PORT):
        print(f"  Port {FLASK_PORT} is already in use!")
        sys.exit(1)

    # Register signal handlers
    signal.signal(signal.SIGINT, lambda s, f: _cleanup())
    signal.signal(signal.SIGTERM, lambda s, f: _cleanup())
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, lambda s, f: _cleanup())

    start_flask_server()


# ---------------------------------------------------------------------------
# Internal state & helpers
# ---------------------------------------------------------------------------
_state = {
    "window": None,
    "tray": None,
    "notifier": None,
}


def _detect_gui_backend():
    """Detect best PyWebView GUI backend for the current platform."""
    if sys.platform == "win32":
        return "edgechromium"  # Edge WebView2 - best on Windows
    elif sys.platform == "darwin":
        return None  # Default WebKit on macOS
    else:
        return None  # Default GTK WebKit on Linux


def _show_window(webview_module):
    """Show/focus the main window (called from tray)."""
    window = _state.get("window")
    if window:
        try:
            window.show()
            window.restore()
        except Exception:
            pass


def _quit_app(webview_module, tray):
    """Clean shutdown from tray quit action (fallback for non-graceful paths)."""
    _cleanup()
    try:
        # Destroy all webview windows
        for win in webview_module.windows:
            win.destroy()
    except Exception:
        pass
    if tray:
        tray.stop()
    # Force exit after brief cleanup window
    threading.Timer(2.0, lambda: os._exit(0)).start()


def _tray_graceful_quit(webview_module, tray):
    """Tray-initiated graceful quit.

    Shows the main window and triggers the in-GUI shutdown modal via the
    JS bridge.  If the window/bridge isn't available, falls back to the
    older hard _quit_app path.  This way tray Exit behaves exactly like
    clicking the X button in the custom titlebar — letting users cancel
    offers first if the bot is running.
    """
    window = _state.get("window")
    if not window:
        # No window — nothing to show, just quit.
        _quit_app(webview_module, tray)
        return

    try:
        # Bring the window up so the modal is visible.
        window.show()
        window.restore()
    except Exception:
        pass

    try:
        # Trigger the same shutdown modal the X button uses.
        window.evaluate_js(
            "window.showShutdownModal && window.showShutdownModal();"
        )
    except Exception as e:
        print(f"[TRAY] Graceful quit via JS bridge failed: {e}", flush=True)
        _quit_app(webview_module, tray)


def _cleanup():
    """Clean shutdown of bot and modules."""
    try:
        import api_server
        if api_server.bot and api_server.bot._running:
            print("  Stopping bot...")
            api_server.bot.stop()
    except Exception:
        pass

    try:
        from database import log_event
        log_event("info", "app_shutdown", f"Desktop app v{APP_VERSION} shutting down")
    except Exception:
        pass


def _poll_tray_status(tray, interval: float = 3.0):
    """
    Phase 3: Background thread that polls /api/status every `interval` seconds
    and calls tray.update_tray_state() to keep the icon/tooltip/menu current.

    Maps bot running state → tray status:
        running=True, circuit_breaker=False  → "running"
        running=True, circuit_breaker=True   → "warning"
        running=False                        → "stopped"
        HTTP error / Flask not up            → keeps last known state
    """
    import urllib.request
    import json as _json

    last_status = "stopped"

    while True:
        try:
            req = urllib.request.Request(
                f"http://{FLASK_HOST}:{FLASK_PORT}/api/status",
                headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = _json.loads(resp.read().decode())

            running = bool(data.get("running", False))
            cb_tripped = bool(data.get("circuit_breaker_tripped", False))

            # Derive tray status
            if running and cb_tripped:
                new_status = "warning"
            elif running:
                new_status = "running"
            else:
                new_status = "stopped"

            # Active CAT name for tooltip (e.g. "MZ")
            cat_name = ""
            cat_info = data.get("current_cat") or {}
            cat_name = (
                cat_info.get("ticker_id")
                or cat_info.get("name")
                or ""
            )
            if not cat_name:
                # Fallback: check top-level fields some status endpoints return
                cat_name = data.get("cat_ticker") or data.get("cat_name") or ""

            if new_status != last_status or cat_name:
                tray.update_tray_state(new_status, cat_name=cat_name)
                last_status = new_status

        except Exception:
            pass   # Flask not ready or transient error — keep last state

        time.sleep(interval)


def _wire_notifications(notifier):
    """Bridge bot EventBus events to OS-level notifications.

    EventBus.subscribe() returns a Queue of {"type", "data", "ts"} messages
    for ALL event types — it is NOT a topic+callback subscription. The
    previous implementation passed ("fill", on_fill) to subscribe(), which
    raised TypeError and silently failed inside the try/except, leaving
    OS notifications completely unwired for the entire session.

    Here we subscribe once to get a queue, then spawn a daemon thread that
    reads messages and dispatches them to the notifier by event type. The
    thread is a daemon so it dies with the app; if EventBus is missing
    (tests, headless mode) the function returns quietly.
    """
    import threading as _threading

    try:
        import api_server
    except Exception:
        return

    bus = getattr(api_server, "events", None)
    if not bus or not hasattr(bus, "subscribe"):
        return

    try:
        q = bus.subscribe()
    except Exception as _sub_err:
        try:
            from super_log import slog as _slog
            _slog("DESKTOP", f"Notification subscribe failed: {_sub_err}",
                  level="warn")
        except Exception:
            pass
        return

    def _dispatch():
        while True:
            try:
                msg = q.get()
            except Exception:
                return
            if not isinstance(msg, dict):
                continue
            ev_type = str(msg.get("type") or "")
            data = msg.get("data") or {}
            try:
                if ev_type == "fill":
                    side = str(data.get("side", "?"))
                    amount = data.get("amount", "?")
                    price = data.get("price", "?")
                    notifier.notify(
                        title="Offer Filled",
                        message=f"{side.upper()}: {amount} at {price}",
                        category="fill",
                    )
                elif ev_type in ("critical", "error"):
                    notifier.notify(
                        title="Bot Error",
                        message=str(data.get("message") or data.get("msg")
                                    or "Unknown error"),
                        category="error",
                    )
                elif ev_type == "alert":
                    severity = str(data.get("severity", "info")).lower()
                    if severity in ("error", "warning", "critical"):
                        notifier.notify(
                            title=str(data.get("title") or "CATalyst alert"),
                            message=str(data.get("message") or ""),
                            category=severity,
                        )
            except Exception:
                # A misbehaving notifier must never kill the dispatcher —
                # swallow so other events keep flowing.
                continue

    t = _threading.Thread(target=_dispatch,
                          name="catalyst-notifications",
                          daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _run_coin_prep_worker_mode(worker_args):
    """Run the coin prep worker inside the packaged Catalyst executable."""
    old_argv = list(sys.argv)
    sys.argv = [old_argv[0] if old_argv else "Catalyst", *worker_args]
    os.environ.setdefault("_CATALYST_PRESERVE_PROCESS_ENV", "1")
    try:
        from coin_prep_worker import main as coin_prep_main
        try:
            result = coin_prep_main()
            return int(result or 0)
        except SystemExit as exc:
            if exc.code is None:
                return 0
            if isinstance(exc.code, int):
                return exc.code
            return 1
    finally:
        sys.argv = old_argv


def main(argv=None):
    """Desktop app entry point for both .py and .pyw launchers."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--coin-prep-worker" in raw_argv:
        worker_flag_index = raw_argv.index("--coin-prep-worker")
        worker_args = raw_argv[:worker_flag_index] + raw_argv[worker_flag_index + 1:]
        return _run_coin_prep_worker_mode(worker_args)

    # Set Windows AUMID as early as possible — must happen before any window
    # creation so that the taskbar groups all CATalyst windows together under
    # "com.monkeyzoo.catalyst" regardless of how the process was launched.
    _set_windows_app_user_model_id()

    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    parser.add_argument("--dev", action="store_true", help="Enable dev mode (browser accessible + debug)")
    parser.add_argument("--flask", action="store_true", help="Flask-only mode (no desktop window)")
    parser.add_argument("--show-console", action="store_true", help="Keep the Windows console visible in desktop mode")
    args = parser.parse_args(argv)

    if not args.flask and not args.dev and not args.show_console:
        if _respawn_under_pythonw():
            return 0

    # Cross-process singleton: only ONE desktop_app may run at a time.
    # Without this, a second double-click within the 1-2s Python startup
    # window slips past the port check, both instances start their own
    # Flask/AppBridge/coin-prep workers, and the workers race the same
    # wallet coins → MEMPOOL_CONFLICT cascade in Sage.
    if not _acquire_instance_lock():
        _open_existing_instance_in_browser()
        return 0

    # Kill-on-close Job Object: ensures default child processes (coin-prep
    # workers, helper commands, etc.) die when this parent dies, even on
    # Task Manager force-kill. External apps can opt into breakaway.
    _attach_to_kill_on_close_job()

    # Auto-recover the SQLite DB if the previous run left it corrupt.
    # The singleton lock guarantees no other process holds bot.db open,
    # which is the precondition for safely swapping in a recovered file.
    # Without this, a once-corrupt DB persists across restarts and bleeds
    # "database disk image is malformed" errors mid-trade until the user
    # manually runs scripts/recover_db.py.
    try:
        from database import attempt_db_recovery
        _rec = attempt_db_recovery() or {}
        _action = _rec.get("action")
        if _action == "recovered":
            print(
                f"\n  [DB] Auto-recovered corrupt bot.db — backed up as "
                f"{_rec.get('corrupt_backup')}, "
                f"{_rec.get('skipped_statements', 0)} unreadable statement(s) skipped",
                flush=True,
            )
        elif _action == "failed":
            print(
                f"\n  [DB] WARNING: bot.db is corrupt and auto-recovery "
                f"failed: {_rec.get('error')}\n"
                f"  Original: {_rec.get('result')}\n"
                f"  Run: python scripts/recover_db.py",
                flush=True,
            )
    except Exception as _rec_err:
        print(f"\n  [DB] Auto-recovery skipped: {_rec_err}", flush=True)

    if not args.flask and not args.dev and not args.show_console:
        _hide_windows_console()

    try:
        if args.flask:
            run_flask_mode()
        else:
            run_desktop_mode(dev_mode=args.dev)
        return 0
    except Exception as e:
        # Log crash to file so we can diagnose even if console is hidden.
        # The crash log lives under the user data directory so it's
        # writable regardless of install location.
        import traceback
        try:
            from user_paths import crash_log_file
            crash_log = crash_log_file()
        except Exception:
            crash_log = os.path.join(APP_DIR, "crash.log")

        # Capture the full traceback as a string so we can include it
        # in the crash dialog as well as the file.
        tb_str = traceback.format_exc()

        # Write the crash log — UTF-8 encoded with ASCII-safe fallbacks
        # so the handler itself never crashes on cp1252 Windows locales.
        try:
            with open(crash_log, "w", encoding="utf-8", errors="replace") as f:
                f.write("CATalyst V4 - Crash Report\n")
                f.write("=" * 50 + "\n")
                f.write(f"Time:    {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Version: {APP_VERSION}\n")
                f.write(f"Python:  {sys.version}\n")
                f.write(f"Platform: {sys.platform}\n")
                f.write(f"Install: {APP_DIR}\n")
                try:
                    from user_paths import data_dir as _dd
                    f.write(f"Data:    {_dd()}\n")
                except Exception:
                    pass
                f.write("\n")
                f.write(f"Error: {e}\n\n")
                f.write("Traceback:\n")
                f.write(tb_str)
        except Exception as write_err:
            # Last-ditch: if we can't write the log file, at least print
            # the traceback to stderr so `--show-console` users see it.
            print(f"[CRASH] Could not write crash log to {crash_log}: {write_err}",
                  file=sys.stderr, flush=True)
            print(tb_str, file=sys.stderr, flush=True)

        # Build a user-friendly message for the fatal dialog. Include
        # the actual error text (not just the filename) so the user can
        # read what went wrong without hunting for the log file.
        short_err = str(e)[:300]
        fatal_msg = (
            f"The app crashed on startup.\n\n"
            f"Error: {short_err}\n\n"
            f"A full crash report has been saved to:\n{crash_log}\n\n"
            f"If this keeps happening, please send the crash.log file to "
            f"support so we can diagnose the issue."
        )

        if _CONSOLE_HIDDEN:
            _show_fatal_error_dialog(fatal_msg)
        else:
            print(f"\n  CRASH: {e}")
            print(f"  {tb_str}")
            print(f"  Details saved to: {crash_log}")
            try:
                input("\n  Press Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
