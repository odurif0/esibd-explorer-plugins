"""Native worker binaries and transports shared by the native tests."""
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
import tomllib
import zipfile

import pytest

from conftest import PluginSpec

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("native_transport_test", ROOT / "native/python/_native_worker.py")
TRANSPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRANSPORT)


@pytest.fixture(scope="module")
def worker_binary():
    if shutil.which("cargo") is None:
        pytest.skip("Native process tests require local Rust toolchain")
    subprocess.run(["cargo", "build", "--offline", "--manifest-path", str(ROOT / "native/Cargo.toml"),
                    "--target-dir", str(ROOT / "native/target/transport-tests"),
                    "--no-default-features", "--features", "test-backend"], check=True, capture_output=True, timeout=120)
    return ROOT / "native/target/transport-tests/debug/esibd-native-worker"


class NativeDevice:
    NO_ERR = 0
    _backend_mode = "process"
    _open_failed = _failed_open_released = False

    def __init__(self, backend, *, opening=False):
        self._backend = backend
        self._opening_in_progress = opening

    @property
    def connected(self):
        return not self._backend.closed

    @property
    def _transport_poisoned(self):
        return self._backend.closed

    @property
    def _dll_port_claimed(self):
        return self._backend._process.poll() is None

    def disconnect(self, **kwargs):
        raise AssertionError("A dead/hung process must not receive a new hardware command")

    def close(self):
        return self._backend.close(grace_s=0)


def proxy(binary, tmp_path, family):
    worker = TRANSPORT.NativeWorkerProxy(ROOT, "test", {}, log_dir=tmp_path,
                                        command=[str(binary), "--family", "test"], stop_grace_s=.05)
    # Only the GUI device marker is adapted; the child is explicitly the fault
    # backend, not evidence that an instrument DLL or real COM port was tested.
    worker.family = family
    return worker


PORTABLE_FAMILIES = frozenset({"mscan", "tpg366", "transmission"})
GPL_FAMILIES = frozenset({"mscan", "tpg366"})
SOURCE_PLATFORMS = ("x86_64-pc-windows-gnu", "x86_64-unknown-linux-gnu")
TARGET_TRIPLES = {"windows-x86_64": {"x86_64-pc-windows-gnu", "x86_64-pc-windows-msvc"},
                  "linux-x86_64": {"x86_64-unknown-linux-gnu"}}


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
