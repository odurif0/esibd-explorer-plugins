"""Release archive policy checks."""

from __future__ import annotations

import os
import re
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from conftest import PLUGIN_SPECS, PluginSpec
from support.release import assert_archive_native_assets, required_members, validate_member_names


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_ZIP_PATTERN = "esibd-explorer-plugins-v*.zip"
RELEASE_ZIP_NAME = re.compile(r"^esibd-explorer-plugins-v(\d+(?:\.\d+)*)\.zip$")


def discover_release_archives(
    repo_root: Path = REPO_ROOT,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    environ = os.environ if env is None else env
    configured = environ.get("ESIBD_RELEASE_ZIP")
    if configured:
        archive = Path(configured)
        if not archive.exists():
            raise FileNotFoundError(
                f"ESIBD_RELEASE_ZIP points to a missing archive: {archive}"
            )
        return (archive,)

    archives = tuple(repo_root.glob(RELEASE_ZIP_PATTERN))
    if not archives:
        return ()

    def version(path: Path) -> tuple[int, ...]:
        match = RELEASE_ZIP_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"invalid release archive name: {path.name}")
        return tuple(int(part) for part in match.group(1).split("."))

    return (max(archives, key=version),)


def release_archives_or_skip(
    repo_root: Path = REPO_ROOT,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    archives = discover_release_archives(repo_root, env)
    if not archives:
        pytest.skip(
            "no root esibd-explorer-plugins-v*.zip archives found; "
            "set ESIBD_RELEASE_ZIP to inspect a release archive"
        )
    return archives


def assert_archive_members(path: Path, plugin_specs: tuple[PluginSpec, ...]) -> None:
    with zipfile.ZipFile(path) as archive:
        problems = validate_member_names(archive.namelist(), plugin_specs)

    assert problems == [], f"{path}:\n" + "\n".join(problems)


def write_zip(path: Path, members: Iterable[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(member, b"")
    return path


def valid_members(plugin_specs: tuple[PluginSpec, ...]) -> tuple[str, ...]:
    members: set[str] = set()
    for spec in plugin_specs:
        members.add(f"{spec.folder}/")
        members.update(required_members(spec))
    return tuple(sorted(members))


def test_release_archives_match_plugin_policy(
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    archives = release_archives_or_skip()
    for archive in archives:
        assert_archive_members(archive, plugin_specs)
        assert_archive_native_assets(archive, plugin_specs)


@pytest.mark.parametrize("spec", PLUGIN_SPECS, ids=lambda spec: f"{spec.folder}/native")
def test_validator_requires_every_plugins_native_assets(spec: PluginSpec) -> None:
    names = set(valid_members(PLUGIN_SPECS))
    assets = {f"{spec.folder}/{name}" for name in spec.bundled_files
              if name.startswith("native/") or name.endswith("/_native_worker.py")}
    assert assets, spec.folder
    for member in sorted(assets):
        assert validate_member_names(names - {member}, PLUGIN_SPECS) == [
            f"{spec.folder}: missing required members {[member]!r}"]


@pytest.mark.parametrize("member", ["esi/vendor/python/python.exe", "esi/vendor/python/manifest.json",
                                    "esi/vendor/python/python37.zip", "psu_a/vendor/python/python3.dll"])


def test_validator_rejects_private_interpreter_bundles(member: str) -> None:
    assert validate_member_names((*valid_members(PLUGIN_SPECS), member), PLUGIN_SPECS) == [
        f"{member}: private Python interpreters are not allowed in native releases"]


@pytest.mark.parametrize("member", ["esi/../outside", "esi/./native/extra", "esi//native/extra",
                                    "/esi/native/extra", "esi\\native\\extra", "esi/C:outside"])


def test_validator_rejects_unsafe_release_paths(member: str) -> None:
    problems = validate_member_names((*valid_members(PLUGIN_SPECS), member), PLUGIN_SPECS)
    assert f"{member}: unsafe release member path" in problems


def test_archive_rejects_missing_usb_protocol(plugin_specs: tuple[PluginSpec, ...]) -> None:
    members = set(valid_members(plugin_specs)) - {"tpg366/_runtime/_tpg366.py"}
    problems = validate_member_names(members, plugin_specs)
    assert problems == ["tpg366: missing required members ['tpg366/_runtime/_tpg366.py']"]


def test_synthetic_archive_accepts_valid_members(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    archive = write_zip(tmp_path / "valid.zip", valid_members(plugin_specs))

    assert_archive_members(archive, plugin_specs)


@pytest.mark.parametrize("helper", ["_heater_stability.py", "_experiment_guard.py", "_heater_limits.py"])
def test_validator_rejects_missing_esi_helper(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
    helper: str,
) -> None:
    member = f"esi/{helper}"
    archive = write_zip(
        tmp_path / "missing-helper.zip",
        tuple(name for name in valid_members(plugin_specs) if name != member),
    )
    with pytest.raises(AssertionError, match=helper):
        assert_archive_members(archive, plugin_specs)


def test_validator_rejects_readme_with_member_name(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    bad_member = f"{plugin_specs[0].folder}/ReadMe.MD"
    archive = write_zip(
        tmp_path / "readme.zip",
        (*valid_members(plugin_specs), bad_member),
    )

    with pytest.raises(AssertionError) as excinfo:
        assert_archive_members(archive, plugin_specs)

    assert bad_member in str(excinfo.value)
    assert "README.md files are not allowed" in str(excinfo.value)


@pytest.mark.parametrize(
    ("member", "reason"),
    (
        ("psu_a/tests/test_plugin.py", "banned path segment 'tests'"),
        ("psu_a/vendor/__pycache__/cache.py", "banned path segment '__pycache__'"),
        ("psu_a/logs/run.txt", "banned path segment 'logs'"),
        ("psu_a/plugin.pyc", ".pyc files are not allowed"),
        ("psu_a/.gitignore", ".gitignore files are not allowed"),
    ),
)


def test_validator_rejects_banned_metadata(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
    member: str,
    reason: str,
) -> None:
    archive = write_zip(
        tmp_path / "metadata.zip",
        (*valid_members(plugin_specs), member),
    )

    with pytest.raises(AssertionError) as excinfo:
        assert_archive_members(archive, plugin_specs)

    assert member in str(excinfo.value)
    assert reason in str(excinfo.value)


def test_discovery_requires_existing_env_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.zip"

    with pytest.raises(FileNotFoundError) as excinfo:
        discover_release_archives(tmp_path, {"ESIBD_RELEASE_ZIP": str(missing)})

    assert str(missing) in str(excinfo.value)
    assert "ESIBD_RELEASE_ZIP" in str(excinfo.value)


def test_discovery_selects_latest_semantic_version(tmp_path: Path) -> None:
    old = tmp_path / "esibd-explorer-plugins-v0.9.zip"
    latest = tmp_path / "esibd-explorer-plugins-v0.10.zip"
    old.touch()
    latest.touch()

    assert discover_release_archives(tmp_path, {}) == (latest,)


def test_no_archive_discovery_skips_only_archive_inspection(
    tmp_path: Path,
) -> None:
    with pytest.raises(pytest.skip.Exception) as excinfo:
        release_archives_or_skip(tmp_path, {})

    assert "no root esibd-explorer-plugins-v*.zip archives found" in str(excinfo.value)
