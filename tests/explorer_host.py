"""Which ESIBD Explorer release the real-Explorer tests exercise.

Those tests compile or import the host classes found in the environment: the
installed esibd-explorer, ESIBD_EXPLORER_SOURCE (a source checkout) or the
interpreter named by ESIBD_QT_PYTHON. They prove compatibility only with that
host, so the session reports it and warns when it is not the targeted release.
ESIBD_REQUIRE_TARGET_HOST=1 turns a mismatch into a session error.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, distribution
import os
from pathlib import Path
import re
import subprocess

TARGET_VERSION = "1.0.2"

_VERSION = re.compile(r"""PROGRAM_VERSION\s*=\s*version\.parse\(\s*['"]([^'"]+)['"]\s*\)""")


def source_version(esibd_dir: Path) -> str | None:
    """PROGRAM_VERSION of an Explorer source tree (installed or checkout)."""
    try:
        match = _VERSION.search((Path(esibd_dir) / "config.py").read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def installed_version() -> str | None:
    try:
        return distribution("esibd-explorer").version
    except PackageNotFoundError:
        return None


def interpreter_version(python: str) -> str | None:
    try:
        result = subprocess.run(
            [python, "-c", "import importlib.metadata as m; print(m.version('esibd-explorer'))"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def hosts() -> list[tuple[str, str | None]]:
    """(description, version) of every host the real-Explorer tests may use."""
    found = [("installed esibd-explorer", installed_version())]
    source = os.environ.get("ESIBD_EXPLORER_SOURCE")
    if source:
        found.append((f"ESIBD_EXPLORER_SOURCE={source}", source_version(Path(source) / "esibd")))
    python = os.environ.get("ESIBD_QT_PYTHON")
    if python:
        found.append((f"ESIBD_QT_PYTHON={python}", interpreter_version(python)))
    return found


def mismatches(found: list[tuple[str, str | None]]) -> list[str]:
    return [f"{where}: {version}" for where, version in found
            if version is not None and version != TARGET_VERSION]


def add_const_names(esibd_dir: Path, ns: dict) -> dict:
    """Give AST-extracted host classes the esibd/const.py names they reference.

    Literal constants (e.g. 1.0.1's ``valid_chars``) and ``validateText`` are
    taken from the host itself; existing harness stubs are never replaced.
    """
    import ast

    tree = ast.parse((Path(esibd_dir) / "const.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and all(isinstance(target, ast.Name) for target in node.targets)):
            for target in node.targets:
                ns.setdefault(target.id, node.value.value)
        elif isinstance(node, ast.FunctionDef) and node.name == "validateText" and node.name not in ns:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(Path(esibd_dir) / "const.py"), "exec"), ns)
    ns.setdefault("getTestMode", lambda: False)
    return ns


def has_method(esibd_dir: Path, class_name: str, method: str, filename: str = "plugins.py") -> bool:
    """Whether this host release defines ``class_name.method`` (APIs move between releases)."""
    import ast

    tree = ast.parse((Path(esibd_dir) / filename).read_text(encoding="utf-8"))
    return any(isinstance(node, ast.ClassDef) and node.name == class_name
               and any(isinstance(item, ast.FunctionDef) and item.name == method for item in node.body)
               for node in tree.body)
