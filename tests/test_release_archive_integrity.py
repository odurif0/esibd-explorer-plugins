"""Release archive policy checks."""

from __future__ import annotations

import os
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from conftest import PluginSpec


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_ZIP_PATTERN = "esibd-explorer-plugins-v*.zip"
BANNED_SEGMENTS = {"tests", "__pycache__", "logs"}


def member_segments(member: str) -> tuple[str, ...]:
    return tuple(part for part in member.split("/") if part)


def required_members(spec: PluginSpec) -> frozenset[str]:
    root = spec.folder
    runtime = f"{root}/vendor/runtime"
    device = f"{runtime}/{spec.runtime_family}"
    vendor = f"{device}/vendor"
    return frozenset(
        {
            f"{root}/{spec.entrypoint}",
            f"{root}/{spec.icon_stem}.png",
            f"{root}/{spec.icon_stem}.svg",
            f"{root}/switch-medium_off.png",
            f"{root}/switch-medium_on.png",
            f"{runtime}/__init__.py",
            f"{runtime}/_controller_process.py",
            f"{runtime}/_driver_common.py",
            f"{runtime}/_process_proxy_support.py",
            f"{runtime}/error_codes.json",
            f"{device}/__init__.py",
            f"{device}/{spec.runtime_family}.py",
            f"{device}/{spec.runtime_family}_base.py",
            f"{vendor}/{spec.header}",
            f"{vendor}/x64/{spec.dll}",
        }
    )


def validate_member_names(
    members: Iterable[str],
    plugin_specs: tuple[PluginSpec, ...],
) -> list[str]:
    names = tuple(members)
    expected_roots = {spec.folder for spec in plugin_specs}
    actual_roots = {
        parts[0] for name in names if (parts := member_segments(name))
    }
    problems: list[str] = []

    if actual_roots != expected_roots:
        problems.append(
            "top-level archive entries differ from plugin manifest: "
            f"expected {sorted(expected_roots)!r}, found {sorted(actual_roots)!r}"
        )

    normalized_files = {name.rstrip("/") for name in names if not name.endswith("/")}
    for name in names:
        parts = member_segments(name)
        if not parts:
            continue
        basename = parts[-1]
        banned = sorted(BANNED_SEGMENTS.intersection(parts))
        if basename.lower() == "readme.md":
            problems.append(f"{name}: README.md files are not allowed in releases")
        if banned:
            problems.append(f"{name}: banned path segment {banned[0]!r}")
        if basename.endswith(".pyc"):
            problems.append(f"{name}: .pyc files are not allowed in releases")
        if basename == ".gitignore":
            problems.append(f"{name}: .gitignore files are not allowed in releases")

    for spec in plugin_specs:
        missing = sorted(required_members(spec) - normalized_files)
        if missing:
            problems.append(f"{spec.folder}: missing required members {missing!r}")

    return problems


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

    return tuple(sorted(repo_root.glob(RELEASE_ZIP_PATTERN)))


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


def test_synthetic_archive_accepts_valid_members(
    tmp_path: Path,
    plugin_specs: tuple[PluginSpec, ...],
) -> None:
    archive = write_zip(tmp_path / "valid.zip", valid_members(plugin_specs))

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


def test_no_archive_discovery_skips_only_archive_inspection(
    tmp_path: Path,
) -> None:
    with pytest.raises(pytest.skip.Exception) as excinfo:
        release_archives_or_skip(tmp_path, {})

    assert "no root esibd-explorer-plugins-v*.zip archives found" in str(excinfo.value)
