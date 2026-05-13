import base64
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]


class TestAppUpdateSecurity(unittest.TestCase):
    def _keypair(self):
        private = Ed25519PrivateKey.generate()
        public_b64 = base64.b64encode(
            private.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        ).decode("ascii")
        return private, public_b64

    def _manifest(self, *, version="1.2.6", expires_delta_days=14, installer_url=None):
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_delta_days)
        installer_name = f"Catalyst-Setup-v{version}.exe"
        return {
            "schema": 1,
            "app": "CATalyst",
            "channel": "stable",
            "version": version,
            "tag": f"v{version}",
            "published_at": "2026-05-04T10:00:00Z",
            "expires_at": expires_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "release_url": f"https://github.com/Lowestofttim/catalyst-releases/releases/tag/v{version}",
            "release_notes": "Signed manifest update.",
            "platforms": {
                "windows-x64": {
                    "installer": {
                        "name": installer_name,
                        "url": installer_url or (
                            "https://github.com/Lowestofttim/catalyst-releases/releases/download/"
                            f"v{version}/{installer_name}"
                        ),
                        "size": 456,
                        "sha256": "a" * 64,
                    }
                }
            },
        }

    def _signature(self, private, manifest):
        from app_update import canonical_manifest_bytes

        return base64.b64encode(private.sign(canonical_manifest_bytes(manifest))).decode("ascii")

    def test_official_manifest_url_is_allowed(self):
        from app_update import OFFICIAL_MANIFEST_URL, is_allowed_manifest_url

        self.assertTrue(is_allowed_manifest_url(OFFICIAL_MANIFEST_URL))
        self.assertFalse(is_allowed_manifest_url("https://example.invalid/latest.json"))
        self.assertFalse(
            is_allowed_manifest_url(
                "https://api.github.com/repos/Lowestofttim/catalyst-bot/releases/latest"
            )
        )

    def test_signed_manifest_builds_verified_update_info(self):
        from app_update import build_update_info_from_manifest, verify_signed_manifest

        private, public_b64 = self._keypair()
        manifest = self._manifest(version="1.2.6")
        signature = self._signature(private, manifest)

        verified = verify_signed_manifest(manifest, signature, public_b64=public_b64)
        info = build_update_info_from_manifest("1.2.5", verified)

        self.assertTrue(info["success"])
        self.assertTrue(info["manifest_verified"])
        self.assertTrue(info["update_available"])
        self.assertTrue(info["installer_ready"])
        self.assertEqual(info["latest"], "1.2.6")
        self.assertEqual(info["installer_name"], "Catalyst-Setup-v1.2.6.exe")
        self.assertEqual(info["_assets"]["installer"]["sha256"], "a" * 64)

    def test_fetch_signed_manifest_uses_resolved_release_for_signature(self):
        from app_update import OFFICIAL_MANIFEST_URL, fetch_signed_manifest

        class FakeResponse:
            def __init__(self, *, url, payload=None, text="", history=None):
                self.url = url
                self._payload = payload
                self.text = text
                self.history = history or []

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        private, public_b64 = self._keypair()
        manifest = self._manifest(version="1.2.6")
        signature = self._signature(private, manifest)
        resolved_manifest_url = (
            "https://github.com/Lowestofttim/catalyst-releases/releases/download/"
            "v1.2.6/latest.json"
        )
        expected_sig_url = (
            "https://github.com/Lowestofttim/catalyst-releases/releases/download/"
            "v1.2.6/latest.json.sig"
        )
        calls = []

        def fake_get(url, **_kwargs):
            calls.append(url)
            if url == OFFICIAL_MANIFEST_URL:
                return FakeResponse(
                    url="https://release-assets.githubusercontent.com/signed-download",
                    payload=manifest,
                    history=[FakeResponse(url=resolved_manifest_url)],
                )
            if url == expected_sig_url:
                return FakeResponse(url=url, text=signature)
            raise AssertionError(f"unexpected URL: {url}")

        with patch("requests.get", side_effect=fake_get):
            verified = fetch_signed_manifest(OFFICIAL_MANIFEST_URL, public_key_b64=public_b64)

        self.assertEqual(verified["version"], "1.2.6")
        self.assertEqual(calls, [OFFICIAL_MANIFEST_URL, expected_sig_url])

    def test_signed_manifest_rejects_tampering(self):
        from app_update import verify_signed_manifest

        private, public_b64 = self._keypair()
        manifest = self._manifest(version="1.2.6")
        signature = self._signature(private, manifest)
        manifest["version"] = "1.2.7"

        with self.assertRaises(ValueError):
            verify_signed_manifest(manifest, signature, public_b64=public_b64)

    def test_signed_manifest_rejects_expired_metadata(self):
        from app_update import verify_signed_manifest

        private, public_b64 = self._keypair()
        manifest = self._manifest(version="1.2.6", expires_delta_days=-1)
        signature = self._signature(private, manifest)

        with self.assertRaises(ValueError):
            verify_signed_manifest(manifest, signature, public_b64=public_b64)

    def test_manifest_rejects_download_url_outside_release_channel(self):
        from app_update import build_update_info_from_manifest, verify_signed_manifest

        private, public_b64 = self._keypair()
        manifest = self._manifest(
            version="1.2.6",
            installer_url="https://example.invalid/Catalyst-Setup-v1.2.6.exe",
        )
        signature = self._signature(private, manifest)

        verified = verify_signed_manifest(manifest, signature, public_b64=public_b64)
        info = build_update_info_from_manifest("1.2.5", verified)

        self.assertFalse(info["installer_ready"])

    def test_parse_checksum_accepts_matching_filename_only(self):
        from app_update import parse_sha256_checksum_text

        digest = "a" * 64
        self.assertIsNone(
            parse_sha256_checksum_text(
                f"{digest}\n",
                "Catalyst-Setup-v1.2.6.exe",
            )
        )
        self.assertEqual(
            parse_sha256_checksum_text(
                f"{digest}  Catalyst-Setup-v1.2.6.exe\n",
                "Catalyst-Setup-v1.2.6.exe",
            ),
            digest,
        )
        self.assertIsNone(
            parse_sha256_checksum_text(
                f"{digest}  Other.exe\n",
                "Catalyst-Setup-v1.2.6.exe",
            )
        )

    def test_verify_file_sha256_requires_exact_digest(self):
        from app_update import verify_file_sha256

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "installer.exe"
            path.write_bytes(b"safe bytes")
            digest = hashlib.sha256(b"safe bytes").hexdigest()

            self.assertTrue(verify_file_sha256(str(path), digest))
            self.assertFalse(verify_file_sha256(str(path), "0" * 64))


class TestAppUpdateApi(unittest.TestCase):
    def setUp(self):
        import api_server

        self.api_server = api_server
        self.api_server.app.testing = True
        self.client = self.api_server.app.test_client()
        self.auth = {"X-Bot-Local-Token": self.api_server._LOCAL_API_TOKEN}
        self.loopback = {"REMOTE_ADDR": "127.0.0.1"}

    def test_update_install_rejects_running_bot(self):
        class RunningBot:
            def is_running(self):
                return True

        with patch.object(self.api_server, "bot", RunningBot()):
            resp = self.client.post(
                "/api/update/install",
                headers=self.auth,
                environ_base=self.loopback,
            )

        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertFalse(body["success"])
        self.assertIn("Stop the bot", body["error"])

    def test_check_update_includes_release_notes_and_installer_readiness(self):
        with patch.object(self.api_server, "get_app_version", return_value="1.2.5"), \
                patch("app_update.fetch_signed_manifest") as fetch_manifest:
            fetch_manifest.return_value = {
                "schema": 1,
                "app": "CATalyst",
                "channel": "stable",
                "version": "1.2.6",
                "tag": "v1.2.6",
                "published_at": "2026-05-04T10:00:00Z",
                "expires_at": "2026-06-01T00:00:00Z",
                "release_url": "https://github.com/Lowestofttim/catalyst-releases/releases/tag/v1.2.6",
                "release_notes": "Fixed Sage startup.\nAdded secure updater.",
                "platforms": {
                    "windows-x64": {
                        "installer": {
                            "name": "Catalyst-Setup-v1.2.6.exe",
                            "url": (
                                "https://github.com/Lowestofttim/catalyst-releases/releases/download/"
                                "v1.2.6/Catalyst-Setup-v1.2.6.exe"
                            ),
                            "size": 456,
                            "sha256": "a" * 64,
                        }
                    }
                },
            }
            resp = self.client.get("/api/check-update", environ_base=self.loopback)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertTrue(body["manifest_verified"])
        self.assertTrue(body["update_available"])
        self.assertTrue(body["installer_ready"])
        self.assertEqual(body["latest"], "1.2.6")
        self.assertIn("Fixed Sage startup", body["release_notes"])

    def test_check_update_force_query_bypasses_cache(self):
        update_info = {
            "success": True,
            "enabled": True,
            "current": "1.2.5",
            "latest": "1.2.6",
            "latest_tag": "v1.2.6",
            "update_available": True,
            "installer_ready": True,
            "manifest_verified": True,
            "url": "https://github.com/Lowestofttim/catalyst-releases/releases/tag/v1.2.6",
            "release_notes": "New release.",
        }
        with patch.object(self.api_server, "get_app_version", return_value="1.2.5"), \
                patch("app_update.get_update_info", return_value=update_info) as get_update_info:
            resp = self.client.get("/api/check-update?force=1", environ_base=self.loopback)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["update_available"])
        self.assertTrue(get_update_info.call_args.kwargs["force"])


class TestAppUpdateBridge(unittest.TestCase):
    def test_desktop_bridge_check_update_uses_loopback_and_forwards_force(self):
        import api_server
        from app_bridge import AppBridge

        update_info = {
            "success": True,
            "enabled": True,
            "current": "1.2.26",
            "latest": "1.2.27",
            "latest_tag": "v1.2.27",
            "update_available": True,
            "installer_ready": True,
            "manifest_verified": True,
            "url": "https://github.com/Lowestofttim/catalyst-releases/releases/tag/v1.2.27",
            "release_notes": "Maintenance update.",
        }

        with patch.object(api_server, "get_app_version", return_value="1.2.26"), \
                patch("app_update.get_update_info", return_value=update_info) as get_update_info:
            result = AppBridge().check_update({"force": "1"})

        self.assertTrue(result["success"])
        self.assertTrue(result["update_available"])
        self.assertTrue(get_update_info.call_args.kwargs["force"])


class TestAppUpdateFrontendAndReleaseWorkflow(unittest.TestCase):
    def test_gui_has_upgrade_modal_and_install_call(self):
        html = (ROOT / "bot_gui.html").read_text(encoding="utf-8")

        self.assertIn('id="appUpdateModal"', html)
        self.assertIn("function startAppUpgrade()", html)
        self.assertIn("/api/update/install", html)
        self.assertIn("/api/update/status", html)

    def test_gui_polls_for_update_availability_while_open(self):
        html = (ROOT / "bot_gui.html").read_text(encoding="utf-8")

        self.assertIn("const APP_UPDATE_POLL_INTERVAL_MS", html)
        self.assertIn("function startUpdateAvailabilityPolling()", html)
        self.assertIn("checkForUpdates({ force: true, reason: 'periodic' })", html)
        self.assertIn("startUpdateAvailabilityPolling();", html)
        self.assertIn("force=1", html)

    def test_release_workflow_publishes_signed_manifest_channel(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("scripts/sign_update_manifest.py", workflow)
        self.assertIn("latest.json", workflow)
        self.assertIn("latest.json.sig", workflow)
        self.assertIn("CATALYST_UPDATE_SIGNING_KEY_B64", workflow)
        self.assertIn("CATALYST_RELEASE_CHANNEL_TOKEN", workflow)
        self.assertIn("Catalyst-Setup-${{ github.ref_name }}.exe.sha256", workflow)


if __name__ == "__main__":
    unittest.main()
