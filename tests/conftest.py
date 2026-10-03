"""Shared immutable plugin contract data for repository-only tests."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import os

import pytest

import explorer_host


@dataclass(frozen=True, slots=True)
class PluginSpec:
    folder: str
    manager_name: str
    title_name: str
    entrypoint: str
    runtime_family: str | None
    icon_stem: str
    sibling_family: str | None
    header: str | None
    dll: str | None
    bundled_files: tuple[str, ...] = ()


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
        "esi",
        "ESI",
        "ESI",
        "esi_plugin.py",
        "esi",
        "esi",
        None,
        "COM-ESI-CTRL.h",
        "COM-ESI-CTRL.dll",
        bundled_files=("_heater_stability.py", "_experiment_guard.py", "_heater_limits.py"),
    ),
    PluginSpec("mscan", "MScan", "MScan", "mscan_plugin.py", None, "mscan", None, None, None),
    PluginSpec("tpg366", "TPG366", "TPG366", "tpg366_plugin.py", None, "tpg366", None, None, None,
               bundled_files=("_runtime/_tpg366.py", "_readout_panel.py", "tpg366.svg", "switch-medium_on.png", "switch-medium_off.png")),
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


_HOSTS = explorer_host.hosts()


def pytest_report_header(config):
    described = ", ".join(f"{where} {version or 'absent'}" for where, version in _HOSTS)
    return f"ESIBD Explorer hosts: {described} (plugins target {explorer_host.TARGET_VERSION})"


def pytest_sessionstart(session):
    wrong = explorer_host.mismatches(_HOSTS)
    if wrong and os.environ.get("ESIBD_REQUIRE_TARGET_HOST") == "1":
        pytest.exit(f"Real-Explorer tests need ESIBD Explorer {explorer_host.TARGET_VERSION}; found "
                    + "; ".join(wrong), returncode=4)


def pytest_terminal_summary(terminalreporter):
    wrong = explorer_host.mismatches(_HOSTS)
    if wrong:
        terminalreporter.write_line(
            f"WARNING: real-Explorer tests ran against {'; '.join(wrong)}, not the targeted "
            f"{explorer_host.TARGET_VERSION}: they do not validate the release host "
            "(see README, Running Tests).", yellow=True)


def pytest_collection_modifyitems(config, items):
    """Mark tests that start a subprocess (real Explorer/Qt probes) as slow."""
    sources: dict = {}
    for item in items:
        function = getattr(item, "function", None)
        if function is None:
            continue
        if function not in sources:
            try:
                sources[function] = "subprocess.run(" in inspect.getsource(function)
            except (OSError, TypeError):
                sources[function] = False
        if sources[function]:
            item.add_marker(pytest.mark.slow)
