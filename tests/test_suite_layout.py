"""Shared test code lives in tests/support; test modules never import one another.

2026-10-12: 108 test modules imported helpers and fixtures from 42 others, eleven of them
carried their own copy of the Explorer stand-ins, and fixtures with the same name (`rig`)
came from different modules through `noqa` imports.
"""
import ast
from pathlib import Path
import re

import conftest

TESTS = Path(__file__).resolve().parent


def _imported_modules(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module


def test_test_modules_and_support_never_import_test_modules():
    offenders = [f"{path.relative_to(TESTS)}: {module}"
                 for path in sorted(TESTS.glob("*.py")) + sorted((TESTS / "support").glob("*.py"))
                 for module in _imported_modules(path) if module.split(".")[0].startswith("test_")]
    assert offenders == [], offenders


def test_python_outside_tests_never_imports_test_modules():
    # The MScan Rust tests run Python snippets on tests/ that imported moved fixtures.
    root = TESTS.parent
    pattern = re.compile(r"^\s*(?:from|import)\s+(test_\w+)", re.MULTILINE)
    sources = [*root.glob("native/worker/tests/*.rs"), *root.glob("tools/*.py"), *root.glob("notebooks/*.py")]
    offenders = [f"{path.relative_to(root)}: {name}" for path in sorted(sources)
                 for name in pattern.findall(path.read_text(encoding="utf-8"))]
    assert offenders == [], offenders


def test_support_fixtures_are_registered_plugins():
    with_fixtures = sorted(
        f"support.{path.stem}" for path in (TESTS / "support").glob("*.py")
        if any(isinstance(node, ast.FunctionDef) and any("fixture" in ast.unparse(d) for d in node.decorator_list)
               for node in ast.parse(path.read_text(encoding="utf-8")).body))
    assert with_fixtures == sorted(conftest.pytest_plugins)


def test_a_module_that_runs_itself_as_a_probe_has_an_entry_point():
    # 2026-11-13: since 42418c9 two probe modules ran themselves without a `__main__` block, so the
    # subprocess did nothing, exited 0, and the setpoint-entry and AMPR-startup probes always passed.
    def runs_itself(tree):
        return any(isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_probe"
                   and node.args and getattr(node.args[0], "id", None) == "__file__" for node in ast.walk(tree))

    def entry_point(tree):
        return any(isinstance(node, ast.If) and any(
            isinstance(test, ast.Compare) and getattr(test.left, "id", None) == "__name__"
            and getattr(test.comparators[0], "value", None) == "__main__" for test in ast.walk(node.test))
            for node in tree.body)

    trees = {path.name: ast.parse(path.read_text(encoding="utf-8")) for path in sorted(TESTS.glob("test_*.py"))}
    offenders = [name for name, tree in trees.items() if runs_itself(tree) and not entry_point(tree)]
    assert offenders == [], offenders
