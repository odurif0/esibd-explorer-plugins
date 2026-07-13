"""Autonomous plugin folder characterization tests."""

from __future__ import annotations

import ast
import os
import shutil
from pathlib import Path

import pytest

from conftest import PLUGIN_SPECS, PluginSpec


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FOLDERS = tuple(spec.folder for spec in PLUGIN_SPECS)


def required_paths(spec: PluginSpec, plugin_root: Path) -> tuple[Path, ...]:
    runtime_root = plugin_root / "vendor" / "runtime"
    device_root = runtime_root / spec.runtime_family
    vendor_root = device_root / "vendor"
    return (
        plugin_root / spec.entrypoint,
        plugin_root / f"{spec.icon_stem}.png",
        plugin_root / f"{spec.icon_stem}.svg",
        plugin_root / "switch-medium_on.png",
        plugin_root / "switch-medium_off.png",
        runtime_root / "__init__.py",
        runtime_root / "_driver_common.py",
        runtime_root / "_controller_process.py",
        runtime_root / "_process_proxy_support.py",
        runtime_root / "error_codes.json",
        device_root / "__init__.py",
        device_root / f"{spec.runtime_family}.py",
        device_root / f"{spec.runtime_family}_base.py",
        vendor_root / spec.header,
        vendor_root / "x64" / spec.dll,
    )


def import_top_level_modules(entrypoint: Path) -> tuple[str, ...]:
    tree = ast.parse(entrypoint.read_text(encoding="utf-8"), filename=str(entrypoint))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module.split(".", 1)[0])
    return tuple(modules)


def validate_plugin_autonomy(
    spec: PluginSpec,
    plugin_root: Path,
    plugin_slugs: set[str],
) -> None:
    problems: list[str] = []
    entrypoints = sorted(path.name for path in plugin_root.glob("*_plugin.py"))
    if entrypoints != [spec.entrypoint]:
        problems.append(
            f"{plugin_root}: expected only root entrypoint "
            f"{spec.entrypoint!r}, found {entrypoints!r}"
        )

    for path in plugin_root.rglob("*"):
        if path.is_symlink():
            problems.append(f"{path}: symlink points to {os.readlink(path)!r}")
        elif not path.is_dir() and not path.is_file():
            problems.append(f"{path}: expected a regular file or directory")

    for path in required_paths(spec, plugin_root):
        if path.is_symlink():
            problems.append(f"{path}: required path must not be a symlink")
        if not path.is_file():
            problems.append(f"{path}: required regular file is missing")

    entrypoint = plugin_root / spec.entrypoint
    if entrypoint.is_file() and not entrypoint.is_symlink():
        sibling_slugs = plugin_slugs - {spec.folder}
        for module in sorted(set(import_top_level_modules(entrypoint)) & sibling_slugs):
            problems.append(f"{entrypoint}: imports sibling plugin slug {module!r}")

    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("folder", PLUGIN_FOLDERS)
def test_plugin_tree_is_autonomous(
    plugin_specs: tuple[PluginSpec, ...],
    folder: str,
) -> None:
    specs_by_folder = {spec.folder: spec for spec in plugin_specs}
    assert tuple(specs_by_folder) == PLUGIN_FOLDERS

    validate_plugin_autonomy(
        specs_by_folder[folder],
        REPO_ROOT / folder,
        set(specs_by_folder),
    )


def copied_plugin(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
    folder: str = "psu_a",
) -> tuple[PluginSpec, Path, set[str]]:
    spec = next(spec for spec in plugin_specs if spec.folder == folder)
    plugin_root = tmp_path / spec.folder
    shutil.copytree(REPO_ROOT / spec.folder, plugin_root)
    return spec, plugin_root, {plugin.folder for plugin in plugin_specs}


def test_validator_rejects_missing_dll(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    spec, plugin_root, plugin_slugs = copied_plugin(tmp_path, plugin_specs)
    dll_path = (
        plugin_root
        / "vendor"
        / "runtime"
        / spec.runtime_family
        / "vendor"
        / "x64"
        / spec.dll
    )
    dll_path.unlink()

    with pytest.raises(AssertionError) as excinfo:
        validate_plugin_autonomy(spec, plugin_root, plugin_slugs)

    assert str(dll_path) in str(excinfo.value)
    assert "required regular file is missing" in str(excinfo.value)


def test_validator_rejects_symlink(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    spec, plugin_root, plugin_slugs = copied_plugin(tmp_path, plugin_specs)
    symlink_path = plugin_root / "entrypoint-link.py"
    symlink_path.symlink_to(plugin_root / spec.entrypoint)

    with pytest.raises(AssertionError) as excinfo:
        validate_plugin_autonomy(spec, plugin_root, plugin_slugs)

    assert str(symlink_path) in str(excinfo.value)
    assert "symlink points to" in str(excinfo.value)


def test_validator_rejects_static_sibling_import(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    spec, plugin_root, plugin_slugs = copied_plugin(tmp_path, plugin_specs)
    entrypoint = plugin_root / spec.entrypoint
    entrypoint.write_text(
        f"{entrypoint.read_text(encoding='utf-8')}\nimport psu_b\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError) as excinfo:
        validate_plugin_autonomy(spec, plugin_root, plugin_slugs)

    assert str(entrypoint) in str(excinfo.value)
    assert "imports sibling plugin slug 'psu_b'" in str(excinfo.value)
