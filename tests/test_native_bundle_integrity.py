"""Every standalone plugin ships its exact native worker and corresponding sources."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys

import pytest

from conftest import PLUGIN_SPECS
from support.native import assert_elf64, assert_native_payload, assert_pe64, family_for, sha, supervisor_for, target_keys


ROOT = Path(__file__).resolve().parents[1]


def _git_ignored(paths):
    result = subprocess.run(["git", "check-ignore", "--no-index", "--", *paths],
                            cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode in {0, 1}, result.stderr
    return result.stdout.splitlines()


def test_corresponding_sources_can_be_tracked_but_release_zips_remain_ignored():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("Git ignore policy requires a repository checkout")
    required = [f"{spec.folder}/native/source.zip" for spec in PLUGIN_SPECS]
    assert not _git_ignored(required), "Required corresponding sources must not be ignored"
    generated = "esibd-explorer-plugins-v9.9.9.zip"
    assert _git_ignored([generated]) == [generated]


@pytest.fixture(scope="module")
def source_provenance():
    spec = importlib.util.spec_from_file_location("_native_packaging_source_hash", ROOT / "tools/bundle_native_sources.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module.source_data_sha256(), sha((ROOT / "native/Cargo.lock").read_bytes())
    finally:
        sys.modules.pop(spec.name, None)


# Slash-delimited IDs retain all 15 folders under the default sibling filter.


@pytest.mark.parametrize("spec", PLUGIN_SPECS, ids=lambda spec: f"{spec.folder}/native")
def test_every_native_bundle_matches_binary_source_and_notice_provenance(spec, source_provenance):
    directory = ROOT / spec.folder / "native"

    def read_asset(filename):
        path = directory / filename
        assert path.is_file() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode), path
        return path.read_bytes()

    manifest = assert_native_payload(spec, read_asset, expected_source_data=source_provenance[0],
                                     expected_cargo_lock=source_provenance[1])
    assert {path.name for path in directory.iterdir()} == {
        "manifest.json", "source.zip", "THIRD_PARTY_NOTICES.txt",
        *(record["file"] for record in manifest["targets"].values())}
    for target, record in manifest["targets"].items():
        assert stat.S_IMODE((directory / record["file"]).stat().st_mode) == (0o755 if target == "linux-x86_64" else 0o644)


@pytest.mark.parametrize("spec", PLUGIN_SPECS, ids=lambda spec: f"{spec.folder}/native")
def test_plugin_tree_is_regular_and_supervision_is_plugin_local(spec):
    directory = ROOT / spec.folder
    for path in (directory, *directory.rglob("*")):
        assert not path.is_symlink(), path
        assert path.is_dir() or stat.S_ISREG(path.stat().st_mode), path
    assert not (directory / "vendor/python").exists()
    assert (directory / supervisor_for(spec)).read_bytes() == (ROOT / "native/python/_native_worker.py").read_bytes()


SELECTIONS = tuple((spec, target) for spec in PLUGIN_SPECS for target in target_keys(spec))


@pytest.mark.parametrize("spec,target", SELECTIONS, ids=[f"{spec.folder}/{target}" for spec, target in SELECTIONS])
def test_isolated_plugin_selects_its_hashed_worker_without_python_or_path_fallback(tmp_path, monkeypatch, spec, target):
    isolated = tmp_path / spec.folder
    shutil.copytree(ROOT / spec.folder / "native", isolated / "native")
    supervisor = isolated / supervisor_for(spec)
    supervisor.parent.mkdir(parents=True)
    shutil.copy2(ROOT / spec.folder / supervisor_for(spec), supervisor)
    name = f"_native_isolated_{spec.folder}"
    module_spec = importlib.util.spec_from_file_location(name, supervisor)
    module = importlib.util.module_from_spec(module_spec)
    monkeypatch.setitem(sys.modules, name, module)
    before = list(sys.path)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", "win32" if target == "windows-x86_64" else "linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("ESIBD_ESI_WORKER_PYTHON", "external-python-is-forbidden")

    def no_launch(*args, **kwargs):
        pytest.fail("worker selection must not launch a process or probe Python")

    monkeypatch.setattr(module.subprocess, "Popen", no_launch)
    manifest = json.loads((isolated / "native/manifest.json").read_text(encoding="utf-8"))
    worker = module.executable(isolated, family_for(spec))
    assert worker == isolated / "native" / manifest["targets"][target]["file"]
    assert sys.path == before
    worker.write_bytes(worker.read_bytes() + b"corrupt")
    with pytest.raises(RuntimeError, match="integrity check failed"):
        module.executable(isolated, family_for(spec))
    worker.unlink()
    with pytest.raises(FileNotFoundError, match="Missing or non-regular native worker"):
        module.executable(isolated, family_for(spec))


@pytest.mark.parametrize("data", [b"", b"MZ" + bytes(62), b"MZ" + bytes(58) + struct.pack("<I", 0xFFFFFFF0)],
                         ids=["empty", "missing-pe", "offset-overflow"])


def test_pe_parser_rejects_missing_or_truncated_headers(data):
    with pytest.raises(AssertionError):
        assert_pe64(data)


@pytest.mark.parametrize("machine,magic,characteristics", [(0x14C, 0x20B, 2), (0x8664, 0x10B, 2), (0x8664, 0x20B, 0x2002)],
                         ids=["x86", "pe32", "dll"])


def test_pe_parser_rejects_x86_pe32_and_dlls(machine, magic, characteristics):
    data = bytearray(64 + 24 + 112)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<HH", data, 68, machine, 1)
    struct.pack_into("<HHH", data, 84, 112, characteristics, magic)
    with pytest.raises(AssertionError):
        assert_pe64(data)


@pytest.mark.parametrize("ident,machine", [(b"\x01\x01\x01", 62), (b"\x02\x02\x01", 62), (b"\x02\x01\x01", 183)],
                         ids=["32bit", "big-endian", "arm"])


def test_elf_parser_rejects_32bit_big_endian_and_arm(ident, machine):
    data = bytearray(64)
    data[:7] = b"\x7fELF" + ident
    struct.pack_into("<HHI", data, 16, 3, machine, 1)
    with pytest.raises(AssertionError):
        assert_elf64(data)
