"""Shared runtime helper files must stay byte-identical across all plugins.

Each plugin bundles its runtime privately (one plugin = one autonomous
device); these three files are the shared infrastructure duplicated into
every tree. Any divergence must be replicated to all 12 plugins on purpose.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PLUGIN_FOLDERS = [
    "ampr_a",
    "ampr_b",
    "amx_a",
    "amx_b",
    "amx_hd",
    "dmmr",
    "esi",
    "psu_a",
    "psu_b",
    "psu_c",
    "psu_d",
    "psu_e",
]

SHARED_RUNTIME_FILES = [
    "vendor/runtime/_driver_common.py",
    "vendor/runtime/_controller_process.py",
    "vendor/runtime/_process_proxy_support.py",
]

CANONICAL_PLUGIN = "amx_a"


def _shared_file(plugin: str, relative_path: str) -> Path:
    path = ROOT / plugin / relative_path
    assert path.is_file(), f"Missing shared runtime file: {path}"
    return path


def test_plugin_folder_list_matches_bundle():
    folders = sorted(
        entry.name
        for entry in ROOT.iterdir()
        if entry.is_dir()
        and not entry.name.startswith((".", "_"))
        and (entry / "vendor" / "runtime").is_dir()
    )
    assert folders == sorted(PLUGIN_FOLDERS)


@pytest.mark.parametrize("relative_path", SHARED_RUNTIME_FILES)
def test_shared_runtime_file_matches_canonical(relative_path):
    canonical = _shared_file(CANONICAL_PLUGIN, relative_path).read_bytes()
    for plugin in PLUGIN_FOLDERS:
        content = _shared_file(plugin, relative_path).read_bytes()
        assert content == canonical, (
            f"{plugin}/{relative_path} diverges from the canonical "
            f"{CANONICAL_PLUGIN} copy. Replicate the change to all 12 plugins."
        )
