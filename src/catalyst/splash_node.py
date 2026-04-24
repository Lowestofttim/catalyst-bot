"""Auto-launch and monitor the Splash P2P binary as a managed subprocess

Owns the lifecycle of the Splash P2P node that broadcasts and receives
offers across the Chia ecosystem. Discovers the executable, clears any
stale processes holding the target port, starts Splash with the correct
CLI flags, captures stdout for status, and restarts on crash up to a
configured maximum.

Key responsibilities:
    - Locate splash.exe (project folder, PATH, or configured path)
    - Clean stale listeners on the submission port (default 4000)
    - Launch as a hidden subprocess and pipe stdout into logs
    - Health/status reporting and crash-restart up to a max count

Configured via SPLASH_* env vars. Pairs with splash_manager (outbound
posting) and splash_receive (inbound classification).
"""

import os
import sys
import time
import signal
import socket
import threading
import subprocess
import requests
from typing import Dict, Optional

from config import cfg
from database import log_event
from win_subprocess import hidden_subprocess_kwargs


# Default binary names by platform
_BINARY_NAME = "splash.exe" if sys.platform == "win32" else "splash"


class SplashNode:
    """Manages the Splash P2P binary as a subprocess.

    The bot auto-starts Splash when SPLASH_ENABLED=true and a binary
    is found. If the binary isn't found, it logs a helpful message
    and the bot continues without P2P (still posts to Dexie normally).
    """

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._restart_count: int = 0
        self._max_restarts: int = 5
        self._restart_cooldown: float = 10.0  # seconds between restarts
        self._last_start_time: float = 0
        self._binary_path: Optional[str] = None
        self._pid: Optional[int] = None

        # Output capture
        self._last_output_lines: list = []
        self._max_output_lines: int = 50

        # Metrics: cached from Splash's Prometheus-ish JSON endpoint
        # populated by --listen-metrics. See _poll_metrics for the poller.
        # Fields as returned by splash v0.2.0:
        #   peers              — current peer count
        #   offers_broadcasted — cumulative offers sent (outbound)
        #   offers_received    — cumulative offers received from peers
        #   total_connections  — lifetime peer connections
        # Plus our own:
        #   last_polled_at, last_error, metrics_url
        self._metrics_bind: Optional[str] = None  # set at launch
        self._metrics: dict = {}
        self._metrics_lock = threading.Lock()
        self._metrics_thread: Optional[threading.Thread] = None
        self._metrics_poll_secs: float = 5.0

    # -------------------------------------------------------------------
    # Binary discovery
    # -------------------------------------------------------------------

    def find_binary(self) -> Optional[str]:
        """Find the Splash binary. Search order:
        1. SPLASH_BINARY_PATH from .env (explicit config)
        2. Same directory as this script (V3 folder)
        3. System PATH
        """
        # 1. Explicit config
        configured = getattr(cfg, "SPLASH_BINARY_PATH", "")
        if configured and os.path.isfile(configured):
            self._binary_path = configured
            return configured

        # 2. Same directory as this script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.join(script_dir, _BINARY_NAME)
        if os.path.isfile(local_path):
            self._binary_path = local_path
            return local_path

        # Also check a "splash" subdirectory
        subdir_path = os.path.join(script_dir, "splash", _BINARY_NAME)
        if os.path.isfile(subdir_path):
            self._binary_path = subdir_path
            return subdir_path

        # 3. System PATH
        import shutil
        found = shutil.which(_BINARY_NAME)
        if found:
            self._binary_path = found
            return found

        return None

    # -------------------------------------------------------------------
    # Start / Stop
    # -------------------------------------------------------------------

    def start(self) -> bool:
        """Launch the Splash binary in a background thread.

        Returns True if started, False if binary not found or already running.
        """
        if self._running:
            log_event("info", "splash_node", "Splash node already running")
            return False

        binary = self.find_binary()
        if not binary:
            # Try auto-downloading if enabled
            log_event("info", "splash_node_not_found",
                      "Splash binary not found — attempting auto-download...")
            try:
                from splash_setup import download_splash
                result = download_splash()
                if result.get("success"):
                    binary = self.find_binary()
                    log_event("info", "splash_node_auto_download",
                              f"Auto-downloaded Splash: {result.get('message', '')}")
                else:
                    log_event("warning", "splash_node_download_failed",
                              f"Auto-download failed: {result.get('message', '')}. "
                              f"Use the 'Install Splash Node' button in the GUI, or "
                              f"download manually from "
                              f"https://github.com/dexie-space/splash/releases")
            except Exception as e:
                log_event("warning", "splash_node_download_error",
                          f"Auto-download error: {e}")

        if not binary:
            log_event("warning", "splash_node_not_found",
                      f"Splash binary not found! Use the 'Install Splash Node' "
                      f"button in the Market Intelligence tab, or download "
                      f"'{_BINARY_NAME}' from "
                      f"https://github.com/dexie-space/splash/releases "
                      f"and place it in the V3 folder.")
            return False

        self._running = True
        self._restart_count = 0

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="splash-node"
        )
        self._thread.start()

        log_event("info", "splash_node_started",
                  f"Splash node manager started (binary: {binary})")
        return True

    def stop(self):
        """Stop the Splash node."""
        self._running = False

        if self._process:
            try:
                if sys.platform == "win32":
                    self._process.terminate()
                else:
                    self._process.send_signal(signal.SIGTERM)

                # Wait up to 5 seconds for clean exit
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            except Exception as e:
                log_event("debug", "splash_node_stop_error",
                          f"Error stopping Splash: {e}")
            finally:
                self._process = None
                self._pid = None

        log_event("info", "splash_node_stopped", "Splash node stopped")

    # -------------------------------------------------------------------
    # Run loop (background thread)
    # -------------------------------------------------------------------

    def _run_loop(self):
        """Background thread that launches and monitors the Splash process."""
        while self._running:
            if self._restart_count >= self._max_restarts:
                log_event("error", "splash_node_max_restarts",
                          f"Splash node crashed {self._max_restarts} times — "
                          f"giving up. Check the binary and try restarting the bot.")
                self._running = False
                break

            # Cooldown between restarts
            if self._restart_count > 0:
                time.sleep(self._restart_cooldown)
                if not self._running:
                    break

            try:
                self._launch_process()
            except Exception as e:
                log_event("error", "splash_node_launch_error",
                          f"Failed to launch Splash: {e}")
                self._restart_count += 1
                continue

            # Wait for process to exit
            if self._process:
                returncode = self._process.wait()

                if self._running:
                    # Unexpected exit — will restart
                    self._restart_count += 1
                    log_event("warning", "splash_node_crashed",
                              f"Splash exited with code {returncode} "
                              f"(restart {self._restart_count}/{self._max_restarts})")
                else:
                    # Clean shutdown
                    log_event("info", "splash_node_exited",
                              f"Splash exited cleanly (code {returncode})")

    def _is_port_in_use(self, port: int) -> bool:
        """Check if a TCP port is already bound."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex(("127.0.0.1", port))
                return result == 0  # 0 means connection succeeded = port in use
        except Exception:
            return False

    def _kill_stale_process(self, port: int):
        """Kill any stale Splash process holding our port.

        On Windows, uses netstat + taskkill to find and kill the process
        bound to the given port. This handles orphan Splash instances
        left behind from a previous bot run.
        """
        if not self._is_port_in_use(port):
            return  # Port is free, nothing to do

        log_event("warning", "splash_node_stale",
                  f"Port {port} already in use — killing stale process")

        if sys.platform == "win32":
            try:
                # Find PID using the port via netstat
                result = subprocess.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True, timeout=5,
                    **hidden_subprocess_kwargs(),
                )
                stale_pid = None
                for line in result.stdout.splitlines():
                    # Look for LISTENING on our port
                    if f":{port}" in line and "LISTENING" in line:
                        parts = line.split()
                        if parts:
                            stale_pid = parts[-1]
                            break

                if stale_pid and stale_pid.isdigit():
                    # Verify the process is actually a Splash binary before killing
                    is_splash = False
                    try:
                        name_result = subprocess.run(
                            ["wmic", "process", "where",
                             f"ProcessId={stale_pid}", "get", "Name"],
                            capture_output=True, text=True, timeout=5,
                            **hidden_subprocess_kwargs(),
                        )
                        proc_name = name_result.stdout.lower()
                        is_splash = "splash" in proc_name
                    except Exception:
                        pass  # If we can't verify, don't kill
                    if not is_splash:
                        log_event("warning", "splash_node_stale",
                                  f"PID {stale_pid} on port {port} is not a Splash process — skipping kill")
                    else:
                        log_event("info", "splash_node_stale",
                                  f"Found stale Splash PID {stale_pid} on port {port} — killing")
                        subprocess.run(
                            ["taskkill", "/F", "/PID", stale_pid],
                            capture_output=True, timeout=5,
                            **hidden_subprocess_kwargs(),
                        )
                    # Give the OS a moment to release the port
                    time.sleep(1.5)
                else:
                    log_event("warning", "splash_node_stale",
                              f"Port {port} in use but could not identify PID")
            except Exception as e:
                log_event("warning", "splash_node_stale",
                          f"Failed to kill stale process: {e}")
        else:
            # Unix: use lsof + kill, but verify process name before killing
            try:
                result = subprocess.run(
                    ["lsof", "-ti", f":{port}"],
                    capture_output=True, text=True, timeout=5
                )
                pids = result.stdout.strip().split()
                killed_any = False
                for pid in pids:
                    if not pid.isdigit():
                        continue
                    # Verify this is a splash process before killing — /proc
                    # on Linux, ps fallback elsewhere.
                    is_splash = False
                    try:
                        proc_cmd_path = f"/proc/{pid}/cmdline"
                        if os.path.exists(proc_cmd_path):
                            with open(proc_cmd_path, "rb") as _pf:
                                cmdline = _pf.read().decode("utf-8", errors="replace")
                            is_splash = "splash" in cmdline.lower()
                        else:
                            ps_res = subprocess.run(
                                ["ps", "-p", pid, "-o", "comm="],
                                capture_output=True, text=True, timeout=3
                            )
                            is_splash = "splash" in (ps_res.stdout or "").lower()
                    except Exception:
                        is_splash = False

                    if not is_splash:
                        log_event("warning", "splash_node_stale",
                                  f"Refusing to kill PID {pid} on port {port} — "
                                  f"not a splash process")
                        continue

                    log_event("info", "splash_node_stale",
                              f"Killing stale PID {pid} on port {port}")
                    os.kill(int(pid), signal.SIGTERM)
                    killed_any = True
                if killed_any:
                    time.sleep(1.5)
            except Exception as e:
                log_event("warning", "splash_node_stale",
                          f"Failed to kill stale process: {e}")

    def _launch_process(self):
        """Launch the Splash binary with the correct flags."""
        binary = self._binary_path
        if not binary:
            raise FileNotFoundError("Splash binary path not set")

        # Kill any stale Splash process from a previous run
        submit_host = getattr(cfg, "SPLASH_SUBMIT_URL", "http://localhost:4000")
        port_str = submit_host.rstrip("/").split(":")[-1]
        stale_port = int(port_str) if port_str.isdigit() else 4000
        self._kill_stale_process(stale_port)

        # Build command line
        submit_host = getattr(cfg, "SPLASH_SUBMIT_URL", "http://localhost:4000")
        # Extract host:port from URL (e.g., "http://localhost:4000" → "127.0.0.1:4000")
        submit_bind = submit_host.replace("http://", "").replace("https://", "")
        # Bind to loopback only — never expose offer submission to the network
        if submit_bind.startswith("localhost"):
            submit_bind = submit_bind.replace("localhost", "127.0.0.1")
        elif submit_bind.startswith("0.0.0.0"):
            submit_bind = submit_bind.replace("0.0.0.0", "127.0.0.1")

        # P2P listen port (optional)
        p2p_port = getattr(cfg, "SPLASH_P2P_PORT", 11511)

        # Prometheus metrics port (loopback only). Splash v0.2.0+ exposes
        # peer count, offers seen, gossip rate, and bandwidth counters via
        # --listen-metrics. Without these numbers the operator has no way
        # to tell whether a silent "0 received" counter means the daemon
        # is starved of peers or its offer-hook is broken. We wire them
        # into the Market Intel panel via the stats endpoint below.
        metrics_port = int(getattr(cfg, "SPLASH_METRICS_PORT", 4001) or 4001)
        self._metrics_bind = f"127.0.0.1:{metrics_port}"

        cmd = [
            binary,
            "--listen-offer-submission", submit_bind,
            "--listen-address", f"/ip4/0.0.0.0/tcp/{p2p_port}",
            "--listen-metrics", self._metrics_bind,
        ]

        # Only add --offer-hook if SPLASH_RECEIVE_ENABLED is True.
        # Without this check, Splash forwards every P2P offer to the bot
        # and the bot rejects them all with 403 — flooding the terminal.
        display_hook = None
        if getattr(cfg, "SPLASH_RECEIVE_ENABLED", False):
            bot_port = getattr(cfg, "PORT", 5000)
            offer_hook = f"http://localhost:{bot_port}/api/splash/incoming"
            # No token in URL — the splash/incoming endpoint is token-exempt
            # (loopback-only). Keeping the token out of CLI args prevents it
            # from leaking in process listings (Task Manager, /proc/cmdline).
            display_hook = offer_hook
            cmd.extend(["--offer-hook", offer_hook])
            log_event("info", "splash_node_webhook",
                      f"Offer webhook enabled -> {display_hook}")
        else:
            log_event("info", "splash_node_no_webhook",
                      "Offer webhook disabled (SPLASH_RECEIVE_ENABLED=false) — "
                      "outbound posting only")

        # Add testnet flag if configured
        if getattr(cfg, "SPLASH_TESTNET", False):
            cmd.append("--testnet")

        launch_cmd = " ".join(
            display_hook
            if (
                display_hook
                and part.startswith("http://localhost:")
                and "/api/splash/incoming" in part
            )
            else part
            for part in cmd
        )
        log_event("info", "splash_node_launching",
                  f"Launching: {launch_cmd}")

        # Launch with output capture
        # On Windows, use DETACHED_PROCESS instead of CREATE_NO_WINDOW.
        # CREATE_NO_WINDOW prevents the Splash HTTP listener from binding
        # to its port (confirmed by testing — port 4000 refuses connections).
        # DETACHED_PROCESS still hides the console window but allows
        # full networking (HTTP + P2P).
        kwargs = hidden_subprocess_kwargs(detached=True)

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # Line buffered
            **kwargs
        )

        self._pid = self._process.pid
        self._last_start_time = time.time()

        log_event("info", "splash_node_running",
                  f"Splash node running (PID: {self._pid})")

        # Start metrics poller thread. Pulls the JSON `/metrics` endpoint
        # every few seconds so the GUI + bot_health can reason about peer
        # count and offer throughput without re-hitting splash on every
        # dashboard request. Runs as a daemon — exits with the process.
        if self._metrics_thread is None or not self._metrics_thread.is_alive():
            self._metrics_thread = threading.Thread(
                target=self._poll_metrics,
                daemon=True,
                name="splash-metrics",
            )
            self._metrics_thread.start()

        # Start output reader thread
        reader = threading.Thread(
            target=self._read_output,
            daemon=True,
            name="splash-output"
        )
        reader.start()

    def _read_output(self):
        """Read stdout from the Splash process and capture last N lines."""
        if not self._process or not self._process.stdout:
            return

        try:
            for line in self._process.stdout:
                line = line.rstrip()
                if line:
                    self._last_output_lines.append(line)
                    # Trim to max
                    if len(self._last_output_lines) > self._max_output_lines:
                        self._last_output_lines = self._last_output_lines[-self._max_output_lines:]

                    # Log interesting lines
                    lower = line.lower()
                    # F62 (2026-04-09): suppress noisy startup burst.
                    # When the bot rebroadcasts its existing offers on start,
                    # Splash hasn't finished building its peer list yet and
                    # returns "InsufficientPeers" for every single offer.
                    # That produces 70+ warnings in the first second. Treat
                    # them as debug during the first 30 s after the process
                    # started, and only escalate to warning if they keep
                    # coming after Splash has had time to bootstrap.
                    _since_start = time.time() - float(self._start_time or 0)
                    _is_startup_burst = (
                        _since_start < 30
                        and "insufficientpeers" in lower.replace(" ", "")
                    )
                    if "duplicate" in lower:
                        log_event("debug", "splash_node_output", f"Splash: {line}")
                    elif _is_startup_burst:
                        log_event("debug", "splash_node_output",
                                  f"Splash (startup): {line}")
                    elif "error" in lower or "failed" in lower:
                        log_event("warning", "splash_node_output", f"Splash: {line}")
                    elif "listening" in lower or "connected" in lower or "peer" in lower:
                        log_event("debug", "splash_node_output", f"Splash: {line}")
        except Exception:
            pass  # Process ended

    # -------------------------------------------------------------------
    # Health / Status
    # -------------------------------------------------------------------

    def is_running(self) -> bool:
        """Check if the Splash process is alive."""
        if self._process is None:
            return False
        return self._process.poll() is None

    def check_health(self) -> Dict:
        """Check Splash node health by pinging the submission endpoint."""
        submit_url = getattr(cfg, "SPLASH_SUBMIT_URL", "http://localhost:4000")

        # If the user never clicked "Start Splash Node", _binary_path is
        # still None because find_binary() only runs inside start(). Run
        # a lazy lookup here so the health check reflects the real state
        # of the filesystem rather than a stale "No binary" label when
        # splash.exe is sitting right next to the app. Cheap — it's an
        # os.path.isfile on a couple of known paths.
        if self._binary_path is None:
            try:
                self.find_binary()
            except Exception:
                pass

        result = {
            "binary_found": self._binary_path is not None,
            "binary_path": self._binary_path,
            "process_running": self.is_running(),
            "pid": self._pid,
            "restart_count": self._restart_count,
            "uptime_seconds": 0,
            "api_reachable": False,
        }

        if self._last_start_time > 0 and self.is_running():
            result["uptime_seconds"] = round(time.time() - self._last_start_time)

        # Quick connectivity check
        try:
            r = requests.get(submit_url, timeout=2)
            # Splash returns 405 for GET (it only accepts POST)
            # but that means the API is reachable
            result["api_reachable"] = r.status_code in (200, 405, 404)
        except Exception:
            result["api_reachable"] = False

        return result

    def get_status(self) -> Dict:
        """Full status for the GUI."""
        health = self.check_health()
        health["last_output"] = self._last_output_lines[-10:] if self._last_output_lines else []
        health["manager_running"] = self._running
        health["metrics"] = self.get_metrics()
        return health

    def get_metrics(self) -> Dict:
        """Snapshot of the latest Splash internal metrics.

        Source: the daemon's `--listen-metrics` HTTP endpoint, polled every
        few seconds by the poller thread. Gives the GUI and bot_health a
        real window into daemon health: peer count, offers_received/broadcasted,
        and total_connections. Without this the only visible signal is
        our DB row count, which is silent when splash has peers but the
        offer-hook isn't firing.
        """
        with self._metrics_lock:
            return dict(self._metrics)

    def _poll_metrics(self) -> None:
        """Poll splash.exe's `/metrics` endpoint into `_metrics` forever.

        Exits when the managed process is no longer running. Short-circuits
        to a zeroed snapshot if the endpoint isn't reachable yet (splash
        needs a couple of seconds after Popen before binding the port).
        """
        import urllib.request
        import json as _json

        if not self._metrics_bind:
            return
        url = f"http://{self._metrics_bind}/metrics"

        while True:
            if not self.is_running():
                # Process is gone — stop polling. A fresh start spawns a
                # new poller via _launch_process().
                return

            snapshot: dict = {
                "peers": 0,
                "offers_broadcasted": 0,
                "offers_received": 0,
                "total_connections": 0,
                "last_polled_at": time.time(),
                "last_error": None,
                "metrics_url": url,
                "reachable": False,
            }
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    snapshot["peers"] = int(parsed.get("peers", 0) or 0)
                    snapshot["offers_broadcasted"] = int(
                        parsed.get("offers_broadcasted", 0) or 0)
                    snapshot["offers_received"] = int(
                        parsed.get("offers_received", 0) or 0)
                    snapshot["total_connections"] = int(
                        parsed.get("total_connections", 0) or 0)
                    snapshot["reachable"] = True
            except Exception as e:
                snapshot["last_error"] = str(e)[:120]

            with self._metrics_lock:
                # Preserve previously-observed cumulative highs in case the
                # endpoint hiccuped and returned zeros on a single poll.
                prev = self._metrics
                for k in ("offers_broadcasted", "offers_received",
                          "total_connections"):
                    if not snapshot["reachable"]:
                        snapshot[k] = int(prev.get(k, 0) or 0)
                self._metrics = snapshot

            # Sleep between polls. No tight loop on error — the same
            # interval applies so a dead endpoint doesn't hot-loop.
            time.sleep(max(1.0, float(self._metrics_poll_secs)))

    def get_recent_output(self, lines: int = 20) -> list:
        """Get recent output lines from Splash for debugging."""
        return self._last_output_lines[-lines:]

