"""Release-archive checks shared by the archive and packaging tests."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import stat
import zipfile

from conftest import PluginSpec
from support.native import assert_native_payload, supervisor_for

BANNED_SEGMENTS = {"tests", "__pycache__", "logs"}


def member_segments(member: str) -> tuple[str, ...]:
    return tuple(part for part in member.split("/") if part)


def required_members(spec: PluginSpec) -> frozenset[str]:
    root = spec.folder
    if spec.runtime_family is None:
        return frozenset({f"{root}/{spec.entrypoint}", f"{root}/{spec.icon_stem}.png", f"{root}/LICENSE",
                          *(f"{root}/{name}" for name in spec.bundled_files)})
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
            *(f"{root}/{name}" for name in spec.bundled_files),
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
        if ("\\" in name or "\0" in name or name.startswith("/")
                or any(part in {"", ".", ".."} or ":" in part for part in name.rstrip("/").split("/"))):
            problems.append(f"{name}: unsafe release member path")
        banned = sorted(BANNED_SEGMENTS.intersection(parts))
        if basename.lower() == "readme.md":
            problems.append(f"{name}: README.md files are not allowed in releases")
        if banned:
            problems.append(f"{name}: banned path segment {banned[0]!r}")
        if basename.endswith(".pyc"):
            problems.append(f"{name}: .pyc files are not allowed in releases")
        if basename == ".gitignore":
            problems.append(f"{name}: .gitignore files are not allowed in releases")
        if parts[1:3] == ("vendor", "python"):
            problems.append(f"{name}: private Python interpreters are not allowed in native releases")

    for spec in plugin_specs:
        missing = sorted(required_members(spec) - normalized_files)
        if missing:
            problems.append(f"{spec.folder}: missing required members {missing!r}")

    return problems


def assert_archive_native_assets(path: Path, plugin_specs: tuple[PluginSpec, ...]) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), f"{path}: duplicate release members"
        for info in archive.infolist():
            assert not info.is_dir() and stat.S_ISREG(info.external_attr >> 16), info.filename
        source_hashes, lock_hashes, supervisors = set(), set(), set()
        executable_workers = set()
        for spec in plugin_specs:
            manifest = assert_native_payload(spec, lambda name: archive.read(f"{spec.folder}/native/{name}"))
            source_hashes.add(manifest["source_data_sha256"])
            lock_hashes.add(manifest["cargo_lock_sha256"])
            supervisors.add(archive.read(f"{spec.folder}/{supervisor_for(spec)}"))
            if "linux-x86_64" in manifest["targets"]:
                executable_workers.add(f"{spec.folder}/native/{manifest['targets']['linux-x86_64']['file']}")
        assert len(source_hashes) == len(lock_hashes) == len(supervisors) == 1, "inconsistent native bundle provenance"
        for info in archive.infolist():
            expected_mode = 0o100755 if info.filename in executable_workers else 0o100644
            assert info.external_attr >> 16 == expected_mode, f"{info.filename}: incorrect release permissions"
