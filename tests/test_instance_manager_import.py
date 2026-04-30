import ast
import importlib
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BOT_ROOT = REPO_ROOT / "bot"
WEB_FILES = (
    BOT_ROOT / "web" / "admin_api.py",
    BOT_ROOT / "web" / "client_api.py",
    BOT_ROOT / "web" / "server.py",
)


class InstanceManagerImportTests(unittest.TestCase):
    def setUp(self):
        bot_root = str(BOT_ROOT)
        if bot_root not in sys.path:
            sys.path.insert(0, bot_root)

    def test_instance_manager_singleton_is_exported(self):
        module = importlib.import_module("hub.instance_manager")

        self.assertTrue(hasattr(module, "instance_manager"))
        self.assertIsInstance(module.instance_manager, module.InstanceManager)

    def test_web_routes_avoid_direct_singleton_import(self):
        for path in WEB_FILES:
            with self.subTest(path=path):
                tree = ast.parse(path.read_text(), filename=str(path))
                bad_imports = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and node.module == "hub.instance_manager"
                    and any(alias.name == "instance_manager" for alias in node.names)
                ]

                self.assertEqual(
                    bad_imports,
                    [],
                    f"{path} should import hub.instance_manager as a module before "
                    "accessing the singleton attribute.",
                )


if __name__ == "__main__":
    unittest.main()
