"""tools/sync_family_siblings.py keeps sibling copies identical to their canonical plugin."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

from conftest import PLUGIN_SPECS
from test_plugin_family_parity import (
    PARITY_FAMILIES,
    assert_family_parity,
    group_by_family,
    plugin_source,
    runtime_root,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tool():
    path = REPO_ROOT / "tools" / "sync_family_siblings.py"
    spec = importlib.util.spec_from_file_location("_sync_family_siblings_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_families(tmp_path: Path) -> None:
    for spec in PLUGIN_SPECS:
        if spec.sibling_family in PARITY_FAMILIES:
            shutil.copytree(REPO_ROOT / spec.folder, tmp_path / spec.folder,
                            ignore=shutil.ignore_patterns("__pycache__", "logs"))


def test_tool_families_match_plugin_specs() -> None:
    tool = _tool()
    groups = group_by_family(PLUGIN_SPECS)
    assert set(tool.FAMILIES) == set(PARITY_FAMILIES)
    for family, (canonical, siblings, entrypoint) in tool.FAMILIES.items():
        assert tuple(spec.folder for spec in groups[family]) == (canonical, *siblings)
        assert {spec.entrypoint for spec in groups[family]} == {entrypoint}


def test_repository_has_no_sibling_drift() -> None:
    assert _tool().sync(check=True) == []


def test_sync_propagates_canonical_edits_and_keeps_device_names(tmp_path: Path) -> None:
    tool = _tool()
    _copy_families(tmp_path)
    groups = group_by_family(PLUGIN_SPECS)
    psu = groups["psu"]
    canonical_entry = plugin_source(tmp_path, psu[0])
    canonical_entry.write_text(canonical_entry.read_text(encoding="utf-8") + "\n# sync probe\n", encoding="utf-8")
    canonical_runtime = runtime_root(tmp_path, psu[0])
    (canonical_runtime / "sync_probe.txt").write_text("probe", encoding="utf-8")
    extra = runtime_root(tmp_path, psu[1]) / "stale.txt"
    extra.write_text("stale", encoding="utf-8")

    drift = tool.sync(tmp_path, check=True)
    assert any(path.endswith("psu_e/psu_plugin.py") for path in drift)
    assert extra.exists(), "--check must not change files"

    tool.sync(tmp_path)
    assert tool.sync(tmp_path, check=True) == []
    assert not extra.exists()
    for family in PARITY_FAMILIES:
        assert_family_parity(groups[family], tmp_path)
    for spec in psu:
        source = plugin_source(tmp_path, spec).read_text(encoding="utf-8")
        assert f'name = "{spec.manager_name}"' in source
        assert source.endswith("# sync probe\n")
