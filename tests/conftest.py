"""Shared immutable plugin contract data for repository-only tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass(frozen=True, slots=True)
class PluginSpec:
    folder: str
    manager_name: str
    title_name: str
    entrypoint: str
    runtime_family: str
    icon_stem: str
    sibling_family: str | None
    header: str
    dll: str


PLUGIN_SPECS: tuple[PluginSpec, ...] = (
    PluginSpec(
        "ampr_a",
        "AMPR_A",
        "AMPR_A",
        "ampr_plugin.py",
        "ampr",
        "ampr",
        "ampr",
        "COM-AMPR-12.h",
        "COM-AMPR-12.dll",
    ),
    PluginSpec(
        "ampr_b",
        "AMPR_B",
        "AMPR_B",
        "ampr_plugin.py",
        "ampr",
        "ampr",
        "ampr",
        "COM-AMPR-12.h",
        "COM-AMPR-12.dll",
    ),
    PluginSpec(
        "amx",
        "AMX",
        "AMX",
        "amx_plugin.py",
        "amx",
        "amx",
        "amx",
        "COM-HVAMX4ED.h",
        "COM-HVAMX4ED.dll",
    ),
    PluginSpec(
        "amx_a",
        "AMX_A",
        "AMX_A",
        "amx_plugin.py",
        "amx",
        "amx",
        "amx",
        "COM-HVAMX4ED.h",
        "COM-HVAMX4ED.dll",
    ),
    PluginSpec(
        "amx_b",
        "AMX_B",
        "AMX_B",
        "amx_plugin.py",
        "amx",
        "amx",
        "amx",
        "COM-HVAMX4ED.h",
        "COM-HVAMX4ED.dll",
    ),
    PluginSpec(
        "amx_hd",
        "AMX_HD",
        "AMX HD",
        "amx_hd_plugin.py",
        "amx_hd",
        "amx_hd",
        None,
        "COM-HVAMX4EDH.h",
        "COM-HVAMX4EDH.dll",
    ),
    PluginSpec(
        "dmmr",
        "DMMR",
        "DMMR",
        "dmmr_plugin.py",
        "dmmr",
        "dmmr",
        None,
        "COM-DMMR-8.h",
        "COM-DMMR-8.dll",
    ),
    PluginSpec(
        "psu_a",
        "PSU_A",
        "PSU_A",
        "psu_plugin.py",
        "psu",
        "psu",
        "psu",
        "COM-HVPSU2D.h",
        "COM-HVPSU2D.dll",
    ),
    PluginSpec(
        "psu_b",
        "PSU_B",
        "PSU_B",
        "psu_plugin.py",
        "psu",
        "psu",
        "psu",
        "COM-HVPSU2D.h",
        "COM-HVPSU2D.dll",
    ),
    PluginSpec(
        "psu_c",
        "PSU_C",
        "PSU_C",
        "psu_plugin.py",
        "psu",
        "psu",
        "psu",
        "COM-HVPSU2D.h",
        "COM-HVPSU2D.dll",
    ),
    PluginSpec(
        "psu_d",
        "PSU_D",
        "PSU_D",
        "psu_plugin.py",
        "psu",
        "psu",
        "psu",
        "COM-HVPSU2D.h",
        "COM-HVPSU2D.dll",
    ),
    PluginSpec(
        "psu_e",
        "PSU_E",
        "PSU_E",
        "psu_plugin.py",
        "psu",
        "psu",
        "psu",
        "COM-HVPSU2D.h",
        "COM-HVPSU2D.dll",
    ),
)


@pytest.fixture(scope="session")
def plugin_specs() -> tuple[PluginSpec, ...]:
    return PLUGIN_SPECS
