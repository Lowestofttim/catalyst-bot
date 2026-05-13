import os
import unittest
from unittest.mock import patch

try:
    import sage_node
    _IMPORT_ERROR = None
except ModuleNotFoundError as exc:
    sage_node = None
    _IMPORT_ERROR = exc


@unittest.skipIf(sage_node is None, f"sage_node import unavailable: {_IMPORT_ERROR}")
class TestSageStartupVersionGate(unittest.TestCase):
    def setUp(self):
        self._original_selected_fingerprint = sage_node._selected_fingerprint
        self._original_triggered = sage_node._start_triggered.is_set()
        sage_node._selected_fingerprint = None
        sage_node._start_triggered.clear()

    def tearDown(self):
        sage_node._selected_fingerprint = self._original_selected_fingerprint
        if self._original_triggered:
            sage_node._start_triggered.set()
        else:
            sage_node._start_triggered.clear()

    def test_minimum_supported_version_is_allowed(self):
        with patch.object(sage_node, "_load_current_sage_version", return_value="0.12.9"):
            requirement = sage_node.get_sage_version_requirement()
        self.assertTrue(requirement["supported"])
        self.assertEqual(requirement["minimum_required_version"], "0.12.9")

    def test_older_version_is_blocked(self):
        with patch.object(sage_node, "_load_current_sage_version", return_value="0.12.8"):
            requirement = sage_node.get_sage_version_requirement()
        self.assertFalse(requirement["supported"])
        self.assertIn("0.12.9", requirement["reason"])

    def test_trigger_start_rejects_unsupported_sage_version(self):
        blocked_requirement = {
            "installed_version": "0.12.9",
            "minimum_required_version": "0.12.10",
            "supported": False,
            "reason": "Sage v0.12.9 is too old.",
        }
        with patch.dict(os.environ, {"WALLET_TYPE": "sage"}, clear=False):
            with patch.object(sage_node, "get_sage_version_requirement", return_value=blocked_requirement):
                result = sage_node.trigger_start("1234567890")

        self.assertFalse(result["success"])
        self.assertTrue(result["unsupported_version"])
        self.assertEqual(result["sage_version"], "0.12.9")
        self.assertIsNone(sage_node._selected_fingerprint)
        self.assertFalse(sage_node._start_triggered.is_set())

    def test_saved_sage_fingerprint_does_not_skip_startup_picker(self):
        with patch.dict(
            os.environ,
            {
                "WALLET_TYPE": "sage",
                "SAGE_FINGERPRINT": "1234567890",
                "WALLET_FINGERPRINT": "9876543210",
            },
            clear=False,
        ):
            fingerprint = sage_node._resolve_startup_fingerprint()

        self.assertIsNone(fingerprint)


if __name__ == "__main__":
    unittest.main()
