"""Secure self-update helpers for the packaged CATalyst desktop app.

The updater trusts a public release channel, not the private source repo:
a signed manifest, a pinned Ed25519 public key, an exact Windows installer
asset name, and a SHA-256 digest inside the signed metadata.
No user-provided URL is ever executed.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import unquote, urlparse, urlunparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

OWNER = "Lowestofttim"
RELEASE_CHANNEL_REPO = "catalyst-releases"
OFFICIAL_MANIFEST_URL = f"https://github.com/{OWNER}/{RELEASE_CHANNEL_REPO}/releases/latest/download/latest.json"
OFFICIAL_MANIFEST_SIG_URL = f"{OFFICIAL_MANIFEST_URL}.sig"

# Raw Ed25519 public key, base64-encoded. The matching private key belongs only
# in the release workflow secret CATALYST_UPDATE_SIGNING_KEY_B64.
UPDATE_MANIFEST_PUBLIC_KEY_B64 = "cyjvFTb0quqOQdl2c0TMwzwF8PBb74qwGztqxLSazBQ="
MANIFEST_SCHEMA_VERSION = 1
WINDOWS_PLATFORM_KEY = "windows-x64"

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
_RELAUNCH_INTENT_FILE = "update_relaunch_intent.json"
_RELAUNCH_INTENT_MAX_AGE_SECONDS = 6 * 3600


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


def _parse_iso_utc(value: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("timestamp is missing")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canonical_manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    """Return deterministic bytes for signing/verifying update metadata."""
    clean = dict(manifest or {})
    clean.pop("signature", None)
    return json.dumps(
        clean,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def is_allowed_manifest_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except Exception:
        return False
    path = unquote(parsed.path or "").strip("/")
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "github.com"
        and path
        == f"{OWNER}/{RELEASE_CHANNEL_REPO}/releases/latest/download/latest.json"
    )


def _replace_channel_asset_filename(
    raw_url: str,
    *,
    current_filename: str,
    target_filename: str,
) -> str:
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except Exception:
        return ""
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        return ""
    parts = [unquote(p) for p in (parsed.path or "").split("/") if p]
    if len(parts) != 6:
        return ""
    if not (
        parts[0].lower() == OWNER.lower()
        and parts[1] == RELEASE_CHANNEL_REPO
        and parts[2] == "releases"
        and parts[3] == "download"
        and parts[5] == current_filename
    ):
        return ""
    path = "/" + "/".join(parts[:5] + [target_filename])
    return urlunparse(("https", "github.com", path, "", "", ""))


def _signature_url_for_manifest(
    manifest_url: str, manifest_response: Any = None
) -> str:
    if manifest_response is not None:
        candidates = [
            manifest_response,
            *reversed(getattr(manifest_response, "history", []) or []),
        ]
        for response in candidates:
            resolved = _replace_channel_asset_filename(
                getattr(response, "url", ""),
                current_filename="latest.json",
                target_filename="latest.json.sig",
            )
            if resolved:
                return resolved
    return f"{str(manifest_url or OFFICIAL_MANIFEST_URL).strip()}.sig"


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
        RELEASE_CHANNEL_REPO,
        "releases",
        "download",
        _ensure_v_tag(tag),
        filename,
    ]
    if len(parts) != len(expected):
        return False
    return (
        parts[0].lower() == expected[0].lower()
        and parts[1] == expected[1]
        and parts[2:5] == expected[2:5]
        and parts[5] == expected[5]
    )


def _normalise_manifest(
    manifest: Dict[str, Any], *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("update manifest was not an object")
    if manifest.get("schema") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("update manifest schema is unsupported")
    if manifest.get("app") != "CATalyst":
        raise ValueError("update manifest is for a different app")

    version = normalise_version(str(manifest.get("version") or ""))
    tag = _ensure_v_tag(str(manifest.get("tag") or version))
    if not version or not parse_semver(version) or tag != _ensure_v_tag(version):
        raise ValueError("update manifest version/tag mismatch")

    expires_at = _parse_iso_utc(str(manifest.get("expires_at") or ""))
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at <= now_utc:
        raise ValueError("update manifest has expired")

    return dict(manifest)


def verify_signed_manifest(
    manifest: Dict[str, Any],
    signature_b64: str,
    *,
    public_key_b64: str = UPDATE_MANIFEST_PUBLIC_KEY_B64,
    public_b64: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Verify the manifest signature and return validated metadata."""
    if public_b64:
        public_key_b64 = public_b64
    try:
        import base64

        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_b64)
        )
        signature = base64.b64decode(str(signature_b64 or "").strip())
    except Exception as exc:
        raise ValueError("update manifest signature material is invalid") from exc

    try:
        public_key.verify(signature, canonical_manifest_bytes(manifest))
    except InvalidSignature as exc:
        raise ValueError("update manifest signature is invalid") from exc

    return _normalise_manifest(manifest, now=now)


def expected_windows_installer_name(tag: str) -> str:
    return f"Catalyst-Setup-{_ensure_v_tag(tag)}.exe"


def select_windows_manifest_installer(
    manifest: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    tag = _ensure_v_tag(str(manifest.get("tag") or manifest.get("version") or ""))
    installer_name = expected_windows_installer_name(tag)
    try:
        installer = (
            manifest.get("platforms", {})
            .get(WINDOWS_PLATFORM_KEY, {})
            .get("installer", {})
        )
    except AttributeError:
        return None

    name = str(installer.get("name") or "")
    url = str(installer.get("url") or "")
    digest = str(installer.get("sha256") or "").strip().lower()
    try:
        size = int(installer.get("size") or 0)
    except (TypeError, ValueError):
        size = 0

    if name != installer_name:
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    if size <= 0 or size > _MAX_INSTALLER_BYTES:
        return None
    if not _is_allowed_release_download_url(url, tag, installer_name):
        return None

    return {
        "name": name,
        "url": url,
        "size": size,
        "sha256": digest,
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


def fetch_signed_manifest(
    manifest_url: str = "",
    *,
    timeout: Any = _HTTP_TIMEOUT,
    public_key_b64: str = UPDATE_MANIFEST_PUBLIC_KEY_B64,
) -> Dict[str, Any]:
    url = str(manifest_url or OFFICIAL_MANIFEST_URL).strip() or OFFICIAL_MANIFEST_URL
    if not is_allowed_manifest_url(url):
        raise ValueError(
            "update manifest source is not the official CATalyst release channel"
        )

    import requests

    manifest_response = requests.get(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "CATalyst-Updater",
        },
        timeout=timeout,
    )
    manifest_response.raise_for_status()
    manifest = manifest_response.json()

    signature_response = requests.get(
        _signature_url_for_manifest(url, manifest_response),
        headers={
            "Accept": "text/plain",
            "User-Agent": "CATalyst-Updater",
        },
        timeout=timeout,
    )
    signature_response.raise_for_status()
    return verify_signed_manifest(
        manifest,
        signature_response.text.strip(),
        public_key_b64=public_key_b64,
    )


def build_update_info_from_manifest(
    current_version: str, manifest: Dict[str, Any]
) -> Dict[str, Any]:
    latest_tag = _ensure_v_tag(
        str(manifest.get("tag") or manifest.get("version") or "")
    )
    latest = normalise_version(latest_tag)
    current = normalise_version(current_version)
    cur_sv = parse_semver(current)
    lat_sv = parse_semver(latest)
    update_available = bool(cur_sv and lat_sv and lat_sv > cur_sv)
    installer = select_windows_manifest_installer(manifest)

    result: Dict[str, Any] = {
        "success": True,
        "enabled": True,
        "manifest_verified": True,
        "current": current,
        "latest": latest or None,
        "latest_tag": latest_tag or None,
        "update_available": update_available,
        "url": str(manifest.get("release_url") or "") or None,
        "release_notes": str(manifest.get("release_notes") or "").strip(),
        "published_at": str(manifest.get("published_at") or ""),
        "manifest_expires_at": str(manifest.get("expires_at") or ""),
        "channel": str(manifest.get("channel") or "stable"),
        "installer_ready": bool(installer),
        "installer_name": installer["name"] if installer else None,
        "installer_size": installer["size"] if installer else None,
        "checksum_name": None,
        "security": (
            "Windows auto-upgrade requires the official public release channel, "
            "a valid signed manifest, exact installer name, and matching SHA-256 "
            "digest before anything runs."
        ),
    }
    if installer:
        result["_assets"] = {"installer": installer}
    return result


def public_update_info(info: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in dict(info or {}).items() if not str(k).startswith("_")}


def get_update_info(
    current_version: str,
    manifest_url: str = "",
    *,
    force: bool = False,
    ttl_seconds: int = _CACHE_TTL_SECONDS,
) -> Dict[str, Any]:
    url = str(manifest_url or OFFICIAL_MANIFEST_URL).strip() or OFFICIAL_MANIFEST_URL
    if not is_allowed_manifest_url(url):
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
            "manifest_verified": False,
            "error": "update manifest source is not the official CATalyst release channel",
            "checked_at": time.time(),
        }

    key = (normalise_version(current_version), url)
    now = time.time()
    if not force and _CHECK_CACHE.get("key") == key:
        cached_at = float(_CHECK_CACHE.get("at") or 0)
        cached = _CHECK_CACHE.get("data")
        if cached and (now - cached_at) < ttl_seconds:
            return dict(cached)

    manifest = fetch_signed_manifest(url)
    info = build_update_info_from_manifest(current_version, manifest)
    info["checked_at"] = now
    _CHECK_CACHE.update({"key": key, "at": now, "data": dict(info)})
    return info


def _set_status(**changes: Any) -> None:
    with _STATUS_LOCK:
        _UPDATE_STATUS.update(changes)


def get_update_status() -> Dict[str, Any]:
    with _STATUS_LOCK:
        return dict(_UPDATE_STATUS)


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
                    progress(
                        pct,
                        f"Downloading installer ({downloaded // (1024 * 1024)} MB)...",
                    )
    if expected_size and downloaded != expected_size:
        raise ValueError("downloaded installer size did not match GitHub metadata")


def _updates_dir(tag: str) -> Path:
    from user_paths import data_dir

    root = Path(data_dir()) / "updates" / _ensure_v_tag(tag)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _relaunch_intent_path() -> Path:
    from user_paths import data_dir

    root = Path(data_dir()) / "updates"
    root.mkdir(parents=True, exist_ok=True)
    return root / _RELAUNCH_INTENT_FILE


def write_update_relaunch_intent(intent: Dict[str, Any]) -> None:
    clean = {
        "source": "in_app_update",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "created_at_ts": time.time(),
        "auto_start_bot": bool((intent or {}).get("auto_start_bot")),
        "resume_existing_offers": bool(
            (intent or {}).get("resume_existing_offers", True)
        ),
        "cancel_offers": bool((intent or {}).get("cancel_offers", False)),
        "source_version": normalise_version(
            str((intent or {}).get("source_version") or "")
        ),
        "target_version": normalise_version(
            str((intent or {}).get("target_version") or "")
        ),
        "latest_tag": _ensure_v_tag(str((intent or {}).get("latest_tag") or "")),
    }
    path = _relaunch_intent_path()
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(clean, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, path)


def clear_update_relaunch_intent() -> None:
    try:
        _relaunch_intent_path().unlink(missing_ok=True)
    except OSError:
        # Best-effort cleanup; a stale intent is ignored once it expires.
        pass


def get_update_relaunch_intent(
    *,
    max_age_seconds: int = _RELAUNCH_INTENT_MAX_AGE_SECONDS,
) -> Dict[str, Any]:
    try:
        path = _relaunch_intent_path()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if not isinstance(raw, dict):
        clear_update_relaunch_intent()
        return {}

    created_at_ts = float(raw.get("created_at_ts") or 0)
    if created_at_ts and (time.time() - created_at_ts) > max_age_seconds:
        clear_update_relaunch_intent()
        return {}

    return {
        "source": str(raw.get("source") or "in_app_update"),
        "created_at": str(raw.get("created_at") or ""),
        "auto_start_bot": bool(raw.get("auto_start_bot")),
        "resume_existing_offers": bool(raw.get("resume_existing_offers", True)),
        "cancel_offers": bool(raw.get("cancel_offers", False)),
        "source_version": normalise_version(str(raw.get("source_version") or "")),
        "target_version": normalise_version(str(raw.get("target_version") or "")),
        "latest_tag": _ensure_v_tag(str(raw.get("latest_tag") or "")),
    }


def _safe_cmd_value(value: Any) -> str:
    return (
        str(value or "")
        .replace('"', "")
        .replace("\r", "")
        .replace("\n", "")
        .replace("%", "%%")
    )


def _launch_installer(installer_path: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError(
            "automatic installer launch is only supported on Windows builds"
        )

    creationflags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags |= subprocess.DETACHED_PROCESS
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP

    # The helper waits until the old process has fully exited before running
    # the installer, then relaunches the updated exe after Inno Setup returns.
    # This avoids the new app immediately exiting on the single-instance lock.
    app_pid = int(os.getpid())
    helper_path = installer_path.parent / f"catalyst-update-{app_pid}.cmd"
    safe_installer = _safe_cmd_value(installer_path)
    safe_app_exe = _safe_cmd_value(sys.executable)
    safe_app_dir = _safe_cmd_value(ntpath.dirname(ntpath.abspath(sys.executable)))
    helper_script = f"""@echo off
setlocal
set "INSTALLER={safe_installer}"
set "APP_EXE={safe_app_exe}"
set "APP_DIR={safe_app_dir}"
for /l %%I in (1,1,120) do (
    tasklist /FI "PID eq {app_pid}" 2>NUL | findstr /C:"{app_pid}" >NUL
    if errorlevel 1 goto app_gone
    timeout /t 1 /nobreak >NUL
)
set "INSTALL_EXIT=1"
goto finish
:app_gone
start /wait "" "%INSTALLER%" /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /CATALYST_RELAUNCH=0 /DIR="%APP_DIR%"
set "INSTALL_EXIT=%ERRORLEVEL%"
if "%INSTALL_EXIT%"=="0" goto relaunch
if "%INSTALL_EXIT%"=="3010" goto relaunch
goto finish
:relaunch
timeout /t 2 /nobreak >NUL
start "" "%APP_EXE%"
:finish
del "%~f0" >NUL 2>NUL
exit /b %INSTALL_EXIT%
"""
    helper_path.write_text(helper_script, encoding="utf-8")
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(helper_path)],
        cwd=str(installer_path.parent),
        close_fds=True,
        creationflags=creationflags,
    )


def _run_update_worker(
    info: Dict[str, Any], launcher: Optional[Callable[[Path], None]]
) -> None:
    try:
        assets = info.get("_assets") or {}
        installer = assets.get("installer") or {}
        latest_tag = info.get("latest_tag") or ""
        installer_name = installer.get("name") or ""
        installer_url = installer.get("url") or ""
        expected_digest = str(installer.get("sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise ValueError(
                "signed update manifest did not include a valid installer digest"
            )

        update_dir = _updates_dir(str(latest_tag))
        final_path = update_dir / installer_name
        temp_path = update_dir / f"{installer_name}.download"
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass

        _set_status(
            phase="download",
            percent=20,
            message="Downloading installer from verified release manifest...",
        )
        _download_file(
            installer_url,
            temp_path,
            expected_size=int(installer.get("size") or 0),
            progress=lambda pct, msg: _set_status(percent=pct, message=msg),
        )

        _set_status(
            phase="verify", percent=85, message="Verifying installer SHA-256..."
        )
        if not verify_file_sha256(str(temp_path), expected_digest):
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise ValueError("downloaded installer failed SHA-256 verification")

        os.replace(temp_path, final_path)
        _set_status(
            phase="launch", percent=95, message="Launching verified installer..."
        )
        (launcher or _launch_installer)(final_path)
        _set_status(
            in_progress=False,
            phase="launched",
            percent=100,
            message="Installer launched. CATalyst will close so the upgrade can finish.",
            error="",
        )
    except Exception as exc:
        clear_update_relaunch_intent()
        _set_status(
            in_progress=False,
            phase="error",
            percent=0,
            message="Update failed.",
            error=str(exc),
        )


def start_update_install(
    current_version: str,
    manifest_url: str = "",
    *,
    launcher: Optional[Callable[[Path], None]] = None,
    relaunch_intent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with _STATUS_LOCK:
        if _UPDATE_STATUS.get("in_progress"):
            return {"success": False, "error": "An update is already in progress."}

    info = get_update_info(current_version, manifest_url, force=True)
    if not info.get("enabled", False):
        return {
            "success": False,
            "error": info.get("error") or "Update checking is disabled.",
        }
    if not info.get("update_available", False):
        return {"success": False, "error": "No newer CATalyst release is available."}
    if not info.get("installer_ready", False) or not info.get("_assets"):
        return {
            "success": False,
            "error": "The signed update manifest is missing a verified Windows installer.",
        }
    if sys.platform != "win32" and launcher is None:
        return {
            "success": False,
            "error": "Automatic upgrade is only available on Windows.",
        }

    if relaunch_intent is not None:
        write_update_relaunch_intent(
            {
                **dict(relaunch_intent or {}),
                "source_version": current_version,
                "target_version": info.get("latest"),
                "latest_tag": info.get("latest_tag"),
            }
        )

    _set_status(
        in_progress=True,
        phase="start",
        percent=1,
        message="Preparing secure update from signed manifest...",
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
