import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class TestAppUpdateSecurity(unittest.TestCase):
    def _asset(self, name, url=None, size=123):
        return {
            "name": name,
            "browser_download_url": url or (
                "https://github.com/Lowestofttim/catalyst-bot/releases/download/"
                f"v1.2.6/{name}"
            ),
            "size": size,
            "state": "uploaded",
        }

    def test_official_releases_api_url_is_allowed(self):
        from app_update import OFFICIAL_RELEASES_API_URL, is_allowed_releases_api_url

        self.assertTrue(is_allowed_releases_api_url(OFFICIAL_RELEASES_API_URL))
        self.assertFalse(is_allowed_releases_api_url("https://example.invalid/releases/latest"))
        self.assertFalse(
            is_allowed_releases_api_url(
                "https://api.github.com/repos/SomeoneElse/catalyst-bot/releases/latest"
            )
        )

    def test_selects_exact_windows_installer_and_sha256_sidecar(self):
        from app_update import select_windows_update_assets

        release = {
            "tag_name": "v1.2.6",
            "assets": [
                self._asset("Catalyst-windows-v1.2.6.zip"),
                self._asset("Catalyst-Setup-v1.2.6.exe"),
                self._asset("Catalyst-Setup-v1.2.6.exe.sha256"),
            ],
        }

        selected = select_windows_update_assets(release)

        self.assertEqual(selected["installer"]["name"], "Catalyst-Setup-v1.2.6.exe")
        self.assertEqual(selected["checksum"]["name"], "Catalyst-Setup-v1.2.6.exe.sha256")

    def test_rejects_installer_without_checksum_sidecar(self):
        from app_update import select_windows_update_assets

        release = {
            "tag_name": "v1.2.6",
            "assets": [self._asset("Catalyst-Setup-v1.2.6.exe")],
        }

        self.assertIsNone(select_windows_update_assets(release))

    def test_rejects_download_url_outside_official_release_path(self):
        from app_update import select_windows_update_assets

        release = {
            "tag_name": "v1.2.6",
            "assets": [
                self._asset(
                    "Catalyst-Setup-v1.2.6.exe",
                    url="https://example.invalid/Catalyst-Setup-v1.2.6.exe",
                ),
                self._asset("Catalyst-Setup-v1.2.6.exe.sha256"),
            ],
        }

        self.assertIsNone(select_windows_update_assets(release))

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
        release = {
            "tag_name": "v1.2.6",
            "html_url": "https://github.com/Lowestofttim/catalyst-bot/releases/tag/v1.2.6",
            "body": "Fixed Sage startup.\nAdded secure updater.",
            "assets": [
                {
                    "name": "Catalyst-Setup-v1.2.6.exe",
                    "browser_download_url": (
                        "https://github.com/Lowestofttim/catalyst-bot/releases/download/"
                        "v1.2.6/Catalyst-Setup-v1.2.6.exe"
                    ),
                    "size": 456,
                    "state": "uploaded",
                },
                {
                    "name": "Catalyst-Setup-v1.2.6.exe.sha256",
                    "browser_download_url": (
                        "https://github.com/Lowestofttim/catalyst-bot/releases/download/"
                        "v1.2.6/Catalyst-Setup-v1.2.6.exe.sha256"
                    ),
                    "size": 96,
                    "state": "uploaded",
                },
            ],
        }

        with patch.object(self.api_server, "get_app_version", return_value="1.2.5"), \
                patch("app_update.fetch_latest_release", return_value=release):
            resp = self.client.get("/api/check-update", environ_base=self.loopback)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertTrue(body["update_available"])
        self.assertTrue(body["installer_ready"])
        self.assertEqual(body["latest"], "1.2.6")
        self.assertIn("Fixed Sage startup", body["release_notes"])


class TestAppUpdateFrontendAndReleaseWorkflow(unittest.TestCase):
    def test_gui_has_upgrade_modal_and_install_call(self):
        html = (ROOT / "bot_gui.html").read_text(encoding="utf-8")

        self.assertIn('id="appUpdateModal"', html)
        self.assertIn("function startAppUpgrade()", html)
        self.assertIn("/api/update/install", html)
        self.assertIn("/api/update/status", html)

    def test_release_workflow_uploads_installer_checksum_sidecar(self):
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("Get-FileHash", workflow)
        self.assertIn("Catalyst-Setup-${{ github.ref_name }}.exe.sha256", workflow)
        self.assertIn("Catalyst-Setup-${{ github.ref_name }}.exe", workflow)


if __name__ == "__main__":
    unittest.main()
