import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import user_secrets


class UserSecretsTests(unittest.TestCase):
    def test_secret_write_replace_keeps_backup_and_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user_secrets.json"
            backup = path.with_suffix(".json.bak")

            with patch.object(user_secrets, "_secrets_path", return_value=path), \
                 patch.object(user_secrets, "_backup_path", return_value=backup):
                user_secrets.set_secret("SPACESCAN_API_KEY", "test-key-one")
                self.assertEqual(user_secrets.get_secret("SPACESCAN_API_KEY"), "test-key-one")

                user_secrets.set_secret("SPACESCAN_API_KEY", "test-key-two")
                self.assertEqual(user_secrets.get_secret("SPACESCAN_API_KEY"), "test-key-two")

                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {
                    "SPACESCAN_API_KEY": "test-key-two"
                })
                self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), {
                    "SPACESCAN_API_KEY": "test-key-one"
                })
                self.assertFalse(list(Path(tmp).glob("*.tmp.*")))

    def test_clear_last_secret_removes_backup_so_it_cannot_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "user_secrets.json"
            backup = path.with_suffix(".json.bak")

            with patch.object(user_secrets, "_secrets_path", return_value=path), \
                 patch.object(user_secrets, "_backup_path", return_value=backup):
                user_secrets.set_secret("SPACESCAN_API_KEY", "test-key-one")
                user_secrets.set_secret("SPACESCAN_API_KEY", "test-key-two")
                self.assertTrue(backup.exists())

                user_secrets.clear_secret("SPACESCAN_API_KEY")

                self.assertEqual(user_secrets.get_secret("SPACESCAN_API_KEY"), "")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {})
                self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()
