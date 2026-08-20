from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
README_PATH = ROOT / "README.md"
CATALOG_START = "<!-- COMMAND_CATALOG_START -->"
CATALOG_END = "<!-- COMMAND_CATALOG_END -->"


def _command_catalog() -> str:
    readme = README_PATH.read_text(encoding="utf-8")
    start = readme.index(CATALOG_START)
    end = readme.index(CATALOG_END, start)
    return readme[start:end]


def _cli_operation_flags() -> set[str]:
    tree = ast.parse((ROOT / "cli_app" / "arguments.py").read_text(encoding="utf-8"))
    flags: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != "operation" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            flags.add(first.value)
    return flags


def _discord_command_names() -> set[str]:
    tree = ast.parse((ROOT / "discord_app" / "bot.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr != "command":
                continue
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    names.add(keyword.value.value)
    return names


class ReadmeCommandCatalogTests(unittest.TestCase):
    def test_catalog_lists_every_cli_operation(self) -> None:
        catalog = _command_catalog()
        missing = sorted(flag for flag in _cli_operation_flags() if flag not in catalog)
        self.assertEqual(missing, [])

    def test_catalog_lists_every_discord_command(self) -> None:
        catalog = _command_catalog()
        missing = sorted(
            name for name in _discord_command_names() if f"!{name}" not in catalog
        )
        self.assertEqual(missing, [])

    def test_catalog_documents_common_cli_controls(self) -> None:
        catalog = _command_catalog()
        for flag in (
            "--help",
            "--version",
            "--provider",
            "--safe-mode",
            "--no-safe-mode",
            "--topic",
            "--reason",
            "--json",
            "--verbose",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, catalog)


if __name__ == "__main__":
    unittest.main()
