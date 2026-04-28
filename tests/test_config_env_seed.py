import tempfile
import unittest
from pathlib import Path

import config


class TestEnvExampleDiscovery(unittest.TestCase):
    def test_finds_template_from_source_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_dir = root / "src" / "catalyst"
            install_dir.mkdir(parents=True)
            expected = root / ".env.example"
            expected.write_text("SAGE_RPC_URL=https://127.0.0.1:9257\n", encoding="utf-8")

            self.assertEqual(
                config._find_env_example_path(str(install_dir)),
                str(expected),
            )

    def test_finds_template_from_pyinstaller_onedir_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "Catalyst"
            install_dir = app_dir / "_internal"
            install_dir.mkdir(parents=True)
            expected = app_dir / ".env.example"
            expected.write_text("SAGE_RPC_URL=https://127.0.0.1:9257\n", encoding="utf-8")

            self.assertEqual(
                config._find_env_example_path(str(install_dir)),
                str(expected),
            )


if __name__ == "__main__":
    unittest.main()
