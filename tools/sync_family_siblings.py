"""Propagate canonical plugin sources to their sibling copies.

Siblings stay self-contained (no symlinks, no shared imports): each one keeps a
full copy of the canonical entrypoint and runtime tree. Only the single
``name = "..."`` declaration of the Device class differs. Edit the canonical
plugin, then run:

    python3 tools/sync_family_siblings.py          # rewrite sibling copies
    python3 tools/sync_family_siblings.py --check  # report drift, change nothing

tests/test_plugin_family_parity.py enforces the same invariant.
"""
from __future__ import annotations

import argparse
import ast
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# family: (canonical folder, sibling folders, entrypoint)
FAMILIES = {
    "ampr": ("ampr_a", ("ampr_b",), "ampr_plugin.py"),
    "amx": ("amx_a", ("amx_b",), "amx_plugin.py"),
    "psu": ("psu_a", ("psu_b", "psu_c", "psu_d", "psu_e"), "psu_plugin.py"),
}


def _device_name_literal(source: str, path: Path) -> tuple[int, int, int]:
    """Return (line index, start, end) of the Device class ``name`` literal."""
    spans = []
    for node in ast.parse(source, filename=str(path)).body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(getattr(base, "id", getattr(base, "attr", None)) == "Device" for base in node.bases):
            continue
        for statement in node.body:
            if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == "name"
                    and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str)
                    and statement.value.lineno == statement.value.end_lineno):
                spans.append((statement.value.lineno - 1, statement.value.col_offset, statement.value.end_col_offset))
    if len(spans) != 1:
        raise SystemExit(f"{path}: expected one single-line Device name declaration, found {len(spans)}")
    return spans[0]


def _sibling_source(canonical: Path, sibling: Path) -> bytes:
    canonical_text = canonical.read_text(encoding="utf-8")
    sibling_text = sibling.read_text(encoding="utf-8")
    line, start, end = _device_name_literal(sibling_text, sibling)
    sibling_name = sibling_text.splitlines(keepends=True)[line][start:end]
    line, start, end = _device_name_literal(canonical_text, canonical)
    lines = canonical_text.splitlines(keepends=True)
    lines[line] = lines[line][:start] + sibling_name + lines[line][end:]
    return "".join(lines).encode("utf-8")


def _runtime_files(root: Path) -> dict[Path, Path]:
    return {path.relative_to(root): path for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix != ".pyc" and "__pycache__" not in path.parts}


def sync(root: Path = REPO_ROOT, *, check: bool = False) -> list[str]:
    """Return the drifted paths; rewrite them unless ``check`` is set."""
    drift: list[str] = []
    for canonical_folder, siblings, entrypoint in FAMILIES.values():
        canonical_entry = root / canonical_folder / entrypoint
        canonical_runtime = _runtime_files(root / canonical_folder / "vendor" / "runtime")
        for sibling_folder in siblings:
            sibling_entry = root / sibling_folder / entrypoint
            expected = _sibling_source(canonical_entry, sibling_entry)
            if sibling_entry.read_bytes() != expected:
                drift.append(str(sibling_entry.relative_to(root)))
                if not check:
                    sibling_entry.write_bytes(expected)
            sibling_runtime_root = root / sibling_folder / "vendor" / "runtime"
            sibling_runtime = _runtime_files(sibling_runtime_root)
            for relative, source in canonical_runtime.items():
                target = sibling_runtime_root / relative
                if relative not in sibling_runtime or target.read_bytes() != source.read_bytes():
                    drift.append(str(target.relative_to(root)))
                    if not check:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
            for relative in sorted(set(sibling_runtime) - set(canonical_runtime)):
                drift.append(f"{(sibling_runtime_root / relative).relative_to(root)} (extra)")
                if not check:
                    (sibling_runtime_root / relative).unlink()
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report drift without changing files")
    args = parser.parse_args(argv)
    drift = sync(check=args.check)
    for path in drift:
        print(("drift: " if args.check else "updated: ") + path)
    if not drift:
        print("All sibling copies match their canonical plugin.")
    return 1 if (args.check and drift) else 0


if __name__ == "__main__":
    sys.exit(main())
