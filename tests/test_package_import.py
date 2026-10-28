"""验证 AstrBot 的“包方式”加载路径：data.plugins.<dir>.main + 相对导入。"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(TESTS_DIR))

from test_main_stub import _install_stubs  # noqa: E402


class PackageImportTest(unittest.TestCase):
    def test_package_relative_import(self) -> None:
        _install_stubs()
        package = "data.plugins.bbrs_memory_plugin_v01"
        for name, path in (
            ("data", PLUGIN_DIR.parent),
            ("data.plugins", PLUGIN_DIR.parent),
            (package, PLUGIN_DIR),
        ):
            module = types.ModuleType(name)
            module.__path__ = [str(path)]
            module.__package__ = name
            sys.modules[name] = module
        sys.modules[package].__file__ = str(PLUGIN_DIR / "__init__.py")

        spec = importlib.util.spec_from_file_location(
            f"{package}.main",
            PLUGIN_DIR / "main.py",
        )
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{package}.main"] = module
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, "MemoryPlugin"))
        self.assertIn(f"{package}.memory_db", sys.modules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
