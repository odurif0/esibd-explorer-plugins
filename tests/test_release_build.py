"""tools/build_release.py produces a policy-compliant, reproducible release archive."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import zipfile

from conftest import PLUGIN_SPECS
from test_release_archive_integrity import validate_member_names

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location("_build_release_under_test", ROOT / "tools/build_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_archive_holds_exactly_the_plugin_manifest(tmp_path):
    tool = _tool()
    assert tool.plugin_folders() == sorted(spec.folder for spec in PLUGIN_SPECS)
    archive = tool.build("9.9.9", tmp_path, allow_dirty=True)
    assert archive.name == "esibd-explorer-plugins-v9.9.9.zip"
    with zipfile.ZipFile(archive) as built:
        names = built.namelist()
        assert validate_member_names(names, PLUGIN_SPECS) == []
        assert not any(name.endswith("/") for name in names)
        assert all(info.external_attr >> 16 == 0o100644 for info in built.infolist())


def test_rebuilding_the_same_commit_is_byte_identical(tmp_path):
    tool = _tool()
    digests = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        digests.append(hashlib.sha256(tool.build("9.9.9", tmp_path / name, allow_dirty=True).read_bytes()).digest())
    assert digests[0] == digests[1]
