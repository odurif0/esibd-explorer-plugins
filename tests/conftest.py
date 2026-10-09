"""Shared immutable plugin contract data for repository-only tests."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import os
from pathlib import Path
import re

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


def _native_bundled_files(family: str) -> tuple[str, ...]:
    portable = family in {"mscan", "transmission", "tpg366"}
    supervisor = "_runtime/_native_worker.py" if portable else "vendor/runtime/_native_worker.py"
    return ("native/manifest.json", f"native/esibd-{family}-worker.exe",
            *((f"native/esibd-{family}-worker",) if portable else ()),
            "native/source.zip", "native/THIRD_PARTY_NOTICES.txt", supervisor)


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
        bundled_files=_native_bundled_files("ampr"),
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
        bundled_files=_native_bundled_files("ampr"),
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
        bundled_files=_native_bundled_files("amx"),
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
        bundled_files=_native_bundled_files("amx"),
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
        bundled_files=_native_bundled_files("amx_hd"),
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
        bundled_files=_native_bundled_files("dmmr"),
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
        bundled_files=(*_native_bundled_files("esi"), "_heater_stability.py", "_experiment_guard.py",
                       "_heater_limits.py", "vendor/runtime/esi/_process.py"),
    ),
    PluginSpec("mscan", "MScan", "MScan", "mscan_plugin.py", None, "mscan", None, None, None,
               bundled_files=(*_native_bundled_files("mscan"), "_runtime/_native_scan.py")),
    PluginSpec("transmission", "Transmission", "Transmission", "transmission_plugin.py", None, "transmission", None, None, None,
               bundled_files=(*_native_bundled_files("transmission"), "_runtime/__init__.py", "_runtime/_engine.py",
                              "_runtime/_simulator.py", "_runtime/_beamline.py", "_runtime/_log.py", "_runtime/_native_engine.py")),
    PluginSpec("tpg366", "TPG366", "TPG366", "tpg366_plugin.py", None, "tpg366", None, None, None,
               bundled_files=(*_native_bundled_files("tpg366"), "_runtime/_tpg366.py", "_runtime/_native_link.py",
                              "_readout_panel.py", "tpg366.svg", "switch-medium_on.png", "switch-medium_off.png")),
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
        bundled_files=_native_bundled_files("psu"),
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
        bundled_files=_native_bundled_files("psu"),
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
        bundled_files=_native_bundled_files("psu"),
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
        bundled_files=_native_bundled_files("psu"),
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
        bundled_files=_native_bundled_files("psu"),
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
    if wrong and _USES_HOST:
        terminalreporter.write_line(
            f"WARNING: real-Explorer tests ran against {'; '.join(wrong)}, not the targeted "
            f"{explorer_host.TARGET_VERSION}: they do not validate the release host "
            "(see README, Running Tests).", yellow=True)


_HOST_MARKERS = ('distribution("esibd-explorer")', "distribution('esibd-explorer')",
                 "ESIBD_EXPLORER_SOURCE", "from esibd import")
_USES_HOST = []


# Sibling folders are byte-identical copies of their canonical plugin apart from
# the Device name (test_plugin_family_parity, tools/sync_family_siblings.py), so
# behaviour tests run on the canonical copy only. Tests where the folder or
# plugin name itself matters still run on every copy; --all-siblings runs all.
_SIBLING_ID = re.compile(r"(?:^|[\[\-])(psu_[b-e]|ampr_b|amx_b)(?:$|[\]\-])")
_PER_FOLDER_MODULES = re.compile(
    r"test_(documentation_integrity|error_catalog_integrity|plugin_autonomy|.*_packaging|amx_psu_icons"
    r"|amx_psu_links|plugin_family_parity|family_sync_tool|release_archive_integrity|runtime_logging|mscan_.*)$")


def pytest_addoption(parser):
    parser.addoption("--all-siblings", action="store_true",
                     help="also run behaviour tests on sibling plugin copies (release validation)")


def _sibling_duplicate(item) -> bool:
    callspec = getattr(item, "callspec", None)
    return (callspec is not None and not _PER_FOLDER_MODULES.match(Path(str(item.fspath)).stem)
            and bool(_SIBLING_ID.search(callspec.id)))


def pytest_collection_modifyitems(config, items):
    """Mark subprocess (real Explorer/Qt) tests slow; deselect sibling duplicates."""
    if not config.getoption("--all-siblings"):
        duplicates = [item for item in items if _sibling_duplicate(item)]
        if duplicates:
            config.hook.pytest_deselected(items=duplicates)
            items[:] = [item for item in items if not _sibling_duplicate(item)]
    sources: dict = {}
    modules: dict = {}
    for item in items:
        path = Path(str(item.fspath))
        if path not in modules:
            text = path.read_text(encoding="utf-8") if path.suffix == ".py" else ""
            modules[path] = any(marker in text for marker in _HOST_MARKERS)
        if modules[path] and not _USES_HOST:
            _USES_HOST.append(path)
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
