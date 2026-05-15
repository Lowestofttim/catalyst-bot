"""Create and sign CATalyst update manifests for the public release channel."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _canonical_manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_sidecar_digest(path: Path, installer_name: str) -> str:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parts = raw_line.strip().split()
        if len(parts) < 2:
            continue
        digest = parts[0].lower().lstrip("\ufeff")
        name = os.path.basename(parts[-1].lstrip("*"))
        if name == installer_name and len(digest) == 64:
            return digest
    raise ValueError(f"{path} does not contain a sha256 line for {installer_name}")


def _version_tag(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("version is required")
    return raw if raw.lower().startswith("v") else f"v{raw}"


def build_manifest(args: argparse.Namespace) -> dict:
    installer = Path(args.installer).resolve()
    if not installer.is_file():
        raise FileNotFoundError(installer)

    tag = _version_tag(args.version)
    version = tag.lstrip("vV")
    installer_name = installer.name
    expected_name = f"Catalyst-Setup-{tag}.exe"
    if installer_name != expected_name:
        raise ValueError(f"installer must be named {expected_name}")

    digest = _sha256_file(installer)
    if args.sha256_file:
        sidecar_digest = _read_sidecar_digest(Path(args.sha256_file), installer_name)
        if sidecar_digest != digest:
            raise ValueError("installer digest does not match sha256 sidecar")

    notes = ""
    if args.release_notes_file:
        notes_path = Path(args.release_notes_file)
        if notes_path.is_file():
            notes = notes_path.read_text(encoding="utf-8").strip()
    if not notes:
        notes = "See the release page for changes."

    now = datetime.now(timezone.utc).replace(microsecond=0)
    expires_at = now + timedelta(days=int(args.expires_days))
    download_base = str(args.download_base_url).rstrip("/")
    release_url = str(args.release_url).strip()

    return {
        "schema": 1,
        "app": "CATalyst",
        "channel": "stable",
        "version": version,
        "tag": tag,
        "published_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "release_url": release_url,
        "release_notes": notes,
        "platforms": {
            "windows-x64": {
                "installer": {
                    "name": installer_name,
                    "url": f"{download_base}/{installer_name}",
                    "size": installer.stat().st_size,
                    "sha256": digest,
                }
            }
        },
    }


def sign_manifest(manifest: dict, private_key_b64: str) -> str:
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(private_key_b64)
        )
    except Exception as exc:
        raise ValueError(
            "CATALYST_UPDATE_SIGNING_KEY_B64 is not a raw base64 Ed25519 key"
        ) from exc
    return base64.b64encode(
        private_key.sign(_canonical_manifest_bytes(manifest))
    ).decode("ascii")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", required=True, help="Release tag or version, e.g. v1.2.7"
    )
    parser.add_argument(
        "--installer", required=True, help="Path to Catalyst-Setup-vX.Y.Z.exe"
    )
    parser.add_argument("--sha256-file", help="Path to installer .sha256 sidecar")
    parser.add_argument(
        "--download-base-url", required=True, help="Public release asset base URL"
    )
    parser.add_argument("--release-url", required=True, help="Public release page URL")
    parser.add_argument("--release-notes-file", help="Markdown release notes file")
    parser.add_argument("--out-manifest", default="latest.json")
    parser.add_argument("--out-signature", default="latest.json.sig")
    parser.add_argument("--expires-days", default="90")
    args = parser.parse_args(argv)

    signing_key = os.environ.get("CATALYST_UPDATE_SIGNING_KEY_B64", "").strip()
    if not signing_key:
        raise SystemExit("CATALYST_UPDATE_SIGNING_KEY_B64 is required")

    manifest = build_manifest(args)
    signature = sign_manifest(manifest, signing_key)
    Path(args.out_manifest).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    Path(args.out_signature).write_text(signature + "\n", encoding="utf-8")
    print(f"Signed update manifest for {manifest['tag']} -> {args.out_manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
