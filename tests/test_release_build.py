"""tools/build_release.py produces a policy-compliant, reproducible release archive."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import zipfile

import pytest

from conftest import PLUGIN_SPECS
from test_release_archive_integrity import assert_archive_native_assets, validate_member_names

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location("_build_release_under_test", ROOT / "tools/build_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(snapshot: Path, *args: str) -> str:
    assert snapshot.resolve() != ROOT and snapshot.resolve().is_relative_to(Path("/tmp"))
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}}
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                           "-c", "core.autocrlf=false", "-c", "core.fileMode=true",
                           "-c", "user.name=Packaging test", "-c", "user.email=packaging@example.invalid", *args],
                          cwd=snapshot, env=environment, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def release_tool(monkeypatch):
    # Only this disposable repository is staged/committed; the shared index is never used.
    with tempfile.TemporaryDirectory(prefix="esibd-release-snapshot-", dir="/tmp") as temporary:
        snapshot = Path(temporary)
        for spec in PLUGIN_SPECS:
            source = ROOT / spec.folder
            assert source.is_dir() and not source.is_symlink()
            for path in source.rglob("*"):
                assert not path.is_symlink(), path
                assert path.is_dir() or stat.S_ISREG(path.stat().st_mode), path
                if path.is_dir():
                    continue
                relative = path.relative_to(source)
                if (path.name.lower() == "readme.md" or path.name == ".gitignore" or path.suffix == ".pyc"
                        or {"tests", "__pycache__", "logs", ".git"}.intersection(relative.parts)):
                    continue
                destination = snapshot / spec.folder / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        _git(snapshot, "init", "--quiet")
        _git(snapshot, "add", "--all")
        _git(snapshot, "commit", "--quiet", "-m", "Disposable packaging fixture")
        assert Path(_git(snapshot, "rev-parse", "--show-toplevel").strip()) == snapshot
        tool = _tool()
        monkeypatch.setattr(tool, "ROOT", snapshot)
        assert not (snapshot / "tools").exists()
        yield tool


def test_archive_holds_exactly_the_plugin_manifest(tmp_path, release_tool):
    tool = release_tool
    assert tool.plugin_folders() == sorted(spec.folder for spec in PLUGIN_SPECS)
    assert _git(tool.ROOT, "status", "--porcelain").strip() == ""
    for spec in PLUGIN_SPECS:
        for name in spec.bundled_files:
            assert _git(tool.ROOT, "ls-files", "--", f"{spec.folder}/{name}").strip() == f"{spec.folder}/{name}"
    archive = tool.build("9.9.9", tmp_path)
    assert archive.name == "esibd-explorer-plugins-v9.9.9.zip"
    with zipfile.ZipFile(archive) as built:
        names = built.namelist()
        assert validate_member_names(names, PLUGIN_SPECS) == []
        assert not any(name.endswith("/") for name in names)
        assert all(built.read(name) == (tool.ROOT / name).read_bytes() for name in names)
    assert_archive_native_assets(archive, PLUGIN_SPECS)


def test_rebuilding_the_same_commit_is_byte_identical(tmp_path, release_tool):
    tool = release_tool
    digests = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        digests.append(hashlib.sha256(tool.build("9.9.9", tmp_path / name).read_bytes()).digest())
    assert digests[0] == digests[1]


def test_only_tracked_regular_plugin_files_are_shipped(tmp_path, release_tool):
    tool = release_tool
    metadata = ("psu_a/README.md", "psu_a/.gitignore", "psu_a/tests/test_packaging.py",
                "psu_a/__pycache__/cache.py", "psu_a/logs/run.txt", "psu_a/runtime.pyc")
    for name in metadata:
        path = tool.ROOT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"excluded test metadata")
    tracked = "psu_a/packaging-tracked.txt"
    (tool.ROOT / tracked).write_bytes(b"tracked fixture payload")
    _git(tool.ROOT, "add", "--all")
    _git(tool.ROOT, "commit", "--quiet", "-m", "Disposable tracked-file policy case")
    untracked = "psu_a/packaging-untracked.txt"
    (tool.ROOT / untracked).write_bytes(b"must not ship")
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        tool.build("9.9.9", tmp_path)
    archive = tool.build("9.9.9", tmp_path, allow_dirty=True)
    with zipfile.ZipFile(archive) as built:
        assert tracked in built.namelist() and built.read(tracked) == b"tracked fixture payload"
        assert not set(metadata).intersection(built.namelist()) and untracked not in built.namelist()
        assert validate_member_names(built.namelist(), PLUGIN_SPECS) == []


NATIVE_ASSETS = ("mscan/native/manifest.json", "mscan/native/esibd-mscan-worker.exe",
                 "mscan/native/esibd-mscan-worker", "mscan/native/source.zip",
                 "mscan/native/THIRD_PARTY_NOTICES.txt", "mscan/_runtime/_native_worker.py")


@pytest.mark.parametrize("member", NATIVE_ASSETS)
def test_untracked_native_assets_fail_even_with_allow_dirty(tmp_path, release_tool, member):
    _git(release_tool.ROOT, "rm", "--cached", "--", member)
    assert (release_tool.ROOT / member).is_file()
    with pytest.raises(RuntimeError, match="Native assets must be tracked"):
        release_tool.build("9.9.9", tmp_path, allow_dirty=True)
    assert not (tmp_path / "esibd-explorer-plugins-v9.9.9.zip").exists()


@pytest.mark.parametrize("member", NATIVE_ASSETS)
def test_missing_native_assets_fail_before_archive_creation(tmp_path, release_tool, member):
    (release_tool.ROOT / member).unlink()
    with pytest.raises(RuntimeError, match="Invalid native bundle|Missing or non-regular release file"):
        release_tool.build("9.9.9", tmp_path, allow_dirty=True)
    assert not (tmp_path / "esibd-explorer-plugins-v9.9.9.zip").exists()


@pytest.mark.parametrize("member", ("mscan/native/esibd-mscan-worker.exe", "mscan/native/source.zip",
                                    "mscan/native/THIRD_PARTY_NOTICES.txt"))
def test_tampered_native_assets_fail_before_archive_creation(tmp_path, release_tool, member):
    path = release_tool.ROOT / member
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(RuntimeError, match="native asset integrity failure"):
        release_tool.build("9.9.9", tmp_path, allow_dirty=True)
    assert not (tmp_path / "esibd-explorer-plugins-v9.9.9.zip").exists()


@pytest.mark.parametrize("member,mode", [("mscan/native/esibd-mscan-worker", 0o644),
                                       ("mscan/native/esibd-mscan-worker.exe", 0o755)])
def test_worker_executable_modes_are_enforced_from_git_index(tmp_path, release_tool, member, mode):
    (release_tool.ROOT / member).chmod(mode)
    _git(release_tool.ROOT, "add", "--", member)
    assert _git(release_tool.ROOT, "ls-files", "--stage", "--", member).startswith(f"{stat.S_IFREG | mode:o} ")
    with pytest.raises(RuntimeError, match="not executable|Incorrect tracked worker permissions"):
        release_tool.build("9.9.9", tmp_path, allow_dirty=True)
    assert not (tmp_path / "esibd-explorer-plugins-v9.9.9.zip").exists()
