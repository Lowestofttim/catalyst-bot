"""Download the Splash P2P binary from GitHub Releases with platform detection

Picks the right asset for the current OS and architecture, downloads it
from the dexie-space/splash GitHub release, optionally verifies a SHA-256
checksum when the release ships a `.sha256` sidecar, and stores the
binary under the per-user CATalyst data directory. Exposes a non-blocking
background download with a progress callback so the GUI can show a
progress bar.

Key responsibilities:
    - Detect OS/arch and pick the matching release asset
    - Stream-download the binary with progress reporting
    - Verify SHA-256 when a checksum sidecar is available
    - Make the binary executable and report the final install path

Called from the GUI via /api/splash/setup or from splash_node.py when
SPLASH_AUTO_START is enabled and no binary is found.
"""

import os
import stat
import hashlib
import platform
import requests
import threading
from typing import Dict, Optional, Callable

from database import log_event
from user_paths import data_dir
from win_subprocess import hidden_subprocess_kwargs


# GitHub release info
GITHUB_REPO = "dexie-space/splash"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# Binary naming convention from Dexie's releases:
#   Windows:  splash-amd64.exe
#   macOS:    splash-darwin-amd64  /  splash-darwin-arm64
#   Linux:    splash-linux-amd64   /  splash-linux-arm64
#   FreeBSD:  splash-freebsd-amd64

# Where to save the binary. Keep downloaded/runtime binaries in the per-user
# writable data dir so source checkouts and packaged install dirs stay clean.
INSTALL_DIR = os.path.join(data_dir(), "splash")


def detect_platform() -> Dict:
    """Detect OS and architecture for correct binary selection.

    Returns: {os: str, arch: str, binary_name: str, asset_name: str}
    """
    system = platform.system().lower()   # 'windows', 'darwin', 'linux'
    machine = platform.machine().lower()  # 'x86_64', 'amd64', 'arm64', 'aarch64'

    # Normalize architecture
    if machine in ("x86_64", "amd64", "x64"):
        arch = "amd64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = machine  # Best guess

    # Build asset name based on Dexie's naming convention
    if system == "windows":
        asset_name = f"splash-{arch}.exe"
        binary_name = "splash.exe"
    elif system == "darwin":
        asset_name = f"splash-darwin-{arch}"
        binary_name = "splash"
    elif system == "linux":
        asset_name = f"splash-linux-{arch}"
        binary_name = "splash"
    else:
        asset_name = f"splash-{system}-{arch}"
        binary_name = "splash"

    return {
        "os": system,
        "arch": arch,
        "binary_name": binary_name,
        "asset_name": asset_name,
        "install_path": os.path.join(INSTALL_DIR, binary_name),
    }


def check_installed() -> Dict:
    """Check if Splash is already installed.

    Returns: {installed: bool, path: str, version: str|None}
    """
    info = detect_platform()
    path = info["install_path"]

    result = {
        "installed": os.path.isfile(path),
        "path": path,
        "version": None,
        "platform": info,
    }

    if result["installed"]:
        # Try to get version
        try:
            import subprocess
            proc = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, timeout=5,
                **hidden_subprocess_kwargs(),
            )
            if proc.returncode == 0:
                result["version"] = proc.stdout.strip()
        except Exception:
            pass

    return result


def get_latest_release() -> Optional[Dict]:
    """Fetch the latest release info from GitHub.

    Returns: {tag: str, assets: [{name, size, url}, ...]}
    or None on failure.
    """
    try:
        try:
            from api_call_tracker import record as _t
            _t("github", f"/repos/{GITHUB_REPO}/releases/latest")
        except Exception:
            pass
        r = requests.get(
            GITHUB_API_URL,
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10
        )

        if r.status_code != 200:
            log_event("warning", "splash_setup",
                      f"GitHub API returned {r.status_code}")
            return None

        data = r.json()
        assets = []
        for a in data.get("assets", []):
            assets.append({
                "name": a["name"],
                "size": a["size"],
                "url": a["browser_download_url"],
            })

        return {
            "tag": data.get("tag_name", "unknown"),
            "name": data.get("name", ""),
            "assets": assets,
        }

    except Exception as e:
        log_event("warning", "splash_setup", f"Failed to fetch releases: {e}")
        return None


def download_splash(progress_callback: Callable = None) -> Dict:
    """Download and install the correct Splash binary.

    Args:
        progress_callback: Optional function(pct, message) for GUI updates

    Returns: {success: bool, message: str, path: str}
    """
    def _progress(pct, msg):
        if progress_callback:
            try:
                progress_callback(pct, msg)
            except Exception:
                pass

    _progress(5, "Detecting platform...")
    info = detect_platform()

    _progress(10, f"Platform: {info['os']} {info['arch']}")
    log_event("info", "splash_setup",
              f"Downloading Splash for {info['os']}/{info['arch']}...")

    # Fetch latest release
    _progress(15, "Checking latest release...")
    release = get_latest_release()

    if not release:
        return {
            "success": False,
            "message": "Could not reach GitHub. Check your internet connection.",
            "path": "",
        }

    _progress(20, f"Found release: {release['tag']}")

    # Find the right asset
    target_asset = info["asset_name"]
    download_url = None
    download_size = 0
    sha256_url = None

    for asset in release["assets"]:
        if asset["name"] == target_asset:
            download_url = asset["url"]
            download_size = asset["size"]
        elif asset["name"] == target_asset + ".sha256":
            sha256_url = asset["url"]

    if not download_url:
        # List available assets for troubleshooting
        available = [a["name"] for a in release["assets"]]
        msg = (f"No binary found for {info['os']}/{info['arch']}. "
               f"Looked for '{target_asset}'. "
               f"Available: {', '.join(available)}")
        log_event("warning", "splash_setup", msg)
        return {"success": False, "message": msg, "path": ""}

    allow_unverified = os.environ.get(
        "CATALYST_ALLOW_UNVERIFIED_SPLASH_DOWNLOAD", ""
    ).strip() == "1"
    if not sha256_url and not allow_unverified:
        msg = (
            f"No SHA256 checksum found for '{target_asset}'. "
            "Refusing to install an unverified Splash binary."
        )
        log_event("error", "splash_setup", msg)
        return {"success": False, "message": msg, "path": ""}

    # Download the binary
    _progress(25, f"Downloading {target_asset} ({download_size / 1024 / 1024:.1f} MB)...")
    install_path = info["install_path"]
    os.makedirs(os.path.dirname(install_path), exist_ok=True)

    try:
        # (connect, read) — a 30s stall on the body is enough to declare the
        # download dead.  Without a read timeout a stalled connection can
        # hang the download thread indefinitely.
        r = requests.get(download_url, stream=True, timeout=(15, 30))
        r.raise_for_status()

        total = int(r.headers.get("content-length", download_size))
        downloaded = 0

        with open(install_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = 25 + int((downloaded / total) * 60)  # 25% → 85%
                    _progress(pct, f"Downloading... {downloaded / 1024 / 1024:.1f} MB")

    except Exception as e:
        msg = f"Download failed: {e}"
        log_event("error", "splash_setup", msg)
        # Clean up partial download
        if os.path.exists(install_path):
            try:
                os.remove(install_path)
            except Exception:
                pass
        return {"success": False, "message": msg, "path": ""}

    _progress(85, "Verifying download...")

    # Verify SHA256 if available
    if sha256_url:
        try:
            sha_r = requests.get(sha256_url, timeout=10)
            expected_hash = sha_r.text.strip().split()[0].lower()

            with open(install_path, "rb") as f:
                actual_hash = hashlib.sha256(f.read()).hexdigest().lower()

            if actual_hash != expected_hash:
                msg = (f"SHA256 mismatch! Expected: {expected_hash[:16]}... "
                       f"Got: {actual_hash[:16]}... — download may be corrupted.")
                log_event("error", "splash_setup", msg)
                os.remove(install_path)
                return {"success": False, "message": msg, "path": ""}

            _progress(90, "SHA256 verified OK")
            log_event("info", "splash_setup", "SHA256 checksum verified")

        except Exception as e:
            # Checksum verification failed — refuse to use unverified binary
            msg = f"SHA256 verification failed: {e} — refusing to install unverified binary"
            log_event("error", "splash_setup", msg)
            try:
                os.remove(install_path)
            except Exception:
                pass
            return {"success": False, "message": msg, "path": ""}

    # Make executable on Unix
    if info["os"] != "windows":
        try:
            st = os.stat(install_path)
            os.chmod(install_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            _progress(92, "Set executable permissions")
        except Exception as e:
            log_event("debug", "splash_setup",
                      f"chmod failed (may need manual fix): {e}")

    # Verify the binary runs
    _progress(95, "Testing binary...")
    try:
        import subprocess
        proc = subprocess.run(
            [install_path, "--version"],
            capture_output=True, text=True, timeout=10,
            **hidden_subprocess_kwargs(),
        )
        version = proc.stdout.strip() if proc.returncode == 0 else "unknown"
        _progress(98, f"Splash {version} ready!")
    except Exception as e:
        version = "unknown"
        log_event("debug", "splash_setup", f"Version check failed: {e}")

    _progress(100, "Installation complete!")

    file_size_mb = os.path.getsize(install_path) / 1024 / 1024
    msg = (f"Splash {release['tag']} installed successfully! "
           f"({file_size_mb:.1f} MB, {info['os']}/{info['arch']})")
    log_event("info", "splash_setup", msg)

    return {
        "success": True,
        "message": msg,
        "path": install_path,
        "version": version,
        "release_tag": release["tag"],
    }


# ---------------------------------------------------------------------------
# Background download (for non-blocking GUI usage)
# ---------------------------------------------------------------------------

_download_status = {
    "in_progress": False,
    "percent": 0,
    "message": "",
    "result": None,
}
_download_lock = threading.Lock()


def start_background_download():
    """Start Splash download in a background thread. Non-blocking."""
    with _download_lock:
        if _download_status["in_progress"]:
            return {"error": "Download already in progress"}
        _download_status["in_progress"] = True
        _download_status["percent"] = 0
        _download_status["message"] = "Starting..."
        _download_status["result"] = None

    def _bg_download():
        def _progress(pct, msg):
            with _download_lock:
                _download_status["percent"] = pct
                _download_status["message"] = msg

        result = download_splash(progress_callback=_progress)

        with _download_lock:
            _download_status["in_progress"] = False
            _download_status["result"] = result

    t = threading.Thread(target=_bg_download, daemon=True, name="splash-download")
    t.start()
    return {"started": True}


def get_download_status() -> Dict:
    """Get current download progress."""
    with _download_lock:
        return dict(_download_status)

