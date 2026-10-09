"""Deterministic, bounded native source bundles with preserved component notices."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_source_bundle_test", ROOT / "tools/bundle_native_sources.py")
BUNDLE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUNDLE
SPEC.loader.exec_module(BUNDLE)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    if shutil.which("cargo") is None:
        pytest.skip("Source-bundle dependency inventory requires local Cargo")
    directory = tmp_path_factory.mktemp("native-source-bundles")
    source = directory / "source"
    original = BUNDLE._own_sources()
    for name, entry in original.items():
        path = source / "native" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry.data)
        path.chmod(entry.mode)
    products = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(BUNDLE, "ROOT", source)
        for family in ("psu", "mscan", "tpg366", "transmission"):
            destination = directory / family / "native"
            metadata = BUNDLE.bundle_family(family, destination)
            with zipfile.ZipFile(destination / "source.zip") as archive:
                manifest = json.loads(archive.read("SOURCE_MANIFEST.json"))
            products[family] = {"destination": destination, "metadata": metadata, "manifest": manifest}
    return {"products": products, "source": source, "original": original}


def test_reachable_follows_filtered_normal_build_edges_not_package_pool():
    def dependency(package, kind=None):
        return {"pkg": package, "dep_kinds": [{"kind": kind, "target": None}]}

    metadata = {"version": 1, "workspace_members": ["root"],
                "packages": [{"id": name, "name": "esibd-native-worker" if name == "root" else name}
                             for name in ("root", "normal", "build", "dev", "unreachable")],
                "resolve": {"nodes": [
                    {"id": "root", "deps": [dependency("normal"), dependency("build", "build"), dependency("dev", "dev")]},
                    {"id": "normal", "deps": [dependency("build")]},
                    {"id": "build", "deps": []}]}}
    assert BUNDLE._reachable(metadata) == {"root", "normal", "build"}


@pytest.mark.parametrize("family", ["psu", "mscan", "tpg366", "transmission"])
def test_family_archives_are_regular_bounded_and_have_verifiable_provenance(bundles, family):
    product = bundles["products"][family]
    metadata, manifest = product["metadata"], product["manifest"]
    destination = product["destination"]
    assert metadata["source"]["sha256"] == sha((destination / "source.zip").read_bytes())
    assert metadata["third_party_notices"]["sha256"] == sha((destination / "THIRD_PARTY_NOTICES.txt").read_bytes())
    assert metadata["source_data_sha256"] == sha(json_bytes(manifest["own_source_files"]))
    assert manifest["archive_data_sha256"] == sha(json_bytes(manifest["files"]))
    assert manifest["features"] == [family]
    assert manifest["worker_version"] == "0.1.0"
    assert manifest["platforms"] == list(BUNDLE.PLATFORMS)
    assert str(bundles["source"]) not in json.dumps(manifest)
    with zipfile.ZipFile(destination / "source.zip") as archive:
        names = archive.namelist()
        assert names == sorted(set(names))
        assert set(names) == {record["path"] for record in manifest["files"]} | {"SOURCE_MANIFEST.json"}
        assert not any(name.startswith(("target/", "worker/tests/", "python/")) for name in names)
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
        assert not any(Path(name).name.startswith("esibd-") and name.endswith(("worker", "worker.exe")) for name in names)
        for info in archive.infolist():
            assert info.date_time == BUNDLE.ZIP_STAMP
            assert info.create_system == 3 and stat.S_ISREG(info.external_attr >> 16)
            assert not info.extra and not info.comment and not info.is_dir()
            assert BUNDLE._safe_name(info.filename) == info.filename
            assert info.file_size <= BUNDLE.MAX_FILE_BYTES
        assert sum(info.file_size for info in archive.infolist()) <= BUNDLE.MAX_SOURCE_BYTES
        for record in manifest["files"]:
            data = archive.read(record["path"])
            assert sha(data) == record["sha256"] and len(data) == record["size_bytes"]
        assert archive.read("THIRD_PARTY_NOTICES.txt") == (destination / "THIRD_PARTY_NOTICES.txt").read_bytes()
        assert archive.read("worker/src/generated/error_codes.json") == bundles["original"]["worker/src/generated/error_codes.json"].data
        for name, entry in bundles["original"].items():
            original_path = name
            if family in BUNDLE.GPL_FAMILIES:
                original_path = {"worker/Cargo.toml": "provenance/worker-Cargo.toml", "Cargo.lock": "provenance/Cargo.lock"}.get(name, name)
            assert archive.read(original_path) == entry.data


def test_raw_source_identity_is_family_independent_and_recomputable(bundles, monkeypatch):
    monkeypatch.setattr(BUNDLE, "ROOT", bundles["source"])
    live = BUNDLE.source_data_sha256()
    assert {product["metadata"]["source_data_sha256"] for product in bundles["products"].values()} == {live}
    assert live != bundles["products"]["mscan"]["manifest"]["archive_data_sha256"]


def test_zip_bytes_are_reproducible_and_check_detects_tampering(bundles, tmp_path, monkeypatch):
    monkeypatch.setattr(BUNDLE, "ROOT", bundles["source"])
    existing = bundles["products"]["psu"]
    destination = tmp_path / "native"
    metadata = BUNDLE.bundle_family("psu", destination)
    assert metadata == existing["metadata"]
    assert (destination / "source.zip").read_bytes() == (existing["destination"] / "source.zip").read_bytes()
    assert BUNDLE.check_family("psu", destination) == []
    (destination / "THIRD_PARTY_NOTICES.txt").write_text("corrupt notice")
    assert BUNDLE.check_family("psu", destination) == ["psu: missing or stale THIRD_PARTY_NOTICES.txt"]


def test_dependencies_licenses_and_resolver_roles_do_not_become_a_blanket_mit_claim(bundles):
    records = {family: {package["name"]: package for package in product["manifest"]["dependencies"]}
               for family, product in bundles["products"].items()}
    assert records["psu"]["libloading"]["license"] == "ISC"
    assert "serialport" not in records["psu"] and "nalgebra" not in records["psu"]
    assert "nalgebra" not in records["mscan"] and "argmin" not in records["tpg366"]
    assert records["tpg366"]["serialport"]["license"] == "MPL-2.0"
    assert records["transmission"]["nalgebra"]["license"] == "Apache-2.0"
    for package in ("argmin", "argmin-math"):
        assert all(license["source_url"].startswith("https://raw.githubusercontent.com/argmin-rs/argmin/")
                   and license["upstream_revision"] in license["source_url"]
                   for license in records["transmission"][package]["license_files"])
    assert "Unicode-3.0" in records["psu"]["unicode-ident"]["license"]
    assert any(file["file"].endswith("LICENSE-UNICODE") for file in records["psu"]["unicode-ident"]["license_files"])
    for family in BUNDLE.GPL_FAMILIES:
        product = bundles["products"][family]
        assert product["metadata"]["source"]["vendored"] is True
        assert any(package["role"] == "resolution-only" and not package["platforms"]
                   for package in product["manifest"]["dependencies"])
        with zipfile.ZipFile(product["destination"] / "source.zip") as archive:
            assert ".cargo/config.toml" in archive.namelist()
            active = tomllib.loads(archive.read("worker/Cargo.toml").decode())
            assert active["features"]["default"] == []
            assert all(not value for name, value in active["features"].items() if name != family)
            assert "argmin" not in active["dependencies"] and "nalgebra" not in active["dependencies"]
            for platform, selected in product["manifest"]["validated_dependency_versions"].items():
                expected = {(package["name"], package["version"], package["source"])
                            for package in product["manifest"]["dependencies"] if platform in package["platforms"]}
                assert {(package["name"], package["version"], package["source"]) for package in selected} == expected
    for family in ("psu", "transmission"):
        with zipfile.ZipFile(bundles["products"][family]["destination"] / "source.zip") as archive:
            assert not any(name.startswith("vendor/") for name in archive.namelist())


def test_gpl_bundle_builds_offline_without_developer_cargo_cache(bundles, tmp_path):
    archive_path = bundles["products"]["mscan"]["destination"] / "source.zip"
    directory = tmp_path / "source"
    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(directory)
    cargo_home = tmp_path / "empty-cargo-home"
    assert not cargo_home.exists()
    environment = os.environ.copy()
    environment.update(CARGO_HOME=str(cargo_home), CARGO_TARGET_DIR=str(tmp_path / "isolated-target"))
    result = subprocess.run(["cargo", "build", "--offline", "--locked", "--no-default-features",
                             "--features", "mscan", "--target", "x86_64-unknown-linux-gnu"],
                            cwd=directory, env=environment, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "isolated-target/x86_64-unknown-linux-gnu/debug/esibd-native-worker").is_file()
    assert not any(path.is_file() for kind in ("index", "cache", "src")
                   for path in (cargo_home / "registry" / kind).rglob("*"))


def test_preserved_project_license_bytes_and_cargo_metadata():
    native = ROOT / "native"
    assert (native / "licenses/Explorer-GPL-2.0-or-later.txt").read_bytes() == (ROOT / "mscan/LICENSE").read_bytes() == (ROOT / "tpg366/LICENSE").read_bytes()
    assert (native / "licenses/Transmission-MIT.txt").read_bytes() == (ROOT / "transmission/LICENSE").read_bytes()
    manifest = tomllib.loads((native / "worker/Cargo.toml").read_text())
    assert manifest["package"]["license-file"] == "../LICENSES.md"
    assert manifest["package"]["rust-version"] == "1.89"
    assert "license" not in manifest["package"]


@pytest.mark.parametrize("family", ["test-backend", "../psu", "", "psu,test-backend"])
def test_unsupported_or_test_feature_cannot_be_packaged(family, tmp_path):
    with pytest.raises(ValueError, match="Unsupported"):
        BUNDLE.bundle_family(family, tmp_path / "native")
    assert not (tmp_path / "native").exists()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "x/../outside", "x//y", "x/./y", "C:outside", "x\\y", "x\0y"])
def test_archive_path_boundaries(name):
    with pytest.raises(ValueError, match="Unsafe bundle path"):
        BUNDLE._safe_name(name)


def test_symlinks_and_non_regular_or_oversized_sources_are_refused(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.write_text("original")
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        BUNDLE._read(link)
    destination = tmp_path / "native"
    destination.mkdir()
    (destination / "source.zip").symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        BUNDLE.bundle_family("psu", destination)
    assert outside.read_text() == "original"
    with pytest.raises(ValueError, match="regular source"):
        BUNDLE._read(destination)
    monkeypatch.setattr(BUNDLE, "MAX_FILE_BYTES", 3)
    with pytest.raises(ValueError, match="size limit"):
        BUNDLE._read(outside)


def test_declared_crate_license_must_stay_inside_package(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "Cargo.toml").write_text("[package]\nname='example'\nversion='0.1.0'\n")
    (tmp_path / "LICENSE").write_text("outside")
    with pytest.raises(ValueError, match="escapes its package"):
        BUNDLE._license_files({"manifest_path": str(package / "Cargo.toml"), "license_file": "../LICENSE"})
