import ast
import time
import unittest
from pathlib import Path


def _load_mapping_helper():
    source_path = Path(__file__).resolve().parent.parent / "src" / "catalyst" / "bot_loop.py"
    module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    fn_node = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "map_sage_terminal_offer_status"
    )
    isolated = ast.Module(body=[fn_node], type_ignores=[])
    ast.fix_missing_locations(isolated)
    namespace = {"time": time}
    exec(compile(isolated, str(source_path), "exec"), namespace)
    return namespace["map_sage_terminal_offer_status"]


class TestBotLoopSageStatusMapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.map_status = staticmethod(_load_mapping_helper())

    def test_confirmed_maps_to_filled(self):
        self.assertEqual(
            self.map_status(4, sage_offer={}, local_offer={}),
            "filled",
        )
        self.assertEqual(
            self.map_status("CONFIRMED", sage_offer={}, local_offer={}),
            "filled",
        )

    def test_pending_cancel_is_not_terminal(self):
        self.assertIsNone(self.map_status(2, sage_offer={}, local_offer={}))
        self.assertIsNone(self.map_status("PENDING_CANCEL", sage_offer={}, local_offer={}))

    def test_failed_maps_to_cancelled(self):
        self.assertEqual(
            self.map_status("FAILED", sage_offer={}, local_offer={}),
            "cancelled",
        )

    def test_expiry_wins_when_offer_has_passed_expiry(self):
        self.assertEqual(
            self.map_status(
                "CONFIRMED",
                sage_offer={},
                local_offer={"expires_at": "2026-03-27T18:00:00+00:00"},
                now_ts=1_774_600_000,
            ),
            "expired",
        )


if __name__ == "__main__":
    unittest.main()
