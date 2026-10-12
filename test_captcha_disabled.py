import ast
import unittest
from pathlib import Path


BOT_SOURCE_PATH = Path(__file__).with_name("bot.py")
BOT_SOURCE = BOT_SOURCE_PATH.read_text(encoding="utf-8")
BOT_TREE = ast.parse(BOT_SOURCE)


class CaptchaDisabledTests(unittest.TestCase):
    def test_start_handlers_do_not_require_captcha(self):
        handlers = [
            node for node in ast.walk(BOT_TREE)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "cmd_start"
        ]
        self.assertGreaterEqual(len(handlers), 1)
        for handler in handlers:
            body = ast.get_source_segment(BOT_SOURCE, handler) or ""
            self.assertIn("require_group_membership", body)
            self.assertNotIn("require_verified", body)
            self.assertNotIn("captcha", body.lower())

    def test_captcha_callbacks_are_not_registered(self):
        for node in ast.walk(BOT_TREE):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            self.assertNotEqual(node.name, "cb_verify")
            for decorator in node.decorator_list:
                text = ast.unparse(decorator).lower()
                if "callback_query_handler" in text:
                    self.assertFalse(
                        "startswith" in text and "verify_" in text,
                        f"captcha callback still registered on {node.name}",
                    )

    def test_stale_captcha_buttons_open_the_menu(self):
        routes = [
            node for node in ast.walk(BOT_TREE)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_route_callback"
        ]
        self.assertGreaterEqual(len(routes), 1)
        self.assertTrue(any(
            "data.startswith(\"verify_\")" in (ast.get_source_segment(BOT_SOURCE, node) or "")
            and "render_main_menu" in (ast.get_source_segment(BOT_SOURCE, node) or "")
            for node in routes
        ))


if __name__ == "__main__":
    unittest.main()
