"""Fixture-only integrity checks for native deployment and legacy retirement."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"native_build_test_{name}", ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILD = load_tool("build_native_workers")
LEGACY = load_tool("vendor_esi_python")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_manifest(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="ascii")


@pytest.fixture
def native_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(BUILD, "ROOT", tmp_path)
    monkeypatch.setattr(BUILD, "_sources", lambda: pytest.fail("check_sources=False must not read native sources"))
    source_hash, lock_hash = sha(b"raw native source"), sha(b"original Cargo.lock")
    manifests = {}
    for family, folders in BUILD.FOLDERS.items():
        for folder in folders:
            directory = tmp_path / folder / "native"
            directory.mkdir(parents=True)

            def asset(filename):
                data = f"fixture:{family}:{filename}\n".encode("ascii")
                (directory / filename).write_bytes(data)
                return {"file": filename, "sha256": sha(data), "size_bytes": len(data)}

            targets = {}
            platforms = ["x86_64-pc-windows-gnu"]
            if family in {"mscan", "tpg366", "transmission"}:
                platforms.append("x86_64-unknown-linux-gnu")
            for target in platforms:
                key, extension = BUILD.TARGETS[target]
                record = asset(f"esibd-{family}-worker{extension}")
                record.update(target=target, features=[family], source_data_sha256=source_hash,
                              cargo_lock_sha256=lock_hash, compiler="fixture rustc")
                if key == "linux-x86_64":
                    (directory / record["file"]).chmod(0o755)
                targets[key] = record
            source = asset("source.zip")
            source.update(source_data_sha256=source_hash, vendored=family in {"mscan", "tpg366"})
            manifest = {"family": family, "protocol": 1, "worker_version": "0.1.0",
                        "targets": targets, "source": source, "third_party_notices": asset("THIRD_PARTY_NOTICES.txt"),
                        "source_data_sha256": source_hash, "cargo_lock_sha256": lock_hash}
            write_manifest(directory / "manifest.json", manifest)
            manifests[folder] = manifest
    return tmp_path, manifests


def assert_rejected(folder="esi"):
    failures = BUILD.check(check_sources=False)
    assert failures and any(message.startswith(f"{folder}:") for message in failures)


def test_all_fifteen_mini_deployments_pass_without_source_or_pe_inspection(native_tree):
    _, manifests = native_tree
    assert len(manifests) == 15
    assert sum("linux-x86_64" in value["targets"] for value in manifests.values()) == 3
    assert BUILD.check(check_sources=False) == []


@pytest.mark.parametrize("kind", ["source", "third_party_notices", "binary"])
def test_asset_tampering_is_detected_even_with_unchanged_size(native_tree, kind):
    root, manifests = native_tree
    record = manifests["esi"]["targets"]["windows-x86_64"] if kind == "binary" else manifests["esi"][kind]
    path = root / "esi/native" / record["file"]
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    assert_rejected()


@pytest.mark.parametrize("keys,value", [
    (("family",), "psu"), (("protocol",), 2), (("worker_version",), "9.0.0"),
    (("targets",), {}), (("source",), None), (("third_party_notices",), []),
    (("source_data_sha256",), None), (("cargo_lock_sha256",), "short"),
    (("source", "source_data_sha256"), sha(b"stale")), (("source", "vendored"), True),
    (("targets", "windows-x86_64"), []),
    (("targets", "windows-x86_64", "file"), None),
    (("targets", "windows-x86_64", "file"), "../outside.exe"),
    (("targets", "windows-x86_64", "features"), ["esi", "test-backend"]),
    (("targets", "windows-x86_64", "cargo_lock_sha256"), sha(b"stale")),
    (("targets", "windows-x86_64", "source_data_sha256"), sha(b"stale")),
    (("targets", "windows-x86_64", "target"), "x86_64-unknown-linux-gnu"),
    (("targets", "windows-x86_64", "target"), []),
])
def test_malformed_or_stale_manifest_records_return_failures(native_tree, keys, value):
    root, manifests = native_tree
    manifest = manifests["esi"]
    record = manifest
    for key in keys[:-1]:
        record = record[key]
    record[keys[-1]] = value
    write_manifest(root / "esi/native/manifest.json", manifest)
    assert_rejected()


@pytest.mark.parametrize("data", [b"[]", b"null", b"{bad json"])
def test_top_level_list_null_or_invalid_json_is_rejected(native_tree, data):
    root, _ = native_tree
    (root / "esi/native/manifest.json").write_bytes(data)
    assert_rejected()


def test_source_archive_digest_is_not_a_binary_or_raw_source_digest(native_tree):
    root, manifests = native_tree
    manifest = manifests["esi"]
    assert len({manifest["source"]["sha256"], manifest["source_data_sha256"],
                manifest["targets"]["windows-x86_64"]["sha256"]}) == 3
    manifest["targets"]["windows-x86_64"]["sha256"] = manifest["source"]["sha256"]
    write_manifest(root / "esi/native/manifest.json", manifest)
    assert_rejected()


def test_live_source_mismatch_is_checked_only_when_requested(native_tree, monkeypatch):
    monkeypatch.setattr(BUILD, "_sources", lambda: SimpleNamespace(source_data_sha256=lambda: sha(b"new native source")))
    assert BUILD.check(check_sources=False) == []
    failures = BUILD.check(check_sources=True)
    assert sum("stale native source provenance" in failure for failure in failures) == 15


@pytest.mark.parametrize("damage", ["missing-linux", "not-executable"])
def test_host_worker_target_and_execute_permission_are_required(native_tree, damage):
    root, manifests = native_tree
    manifest = manifests["mscan"]
    record = manifest["targets"]["linux-x86_64"]
    if damage == "missing-linux":
        del manifest["targets"]["linux-x86_64"]
        write_manifest(root / "mscan/native/manifest.json", manifest)
    else:
        (root / "mscan/native" / record["file"]).chmod(0o644)
    assert_rejected("mscan")


def test_symlinked_plugin_parent_is_refused(native_tree):
    root, _ = native_tree
    plugin = root / "esi"
    plugin.rename(root / "real-esi")
    plugin.symlink_to(root / "real-esi", target_is_directory=True)
    assert_rejected()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO required")
def test_fifo_asset_is_rejected_without_opening_or_hanging(native_tree):
    root, _ = native_tree
    path = root / "esi/native/source.zip"
    path.unlink()
    os.mkfifo(path)
    script = ("import importlib.util,json,pathlib; "
              f"s=importlib.util.spec_from_file_location('build_check',{str(ROOT / 'tools/build_native_workers.py')!r}); "
              "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
              f"m.ROOT=pathlib.Path({str(root)!r}); print(json.dumps(m.check(check_sources=False)))")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert any(message.startswith("esi:") for message in json.loads(result.stdout))


@pytest.fixture
def legacy_tree(tmp_path):
    directory = tmp_path / "private-python"
    directory.mkdir()
    files = {}
    for name in ("python.exe", "python314.dll", "python314.zip", "python314._pth", "LICENSE.txt"):
        data = f"legacy fixture:{name}\n".encode("ascii")
        (directory / name).write_bytes(data)
        files[name] = {"bytes": len(data), "sha256": sha(data)}
    manifest = {"version": LEGACY.VERSION, "architecture": "amd64", "source_url": LEGACY.ARCHIVE_URL,
                "archive_sha256": LEGACY.ARCHIVE_SHA256, "files": files,
                "uncompressed_bytes": sum(record["bytes"] for record in files.values())}
    write_manifest(directory / "manifest.json", manifest)
    return directory, manifest


@pytest.mark.parametrize("damage", ["modified", "unexpected", "version", "archive", "source", "symlink", "manifest-symlink"])
def test_retirement_refuses_untrusted_bundles_without_partial_deletion(legacy_tree, tmp_path, damage):
    directory, manifest = legacy_tree
    if damage == "modified":
        (directory / "python.exe").write_bytes(b"changed")
    elif damage == "unexpected":
        (directory / "unexpected").write_bytes(b"keep")
    elif damage in {"version", "archive", "source"}:
        key = {"version": "version", "archive": "archive_sha256", "source": "source_url"}[damage]
        manifest[key] = "different"
        write_manifest(directory / "manifest.json", manifest)
    else:
        name = "manifest.json" if damage == "manifest-symlink" else "python.exe"
        path = directory / name
        outside = tmp_path / f"external-{name}"
        path.rename(outside)
        path.symlink_to(outside)
    before = {path.name for path in directory.iterdir()}
    with pytest.raises(ValueError):
        LEGACY.retire(directory)
    assert {path.name for path in directory.iterdir()} == before


def test_retirement_removes_only_a_fully_hash_verified_mini_bundle(legacy_tree):
    directory, manifest = legacy_tree
    for name, record in manifest["files"].items():
        assert sha((directory / name).read_bytes()) == record["sha256"]
    assert LEGACY.retire(directory) == manifest
    assert not directory.exists()
