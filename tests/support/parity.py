"""Family-parity helpers shared by the parity and sync-tool tests."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
from pathlib import Path

from conftest import PluginSpec

PARITY_FAMILIES = ("ampr", "amx", "psu")
NAME_SENTINEL = b'"<DEVICE_NAME>"'


@dataclass(frozen=True, slots=True)
class NameSpan:
    line: int
    start: int
    end: int


def plugin_source(root: Path, spec: PluginSpec) -> Path:
    return root / spec.folder / spec.entrypoint


def runtime_root(root: Path, spec: PluginSpec) -> Path:
    return root / spec.folder / "vendor" / "runtime"


def base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def device_name_span(source: bytes, path: Path) -> NameSpan:
    tree = ast.parse(source.decode("utf-8"), filename=str(path))
    spans: list[NameSpan] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(base_name(base) == "Device" for base in node.bases):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            if len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            value = statement.value
            if (
                isinstance(target, ast.Name)
                and target.id == "name"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                if value.end_lineno != value.lineno:
                    raise AssertionError(
                        f"{path}:{value.lineno}: device-class name declaration "
                        "must be a single-line string literal"
                    )
                spans.append(NameSpan(value.lineno, value.col_offset, value.end_col_offset))

    if len(spans) != 1:
        raise AssertionError(
            f"{path}: expected exactly one top-level Device class name declaration, "
            f"found {len(spans)}"
        )
    return spans[0]


def normalized_plugin_source(path: Path) -> bytes:
    source = path.read_bytes()
    span = device_name_span(source, path)
    lines = source.splitlines(keepends=True)
    line = lines[span.line - 1]
    lines[span.line - 1] = line[: span.start] + NAME_SENTINEL + line[span.end :]
    return b"".join(lines)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_python_cache(path: Path) -> bool:
    return path.suffix == ".pyc" or "__pycache__" in path.parts


def runtime_hashes(root: Path) -> dict[Path, str]:
    if not root.is_dir():
        raise AssertionError(f"{root}: runtime root is missing")
    return {
        path.relative_to(root): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not is_python_cache(path)
    }


def parity_groups(
    plugin_specs: tuple[PluginSpec, ...],
) -> tuple[tuple[PluginSpec, ...], ...]:
    return tuple(
        tuple(spec for spec in plugin_specs if spec.sibling_family == family)
        for family in PARITY_FAMILIES
    )


def compare_sources(canonical: Path, sibling: Path) -> list[str]:
    if normalized_plugin_source(canonical) == normalized_plugin_source(sibling):
        return []
    return [
        f"{sibling} != {canonical}: source differs after normalizing the "
        "single device-class name declaration"
    ]


def compare_runtime_trees(canonical: Path, sibling: Path) -> list[str]:
    canonical_hashes = runtime_hashes(canonical)
    sibling_hashes = runtime_hashes(sibling)
    problems: list[str] = []
    for relative_path in sorted(set(canonical_hashes) - set(sibling_hashes)):
        problems.append(
            f"{sibling / relative_path}: missing runtime path; "
            f"canonical path is {canonical / relative_path}"
        )
    for relative_path in sorted(set(sibling_hashes) - set(canonical_hashes)):
        problems.append(
            f"{sibling / relative_path}: extra runtime path; "
            f"canonical root is {canonical}"
        )
    for relative_path in sorted(set(canonical_hashes) & set(sibling_hashes)):
        canonical_hash = canonical_hashes[relative_path]
        sibling_hash = sibling_hashes[relative_path]
        if canonical_hash != sibling_hash:
            problems.append(
                f"{sibling / relative_path} != {canonical / relative_path}: "
                f"SHA-256 mismatch {sibling_hash} != {canonical_hash}"
            )
    return problems


def compare_pair(canonical: PluginSpec, sibling: PluginSpec, root: Path) -> list[str]:
    return [
        *compare_sources(plugin_source(root, canonical), plugin_source(root, sibling)),
        *compare_runtime_trees(runtime_root(root, canonical), runtime_root(root, sibling)),
        *compare_runtime_trees(root / canonical.folder / "native", root / sibling.folder / "native"),
    ]


def assert_family_parity(group: tuple[PluginSpec, ...], root: Path) -> None:
    canonical = group[0]
    problems: list[str] = []
    for sibling in group[1:]:
        problems.extend(compare_pair(canonical, sibling, root))
    assert problems == [], "\n".join(problems)


def group_by_family(plugin_specs: tuple[PluginSpec, ...]) -> dict[str, tuple[PluginSpec, ...]]:
    groups = parity_groups(plugin_specs)
    return {family: groups[index] for index, family in enumerate(PARITY_FAMILIES)}
