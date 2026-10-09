"""Create deterministic family-local native source and third-party notice bundles."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ("ampr", "amx", "amx_hd", "dmmr", "esi", "psu", "tpg366", "mscan", "transmission")
GPL_FAMILIES = frozenset({"mscan", "tpg366"})
PLATFORMS = ("x86_64-pc-windows-gnu", "x86_64-unknown-linux-gnu")
ZIP_STAMP = (1980, 1, 1, 0, 0, 0)
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_FILES = 100_000
EXCLUDED_DIRS = frozenset({"target", ".git", "__pycache__"})


@dataclass(frozen=True)
class Entry:
    data: bytes
    mode: int = 0o644


def _json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or "\\" in name or "\0" in name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or ":" in path.parts[0]):
        raise ValueError(f"Unsafe bundle path: {name!r}")
    return name


def _no_symlinks(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"Symlink is not permitted in native source bundles: {candidate}")


def _read(path: Path) -> Entry:
    _no_symlinks(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected a regular source file: {path}")
    if info.st_size > MAX_FILE_BYTES:
        raise ValueError(f"Source file exceeds the size limit: {path}")
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Source file exceeds the size limit: {path}")
    return Entry(data, 0o755 if info.st_mode & 0o111 else 0o644)


def _walk(directory: Path, *, excluded=EXCLUDED_DIRS):
    _no_symlinks(directory)
    if not directory.is_dir():
        raise ValueError(f"Missing source directory: {directory}")
    for path in sorted(directory.iterdir()):
        if path.name in excluded:
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink is not permitted in native source bundles: {path}")
        if path.is_dir():
            yield from _walk(path, excluded=excluded)
        else:
            yield path


def _put(entries: dict[str, Entry], name: str, entry: Entry) -> None:
    name = _safe_name(name)
    if len(entry.data) > MAX_FILE_BYTES:
        raise ValueError(f"Source entry exceeds the size limit: {name}")
    if name in entries:
        if entries[name] != entry:
            raise ValueError(f"Conflicting source entries: {name}")
        return
    entries[name] = entry
    if len(entries) > MAX_FILES:
        raise ValueError("Native source bundle exceeds the file-count limit")


def _own_sources() -> dict[str, Entry]:
    native = ROOT / "native"
    entries = {name: _read(native / name) for name in
               ("Cargo.toml", "Cargo.lock", "worker/Cargo.toml", "LICENSES.md",
                "zig-linker.py", "zig-dlltool.py")}
    for name in ("worker/src", "licenses"):
        for path in _walk(native / name):
            _put(entries, path.relative_to(native).as_posix(), _read(path))
    return entries


def _reachable(metadata: dict) -> set[str]:
    if metadata.get("version") != 1 or not isinstance(metadata.get("resolve"), dict):
        raise ValueError("Cargo metadata must contain a version-1 resolve graph")
    packages = {package["id"]: package for package in metadata["packages"]}
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    roots = [identifier for identifier in metadata["workspace_members"]
             if packages[identifier]["name"] == "esibd-native-worker"]
    if len(roots) != 1:
        raise ValueError("Expected one esibd-native-worker workspace package")
    seen = set()
    pending = roots.copy()
    while pending:
        identifier = pending.pop()
        if identifier in seen:
            continue
        if identifier not in nodes or identifier not in packages:
            raise ValueError("Incomplete Cargo metadata resolve graph")
        seen.add(identifier)
        for dependency in nodes[identifier]["deps"]:
            kinds = dependency["dep_kinds"]
            if any(kind["kind"] in {None, "build"} for kind in kinds):
                pending.append(dependency["pkg"])
    return seen


def _cargo(command: list[str], *, cwd: Path, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"Cargo source resolution failed:\n{result.stderr.strip()}")
    return result


def _resolve(family: str) -> list[dict]:
    packages = {}
    platforms = (*PLATFORMS, None) if family in GPL_FAMILIES else PLATFORMS
    for platform in platforms:
        command = ["cargo", "metadata", "--format-version", "1", "--offline", "--locked",
                   "--manifest-path", str(ROOT / "native/Cargo.toml"),
                   "--no-default-features", "--features", family]
        if platform is not None:
            command.extend(["--filter-platform", platform])
        result = _cargo(command, cwd=ROOT)
        metadata = json.loads(result.stdout)
        reachable = _reachable(metadata)
        for package in metadata["packages"]:
            if package["id"] not in reachable:
                continue
            if package["name"] == "esibd-native-worker" and package["source"] is None:
                continue
            source = package.get("source")
            if source != "registry+https://github.com/rust-lang/crates.io-index":
                raise ValueError(f"Unsupported dependency source; preserve it explicitly first: {source}")
            if package["id"] not in packages:
                packages[package["id"]] = {**package, "platforms": []}
            if platform is not None:
                packages[package["id"]]["platforms"].append(platform)
    return sorted(packages.values(), key=lambda package: (package["name"], package["version"], package["source"]))


def _upstream_licenses(package: dict) -> dict[Path, dict]:
    native = ROOT / "native/licenses"
    index = json.loads(_read(native / "upstream.json").data)
    origin = index.get(f"{package.get('name')} {package.get('version')}")
    if not origin:
        return {}
    directory = Path(package["manifest_path"]).parent
    vcs = json.loads(_read(directory / ".cargo_vcs_info.json").data)
    if origin["repository"] != package.get("repository") or vcs["git"]["sha1"] != origin["revision"]:
        raise ValueError("Pinned upstream license revision does not match the Cargo package")
    paths = {}
    for license in origin["files"]:
        path = native / _safe_name(license["file"])
        entry = _read(path)
        if _sha(entry.data) != license["sha256"]:
            raise ValueError(f"Pinned upstream license SHA-256 mismatch: {path}")
        paths[path] = {"source_url": license["source_url"], "upstream_revision": origin["revision"]}
    return paths


def _license_files(package: dict) -> list[Path]:
    directory = Path(package["manifest_path"]).parent
    _read(directory / "Cargo.toml")
    paths = set()
    for path in sorted(directory.iterdir()):
        if (path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
                or path.suffix.lower() == ".license"):
            if path.is_symlink():
                raise ValueError(f"Symlink license is not permitted: {path}")
            if path.is_dir():
                paths.update(_walk(path))
            else:
                paths.add(path)
    declared = package.get("license_file")
    if declared:
        path = Path(declared)
        path = path if path.is_absolute() else directory / path
        _no_symlinks(path)
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"Crate license-file escapes its package: {declared}")
        paths.add(path)
    if not paths:
        paths.update(_upstream_licenses(package))
    if not paths:
        raise ValueError(f"No full license/notice text available for {package['name']} {package['version']}")
    return sorted(paths)


def _record(package: dict, checksum: str, licenses: list[dict]) -> dict:
    name, version = package["name"], package["version"]
    return {"name": name, "version": version, "license": package.get("license"),
            "source": package["source"], "repository": package.get("repository"),
            "authors": package.get("authors", []),
            "registry_url": f"https://crates.io/crates/{name}/{version}",
            "download_url": f"https://static.crates.io/crates/{name}/{name}-{version}.crate",
            "cargo_checksum": checksum, "platforms": sorted(package["platforms"]),
            "role": "active" if package["platforms"] else "resolution-only",
            "license_files": licenses}


def _vendor(package: dict, checksum: str, entries: dict[str, Entry]) -> None:
    directory = Path(package["manifest_path"]).parent
    prefix = f"vendor/{package['name']}-{package['version']}"
    files = {}
    for path in _walk(directory):
        relative = path.relative_to(directory).as_posix()
        if relative in {".cargo-ok", ".cargo-checksum.json"}:
            continue
        entry = _read(path)
        _put(entries, f"{prefix}/{relative}", entry)
        files[relative] = _sha(entry.data)
    _put(entries, f"{prefix}/.cargo-checksum.json", Entry(_json({"package": checksum, "files": files})))


def _build_instructions(family: str, vendored: bool, rust_version: str) -> bytes:
    fetch = ("The required registry source union is included under `vendor/`. The local\n"
             "`.cargo/config.toml` replaces crates.io for an offline build. The worker\n"
             "manifest and build lock are specialized to this family; originals are\n"
             "retained under `provenance/`. Implementation source files are unchanged.\n"
             "Extra registry packages marked `resolution-only` satisfy Cargo's inactive\n"
             "target resolution and are not linked for the two supported platforms.\n" if vendored else
             "Registry sources are not vendored in this bundle. First populate a Cargo\n"
             "cache with `cargo fetch --locked --target <target>` for your platform, or\n"
             "use an existing cache containing the exact Cargo.lock dependencies.\n")
    return (f"# Build The {family} Native Worker\n\n"
            "Extract this archive into a regular directory and run Cargo from its root.\n"
            "No plugin directory, Python runtime, Explorer installation, or vendor SDK\n"
            "is needed to compile the Rust source. DLL-backed workers still require their\n"
            "separately supplied SDK DLL when run against real hardware.\n\n"
            "## Prerequisites\n\n"
            f"Use Rust >= {rust_version}, Cargo, the selected Rust standard-library\n"
            "target, and its platform compiler/linker and system libraries. These toolchain\n"
            "components are not vendored here. No Rust tests or deployed executable is\n"
            "included. Generated ABI bindings and the error catalog are already included.\n\n"
            + fetch + "\n## Family Builds\n\n```sh\n"
            f"cargo build --locked --offline --release --no-default-features --features {family} --target x86_64-unknown-linux-gnu\n"
            f"cargo build --locked --offline --release --no-default-features --features {family} --target x86_64-pc-windows-gnu\n"
            "```\n\n"
            "Linux requires a C linker. Windows GNU requires the matching MinGW-w64\n"
            "compiler and import-library tools; cross builds may set the standard Cargo\n"
            "target linker variables. `zig-linker.py` and `zig-dlltool.py` are optional\n"
            "build-time helpers for an explicitly installed Zig and Python 3, not worker\n"
            "runtime dependencies. See their source before choosing that alternate linker.\n\n"
            f"Only `{family}` is enabled. Never omit `--no-default-features`, enable\n"
            "`test-backend`, or deploy a development fault-injection binary. The native\n"
            "worker's protocol expects a supervising Explorer adapter; compiling or\n"
            "running it is not a hardware-validation claim.\n\n"
            "## Provenance\n\n"
            "`SOURCE_MANIFEST.json` records dependency versions, registry archive checksums,\n"
            "license files, and the SHA-256 of every other archive file. Its source-data\n"
            "SHA-256 hashes the original native project file records before manifest/lock\n"
            "specialization. The archive-data SHA-256 covers the sorted archive records,\n"
            "independently of ZIP compression.\n"
            "`LICENSES.md` distinguishes ported plugin terms and third-party licenses from\n"
            "vendor SDK terms. Full upstream texts are retained in `THIRD_PARTY_NOTICES.txt`\n"
            "and `third_party/licenses/`; no SDK redistribution rights are granted.\n").encode("utf-8")


def _file_records(entries: dict[str, Entry]) -> list[dict]:
    return [{"path": name, "sha256": _sha(entry.data), "size_bytes": len(entry.data), "mode": entry.mode}
            for name, entry in sorted(entries.items())]


def source_data_sha256() -> str:
    """Hash raw native project inputs, including original manifests/lock and licenses."""
    return _sha(_json(_file_records(_own_sources())))


def _toml(document: dict) -> bytes:
    lines = []

    def value(item):
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, (str, int, float)):
            return json.dumps(item, ensure_ascii=True, allow_nan=False)
        if isinstance(item, list):
            return "[" + ", ".join(value(element) for element in item) + "]"
        raise ValueError(f"Unsupported Cargo TOML value: {type(item).__name__}")

    def table(contents, keys):
        if keys:
            lines.extend(["", "[" + ".".join(json.dumps(key) for key in keys) + "]"])
        for key, item in contents.items():
            if not isinstance(item, dict):
                lines.append(f"{json.dumps(key)} = {value(item)}")
        for key, item in contents.items():
            if isinstance(item, dict):
                table(item, (*keys, key))

    table(document, ())
    return ("\n".join(lines).lstrip("\n") + "\n").encode("utf-8")


def _family_manifest(manifest: dict, family: str, packages: list[dict]) -> bytes:
    document = deepcopy(manifest)
    names = {package["name"] for package in packages}
    tables = [document]
    tables.extend(document.get("target", {}).values())
    for table in tables:
        table.pop("dev-dependencies", None)
        for kind in ("dependencies", "build-dependencies"):
            dependencies = table.get(kind, {})
            table[kind] = {name: spec for name, spec in dependencies.items()
                           if (spec.get("package", name) if isinstance(spec, dict) else name) in names}
            if not table[kind]:
                table.pop(kind)
    document["features"] = {name: values if name == family else []
                            for name, values in document["features"].items()}
    header = ("# Generated family-only build manifest. Implementation sources are unchanged.\n"
              "# Original manifest: ../provenance/worker-Cargo.toml\n")
    return header.encode("utf-8") + _toml(document)


def _family_lock(family: str, packages: list[dict], entries: dict[str, Entry]) -> tuple[bytes, dict]:
    # Resolve using only shipped sources, not the developer's registry/index cache.
    with tempfile.TemporaryDirectory(prefix="esibd-source-lock-") as temporary:
        directory = Path(temporary)
        for name, entry in entries.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(entry.data)
            path.chmod(entry.mode)
        environment = os.environ.copy()
        environment.update(CARGO_HOME=str(directory / "cargo-home"), CARGO_TARGET_DIR=str(directory / "target"))
        command = ["cargo", "generate-lockfile", "--offline", "--manifest-path", str(directory / "Cargo.toml")]
        _cargo(command, cwd=directory, env=environment)
        resolved = {}
        for platform in PLATFORMS:
            command = ["cargo", "metadata", "--format-version", "1", "--offline", "--locked",
                       "--manifest-path", str(directory / "Cargo.toml"),
                       "--no-default-features", "--features", family, "--filter-platform", platform]
            result = _cargo(command, cwd=directory, env=environment)
            metadata = json.loads(result.stdout)
            identifiers = _reachable(metadata)
            actual = {(package["name"], package["version"], package["source"])
                      for package in metadata["packages"] if package["id"] in identifiers and package["source"]}
            expected = {(package["name"], package["version"], package["source"])
                        for package in packages if platform in package["platforms"]}
            if actual != expected:
                raise ValueError(f"Family source build changes locked dependencies on {platform}: {actual ^ expected}")
            resolved[platform] = sorted([{"name": name, "version": version, "source": source}
                                         for name, version, source in actual], key=lambda package: (package["name"], package["version"]))
        return (directory / "Cargo.lock").read_bytes(), resolved


def _prepare(family: str) -> tuple[dict[str, Entry], bytes, dict]:
    if family not in FAMILIES:
        raise ValueError(f"Unsupported native worker family: {family}")
    entries = _own_sources()
    own_files = _file_records(entries)
    source_hash = _sha(_json(own_files))
    manifest = tomllib.loads(entries["worker/Cargo.toml"].data.decode("utf-8"))
    if manifest["package"].get("license-file") != "../LICENSES.md" or "license" in manifest["package"]:
        raise ValueError("Native Cargo license metadata must refer to the component notice index")
    if family not in manifest["features"]:
        raise ValueError(f"Missing Cargo family feature: {family}")
    lock = tomllib.loads(entries["Cargo.lock"].data.decode("utf-8"))
    checksums = {(package["name"], package["version"], package.get("source")): package.get("checksum")
                 for package in lock["package"]}
    vendored = family in GPL_FAMILIES
    notices = ["Native Worker Third-Party Notices\n",
               f"Family: {family}\nPlatforms: {', '.join(PLATFORMS)}\n",
               "These are preserved component terms, not an SDK or blanket project license grant.\n",
               "See LICENSES.md and SOURCE_MANIFEST.json in source.zip.\n\n",
               entries["LICENSES.md"].data.decode("utf-8"), "\n"]
    records = []
    packages = _resolve(family)
    for package in packages:
        checksum = checksums.get((package["name"], package["version"], package["source"]))
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise ValueError(f"Missing Cargo.lock registry checksum: {package['name']} {package['version']}")
        directory = Path(package["manifest_path"]).parent
        prefix = f"third_party/licenses/{package['name']}-{package['version']}"
        licenses = []
        for path in _license_files(package):
            entry = _read(path)
            upstream = {}
            if path.is_relative_to(directory):
                relative = path.relative_to(directory).as_posix()
            else:
                upstream = _upstream_licenses(package)[path]
                relative = path.name
            name = f"{prefix}/{relative}"
            _put(entries, name, entry)
            licenses.append({"file": name, "sha256": _sha(entry.data), "size_bytes": len(entry.data), **upstream})
        record = _record(package, checksum, licenses)
        records.append(record)
        notices.extend([f"\n{'=' * 72}\n{package['name']} {package['version']}\n",
                        f"License expression: {package.get('license') or 'See upstream license-file'}\n",
                        f"Dependency role: {record['role']}\n",
                        f"Registry: {record['registry_url']}\nSource: {record['download_url']}\n",
                        f"Repository: {record['repository'] or '(not specified)'}\n",
                        f"Crate authors: {'; '.join(record['authors']) or '(not specified)'}\n",
                        f"Cargo.lock archive SHA-256: {checksum}\n"])
        for license in licenses:
            notices.extend([f"\n--- {license['file']} ---\n",
                            f"Upstream license: {license['source_url']}\n" if "source_url" in license else "",
                            entries[license["file"]].data.decode("utf-8"), "\n"])
        if vendored:
            _vendor(package, checksum, entries)
    notice_data = "".join(notices).encode("utf-8")
    _put(entries, "THIRD_PARTY_NOTICES.txt", Entry(notice_data))
    _put(entries, "BUILD.md", Entry(_build_instructions(family, vendored, manifest["package"]["rust-version"])))
    if vendored:
        config = ('[source.crates-io]\nreplace-with = "vendored-sources"\n\n'
                  '[source.vendored-sources]\ndirectory = "vendor"\n\n[net]\noffline = true\n')
        _put(entries, ".cargo/config.toml", Entry(config.encode("utf-8")))
        _put(entries, "provenance/worker-Cargo.toml", entries["worker/Cargo.toml"])
        _put(entries, "provenance/Cargo.lock", entries["Cargo.lock"])
        entries["worker/Cargo.toml"] = Entry(_family_manifest(manifest, family, packages))
        build_lock, resolved = _family_lock(family, packages, entries)
        entries["Cargo.lock"] = Entry(build_lock)
    else:
        resolved = None
    files = _file_records(entries)
    if sum(file["size_bytes"] for file in files) > MAX_SOURCE_BYTES:
        raise ValueError("Native source bundle exceeds the total size limit")
    provenance = {"format_version": 1, "family": family, "worker_version": manifest["package"]["version"],
                  "features": [family], "platforms": list(PLATFORMS), "vendored": vendored,
                  "cargo_lock_sha256": _sha(entries.get("provenance/Cargo.lock", entries["Cargo.lock"]).data),
                  "build_lock_sha256": _sha(entries["Cargo.lock"].data),
                  "source_data_sha256": source_hash, "archive_data_sha256": _sha(_json(files)),
                  "own_source_files": own_files, "validated_dependency_versions": resolved,
                  "dependencies": records, "files": files}
    _put(entries, "SOURCE_MANIFEST.json", Entry(_json(provenance)))
    if sum(len(entry.data) for entry in entries.values()) > MAX_SOURCE_BYTES:
        raise ValueError("Native source bundle exceeds the total size limit")
    return entries, notice_data, provenance


def _archive(entries: dict[str, Entry]) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, entry in sorted(entries.items()):
            info = zipfile.ZipInfo(_safe_name(name), ZIP_STAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | entry.mode) << 16
            info.extra = b""
            info.comment = b""
            archive.writestr(info, entry.data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return result.getvalue()


def _products(family: str) -> tuple[dict[str, bytes], dict]:
    entries, notices, provenance = _prepare(family)
    source = _archive(entries)
    metadata = {"source": {"file": "source.zip", "sha256": _sha(source), "size_bytes": len(source),
                           "source_data_sha256": provenance["source_data_sha256"],
                           "vendored": provenance["vendored"]},
                "third_party_notices": {"file": "THIRD_PARTY_NOTICES.txt", "sha256": _sha(notices), "size_bytes": len(notices)},
                "source_data_sha256": provenance["source_data_sha256"],
                "cargo_lock_sha256": provenance["cargo_lock_sha256"]}
    return {"source.zip": source, "THIRD_PARTY_NOTICES.txt": notices}, metadata


def bundle_family(family: str, destination: str | Path) -> dict:
    """Write source.zip/notices into a plugin's native directory; return manifest fields."""
    destination = Path(destination).absolute()
    _no_symlinks(destination)
    for name in ("source.zip", "THIRD_PARTY_NOTICES.txt"):
        _no_symlinks(destination / name)
    products, metadata = _products(family)
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in products.items():
        path = destination / name
        _no_symlinks(path)
        with tempfile.NamedTemporaryFile(dir=destination, prefix=f".{name}.", delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(data)
                output.flush()
                os.fchmod(output.fileno(), 0o644)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return metadata


def check_family(family: str, destination: str | Path) -> list[str]:
    """Recompute byte-exact products without modifying the deployed directory."""
    destination = Path(destination).absolute()
    _no_symlinks(destination)
    products, _metadata = _products(family)
    failures = []
    for name, data in products.items():
        path = destination / name
        _no_symlinks(path)
        if not path.is_file() or _read(path).data != data:
            failures.append(f"{family}: missing or stale {name}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=FAMILIES, required=True)
    parser.add_argument("--destination", type=Path, required=True, help="Plugin-local native directory")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        failures = check_family(args.family, args.destination)
        for failure in failures:
            print(failure)
        return bool(failures)
    print(json.dumps(bundle_family(args.family, args.destination), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
