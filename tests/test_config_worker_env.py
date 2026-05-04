import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config as config_module


class ConfigWorkerEnvTests(unittest.TestCase):
    def test_worker_preserve_flag_keeps_sage_process_env_over_dotenv_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join([
                    "WALLET_TYPE=chia",
                    "SAGE_RPC_URL=https://127.0.0.1:9257",
                    "SAGE_CERT_PATH=C:/stale/wallet.crt",
                    "SAGE_KEY_PATH=C:/stale/wallet.key",
                    "SAGE_DATA_DIR=C:/stale",
                ]),
                encoding="utf-8",
            )
            process_env = {
                "_CATALYST_PRESERVE_PROCESS_ENV": "1",
                "WALLET_TYPE": "sage",
                "SAGE_RPC_URL": "https://127.0.0.1:43210",
                "SAGE_CERT_PATH": "C:/runtime/wallet.crt",
                "SAGE_KEY_PATH": "C:/runtime/wallet.key",
                "SAGE_DATA_DIR": "C:/runtime",
            }

            with (
                patch.object(config_module, "_ENV_PATH", str(env_path)),
                patch.dict(os.environ, process_env, clear=False),
            ):
                cfg = config_module.Config()

                self.assertEqual(cfg.WALLET_TYPE, "sage")
                self.assertEqual(cfg.SAGE_RPC_URL, "https://127.0.0.1:43210")
                self.assertEqual(cfg.SAGE_CERT_PATH, "C:/runtime/wallet.crt")
                self.assertEqual(cfg.SAGE_KEY_PATH, "C:/runtime/wallet.key")
                self.assertEqual(cfg.SAGE_DATA_DIR, "C:/runtime")
                self.assertEqual(os.environ["SAGE_RPC_URL"], "https://127.0.0.1:43210")


if __name__ == "__main__":
    unittest.main()
