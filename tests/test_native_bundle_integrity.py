"""Every standalone plugin ships its exact native worker and corresponding sources."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tomllib
import zipfile

import pytest

from conftest import PLUGIN_SPECS, PluginSpec


ROOT = Path(__file__).resolve().parents[1]
PORTABLE_FAMILIES = frozenset({"mscan", "tpg366", "transmission"})
GPL_FAMILIES = frozenset({"mscan", "tpg366"})
SOURCE_PLATFORMS = ("x86_64-pc-windows-gnu", "x86_64-unknown-linux-gnu")
TARGET_TRIPLES = {"windows-x86_64": {"x86_64-pc-windows-gnu", "x86_64-pc-windows-msvc"},
                  "linux-x86_64": {"x86_64-unknown-linux-gnu"}}


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


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def family_for(spec: PluginSpec) -> str:
    return spec.runtime_family or spec.folder


def supervisor_for(spec: PluginSpec) -> str:
    return "_runtime/_native_worker.py" if family_for(spec) in PORTABLE_FAMILIES else "vendor/runtime/_native_worker.py"


def target_keys(spec: PluginSpec) -> tuple[str, ...]:
    return ("windows-x86_64", "linux-x86_64") if family_for(spec) in PORTABLE_FAMILIES else ("windows-x86_64",)


def assert_pe64(data: bytes) -> None:
    assert len(data) >= 64 and data[:2] == b"MZ", "worker is not a PE executable"
    offset, = struct.unpack_from("<I", data, 0x3C)
    assert 64 <= offset <= len(data) - 24, "truncated PE header"
    assert data[offset:offset + 4] == b"PE\0\0", "invalid PE signature"
    machine, sections = struct.unpack_from("<HH", data, offset + 4)
    optional_size, characteristics = struct.unpack_from("<HH", data, offset + 20)
    assert machine == 0x8664 and sections > 0, "worker must use AMD64 PE64"
    assert optional_size >= 112 and offset + 24 + optional_size <= len(data), "truncated PE64 optional header"
    magic, = struct.unpack_from("<H", data, offset + 24)
    assert magic == 0x20B, "worker must use PE32+ (64-bit)"
    assert characteristics & 0x2 and not characteristics & 0x2000, "worker must be an executable, not a DLL"


def assert_elf64(data: bytes) -> None:
    assert len(data) >= 64 and data[:4] == b"\x7fELF", "worker is not an ELF executable"
    assert data[4:7] == b"\x02\x01\x01", "worker must use little-endian ELF64"
    kind, machine, version = struct.unpack_from("<HHI", data, 16)
    assert kind in {2, 3} and machine == 62 and version == 1, "worker must use x86-64 ELF"


def _assert_digest(value) -> None:
    assert isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "invalid SHA-256 metadata"


def _asset(record, filename, read_asset) -> bytes:
    assert isinstance(record, dict) and record.get("file") == filename, f"missing or invalid {filename} record"
    _assert_digest(record["sha256"])
    assert type(record["size_bytes"]) is int and record["size_bytes"] > 0, f"invalid {filename} size"
    data = read_asset(filename)
    assert len(data) == record["size_bytes"], f"{filename}: size mismatch"
    assert sha(data) == record["sha256"], f"{filename}: SHA-256 mismatch"
    return data


def _record_map(records) -> dict:
    assert isinstance(records, list) and records, "missing source file inventory"
    names = [record["path"] for record in records]
    assert names == sorted(set(names)), "source inventory must be sorted and unique"
    for record in records:
        name = record["path"]
        path = PurePosixPath(name)
        assert name and not path.is_absolute() and "\\" not in name and "\0" not in name
        assert not any(part in {"", ".", ".."} for part in name.split("/")) and ":" not in path.parts[0]
        _assert_digest(record["sha256"])
        assert type(record["size_bytes"]) is int and record["size_bytes"] >= 0
        assert record["mode"] in {0o644, 0o755}
    return {record["path"]: record for record in records}


def _assert_source_archive(data: bytes, notices: bytes, manifest: dict) -> None:
    family = manifest["family"]
    vendored = family in GPL_FAMILIES
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        assert names == sorted(set(names)), "source ZIP paths must be sorted and unique"
        assert 0 < len(names) <= 100_000
        assert sum(info.file_size for info in archive.infolist()) <= 512 * 1024 * 1024
        source = json.loads(archive.read("SOURCE_MANIFEST.json"))
        assert type(source["format_version"]) is int and source["format_version"] == 1 and source["family"] == family
        assert source["worker_version"] == manifest["worker_version"]
        assert source["features"] == [family] and source["platforms"] == list(SOURCE_PLATFORMS)
        assert source["vendored"] is vendored
        assert source["source_data_sha256"] == manifest["source_data_sha256"]
        assert source["cargo_lock_sha256"] == manifest["cargo_lock_sha256"]
        records = _record_map(source["files"])
        originals = _record_map(source["own_source_files"])
        assert set(names) == set(records) | {"SOURCE_MANIFEST.json"}
        assert sha(json_bytes(source["files"])) == source["archive_data_sha256"]
        assert sha(json_bytes(source["own_source_files"])) == source["source_data_sha256"]
        hashes = {}
        for info in archive.infolist():
            assert info.create_system == 3 and stat.S_ISREG(info.external_attr >> 16), info.filename
            assert not info.is_dir() and not info.extra and not info.comment, info.filename
            assert info.date_time == (1980, 1, 1, 0, 0, 0) and info.file_size <= 128 * 1024 * 1024
            payload = archive.read(info)
            hashes[info.filename] = sha(payload)
            if info.filename != "SOURCE_MANIFEST.json":
                record = records[info.filename]
                assert hashes[info.filename] == record["sha256"], info.filename
                assert info.file_size == record["size_bytes"], info.filename
                assert stat.S_IMODE(info.external_attr >> 16) == record["mode"], info.filename
        assert not any(name.startswith(("target/", "worker/tests/", "python/")) for name in names)
        assert not any(name.endswith(".pyc") or "__pycache__" in name for name in names)
        # Complete registry crates can retain upstream binary test fixtures.
        assert not any(name.endswith((".exe", ".dll", ".pyd")) for name in names if not name.startswith("vendor/"))
        assert {"Cargo.toml", "Cargo.lock", "worker/Cargo.toml", "worker/src/main.rs", "worker/src/lib.rs",
                "worker/src/ffi.rs", "worker/src/context.rs", f"worker/src/{family}.rs",
                "worker/src/generated/abi.json", "worker/src/generated/error_codes.json", "LICENSES.md",
                "licenses/Explorer-GPL-2.0-or-later.txt", "licenses/Transmission-MIT.txt",
                "BUILD.md", "THIRD_PARTY_NOTICES.txt"} <= set(records)
        replacements = {"Cargo.lock": "provenance/Cargo.lock", "worker/Cargo.toml": "provenance/worker-Cargo.toml"}
        for name, record in originals.items():
            archived = replacements.get(name, name) if vendored else name
            assert archived in records, f"missing original {name}"
            assert {key: record[key] for key in ("sha256", "size_bytes", "mode")} == {
                key: records[archived][key] for key in ("sha256", "size_bytes", "mode")}, name
        original_lock = archive.read("provenance/Cargo.lock" if vendored else "Cargo.lock")
        assert sha(original_lock) == source["cargo_lock_sha256"]
        assert hashes["Cargo.lock"] == source["build_lock_sha256"]
        cargo = tomllib.loads(archive.read("worker/Cargo.toml").decode("utf-8"))
        assert cargo["package"]["license-file"] == "../LICENSES.md" and "license" not in cargo["package"]
        assert cargo["package"]["version"] == manifest["worker_version"] and family in cargo["features"]
        build = archive.read("BUILD.md").decode("utf-8")
        assert f"--no-default-features --features {family}" in build
        assert all(f"--target {platform}" in build for platform in SOURCE_PLATFORMS)
        assert archive.read("THIRD_PARTY_NOTICES.txt") == notices
        assert archive.read("LICENSES.md") in notices
        lock = tomllib.loads(original_lock.decode("utf-8"))
        checksums = {(package["name"], package["version"], package.get("source")): package.get("checksum")
                     for package in lock["package"]}
        dependencies = source["dependencies"]
        assert dependencies and len({(item["name"], item["version"]) for item in dependencies}) == len(dependencies)
        for dependency in dependencies:
            name, version = dependency["name"], dependency["version"]
            assert dependency["source"] == "registry+https://github.com/rust-lang/crates.io-index"
            assert dependency["cargo_checksum"] == checksums[(name, version, dependency["source"])]
            _assert_digest(dependency["cargo_checksum"])
            assert dependency["registry_url"] == f"https://crates.io/crates/{name}/{version}"
            assert dependency["download_url"] == f"https://static.crates.io/crates/{name}/{name}-{version}.crate"
            assert set(dependency["platforms"]) <= set(SOURCE_PLATFORMS)
            assert dependency["role"] == ("active" if dependency["platforms"] else "resolution-only")
            assert dependency["license_files"], f"missing license texts: {name}"
            for license_record in dependency["license_files"]:
                filename = license_record["file"]
                assert filename.startswith(f"third_party/licenses/{name}-{version}/")
                assert hashes[filename] == license_record["sha256"]
                assert records[filename]["size_bytes"] == license_record["size_bytes"]
                assert archive.read(filename) in notices, f"truncated upstream license: {filename}"
            if vendored:
                prefix = f"vendor/{name}-{version}/"
                checksum_file = prefix + ".cargo-checksum.json"
                checksum = json.loads(archive.read(checksum_file))
                assert checksum["package"] == dependency["cargo_checksum"]
                assert "Cargo.toml" in checksum["files"], f"missing vendored crate source: {name}"
                assert checksum["files"] == {filename.removeprefix(prefix): digest for filename, digest in hashes.items()
                                             if filename.startswith(prefix) and filename != checksum_file}
        if vendored:
            config = tomllib.loads(archive.read(".cargo/config.toml").decode("utf-8"))
            assert config["source"]["crates-io"]["replace-with"] == "vendored-sources"
            assert config["source"]["vendored-sources"]["directory"] == "vendor" and config["net"]["offline"] is True
            assert cargo["features"]["default"] == []
            assert all(not value for feature, value in cargo["features"].items() if feature != family)
            validated = source["validated_dependency_versions"]
            assert set(validated) == set(SOURCE_PLATFORMS)
            for platform, selected in validated.items():
                assert {(item["name"], item["version"], item["source"]) for item in selected} == {
                    (item["name"], item["version"], item["source"]) for item in dependencies if platform in item["platforms"]}
        else:
            assert not any(name.startswith("vendor/") for name in names)
            assert source["validated_dependency_versions"] is None


def assert_native_payload(spec: PluginSpec, read_asset, *, expected_source_data=None, expected_cargo_lock=None) -> dict:
    """Validate native-directory bytes, also usable against an outer release ZIP."""
    family = family_for(spec)
    manifest = json.loads(read_asset("manifest.json"))
    assert manifest["family"] == family and type(manifest["protocol"]) is int and manifest["protocol"] == 1
    assert manifest["worker_version"] == "0.1.0" and manifest["validation"] == "hardware-not-validated"
    assert set(manifest["targets"]) == set(target_keys(spec)), f"{spec.folder}: incorrect deployment targets"
    _assert_digest(manifest["source_data_sha256"])
    _assert_digest(manifest["cargo_lock_sha256"])
    if expected_source_data is not None:
        assert manifest["source_data_sha256"] == expected_source_data, f"{spec.folder}: stale raw source provenance"
    if expected_cargo_lock is not None:
        assert manifest["cargo_lock_sha256"] == expected_cargo_lock, f"{spec.folder}: stale Cargo.lock provenance"
    for target, record in manifest["targets"].items():
        assert record["target"] in TARGET_TRIPLES[target] and record["features"] == [family]
        assert record["source_data_sha256"] == manifest["source_data_sha256"]
        assert record["cargo_lock_sha256"] == manifest["cargo_lock_sha256"]
        assert isinstance(record["compiler"], str) and record["compiler"].startswith("rustc ")
        filename = f"esibd-{family}-worker" + (".exe" if target == "windows-x86_64" else "")
        data = _asset(record, filename, read_asset)
        (assert_pe64 if target == "windows-x86_64" else assert_elf64)(data)
    source = _asset(manifest["source"], "source.zip", read_asset)
    notices = _asset(manifest["third_party_notices"], "THIRD_PARTY_NOTICES.txt", read_asset)
    assert manifest["source"]["source_data_sha256"] == manifest["source_data_sha256"]
    assert manifest["source"]["vendored"] is (family in GPL_FAMILIES)
    _assert_source_archive(source, notices, manifest)
    return manifest


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
