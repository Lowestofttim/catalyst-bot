import ast
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent  # project root, one level above tests/
ROOT = REPO_ROOT / "src" / "catalyst"                # where python source now lives


def _class_assign_literal(tree: ast.AST, class_name: str, attr_name: str):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and target.id == attr_name:
                            return ast.literal_eval(item.value)
    raise AssertionError(f"{class_name}.{attr_name} not found")


def _module_assign_literal(tree: ast.AST, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"module assignment {name} not found")


class SecurityGuardrailSourceTests(unittest.TestCase):
    def test_sage_change_address_is_allowlisted(self):
        tree = ast.parse((ROOT / "config.py").read_text(encoding="utf-8"))
        updatable = _class_assign_literal(tree, "Config", "_UPDATABLE_KEYS")
        self.assertIn("SAGE_SET_CHANGE_ADDRESS", updatable)

    def test_splash_incoming_is_machine_exempt(self):
        tree = ast.parse((ROOT / "api_server.py").read_text(encoding="utf-8"))
        token_exempt = _module_assign_literal(tree, "_TOKEN_EXEMPT_WRITE_ROUTES")
        rate_exempt = _module_assign_literal(tree, "_RATE_LIMIT_EXEMPT_WRITE_ROUTES")
        self.assertIn("/api/splash/incoming", token_exempt)
        self.assertIn("/api/splash/incoming", rate_exempt)

    def test_bot_start_checks_sage_signing_capability(self):
        # The /api/bot/start route moved from api_server.py into
        # blueprints/bot.py during the blueprint refactor; check there
        # for the guard call (via the re-exported helper on api_server).
        bot_bp_source = (ROOT / "blueprints" / "bot.py").read_text(encoding="utf-8")
        api_source = (ROOT / "api_server.py").read_text(encoding="utf-8")
        bot_loop_source = (ROOT / "bot_loop.py").read_text(encoding="utf-8")
        self.assertIn("signing_block_reason =", bot_bp_source)
        self.assertIn(
            "_get_sage_signing_block_reason()",
            bot_bp_source,
        )
        self.assertIn("def _get_sage_signing_block_reason", api_source)
        self.assertIn("bot_start_blocked_watch_only", bot_loop_source)

    def test_frontend_console_calls_are_debug_gated(self):
        source = (REPO_ROOT / "bot_gui.html").read_text(encoding="utf-8")
        self.assertIn("window.__CATALYST_DEBUG_LOGS", source)
        self.assertNotRegex(source, r"\bconsole\.(log|warn|error|debug)\(")


if __name__ == "__main__":
    unittest.main()
