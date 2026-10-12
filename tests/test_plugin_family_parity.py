"""Sibling plugin family parity guards."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import PluginSpec
from support.parity import PARITY_FAMILIES, assert_family_parity, group_by_family, parity_groups, plugin_source, runtime_root


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GROUP_SIZES = (2, 2, 5)


def copy_family(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
    family: str,
) -> tuple[PluginSpec, ...]:
    group = group_by_family(plugin_specs)[family]
    for spec in group:
        shutil.copytree(REPO_ROOT / spec.folder, tmp_path / spec.folder)
    return group


def mutate_first_byte(path: Path) -> None:
    data = path.read_bytes()
    assert data, f"{path}: expected a non-empty file for mutation probe"
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])


def test_parity_groups_are_limited_to_sibling_variants(
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    groups = parity_groups(plugin_specs)
    compared_names = {spec.manager_name for group in groups for spec in group}

    assert tuple(len(group) for group in groups) == EXPECTED_GROUP_SIZES
    assert "AMX_HD" not in compared_names
    assert "DMMR" not in compared_names


@pytest.mark.parametrize("family", PARITY_FAMILIES)
def test_sibling_family_matches_canonical(
    plugin_specs: tuple[PluginSpec, ...],
    family: str,
) -> None:
    assert_family_parity(group_by_family(plugin_specs)[family], REPO_ROOT)


def test_validator_rejects_non_name_source_mutation(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    group = copy_family(tmp_path, plugin_specs, "ampr")
    canonical, sibling = group[0], group[1]
    sibling_source = plugin_source(tmp_path, sibling)
    sibling_source.write_bytes(sibling_source.read_bytes() + b"\n# parity probe\n")

    with pytest.raises(AssertionError) as excinfo:
        assert_family_parity(group, tmp_path)

    message = str(excinfo.value)
    assert str(plugin_source(tmp_path, canonical)) in message
    assert str(sibling_source) in message


def test_validator_rejects_one_byte_runtime_mutation(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    group = copy_family(tmp_path, plugin_specs, "amx")
    canonical, sibling = group[0], group[1]
    relative_path = Path("error_codes.json")
    canonical_path = runtime_root(tmp_path, canonical) / relative_path
    sibling_path = runtime_root(tmp_path, sibling) / relative_path
    mutate_first_byte(sibling_path)

    with pytest.raises(AssertionError) as excinfo:
        assert_family_parity(group, tmp_path)

    message = str(excinfo.value)
    assert str(canonical_path) in message
    assert str(sibling_path) in message
