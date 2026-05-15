"""Public-readiness smoke tests for clone-and-run safety.

These tests intentionally stay at the boundaries most likely to break a
fresh public install: first-launch config/data paths, local API guardrails,
safe wallet failure responses, token-exempt route protections, and destructive
confirmation gates.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import api_server

    _SKIP = None
except (ModuleNotFoundError, ImportError) as exc:
    api_server = None
    _SKIP = str(exc)


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src" / "catalyst"


@unittest.skipIf(_SKIP is not None, f"api_server unavailable: {_SKIP}")
class PublicReadinessSmokeTests(unittest.TestCase):
    _LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
    _NON_LOOPBACK = {"REMOTE_ADDR": "192.0.2.55"}

    def setUp(self):
        api_server.app.testing = True
        self.client = api_server.app.test_client()
        self.auth = {"X-Bot-Local-Token": api_server._LOCAL_API_TOKEN}
        api_server._rate_limit_log.clear()

    def tearDown(self):
        api_server._rate_limit_log.clear()

    def _post(self, path: str, body: dict | None = None, *, auth: bool = True):
        headers = dict(self.auth) if auth else {}
        return self.client.post(
            path,
            json=body or {},
            headers=headers,
            environ_base=self._LOOPBACK,
        )

    def test_first_launch_seeds_env_inside_cmm_data_dir(self):
        script = textwrap.dedent(
            """
            import json
            import os
            import config
            import user_paths

            print(json.dumps({
                "data_dir": user_paths.data_dir(),
                "env_path": config._ENV_PATH,
                "env_exists": os.path.exists(config._ENV_PATH),
            }))
            """
        )
        with tempfile.TemporaryDirectory(prefix="catalyst-public-ready-") as data_dir:
            env = os.environ.copy()
            env["CMM_DATA_DIR"] = data_dir
            env["PYTHONPATH"] = str(SRC_DIR)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(Path(payload["data_dir"]), Path(data_dir))
            self.assertEqual(Path(payload["env_path"]), Path(data_dir) / ".env")
            self.assertTrue(payload["env_exists"])

    def test_missing_wallet_fingerprints_return_safe_generic_error(self):
        with patch(
            "chia_node.get_available_fingerprints",
            side_effect=RuntimeError("secret wallet rpc traceback"),
        ):
            resp = self.client.get(
                "/api/sage/fingerprints", environ_base=self._LOOPBACK
            )

        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertEqual(body.get("error"), "Internal server error")
        self.assertNotIn("secret wallet rpc traceback", resp.get_data(as_text=True))

    def test_sage_setup_copy_keeps_testers_on_gui_path(self):
        html = (REPO_ROOT / "bot_gui.html").read_text(encoding="utf-8")

        self.assertIn("configure Sage's wallet RPC certificate path from the app", html)
        self.assertNotIn(
            "configure Sage's wallet RPC certificate path without editing .env", html
        )

    def test_spacescan_setup_copy_describes_app_storage(self):
        html = (REPO_ROOT / "bot_gui.html").read_text(encoding="utf-8")

        self.assertIn("Key stored locally by CATalyst", html)
        self.assertNotIn("Key stored locally in <code", html)

    def test_splash_setup_check_handles_unavailable_binary_without_token(self):
        unavailable = {
            "installed": False,
            "version": None,
            "path": "",
            "platform": {"supported": True},
        }
        with patch("splash_setup.check_installed", return_value=unavailable):
            resp = self.client.get(
                "/api/splash/setup/check", environ_base=self._LOOPBACK
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), unavailable)

    def test_public_beta_copy_uses_current_repo_and_build_wording(self):
        html = (REPO_ROOT / "bot_gui.html").read_text(encoding="utf-8")
        contact_config = (
            REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
        ).read_text(encoding="utf-8")
        bug_template = (
            REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"
        ).read_text(encoding="utf-8")
        feedback_template = (
            REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "feedback.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("May 2026 public beta", html)
        self.assertIn("Dashboard layout (May 2026 beta refresh)", html)
        self.assertIn("plants tighter real inner-tier offers", html)
        self.assertNotIn("April 2026", html)
        self.assertNotIn("github.com/Lowestofttim/catalyst-bot", contact_config)
        self.assertIn("github.com/catalystxch/catalyst-bot", contact_config)
        self.assertIn('placeholder: "v1.2.30"', bug_template)
        self.assertIn('placeholder: "v1.2.30"', feedback_template)

    def test_token_exempt_routes_remain_loopback_only(self):
        self.assertEqual(
            api_server._TOKEN_EXEMPT_WRITE_ROUTES, {"/api/splash/incoming"}
        )

        with (
            patch.object(api_server.cfg, "SPLASH_RECEIVE_ENABLED", True),
            patch("api_server._splash_incoming_rate_limited", return_value=False),
        ):
            resp = self.client.post(
                "/api/splash/incoming",
                json={"offer": "offer1publicreadiness"},
                environ_base=self._NON_LOOPBACK,
            )

        self.assertEqual(resp.status_code, 403)

    def test_open_external_get_proxy_is_not_exposed(self):
        resp = self.client.get(
            "/api/open-external?url=https://sagewallet.net/",
            environ_base=self._LOOPBACK,
        )
        self.assertIn(resp.status_code, (404, 405))

    def test_destructive_reset_routes_require_token_and_confirmation(self):
        for path in ("/api/pnl/reset", "/api/reset/offer-history", "/api/reset/full"):
            with self.subTest(path=path, gate="token"):
                resp = self.client.post(
                    path, json={"confirm": "RESET"}, environ_base=self._LOOPBACK
                )
                self.assertEqual(resp.status_code, 401)

            with self.subTest(path=path, gate="confirmation"):
                resp = self._post(path, {})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json().get("error"), "confirmation_required")


if __name__ == "__main__":
    unittest.main()
