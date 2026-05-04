"""Secure self-update helpers for the packaged CATalyst desktop app.

The updater intentionally trusts a narrow source:
GitHub's releases API for Lowestofttim/catalyst-bot, an exact Windows
installer asset name, and a matching .sha256 sidecar in the same release.
No user-provided URL is ever executed.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import unquote, urlparse

OWNER = "Lowestofttim"
REPO = "catalyst-bot"
OFFICIAL_RELEASES_API_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"

_CACHE_TTL_SECONDS = 6 * 3600
_MAX_INSTALLER_BYTES = 512 * 1024 * 1024
_HTTP_TIMEOUT = (15, 30)

_CHECK_CACHE: Dict[str, Any] = {"key": None, "at": 0.0, "data": None}
_STATUS_LOCK = threading.Lock()
_UPDATE_STATUS: Dict[str, Any] = {
    "in_progress": False,
    "phase": "idle",
    "percent": 0,
    "message": "No update running.",
    "error": "",
    "latest": None,
    "installer_name": None,
}


def _ensure_v_tag(tag: str) -> str:
    raw = str(tag or "").strip()
    if not raw:
        return ""
    return raw if raw.lower().startswith("v") else f"v{raw}"


def normalise_version(tag: str) -> str:
    return str(tag or "").strip().lstrip("vV")


def parse_semver(tag: str) -> Optional[tuple[int, int, int]]:
    version = normalise_version(tag)
    head = version.split("-", 1)[0].split("+", 1)[0]
    parts = head.split(".")
    if not parts or len(parts) > 3:
        return None
    try:
        nums = [int(p) for p in parts]
    except (TypeError, ValueError):
        return None
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def is_allowed_releases_api_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except Exception:
        return False
    path = unquote(parsed.path or "").strip("/")
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "api.github.com"
        and path.lower() == f"repos/{OWNER}/{REPO}/releases/latest".lower()
    )


def _asset_public(asset: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": str(asset.get("name") or ""),
        "url": str(asset.get("browser_download_url") or ""),
        "size": int(asset.get("size") or 0),
    }


def _asset_is_uploaded(asset: Dict[str, Any]) -> bool:
    state = str(asset.get("state") or "uploaded").lower()
    return state == "uploaded"


def _is_allowed_release_download_url(raw_url: str, tag: str, filename: str) -> bool:
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except Exception:
        return False
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return False
    parts = [unquote(p) for p in (parsed.path or "").split("/") if p]
    expected = [
        OWNER,
        REPO,
        "releases",
        "download",
        _ensure_v_tag(tag),
        filename,
    ]
    if len(parts) != len(expected):
        return False
    return (
        parts[0].lower() == expected[0].lower()
        and parts[1].lower() == expected[1].lower()
        and parts[2:5] == expected[2:5]
        and parts[5] == expected[5]
    )


def expected_windows_installer_name(tag: str) -> str:
    return f"Catalyst-Setup-{_ensure_v_tag(tag)}.exe"


def select_windows_update_assets(release: Dict[str, Any]) -> Optional[Dict[str, Dict[str, Any]]]:
    tag = _ensure_v_tag(str(release.get("tag_name") or ""))
    if not tag:
        return None

    installer_name = expected_windows_installer_name(tag)
    checksum_name = f"{installer_name}.sha256"
    assets = release.get("assets") or []
    if not isinstance(assets, list):
        return None

    installer = None
    checksum = None
    for asset in assets:
        if not isinstance(asset, dict) or not _asset_is_uploaded(asset):
            continue
        name = str(asset.get("name") or "")
        if name == installer_name:
            installer = asset
        elif name == checksum_name:
            checksum = asset

    if not installer or not checksum:
        return None

    if not _is_allowed_release_download_url(
        installer.get("browser_download_url", ""), tag, installer_name
    ):
        return None
    if not _is_allowed_release_download_url(
        checksum.get("browser_download_url", ""), tag, checksum_name
    ):
        return None

    return {
        "installer": _asset_public(installer),
        "checksum": _asset_public(checksum),
    }


def parse_sha256_checksum_text(text: str, installer_name: str) -> Optional[str]:
    """Parse sha256sum-style text and require the exact installer filename."""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tokens = line.split()
        if not tokens:
            continue
        digest = tokens[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        if len(tokens) < 2:
            continue
        filename = os.path.basename(tokens[-1].lstrip("*"))
        if filename == installer_name:
            return digest
    return None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file_sha256(path: str, expected_digest: str) -> bool:
    expected = str(expected_digest or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    try:
        return sha256_file(path).lower() == expected
    except OSError:
        return False


def fetch_latest_release(releases_url: str = "", timeout: Any = _HTTP_TIMEOUT) -> Dict[str, Any]:
    url = str(releases_url or OFFICIAL_RELEASES_API_URL).strip() or OFFICIAL_RELEASES_API_URL
    if not is_allowed_releases_api_url(url):
        raise ValueError("release source is not the official CATalyst GitHub releases API")

    import requests

    response = requests.get(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "CATalyst-Updater",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("release response was not an object")
    return data


def build_update_info(current_version: str, release: Dict[str, Any]) -> Dict[str, Any]:
    latest_tag = _ensure_v_tag(str(release.get("tag_name") or ""))
    latest = normalise_version(latest_tag)
    current = normalise_version(current_version)
    cur_sv = parse_semver(current)
    lat_sv = parse_semver(latest)
    update_available = bool(cur_sv and lat_sv and lat_sv > cur_sv)
    assets = select_windows_update_assets(release)

    result: Dict[str, Any] = {
        "success": True,
        "enabled": True,
        "current": current,
        "latest": latest or None,
        "latest_tag": latest_tag or None,
        "update_available": update_available,
        "url": str(release.get("html_url") or "") or None,
        "release_notes": str(release.get("body") or "").strip(),
        "published_at": str(release.get("published_at") or ""),
        "installer_ready": bool(assets),
        "installer_name": assets["installer"]["name"] if assets else None,
        "installer_size": assets["installer"]["size"] if assets else None,
        "checksum_name": assets["checksum"]["name"] if assets else None,
        "security": (
            "Windows auto-upgrade requires the official GitHub release, exact "
            "installer name, and matching SHA-256 sidecar before anything runs."
        ),
    }
    if assets:
        result["_assets"] = assets
    return result


def public_update_info(info: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in dict(info or {}).items() if not str(k).startswith("_")}


def get_update_info(
    current_version: str,
    releases_url: str = "",
    *,
    force: bool = False,
    ttl_seconds: int = _CACHE_TTL_SECONDS,
) -> Dict[str, Any]:
    url = str(releases_url or OFFICIAL_RELEASES_API_URL).strip() or OFFICIAL_RELEASES_API_URL
    if not is_allowed_releases_api_url(url):
        return {
            "success": True,
            "enabled": False,
            "current": normalise_version(current_version),
            "latest": None,
            "latest_tag": None,
            "update_available": False,
            "installer_ready": False,
            "url": None,
            "release_notes": "",
            "error": "release source is not the official CATalyst GitHub releases API",
            "checked_at": time.time(),
        }

    key = (normalise_version(current_version), url)
    now = time.time()
    if not force and _CHECK_CACHE.get("key") == key:
        cached_at = float(_CHECK_CACHE.get("at") or 0)
        cached = _CHECK_CACHE.get("data")
        if cached and (now - cached_at) < ttl_seconds:
            return dict(cached)

    release = fetch_latest_release(url)
    info = build_update_info(current_version, release)
    info["checked_at"] = now
    _CHECK_CACHE.update({"key": key, "at": now, "data": dict(info)})
    return info


def _set_status(**changes: Any) -> None:
    with _STATUS_LOCK:
        _UPDATE_STATUS.update(changes)


def get_update_status() -> Dict[str, Any]:
    with _STATUS_LOCK:
        return dict(_UPDATE_STATUS)


def _download_text(url: str, *, timeout: Any = _HTTP_TIMEOUT) -> str:
    import requests

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def _download_file(
    url: str,
    dest_path: Path,
    *,
    expected_size: int = 0,
    progress: Optional[Callable[[int, str], None]] = None,
) -> None:
    if expected_size and expected_size > _MAX_INSTALLER_BYTES:
        raise ValueError("installer asset is larger than the allowed update size")

    import requests

    with requests.get(url, stream=True, timeout=_HTTP_TIMEOUT) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or expected_size or 0)
        if total and total > _MAX_INSTALLER_BYTES:
            raise ValueError("download is larger than the allowed update size")
        downloaded = 0
        with open(dest_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > _MAX_INSTALLER_BYTES:
                    raise ValueError("download exceeded the allowed update size")
                fh.write(chunk)
                if progress and total > 0:
                    pct = 20 + min(60, int((downloaded / total) * 60))
                    progress(pct, f"Downloading installer ({downloaded // (1024 * 1024)} MB)...")
    if expected_size and downloaded != expected_size:
        raise ValueError("downloaded installer size did not match GitHub metadata")


def _updates_dir(tag: str) -> Path:
    from user_paths import data_dir

    root = Path(data_dir()) / "updates" / _ensure_v_tag(tag)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _launch_installer(installer_path: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("automatic installer launch is only supported on Windows builds")

    creationflags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags |= subprocess.DETACHED_PROCESS
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP

    # Schedule the installer from a detached helper so the GUI can trigger the
    # normal /api/shutdown path first. This avoids Windows Restart Manager
    # closing Catalyst.exe before the app has backed up and checkpointed SQLite.
    installer = str(installer_path)
    safe_installer = installer.replace('"', "")
    args = (
        "/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS "
        "/CATALYST_RELAUNCH=1"
    )
    command = f'timeout /t 3 /nobreak >NUL & start "" "{safe_installer}" {args}'
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", command],
        cwd=str(installer_path.parent),
        close_fds=True,
        creationflags=creationflags,
    )


def _run_update_worker(info: Dict[str, Any], launcher: Optional[Callable[[Path], None]]) -> None:
    try:
        assets = info.get("_assets") or {}
        installer = assets.get("installer") or {}
        checksum = assets.get("checksum") or {}
        latest_tag = info.get("latest_tag") or ""
        installer_name = installer.get("name") or ""
        installer_url = installer.get("url") or ""
        checksum_url = checksum.get("url") or ""

        _set_status(phase="checksum", percent=10, message="Fetching checksum...")
        checksum_text = _download_text(checksum_url)
        expected_digest = parse_sha256_checksum_text(checksum_text, installer_name)
        if not expected_digest:
            raise ValueError("release checksum sidecar did not match the installer")

        update_dir = _updates_dir(str(latest_tag))
        final_path = update_dir / installer_name
        temp_path = update_dir / f"{installer_name}.download"
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass

        _set_status(phase="download", percent=20, message="Downloading installer...")
        _download_file(
            installer_url,
            temp_path,
            expected_size=int(installer.get("size") or 0),
            progress=lambda pct, msg: _set_status(percent=pct, message=msg),
        )

        _set_status(phase="verify", percent=85, message="Verifying SHA-256 checksum...")
        if not verify_file_sha256(str(temp_path), expected_digest):
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise ValueError("downloaded installer failed SHA-256 verification")

        os.replace(temp_path, final_path)
        _set_status(phase="launch", percent=95, message="Launching verified installer...")
        (launcher or _launch_installer)(final_path)
        _set_status(
            in_progress=False,
            phase="launched",
            percent=100,
            message="Installer launched. CATalyst will close so the upgrade can finish.",
            error="",
        )
    except Exception as exc:
        _set_status(
            in_progress=False,
            phase="error",
            percent=0,
            message="Update failed.",
            error=str(exc),
        )


def start_update_install(
    current_version: str,
    releases_url: str = "",
    *,
    launcher: Optional[Callable[[Path], None]] = None,
) -> Dict[str, Any]:
    with _STATUS_LOCK:
        if _UPDATE_STATUS.get("in_progress"):
            return {"success": False, "error": "An update is already in progress."}

    info = get_update_info(current_version, releases_url, force=True)
    if not info.get("enabled", False):
        return {"success": False, "error": info.get("error") or "Update checking is disabled."}
    if not info.get("update_available", False):
        return {"success": False, "error": "No newer CATalyst release is available."}
    if not info.get("installer_ready", False) or not info.get("_assets"):
        return {
            "success": False,
            "error": "This release is missing the verified Windows installer checksum.",
        }
    if sys.platform != "win32" and launcher is None:
        return {"success": False, "error": "Automatic upgrade is only available on Windows."}

    _set_status(
        in_progress=True,
        phase="start",
        percent=1,
        message="Preparing secure update...",
        error="",
        latest=info.get("latest"),
        installer_name=info.get("installer_name"),
    )
    thread = threading.Thread(
        target=_run_update_worker,
        args=(dict(info), launcher),
        daemon=True,
        name="catalyst-update",
    )
    thread.start()
    return {"success": True, "started": True, "status": get_update_status()}
