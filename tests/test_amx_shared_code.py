"""The code AMX and AMX HD share: _runtime/_amx_common.py, run by each entrypoint in its own namespace.

A linter reads the entrypoint and the shared file apart and cannot see the names one takes from the other,
so these checks read each plugin as it runs (the shared file in place of the statement that runs it).
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path
import symtable

import pytest

from support.source import entrypoint_code, shared_files

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = (ROOT / "amx_a" / "amx_plugin.py", ROOT / "amx_b" / "amx_plugin.py", ROOT / "amx_hd" / "amx_hd_plugin.py")
SHARED = ROOT / "amx_a" / "_runtime" / "_amx_common.py"
MODULE_NAMES = {"__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__", "__builtins__"}


def _module_bindings(tree: ast.Module) -> set[str]:
    """Names a module's own top-level statements bind (in if/try blocks too, not inside functions)."""
    names: set[str] = set()
    for statement in tree.body:
        for node in ast.walk(statement) if isinstance(statement, (ast.If, ast.Try)) else (statement,):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(name.id for target in targets for name in ast.walk(target) if isinstance(name, ast.Name))
    return names


def _global_reads(table: symtable.SymbolTable):
    """(scope, name) of each global name read in a function or class of the module."""
    for child in table.get_children():
        for symbol in child.get_symbols():
            if symbol.is_referenced() and symbol.is_global():
                yield child.get_name(), symbol.get_name()
        yield from _global_reads(child)


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda path: path.parent.name)
def test_every_global_name_the_plugin_reads_is_defined_where_it_runs(entrypoint):
    """F821 for the plugin as it runs: a name the shared code takes from one plugin only fails at run time."""
    assert shared_files(entrypoint) == [entrypoint.parent / "_runtime" / "_amx_common.py"]
    code = entrypoint_code(entrypoint)
    table = symtable.symtable(code, str(entrypoint), "exec")
    bound = {symbol.get_name() for symbol in table.get_symbols()
             if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()}
    known = bound | set(dir(builtins)) | MODULE_NAMES
    undefined = sorted({f"{scope}: {name}" for scope, name in _global_reads(table) if name not in known})
    undefined += sorted(f"<module>: {symbol.get_name()}" for symbol in table.get_symbols()
                        if symbol.is_referenced() and symbol.get_name() not in known)
    assert undefined == []


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda path: path.parent.name)
def test_the_plugin_and_its_shared_code_never_define_the_same_name(entrypoint):
    """F811 across the two files: the later definition would silently replace the other."""
    shared = _module_bindings(ast.parse(SHARED.read_text(encoding="utf-8")))
    own = _module_bindings(ast.parse(entrypoint.read_text(encoding="utf-8")))
    assert sorted(shared & own) == []


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda path: path.parent.name)
def test_no_plugin_method_hides_a_shared_one(entrypoint):
    """The shared file holds only code both plugins run: a method one plugin needs to change leaves it."""
    shared = {node.name: {item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
              for node in ast.parse(SHARED.read_text(encoding="utf-8")).body if isinstance(node, ast.ClassDef)}
    hidden, mixed = [], 0
    for node in ast.parse(entrypoint.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [base.id for base in node.bases if isinstance(base, ast.Name)]
        mixins = [base for base in bases if base in shared]
        assert not mixins or bases[0] == mixins[0], f"{node.name}: the shared mixin comes first in the bases"
        mixed += bool(mixins)
        own = {item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        hidden += [f"{node.name}.{name}" for mixin in mixins for name in sorted(own & shared[mixin])]
    assert mixed == 3  # Device, Channel and Controller.
    assert hidden == []
